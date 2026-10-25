"""
voice_assistant.py - Voice alerts (Text-to-Speech) for TradeMatrix.

!!! SAVE AS `voice_assistant.py` IN THE SAME FOLDER AS `main.py` !!!

Android design (as required):
  * NO PythonJavaClass and NO OnInitListener anywhere in this file.
  * The engine is created directly:  TextToSpeech(PythonActivity.mActivity, None)
  * Engine start-up is asynchronous, and without a listener Android gives no
    "ready" callback. Readiness is detected by polling setLanguage(), which
    returns a negative code until the engine is bound. speak() is additionally
    retried while the engine reports an error, so early messages are not lost.
  * Every speak() call passes a fresh empty android.os.Bundle(),
    TextToSpeech.QUEUE_ADD and a unique utterance ID string.

Desktop fallbacks: pyttsx3, then the OS speech command (say / spd-say / espeak).
If nothing works, voice is silently disabled and the app keeps running.

Architecture: one background worker thread fed by a bounded queue. Every public
method is thread-safe, returns immediately and never raises.

Install:
    Desktop : pip install pyttsx3        (Linux also: sudo apt install espeak-ng)
    Android : add `pyjnius` to `requirements` in buildozer.spec
"""
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Optional

log = logging.getLogger("voice_assistant")

_STOP = object()           # sentinel: tells the worker thread to exit
MAX_TEXT_LEN = 300         # longer messages are truncated
MAX_QUEUE = 10             # when full, the oldest pending message is dropped
MAX_FAILURES = 3           # consecutive playback failures before giving up


def _is_android() -> bool:
    return "ANDROID_ARGUMENT" in os.environ or "ANDROID_STORAGE" in os.environ


# ---------------------------------------------------------------- backends
class _AndroidBackend:
    """Native Android TextToSpeech through pyjnius, with NO Java listener."""
    name = "android-tts"
    READY_TIMEOUT = 6.0        # seconds to wait for the engine to bind
    SPEAK_RETRIES = 8          # attempts per message while the engine is still starting
    RETRY_DELAY = 0.25

    def __init__(self, rate: int):
        from jnius import autoclass          # imported lazily: absent on desktop

        TextToSpeech = autoclass("android.speech.tts.TextToSpeech")
        self._Bundle = autoclass("android.os.Bundle")
        self._String = autoclass("java.lang.String")
        Locale = autoclass("java.util.Locale")
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        if activity is None:
            raise RuntimeError("PythonActivity.mActivity is not available")

        self._queue_add = TextToSpeech.QUEUE_ADD
        self._success = TextToSpeech.SUCCESS
        self._counter = 0
        self._lang_ok = False

        # Direct initialisation: the listener argument is None on purpose.
        self._tts = TextToSpeech(activity, None)
        self._wait_until_bound(Locale.US)
        try:
            self._tts.setSpeechRate(float(max(0.5, min(2.0, rate / 175.0))))
        except Exception:  # noqa: BLE001
            pass

    def _wait_until_bound(self, locale) -> None:
        """Poll setLanguage(): a negative result means 'not bound yet' (or language
        data missing). We proceed after the timeout either way; say() retries."""
        deadline = time.monotonic() + self.READY_TIMEOUT
        while True:
            try:
                result = int(self._tts.setLanguage(locale))
            except Exception:  # noqa: BLE001
                result = -2
            if result >= 0:                  # LANG_AVAILABLE / COUNTRY / COUNTRY_VAR
                self._lang_ok = True
                return
            if time.monotonic() >= deadline:
                log.warning("TTS engine not confirmed ready after %.0fs "
                            "(setLanguage=%s); trying anyway", self.READY_TIMEOUT, result)
                return
            time.sleep(0.2)

    def say(self, text: str, urgent: bool = False) -> None:
        if urgent:
            try:
                self._tts.stop()             # cut off current speech for urgent alerts
            except Exception:  # noqa: BLE001
                pass
        java_text = self._String(text)       # a real java.lang.String (a CharSequence)
        last = None
        for _ in range(self.SPEAK_RETRIES):
            self._counter += 1
            utterance_id = f"tradematrix-{self._counter}-{int(time.time() * 1000)}"
            result = self._tts.speak(java_text, self._queue_add, self._Bundle(), utterance_id)
            if result == self._success:
                return
            last = result
            time.sleep(self.RETRY_DELAY)     # engine may still be starting
        raise RuntimeError(f"TextToSpeech.speak failed (code {last}). "
                           f"Is a TTS engine installed on this device?")

    def interrupt(self) -> None:
        try:
            self._tts.stop()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            self._tts.stop()
            self._tts.shutdown()
        except Exception:  # noqa: BLE001
            pass


