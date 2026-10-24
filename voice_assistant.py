"""
voice_assistant.py - Voice alerts (Text-to-Speech) for TradeMatrix.

!!! SAVE AS `voice_assistant.py` IN THE SAME FOLDER AS `main.py` AND `risk_manager.py` !!!

Backends (tried in order, first one that works is used):
    Android : native android.speech.tts.TextToSpeech through pyjnius
    Desktop : pyttsx3  ->  system command (macOS `say`, Linux `spd-say`/`espeak-ng`/`espeak`)
    Nothing available : voice is silently disabled, the app keeps running.

Install:
    Desktop : pip install pyttsx3        (Linux also: sudo apt install espeak-ng)
    Android : add `pyjnius` to `requirements` in buildozer.spec (pyttsx3 not needed)

All speech happens on ONE background worker thread fed by a queue, so the Kivy
UI never blocks and messages are spoken in order. Every public method is
thread-safe and never raises.
"""
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from typing import Optional

log = logging.getLogger("voice_assistant")

_STOP = object()           # sentinel: tells the worker thread to exit
MAX_TEXT_LEN = 300         # longer messages are truncated
MAX_QUEUE = 10             # when full, the oldest pending message is dropped
MAX_FAILURES = 3           # consecutive playback failures before giving up


def _is_android() -> bool:
    return "ANDROID_ARGUMENT" in os.environ or "ANDROID_STORAGE" in os.environ


# ---------------------------------------------------------------- backends
class _Pyttsx3Backend:
    """Desktop TTS. Created INSIDE the worker thread (required on Windows)."""
    name = "pyttsx3"

    def __init__(self, rate: int):
        import pyttsx3  # lazy import: a missing package must not break app start
        self._engine = pyttsx3.init()
        self._engine.setProperty("rate", rate)

    def say(self, text: str) -> None:
        self._engine.say(text)
        self._engine.runAndWait()          # blocks only the worker thread

    def close(self) -> None:
        try:
            self._engine.stop()
        except Exception:  # noqa: BLE001
            pass


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

    def say(self, text: str) -> None:
        # a leading "-" could be parsed as a command-line option
        proc = subprocess.run(self._cmd + [text.lstrip("-")], timeout=60, check=False,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if proc.returncode != 0:
            raise RuntimeError(f"{self._cmd[0]} exited with code {proc.returncode}")

    def close(self) -> None:
        pass


class _AndroidBackend:
    """Native Android TextToSpeech via pyjnius."""
    name = "android-tts"

    def __init__(self, rate: int):
        from jnius import PythonJavaClass, autoclass, java_method

        class _InitListener(PythonJavaClass):
            __javainterfaces__ = ["android/speech/tts/TextToSpeech$OnInitListener"]
            __javacontext__ = "app"

            def __init__(self, event):
                super().__init__()
                self.event = event
                self.status = -1

            @java_method("(I)V")
            def onInit(self, status):  # noqa: N802 (Java naming)
                self.status = status
                self.event.set()

        TextToSpeech = autoclass("android.speech.tts.TextToSpeech")
        Locale = autoclass("java.util.Locale")
        activity = autoclass("org.kivy.android.PythonActivity").mActivity

        ready = threading.Event()
        self._listener = _InitListener(ready)      # keep a reference: avoids GC crash
        self._tts = TextToSpeech(activity, self._listener)
        if not ready.wait(timeout=8) or self._listener.status != 0:   # 0 == SUCCESS
            raise RuntimeError("Android TextToSpeech engine did not initialise")
        self._tts.setLanguage(Locale.US)
        self._tts.setSpeechRate(max(0.5, min(2.0, rate / 175.0)))
        self._queue_add = TextToSpeech.QUEUE_ADD
        self._counter = 0

    def say(self, text: str) -> None:
        self._counter += 1
        result = self._tts.speak(text, self._queue_add, None, f"tm{self._counter}")
        if result != 0:                             # TextToSpeech.SUCCESS
            raise RuntimeError(f"TextToSpeech.speak returned {result}")

    def close(self) -> None:
        try:
            self._tts.stop()
            self._tts.shutdown()
        except Exception:  # noqa: BLE001
            pass


# -------------------------------------------------------------- main class
class VoiceAssistant:
    """Thread-safe, non-blocking voice alerts for TradeMatrix."""

    def __init__(self, enabled: bool = True, rate: int = 175):
        self._enabled = bool(enabled)
        self._rate = int(rate)
        self._queue: "queue.Queue" = queue.Queue(maxsize=MAX_QUEUE)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
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
            self.clear_queue()

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

        Returns True if the message was queued. interrupt=True discards
        messages still waiting so an urgent alert is spoken next.
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
            try:
                self._queue.put_nowait(message)
            except queue.Full:                      # drop the oldest, keep the newest
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                self._queue.put_nowait(message)
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

    def shutdown(self) -> None:
        """Stop the worker thread and release the TTS engine. Safe to call twice.
        Call it from App.on_stop()."""
        try:
            self._closed = True
            self.clear_queue()
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
        failures = 0
        while True:
            item = self._queue.get()
            if item is _STOP:
                break
            if not self._enabled:
                continue
            try:
                backend.say(item)
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
