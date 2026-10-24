"""
voice_assistant.py - Voice alerts (Text-to-Speech) for TradeMatrix.

SAVE AS 'voice_assistant.py' IN THE SAME FOLDER AS 'main.py' AND 'buildozer.spec'.
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

_STOP = object()
MAX_TEXT_LEN = 300
MAX_QUEUE = 10
MAX_FAILURES = 3


def _is_android() -> bool:
    return "ANDROID_ARGUMENT" in os.environ or "ANDROID_STORAGE" in os.environ


class _Pyttsx3Backend:
    name = "pyttsx3"

    def __init__(self, self_rate: int):
        import pyttsx3
        self._engine = pyttsx3.init()
        self._engine.setProperty("rate", self_rate)

    def say(self, text: str) -> None:
        self._engine.say(text)
        self._engine.runAndWait()

    def close(self) -> None:
        try:
            self._engine.stop()
        except Exception:
            pass


class _CommandBackend:
    name = "system-command"

    def __init__(self, self_rate: int):
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
            raise RuntimeError("no system speech command found")

    def say(self, text: str) -> None:
        proc = subprocess.run(self._cmd + [text.lstrip("-")], timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if proc.returncode != 0:
            raise RuntimeError(f"{self._cmd[0]} exited with code {proc.returncode}")

    def close(self) -> None:
        pass


class _AndroidBackend:
    """Native Android TextToSpeech via pyjnius."""
    name = "android-tts"

    def __init__(self, self_rate: int):
        from jnius import PythonJavaClass, autoclass, java_method

        class _InitListener(PythonJavaClass):
            __javainterfaces__ = ["android/speech/tts/TextToSpeech$OnInitListener"]
            __javacontext__ = "app"

            def __init__(self, event):
                super().__init__()
                self.event = event
                self.status = -1

            @java_method("(I)V")
            def onInit(self, status):
                self.status = status
                self.event.set()

        TextToSpeech = autoclass("android.speech.tts.TextToSpeech")
        Locale = autoclass("java.util.Locale")
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        activity = PythonActivity.mActivity

        ready = threading.Event()
        self._listener = _InitListener(ready)
        self._tts = TextToSpeech(activity, self._listener)
        if not ready.wait(timeout=8) or self._listener.status != 0:
            raise RuntimeError("Android TextToSpeech engine did not initialize")
        
        self._tts.setLanguage(Locale.US)
        self._tts.setSpeechRate(max(0.5, min(2.0, self_rate / 175.0)))
        self._queue_add = TextToSpeech.QUEUE_ADD
        self._counter = 0

    def say(self, text: str) -> None:
        self._counter += 1
        from jnius import autoclass
        Bundle = autoclass("android.os.Bundle")
        bundle = Bundle()
        # Android Bundle पास करने से JavaException एरर पूरी तरह ठीक हो जाएगा
        result = self._tts.speak(text, self._queue_add, bundle, f"tm{self._counter}")
        if result != 0:
            raise RuntimeError(f"TextToSpeech.speak returned ({result})")

    def close(self) -> None:
        try:
            self._tts.stop()
            self._tts.shutdown()
        except Exception:
            pass


class VoiceAssistant:
    """Thread-safe, non-blocking voice alerts for TradeMatrix."""

    def __init__(self, enabled: bool = True, rate: int = 175):
        self._enabled = bool(enabled)
        self._rate = int(rate)
        self._queue = queue.Queue(maxsize=MAX_QUEUE)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._backend_name = "not started"
        self._backend_failed = False
        self._closed = False
        self._last_error: Optional[str] = None

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
        return self._backend_name

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def speak(self, text: str, interrupt: bool = False) -> bool:
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
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self._queue.put_nowait(message)
        return True

    def clear_queue(self) -> None:
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    def shutdown(self) -> None:
        self._closed = True
        self.clear_queue()
        if self._thread is not None and self._thread.is_alive():
            self._queue.put(_STOP)
            self._thread.join(timeout=2)

    def announce_buy(self, symbol: str, price: float) -> bool:
        return self.speak(f"Buy signal for {self._sym(symbol)} at {price}")

    def announce_take_profit(self, symbol: str, profit_pct: float) -> bool:
        pct = self._to_float(abs(profit_pct))
        return self.speak(f"Take profit triggered for {self._sym(symbol)} with {pct} percent profit", interrupt=True)

    def announce_stop_loss(self, symbol: str, loss_pct: float) -> bool:
        pct = self._to_float(abs(loss_pct))
        return self.speak(f"Stop loss triggered for {self._sym(symbol)} with {pct} percent loss", interrupt=True)

    def announce_status(self, message: str) -> bool:
        return self.speak(message)

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._worker, name="TradeMatrixTTS", daemon=True
                )
                self._thread.start()

    def _make_backend(self):
        classes = [_AndroidBackend] if _is_android() else [_Pyttsx3Backend, _CommandBackend]
        errors = []
        for cls in classes:
            try:
                backend = cls(self._rate)
                self._backend_name = backend.name
                return backend
            except Exception as exc:
                errors.append(f"{cls.name}: {exc}")
        self._last_error = "; ".join(errors) or "no TTS backend available"
        self._backend_name = "none"
        log.warning("TTS unavailable, voice alerts disabled: %s", self._last_error)
        return None

    def _worker(self) -> None:
        backend = self._make_backend()
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
            except Exception as exc:
                failures += 1
                self._last_error = f"{type(backend).__name__}: {exc}"
                log.warning("TTS playback failed (%d/%d): %s", failures, MAX_FAILURES, self._last_error)
                if failures >= MAX_FAILURES:
                    self._backend_failed = True
                    self.clear_queue()
                    break
        backend.close()

    @staticmethod
    def _clean(text) -> str:
        text = "" if text is None else str(text)
        return " ".join(text.split())[:MAX_TEXT_LEN]

    @staticmethod
    def _sym(symbol) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9]", "", str(symbol or "asset"))
        return " ".join(cleaned.split()) or "asset"

    @staticmethod
    def _to_float(value) -> float:
        try:
            v = float(value)
            return v if v == v and abs(v) != float("inf") else 0.0
        except (TypeError, ValueError):
            return 0.0