class _Pyttsx3Backend:
    """Desktop TTS. Created INSIDE the worker thread (required on Windows)."""
    name = "pyttsx3"

    def __init__(self, rate: int):
        import pyttsx3  # lazy import: a missing package must not break app start
        self._engine = pyttsx3.init()
        self._engine.setProperty("rate", rate)

    def say(self, text: str, urgent: bool = False) -> None:
        self._engine.say(text)
        self._engine.runAndWait()            # blocks only the worker thread

    def interrupt(self) -> None:
        try:
            self._engine.stop()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        self.interrupt()


class _CommandBackend:
    """Desktop fallback using the OS speech command (macOS / Linux)."""
    name = "system-command"

    def __init__(self, rate: int):
        if sys.platform == "darwin":
            candidates = [["say"]]
        else:
            candidates = [["spd-say", "-w"], ["espeak-ng"], ["espeak"]]
        self._cmd = None
        for cmd in candidates:
            if shutil.which(cmd[0]):
                self._cmd = cmd
                break
        if self._cmd is None:
            raise RuntimeError("no system speech command found (say/spd-say/espeak)")
        self._proc = None

    def say(self, text: str, urgent: bool = False) -> None:
        # a leading "-" could be parsed as a command-line option
        proc = subprocess.Popen(self._cmd + [text.lstrip("-")],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._proc = proc
        try:
            code = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError("speech command timed out")
        finally:
            self._proc = None
        if code > 0:                          # negative = we terminated it on purpose
            raise RuntimeError(f"{self._cmd[0]} exited with code {code}")

    def interrupt(self) -> None:
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        self.interrupt()


# -------------------------------------------------------------- main class
class VoiceAssistant:
    """Thread-safe, non-blocking voice alerts for TradeMatrix."""

    def __init__(self, enabled: bool = True, rate: int = 175):
        self._enabled = bool(enabled)
        self._rate = int(rate)
        self._queue: "queue.Queue" = queue.Queue(maxsize=MAX_QUEUE)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._backend = None
        self._backend_name = "not started"
        self._backend_failed = False
        self._closed = False
        self._last_error: Optional[str] = None

    # ----------------------------------------------------------- properties
    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value)
        if not self._enabled:
            self.stop()

    @property
    def backend_name(self) -> str:
        """Active backend (known once the first message has been processed)."""
        return self._backend_name

    @property
    def last_error(self) -> Optional[str]:
        """Last TTS error text (None if all is well) - handy for a status label."""
        return self._last_error

    # ---------------------------------------------------------------- core
    def speak(self, text: str, interrupt: bool = False) -> bool:
        """Queue `text` to be read aloud; returns immediately.

        Returns True if the message was queued. interrupt=True discards messages
        still waiting and cuts off the current speech so this one is heard next.
        """
        try:
            if not self._enabled or self._backend_failed or self._closed:
                return False
            message = self._clean(text)
            if not message:
                return False
            self._ensure_thread()
            if interrupt:
                self.clear_queue()
            item = (message, bool(interrupt))
            try:
                self._queue.put_nowait(item)
            except queue.Full:                      # drop the oldest, keep the newest
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                self._queue.put_nowait(item)
            return True
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{exc.__class__.__name__}: {exc}"
            log.warning("speak() failed: %s", self._last_error)
            return False

    def clear_queue(self) -> None:
        """Discard all messages that have not been spoken yet."""
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    def stop(self) -> None:
        """Silence now: drop pending messages and cut off the current speech."""
        try:
            self.clear_queue()
            backend = self._backend
            if backend is not None:
                backend.interrupt()
        except Exception:  # noqa: BLE001
            pass

    def shutdown(self) -> None:
        """Stop the worker thread and release the TTS engine. Safe to call twice.
        Call it from App.on_stop()."""
        try:
            self._closed = True
            self.stop()
            if self._thread is not None and self._thread.is_alive():
                self._queue.put_nowait(_STOP)
                self._thread.join(timeout=2)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------ trading alerts
    def announce_buy(self, symbol: str, price: float) -> bool:
        return self.speak(f"Order placed for {self._sym(symbol)} at {self._num(price)}")

    def announce_take_profit(self, symbol: str, profit_pct: float) -> bool:
        pct = self._num(abs(self._to_float(profit_pct)), 2)
        return self.speak(f"Take profit triggered for {self._sym(symbol)} "
                          f"with {pct} percent profit", interrupt=True)

    def announce_stop_loss(self, symbol: str, loss_pct: float) -> bool:
        pct = self._num(abs(self._to_float(loss_pct)), 2)
        return self.speak(f"Stop loss triggered for {self._sym(symbol)} "
                          f"with {pct} percent loss", interrupt=True)

    def announce_status(self, message: str) -> bool:
        return self.speak(message)

    # ----------------------------------------------------------- internals
    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._worker, name="TradeMatrixTTS", daemon=True)
                self._thread.start()

    def _make_backend(self):
        classes = [_AndroidBackend] if _is_android() else [_Pyttsx3Backend, _CommandBackend]
        errors = []
        for cls in classes:
            try:
                backend = cls(self._rate)
                self._backend_name = backend.name
                return backend
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{cls.name}: {exc.__class__.__name__}: {exc}")
        self._last_error = "; ".join(errors) or "no TTS backend available"
        self._backend_name = "none"
        log.warning("TTS unavailable, voice alerts disabled: %s", self._last_error)
        return None

    def _worker(self) -> None:
        backend = self._make_backend()          # created in this thread on purpose
        if backend is None:
            self._backend_failed = True
            self.clear_queue()
            return
        self._backend = backend
        failures = 0
        while True:
            item = self._queue.get()
            if item is _STOP:
                break
            text, urgent = item
            if not self._enabled:
                continue
            try:
                backend.say(text, urgent)
                failures = 0
                self._last_error = None
            except Exception as exc:  # noqa: BLE001
                failures += 1
                self._last_error = f"{exc.__class__.__name__}: {exc}"
                log.warning("TTS playback failed (%d/%d): %s",
                            failures, MAX_FAILURES, self._last_error)
                if failures >= MAX_FAILURES:
                    self._backend_failed = True
                    self.clear_queue()
                    break
        self._backend = None
        backend.close()

    # ---------------------------------------------------------- formatting
    @staticmethod
    def _clean(text) -> str:
        text = "" if text is None else str(text)
        return " ".join(text.split())[:MAX_TEXT_LEN]

    @staticmethod
    def _sym(symbol) -> str:
        """'BTC/USDT' -> 'BTC USDT' so the engine reads it naturally."""
        cleaned = re.sub(r"[^A-Za-z0-9 ]", " ", str(symbol or "asset"))
        return " ".join(cleaned.split()) or "asset"

    @staticmethod
    def _to_float(value) -> float:
        try:
            v = float(value)
            return v if v == v and abs(v) != float("inf") else 0.0
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _num(cls, value, decimals: Optional[int] = None) -> str:
        """Speech-friendly number: 64231.50 -> '64231.5', 0.000123 -> '0.000123'."""
        v = cls._to_float(value)
        if decimals is None:
            decimals = 2 if abs(v) >= 1 else 6
        text = f"{v:.{decimals}f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"
