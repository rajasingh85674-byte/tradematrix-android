"""
TradeMatrix Voice Assistant Module
Handles Native Android Text-to-Speech (TTS) and Speech-to-Text (STT) for Kivy
"""

import threading
import queue
import logging
import sys

log = logging.getLogger("TradeMatrixVoice")

# -------------------------------------------------------------------
# Android Native Helpers (Pyjnius Integration)
# -------------------------------------------------------------------
try:
    from jnius import autoclass, cast
    from kivy.utils import platform
except ImportError:
    platform = "desktop"


def is_android():
    return platform == "android"


# -------------------------------------------------------------------
# 1. Android Native TTS Backend (Text-to-Speech)
# -------------------------------------------------------------------
class AndroidTTSBackend:
    """Native Android TTS using Direct TextToSpeech(activity, None) to avoid listener crashes."""

    def __init__(self):
        if not is_android():
            raise RuntimeError("AndroidTTSBackend is only available on Android.")

        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        TextToSpeech = autoclass("android.speech.tts.TextToSpeech")

        self.activity = PythonActivity.mActivity
        self._tts = TextToSpeech(self.activity, None)
        self._queue_add = TextToSpeech.QUEUE_ADD
        self._counter = 0

    def say(self, text: str) -> None:
        if not text:
            return

        self._counter += 1
        String = autoclass("java.lang.String")
        Bundle = autoclass("android.os.Bundle")

        java_text = String(text)
        bundle = Bundle()
        utterance_id = f"tm_{self._counter}"

        # Safe native invocation without interface proxies
        res = self._tts.speak(java_text, self._queue_add, bundle, utterance_id)
        if res != 0:
            log.warning(f"TextToSpeech.speak returned non-zero code: {res}")

    def stop(self) -> None:
        if hasattr(self, "_tts") and self._tts:
            self._tts.stop()


# -------------------------------------------------------------------
# 2. Android Native STT / Voice Command (Speech-to-Text)
# -------------------------------------------------------------------
class AndroidSTTBackend:
    """Triggers Android Native Speech Recognizer Intent for Mic input."""

    def __init__(self, callback=None):
        self.callback = callback

    def start_listening(self):
        """Opens Android speech recognition dialog."""
        if not is_android():
            log.info("Speech recognition demo mode (Desktop).")
            return

        try:
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            Intent = autoclass("android.content.Intent")
            RecognizerIntent = autoclass("android.speech.RecognizerIntent")

            intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            intent.putExtra(
                RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM,
            )
            intent.putExtra(RecognizerIntent.EXTRA_PROMPT, "TradeMatrix: Speak Command...")

            # Start activity for result
            PythonActivity.mActivity.startActivityForResult(intent, 5001)
        except Exception as exc:
            log.error(f"Failed to launch speech recognition intent: {exc}")


# -------------------------------------------------------------------
# 3. Desktop Fallback (pyttsx3 / Mock)
# -------------------------------------------------------------------
class DesktopTTSBackend:
    def __init__(self):
        try:
            import pyttsx3
            self.engine = pyttsx3.init()
        except Exception:
            self.engine = None

    def say(self, text: str) -> None:
        if self.engine:
            self.engine.say(text)
            self.engine.runAndWait()
        else:
            print(f"[Voice Output]: {text}")

    def stop(self) -> None:
        if self.engine:
            self.engine.stop()


# -------------------------------------------------------------------
# 4. Main Voice Assistant Controller & Threaded Queue
# -------------------------------------------------------------------
class VoiceAssistant:
    def __init__(self):
        self._speech_queue = queue.Queue()
        self._running = True
        self.backend = None

        # Init backend based on OS
        if is_android():
            try:
                self.backend = AndroidTTSBackend()
                log.info("Initialized Android Native TTS Backend.")
            except Exception as e:
                log.error(f"Failed to init Android TTS: {e}")
                self.backend = DesktopTTSBackend()
        else:
            self.backend = DesktopTTSBackend()

        self.stt = AndroidSTTBackend()

        # Thread for smooth audio playback without blocking Kivy UI thread
        self._worker_thread = threading.Thread(target=self._worker, daemon=True)
        self._worker_thread.start()

    def speak(self, text: str) -> None:
        """Add text to speech queue."""
        if text:
            self._speech_queue.put(text)

    def listen(self) -> None:
        """Trigger Mic button action for voice trading commands."""
        self.stt.start_listening()

    def stop(self) -> None:
        """Stop speech output."""
        while not self._speech_queue.empty():
            try:
                self._speech_queue.get_nowait()
            except queue.Empty:
                break
        if self.backend:
            self.backend.stop()

    def _worker(self) -> None:
        while self._running:
            try:
                text = self._speech_queue.get(timeout=0.5)
                if text is None:
                    break
                if self.backend:
                    self.backend.say(text)
                self._speech_queue.task_done()
            except queue.Empty:
                continue
            except Exception as exc:
                log.error(f"Voice worker thread error: {exc}")

    def shutdown(self) -> None:
        self._running = False
        self._speech_queue.put(None)
