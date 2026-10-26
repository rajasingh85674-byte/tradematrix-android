"""
main.py - TradeMatrix (single file, Kivy / Android) - Zerodha Kite Connect edition

Real broker integration (REST via `requests`):
  * Live LTP (last traded price)  -> dashboard, candle chart (built from LTP polling), EMA / RSI
  * Market BUY / SELL orders      -> POST /orders/regular   (with market_protection)
  * Order status                  -> GET  /orders/<order_id>  (real Order ID / rejection reason in chat)
  * Daily access-token generation -> POST /session/token     (done inside SETTINGS)

Whose account is this?
  The credentials belong to the ACCOUNT HOLDER (for example an approved adult family member).
  The holder creates the Kite Connect app, and logs in on Zerodha's own login page each day. This
  app never sees their Zerodha password or TOTP; it only receives the one-time request_token that
  Zerodha puts in the redirect URL. The holder's name is shown in every order confirmation.

Safety design (real money)
  * Every order is confirmed first: the AI states symbol, quantity, price, value, TP and SL, and
    nothing is sent until you say / type YES (or press CONFIRM). No unattended orders, ever.
  * "Live orders" is OFF by default (DRY-RUN): the whole flow works, nothing is sent to the broker.
    Turning LIVE on in SETTINGS also requires typing the word LIVE. Zerodha has no sandbox.
  * Max order value cap, default quantity, market-hours check, one tracked position at a time,
    dry-run positions can never trigger real orders, no automatic retries of orders.
  * TP / SL lines are drawn on the chart. When price crosses one, the AI alerts you and asks
    whether to SELL; it never sells on its own.

Voice
  * MIC button: Android RecognizerIntent.ACTION_RECOGNIZE_SPEECH via pyjnius.
  * AUTO-SPEAK ON/OFF: native TextToSpeech created directly as TextToSpeech(activity, None),
    with NO PythonJavaClass / OnInitListener anywhere.

buildozer.spec
    requirements = python3,kivy==2.3.0,pyjnius,requests,urllib3,idna,charset-normalizer,certifi
    android.permissions = INTERNET
  (the minimal line  python3,kivy==2.3.0,pyjnius,requests,urllib3  may also work; if the app
   crashes at start with "No module named idna / charset_normalizer / certifi", use the list above)

Commands (typed or spoken): buy reliance | buy 5 tcs | sell | sell 3 infy | yes / no |
  price | status | rsi | trend | analysis | watch infy | set take profit 3 | set stop loss 1 |
  forget position | help
"""
import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from kivy.app import App
from kivy.clock import Clock
from kivy.core.text import Label as CoreLabel
from kivy.core.window import Window
from kivy.graphics import Color, Line, Rectangle, RoundedRectangle
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.switch import Switch
from kivy.uix.textinput import TextInput
from kivy.uix.togglebutton import ToggleButton
from kivy.uix.widget import Widget
from kivy.utils import escape_markup
from kivy.utils import get_color_from_hex as hexc
from kivy.utils import platform

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tradematrix")

# =============================================================================
# CONFIGURATION  -  broker credentials and trading defaults
# -----------------------------------------------------------------------------
# DO NOT put real keys here if this file goes to GitHub: anyone who sees them can trade
# on the account. Leave these empty and enter them in the app (SETTINGS), where they are
# stored only in the app's private folder. Values saved in SETTINGS override these.
# =============================================================================
API_KEY = ""              # Kite Connect API key (of the account holder's Kite Connect app)
API_SECRET = ""           # only needed to generate the daily access token inside the app
ACCESS_TOKEN = ""         # daily token (expires every day; generate it in SETTINGS)
ACCOUNT_HOLDER = ""       # name shown in order confirmations, e.g. "Papa"

WATCH_SYMBOL = "NSE:RELIANCE"   # instrument shown on the dashboard at start
DEFAULT_QTY = 1                 # shares per order unless you say a quantity
MAX_ORDER_VALUE = 10000.0       # refuse any order worth more than this many rupees
PRODUCT = "CNC"                 # CNC = delivery, MIS = intraday
MARKET_PROTECTION = -1          # Kite requires a non-zero value on MARKET orders (-1 = automatic)
ORDER_PROXY = ""                # optional static-IP proxy used ONLY for order calls, e.g.
                                # "http://user:pass@host:port" (see notes about the static IP rule)
TP_PCT = 2.0                    # take-profit distance in percent
SL_PCT = 1.5                    # stop-loss distance in percent
LIVE_ORDERS = False             # False = DRY-RUN (nothing is sent to the broker)

# ---- app constants -----------------------------------------------------------
KITE_BASE = "https://api.kite.trade"
POLL_SECONDS = 2.0              # LTP polling interval while the market is open
CLOSED_POLL_SECONDS = 15.0      # slower polling while the market is closed
CANDLE_SECONDS = 60             # chart candle length (built from LTP samples)
VISIBLE_CANDLES = 60
MAX_CANDLES = 400
EXIT_POLL_TRIES = 8             # seconds spent confirming an order's final status
PENDING_SECONDS = 60            # how long an order confirmation stays valid
IST = timezone(timedelta(hours=5, minutes=30))

BG = "#0B0E14"
PANEL = "#131722"
PANEL2 = "#1B2030"
USER_BUBBLE = "#1E2A4A"
TEXT = "#D1D4DC"
MUTED = "#787B86"
UP = "#26A69A"
DOWN = "#EF5350"
ACCENT = "#2962FF"
AMBER = "#F7B500"
NEUTRAL = "#434651"
GRID = "#1F2430"
MAX_CHAT_BUBBLES = 60

BUY_WORDS = {"buy", "long", "kharid", "kharido", "khareed"}
SELL_WORDS = {"sell", "close", "exit", "bech", "becho"}
YES_WORDS = {"yes", "confirm", "confirmed", "ok", "okay", "haan", "han", "yep", "place"}
NO_WORDS = {"no", "cancel", "nahi", "nope", "abort"}
STOP_WORDS = {"shares", "share", "of", "at", "market", "order", "now", "the", "a", "an",
              "please", "stock", "stocks", "qty", "quantity", "for", "in", "my", "me", "to",
              "and", "rs", "rupees", "lot", "lots", "position", "positions", "trade", "all",
              "everything", "it", "this", "that", "some", "do", "i", "want"}
CRYPTO_WORDS = {"btc", "bitcoin", "eth", "ethereum", "crypto", "usdt", "doge", "xrp", "sol"}
ALIASES = {
    "reliance": "NSE:RELIANCE", "ril": "NSE:RELIANCE", "tcs": "NSE:TCS",
    "infosys": "NSE:INFY", "infy": "NSE:INFY", "hdfc": "NSE:HDFCBANK",
    "hdfcbank": "NSE:HDFCBANK", "icici": "NSE:ICICIBANK", "icicibank": "NSE:ICICIBANK",
    "sbi": "NSE:SBIN", "sbin": "NSE:SBIN", "itc": "NSE:ITC", "wipro": "NSE:WIPRO",
    "airtel": "NSE:BHARTIARTL", "bhartiartl": "NSE:BHARTIARTL", "maruti": "NSE:MARUTI",
    "tatamotors": "NSE:TATAMOTORS", "lt": "NSE:LT", "axisbank": "NSE:AXISBANK",
    "axis": "NSE:AXISBANK", "kotak": "NSE:KOTAKBANK", "adani": "NSE:ADANIENT",
}


# =============================================================================
# helpers
# =============================================================================
def inr(x):
    return "--" if x is None else f"Rs {x:,.2f}"


def ema_series(values, n):
    k = 2.0 / (n + 1)
    out, e = [], None
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def rsi_last(closes, n=14):
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain, avg_loss = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(d, 0.0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-d, 0.0)) / n
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def market_open_now():
    """NSE/BSE cash market regular session: Mon-Fri 09:15-15:30 IST (holidays not known)."""
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    return (9, 15) <= (now.hour, now.minute) < (15, 30)


def load_config(path):
    cfg = {
        "api_key": API_KEY, "api_secret": API_SECRET, "access_token": ACCESS_TOKEN,
        "account_holder": ACCOUNT_HOLDER,
        "live_orders": LIVE_ORDERS, "default_qty": DEFAULT_QTY,
        "max_order_value": MAX_ORDER_VALUE, "product": PRODUCT, "order_proxy": ORDER_PROXY,
        "watch_symbol": WATCH_SYMBOL, "tp_pct": TP_PCT, "sl_pct": SL_PCT, "position": None,
    }
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                cfg.update(data)
    except Exception as exc:  # noqa: BLE001
        log.warning("config load failed: %s", exc)
    return cfg


def save_config(path, cfg):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def open_url(url):
    """Open a web page in the phone's / computer's browser. Returns True on success."""
    try:
        if platform == "android":
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            Uri = autoclass("android.net.Uri")
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            activity.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        else:
            import webbrowser
            webbrowser.open(url)
        return True
    except Exception:  # noqa: BLE001
        return False


def parse_request_token(text):
    text = (text or "").strip()
    m = re.search(r"request_token=([A-Za-z0-9]+)", text)
    if m:
        return m.group(1)
    return text if re.fullmatch(r"[A-Za-z0-9]{16,}", text) else ""


def resolve_symbol(token):
    """'reliance' -> 'NSE:RELIANCE', 'bse:500325' -> 'BSE:500325', unknown -> NSE guess."""
    token = token.strip().lower()
    if ":" in token:
        ex, sym = token.split(":", 1)
        ex, sym = ex.upper(), sym.upper()
        if ex in ("NSE", "BSE") and re.fullmatch(r"[A-Z0-9&\-]{1,20}", sym):
            return f"{ex}:{sym}"
        return None
    if token in ALIASES:
        return ALIASES[token]
    if re.fullmatch(r"[a-z0-9&\-]{1,20}", token):
        return "NSE:" + token.upper()
    return None


def parse_trade(text):
    """(side, quantity, symbol_token) from a spoken/typed command."""
    tokens = re.findall(r"[a-z0-9&\-:\.]+", text.lower())
    side = None
    for t in tokens:
        if t in BUY_WORDS:
            side = "BUY"
            break
        if t in SELL_WORDS:
            side = "SELL"
            break
    qty, sym = None, None
    for t in tokens:
        if t in BUY_WORDS or t in SELL_WORDS or t in STOP_WORDS:
            continue
        if re.fullmatch(r"\d+", t):
            if qty is None:
                qty = int(t)
            continue
        if sym is None:
            sym = t
    return side, qty, sym


# =============================================================================
# Zerodha Kite Connect REST client
# =============================================================================
class BrokerError(Exception):
    """A broker / network failure with a user-friendly message."""

    def __init__(self, message, kind="error", uncertain=False):
        super().__init__(message)
        self.message = message
        self.kind = kind              # "auth", "network", "error"
        self.uncertain = uncertain    # True = an order may or may not have reached the broker


def _parse_kite_response(r):
    try:
        body = r.json()
    except ValueError:
        raise BrokerError(f"Broker returned an unreadable response (HTTP {r.status_code})")
    if r.status_code == 200 and isinstance(body, dict) and body.get("status") == "success":
        return body.get("data")
    msg = (body.get("message") if isinstance(body, dict) else None) or f"HTTP {r.status_code}"
    etype = (body.get("error_type") if isinstance(body, dict) else "") or ""
    low = msg.lower()
    kind = "error"
    if etype == "TokenException" or ("token" in low and r.status_code in (401, 403)):
        kind = "auth"
        msg += ". Your access token is invalid or expired - generate a new one in SETTINGS."
    elif "permission" in low:
        msg += (". Your Kite Connect plan may not include this call "
                "(live market data needs the paid plan).")
    elif re.search(r"\bip\b", low):
        msg += (". Zerodha only accepts API orders from a registered static IP - register "
                "yours in the Kite developer console.")
    raise BrokerError(msg, kind=kind)


class KiteBroker:
    def __init__(self, api_key, access_token, order_proxy=""):
        self.api_key = api_key.strip()
        self.access_token = access_token.strip()
        self.order_proxy = order_proxy.strip()

    def _call(self, method, path, params=None, data=None, is_order=False):
        headers = {"X-Kite-Version": "3",
                   "Authorization": f"token {self.api_key}:{self.access_token}"}
        proxies = None
        if is_order and self.order_proxy:
            proxies = {"http": self.order_proxy, "https": self.order_proxy}
        try:
            r = requests.request(method, KITE_BASE + path, params=params, data=data,
                                 headers=headers, timeout=(6, 12), proxies=proxies)
        except requests.exceptions.ConnectTimeout:
            raise BrokerError("Could not reach the broker (connection timed out)", "network")
        except requests.exceptions.ReadTimeout:
            raise BrokerError("The broker did not answer in time", "network",
                              uncertain=(method == "POST"))
        except requests.exceptions.RequestException as exc:
            raise BrokerError(f"Network error ({exc.__class__.__name__})", "network",
                              uncertain=(method == "POST"))
        return _parse_kite_response(r)

    def ltp(self, instrument):
        data = self._call("GET", "/quote/ltp", params=[("i", instrument)])
        try:
            price = float(data[instrument]["last_price"])
        except (KeyError, TypeError, ValueError):
            raise BrokerError(f"No price found for {instrument}. Check the symbol.")
        if price <= 0:
            raise BrokerError(f"Broker returned an invalid price for {instrument}")
        return price

    def place_market_order(self, exchange, tradingsymbol, side, qty, product):
        try:
            data = self._call("POST", "/orders/regular", data={
                "exchange": exchange, "tradingsymbol": tradingsymbol,
                "transaction_type": side, "order_type": "MARKET", "quantity": str(int(qty)),
                "product": product, "validity": "DAY",
                "market_protection": str(MARKET_PROTECTION), "tag": "TradeMatrix"},
                is_order=True)
        except BrokerError as exc:
            if exc.uncertain:
                exc.message += (" The order may or may not have been placed - check the Kite "
                                "app order book before trying again.")
            raise
        try:
            return str(data["order_id"])
        except (KeyError, TypeError):
            raise BrokerError("Order sent but the broker returned no Order ID. "
                              "Check the Kite app order book.", uncertain=True)

    def order_history(self, order_id):
        data = self._call("GET", f"/orders/{order_id}")
        return data if isinstance(data, list) else []


def generate_session(api_key, api_secret, request_token):
    """Exchange a one-time request_token for the daily access token."""
    checksum = hashlib.sha256((api_key + request_token + api_secret).encode()).hexdigest()
    try:
        r = requests.post(KITE_BASE + "/session/token",
                          data={"api_key": api_key, "request_token": request_token,
                                "checksum": checksum},
                          headers={"X-Kite-Version": "3"}, timeout=(6, 12))
    except requests.exceptions.RequestException as exc:
        raise BrokerError(f"Network error ({exc.__class__.__name__})", "network")
    data = _parse_kite_response(r)
    try:
        return str(data["access_token"])
    except (KeyError, TypeError):
        raise BrokerError("The broker did not return an access token")


# =============================================================================
# text to speech - direct TextToSpeech(activity, None), NO Java listener classes
# =============================================================================
class _AndroidTTS:
    name = "android-tts"
    READY_TIMEOUT = 6.0
    SPEAK_RETRIES = 8
    RETRY_DELAY = 0.25

    def __init__(self):
        from jnius import autoclass          # lazy import: only exists on Android

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

        self._tts = TextToSpeech(activity, None)      # listener argument is None on purpose

        # Without a listener there is no "ready" callback. setLanguage() returns a negative
        # code until the engine is bound, so poll it for a few seconds.
        deadline = time.monotonic() + self.READY_TIMEOUT
        while True:
            try:
                result = int(self._tts.setLanguage(Locale.US))
            except Exception:  # noqa: BLE001
                result = -2
            if result >= 0 or time.monotonic() >= deadline:
                break
            time.sleep(0.2)

    def say(self, text):
        java_text = self._String(text)        # real java.lang.String (a CharSequence)
        last = None
        for _ in range(self.SPEAK_RETRIES):
            self._counter += 1
            utterance_id = f"tradematrix-{self._counter}-{int(time.time() * 1000)}"
            result = self._tts.speak(java_text, self._queue_add, self._Bundle(), utterance_id)
            if result == self._success:
                return
            last = result
            time.sleep(self.RETRY_DELAY)
        raise RuntimeError(f"TextToSpeech.speak failed (code {last}); is a TTS engine installed?")

    def interrupt(self):
        try:
            self._tts.stop()
        except Exception:  # noqa: BLE001
            pass

    def close(self):
        try:
            self._tts.stop()
            self._tts.shutdown()
        except Exception:  # noqa: BLE001
            pass


class _DesktopTTS:
    name = "pyttsx3"

    def __init__(self):
        import pyttsx3
        self._engine = pyttsx3.init()

    def say(self, text):
        self._engine.say(text)
        self._engine.runAndWait()

    def interrupt(self):
        try:
            self._engine.stop()
        except Exception:  # noqa: BLE001
            pass

    def close(self):
        self.interrupt()


_TTS_STOP = object()


class SpeechEngine:
    """Non-blocking, thread-safe speech output. Never raises."""

    def __init__(self):
        self._queue = queue.Queue(maxsize=6)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._backend = None
        self._failed = False
        self._closed = False
        self.last_error: Optional[str] = None

    def speak(self, text):
        try:
            text = " ".join(str(text or "").split())[:300]
            if not text or self._failed or self._closed:
                return
            with self._lock:
                if self._thread is None or not self._thread.is_alive():
                    self._thread = threading.Thread(target=self._worker, name="TradeMatrixTTS",
                                                    daemon=True)
                    self._thread.start()
            try:
                self._queue.put_nowait(text)
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                self._queue.put_nowait(text)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{exc.__class__.__name__}: {exc}"

    def _clear(self):
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    def stop(self):
        try:
            self._clear()
            if self._backend is not None:
                self._backend.interrupt()
        except Exception:  # noqa: BLE001
            pass

    def shutdown(self):
        try:
            self._closed = True
            self.stop()
            if self._thread is not None and self._thread.is_alive():
                self._queue.put_nowait(_TTS_STOP)
                self._thread.join(timeout=2)
        except Exception:  # noqa: BLE001
            pass

    def _worker(self):
        try:
            backend = _AndroidTTS() if platform == "android" else _DesktopTTS()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{exc.__class__.__name__}: {exc}"
            log.warning("TTS unavailable: %s", self.last_error)
            self._failed = True
            self._clear()
            return
        self._backend = backend
        failures = 0
        while True:
            item = self._queue.get()
            if item is _TTS_STOP:
                break
            try:
                backend.say(item)
                failures = 0
                self.last_error = None
            except Exception as exc:  # noqa: BLE001
                failures += 1
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                log.warning("TTS failed (%d/3): %s", failures, self.last_error)
                if failures >= 3:
                    self._failed = True
                    self._clear()
                    break
        self._backend = None
        backend.close()


# =============================================================================
# speech to text - Android RecognizerIntent.ACTION_RECOGNIZE_SPEECH via pyjnius
# =============================================================================
class MicInput:
    @staticmethod
    def listen(locale, on_result):
        """on_result(text_or_None, error_or_None) is always called on the Kivy thread."""
        if platform != "android":
            on_result(None, "Mic input works on Android. Type your command instead.")
            return
        try:
            from android import activity
            from jnius import autoclass

            Intent = autoclass("android.content.Intent")
            RecognizerIntent = autoclass("android.speech.RecognizerIntent")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            request_code = 4242

            def _on_activity_result(req, res, data):
                if req != request_code:
                    return
                try:
                    activity.unbind(on_activity_result=_on_activity_result)
                except Exception:  # noqa: BLE001
                    pass
                heard = None
                try:
                    if res == -1 and data is not None:               # Activity.RESULT_OK
                        arr = data.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
                        if arr is not None and arr.size() > 0:
                            heard = str(arr.get(0))
                except Exception:  # noqa: BLE001
                    heard = None
                Clock.schedule_once(lambda dt: on_result(
                    heard, None if heard else "Could not hear anything."))

            activity.bind(on_activity_result=_on_activity_result)
            intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                            RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE, locale)
            intent.putExtra(RecognizerIntent.EXTRA_PROMPT, "Say a trading command")
            PythonActivity.mActivity.startActivityForResult(intent, request_code)
        except Exception as exc:  # noqa: BLE001
            on_result(None, f"Voice input unavailable ({exc.__class__.__name__}).")


# =============================================================================
# widgets
# =============================================================================
class Panel(BoxLayout):
    def __init__(self, bg=PANEL, radius=10, **kw):
        super().__init__(**kw)
        with self.canvas.before:
            self._color = Color(*hexc(bg))
            self._rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(radius)])
        self.bind(pos=self._upd, size=self._upd)

    def _upd(self, *_):
        self._rect.pos, self._rect.size = self.pos, self.size

    def set_bg(self, hexcolor):
        self._color.rgba = hexc(hexcolor)


def make_label(text="", size=14, color=TEXT, bold=False, halign="center", **kw):
    lb = Label(text=text, font_size=sp(size), color=hexc(color), bold=bold,
               halign=halign, valign="middle", **kw)
    lb.bind(size=lambda w, s: setattr(w, "text_size", s))
    return lb


def flat_button(text, bg, **kw):
    return Button(text=text, bold=True, font_size=sp(14), background_normal="",
                  background_color=hexc(bg), color=(1, 1, 1, 1), **kw)


class Bubble(BoxLayout):
    """One chat message."""

    def __init__(self, who, text, **kw):
        super().__init__(size_hint_y=None, padding=dp(10), **kw)
        is_user = who == "You"
        with self.canvas.before:
            Color(*hexc(USER_BUBBLE if is_user else PANEL2))
            self._rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(10)])
        self.bind(pos=self._upd, size=self._upd)
        name_col = (ACCENT if is_user else UP).lstrip("#")
        self.lb = Label(text=f"[color={name_col}][b]{who}[/b][/color]\n{escape_markup(text)}",
                        markup=True, size_hint_y=None, halign="left", valign="top",
                        font_size=sp(14), color=hexc(TEXT))
        self.lb.bind(width=lambda w, _v: setattr(w, "text_size", (w.width, None)),
                     texture_size=self._fit)
        self.add_widget(self.lb)
        self.height = dp(48)

    def _upd(self, *_):
        self._rect.pos, self._rect.size = self.pos, self.size

    def _fit(self, _w, ts):
        self.lb.height = ts[1]
        self.height = ts[1] + dp(20)


class ChartWidget(Widget):
    """Candlestick chart with EMA line, price tag and ENTRY / TP / SL trade lines."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.candles = []
        self.ema = []
        self.levels = None
        self.price = None
        self.hint = "Waiting for market data..."
        self._tex_cache = {}
        self.bind(pos=self._redraw, size=self._redraw)

    def set_data(self, candles, ema, price, levels, hint=""):
        self.candles, self.ema, self.price, self.levels = candles, ema, price, levels
        self.hint = hint or self.hint
        self._redraw()

    def _tex(self, text, size):
        key = (text, size)
        tex = self._tex_cache.get(key)
        if tex is None:
            lb = CoreLabel(text=text, font_size=size)
            lb.refresh()
            tex = lb.texture
            if len(self._tex_cache) > 300:
                self._tex_cache.clear()
            self._tex_cache[key] = tex
        return tex

    def _redraw(self, *_):
        self.canvas.clear()
        if self.width < dp(40) or self.height < dp(40):
            return
        x0, y0, w, h = self.x, self.y, self.width, self.height
        pad_r = dp(92)
        plot_w = w - pad_r
        margin = dp(8)
        font = sp(10)

        with self.canvas:
            Color(*hexc(PANEL))
            Rectangle(pos=self.pos, size=self.size)
            if not self.candles:
                tex = self._tex(self.hint, sp(12))
                Color(*hexc(MUTED))
                Rectangle(texture=tex, size=tex.size,
                          pos=(x0 + (w - tex.size[0]) / 2, y0 + (h - tex.size[1]) / 2))
                return

            n = min(len(self.candles), VISIBLE_CANDLES)
            vis = self.candles[-n:]
            ema_vis = self.ema[-n:]
            values = [c["l"] for c in vis] + [c["h"] for c in vis]
            tags = []
            if self.price is not None:
                tags.append((self.price, "", "#FFFFFF"))
            if self.levels:
                tags += [(self.levels["entry"], "ENTRY ", ACCENT),
                         (self.levels["tp"], "TP ", UP),
                         (self.levels["sl"], "SL ", DOWN)]
                values += [self.levels["entry"], self.levels["tp"], self.levels["sl"]]
            lo, hi = min(values), max(values)
            span = (hi - lo) or max(abs(hi) * 0.002, 0.01)
            lo -= span * 0.08
            hi += span * 0.08
            span = hi - lo

            def py(p):
                return y0 + margin + (p - lo) / span * (h - 2 * margin)

            tag_ys = [py(t[0]) for t in tags]
            for k in range(1, 5):
                p = lo + span * k / 5.0
                y = py(p)
                Color(*hexc(GRID))
                Line(points=[x0, y, x0 + plot_w, y], width=1)
                if all(abs(y - ty) > dp(14) for ty in tag_ys):
                    tex = self._tex(f"{p:,.2f}", font)
                    Color(*hexc(MUTED))
                    Rectangle(texture=tex, size=tex.size,
                              pos=(x0 + plot_w + dp(4), y - tex.size[1] / 2))

            slot = plot_w / float(VISIBLE_CANDLES)
            body_w = max(1.0, slot * 0.62)
            for i, c in enumerate(vis):
                cx = x0 + plot_w - (n - i - 0.5) * slot
                Color(*hexc(UP if c["c"] >= c["o"] else DOWN))
                Line(points=[cx, py(c["l"]), cx, py(c["h"])], width=1)
                top, bot = py(max(c["o"], c["c"])), py(min(c["o"], c["c"]))
                Rectangle(pos=(cx - body_w / 2, bot), size=(body_w, max(1.0, top - bot)))

            pts = []
            for i, e in enumerate(ema_vis):
                pts += [x0 + plot_w - (n - i - 0.5) * slot, py(e)]
            if len(pts) >= 4:
                Color(*hexc(AMBER))
                Line(points=pts, width=1.2)

            for price, label, colour in tags:
                y = min(max(py(price), y0 + dp(9)), y0 + h - dp(9))
                if label:                                   # dashed trade line
                    Color(*hexc(colour))
                    Line(points=[x0, y, x0 + plot_w, y], width=1.2,
                         dash_length=dp(6), dash_offset=dp(4))
                tex = self._tex(f"{label}{price:,.2f}", font)
                tw, th = tex.size
                Color(*hexc(colour))
                Rectangle(pos=(x0 + plot_w + dp(2), y - th / 2 - dp(1)), size=(tw + dp(6), th + dp(2)))
                if colour in ("#FFFFFF", UP, AMBER):
                    Color(0, 0, 0, 1)
                else:
                    Color(1, 1, 1, 1)
                Rectangle(texture=tex, size=tex.size, pos=(x0 + plot_w + dp(5), y - th / 2))


# =============================================================================
# the app
# =============================================================================
class TradeMatrixApp(App):
    title = "TradeMatrix"

    # ------------------------------------------------------------------ build
    def build(self):
        if platform not in ("android", "ios"):
            Window.size = (420, 840)
        else:
            Window.softinput_mode = "below_target"
        Window.clearcolor = hexc(BG)

        self.cfg = load_config(self._cfg_path())
        self.speech = SpeechEngine()
        self.auto_speak = True
        self.broker: Optional[KiteBroker] = None
        self.watch_symbol = str(self.cfg.get("watch_symbol") or WATCH_SYMBOL)
        self.position = self.cfg.get("position") if isinstance(self.cfg.get("position"), dict) else None
        self.pending = None
        self.alerted = None
        self.candles = []
        self.ema_values = []
        self.rsi = None
        self.last_price = None
        self.prev_price = None
        self.ltp_busy = False
        self.order_busy = False
        self.mic_busy = False
        self.auth_warned = False
        self.last_poll = 0.0
        self.dry_counter = 0
        self.tp_pct = float(self.cfg.get("tp_pct", TP_PCT))
        self.sl_pct = float(self.cfg.get("sl_pct", SL_PCT))
        self._poll_event = None
        self._rebuild_broker()

        root = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(6))

        # --- title bar -----------------------------------------------------
        bar = Panel(size_hint_y=None, height=dp(44), padding=[dp(12), dp(4)], spacing=dp(6))
        bar.add_widget(make_label("TRADEMATRIX", 17, ACCENT, bold=True, halign="left"))
        self.lb_symbol = make_label(self.watch_symbol, 12, MUTED, halign="right")
        bar.add_widget(self.lb_symbol)
        self.badge = Panel(bg=AMBER, radius=6, size_hint_x=None, width=dp(78), padding=dp(3))
        self.lb_badge = make_label("DRY-RUN", 11, "#000000", bold=True)
        self.badge.add_widget(self.lb_badge)
        bar.add_widget(self.badge)
        btn_settings = flat_button("SETTINGS", NEUTRAL, size_hint_x=None, width=dp(86))
        btn_settings.font_size = sp(12)
        btn_settings.bind(on_release=lambda *_: self.open_settings())
        bar.add_widget(btn_settings)
        root.add_widget(bar)

        # --- dashboard ------------------------------------------------------
        dash = Panel(orientation="vertical", size_hint_y=0.34, padding=dp(6), spacing=dp(4))
        stats = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        self.lb_price = make_label("--", 24, TEXT, bold=True, halign="left", size_hint_x=0.46)
        self.lb_ema = make_label("EMA 20\n--", 12, AMBER, size_hint_x=0.27)
        self.lb_rsi = make_label("RSI 14\n--", 12, TEXT, size_hint_x=0.27)
        for w in (self.lb_price, self.lb_ema, self.lb_rsi):
            stats.add_widget(w)
        dash.add_widget(stats)
        self.chart = ChartWidget()
        dash.add_widget(self.chart)
        root.add_widget(dash)

        # --- active trade banner -------------------------------------------
        self.banner = Panel(orientation="vertical", size_hint_y=None, height=dp(58),
                            padding=[dp(10), dp(4)])
        self.lb_banner1 = make_label("", 13, TEXT, bold=True, halign="left")
        self.lb_banner2 = make_label("", 12, MUTED, halign="left")
        self.banner.add_widget(self.lb_banner1)
        self.banner.add_widget(self.lb_banner2)
        root.add_widget(self.banner)

        # --- chat history ---------------------------------------------------
        self.sv_chat = ScrollView(do_scroll_x=False, bar_width=dp(3), size_hint_y=0.36)
        self.chat_box = BoxLayout(orientation="vertical", spacing=dp(6), size_hint_y=None,
                                  padding=[0, dp(2)])
        self.chat_box.bind(minimum_height=self.chat_box.setter("height"))
        self.sv_chat.add_widget(self.chat_box)
        root.add_widget(self.sv_chat)

        self.lb_status = make_label("", 12, MUTED, size_hint_y=None, height=dp(20))
        root.add_widget(self.lb_status)

        # --- order confirmation row (hidden until an order is pending) -------
        self.confirm_row = BoxLayout(size_hint_y=None, height=0, spacing=dp(8), opacity=0)
        self.btn_confirm = flat_button("CONFIRM ORDER", UP)
        self.btn_cancel = flat_button("CANCEL", NEUTRAL, size_hint_x=0.6)
        self.btn_confirm.bind(on_release=lambda *_: self._ui_confirm())
        self.btn_cancel.bind(on_release=lambda *_: self._ui_cancel())
        self.confirm_row.add_widget(self.btn_confirm)
        self.confirm_row.add_widget(self.btn_cancel)
        root.add_widget(self.confirm_row)

        # --- AUTO-SPEAK toggle ---------------------------------------------
        self.tg_speak = ToggleButton(text="AUTO-SPEAK: ON", state="down", bold=True,
                                     font_size=sp(13), size_hint_y=None, height=dp(34),
                                     background_normal="", background_down="",
                                     background_color=hexc(UP), color=(1, 1, 1, 1))
        self.tg_speak.bind(state=self._speak_toggled)
        root.add_widget(self.tg_speak)

        # --- input row ------------------------------------------------------
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        self.ti_chat = TextInput(hint_text="e.g. buy reliance, sell, status...",
                                 multiline=False, font_size=sp(14), write_tab=False,
                                 background_color=hexc(PANEL), foreground_color=hexc(TEXT),
                                 cursor_color=hexc(TEXT), hint_text_color=hexc(MUTED))
        self.ti_chat.bind(on_text_validate=lambda *_: self.on_send())
        btn_mic = flat_button("MIC", ACCENT, size_hint_x=None, width=dp(64))
        btn_send = flat_button("SEND", UP, size_hint_x=None, width=dp(72))
        btn_mic.bind(on_release=lambda *_: self.on_mic())
        btn_send.bind(on_release=lambda *_: self.on_send())
        row.add_widget(self.ti_chat)
        row.add_widget(btn_mic)
        row.add_widget(btn_send)
        root.add_widget(row)

        self._show_confirm(False)
        self._refresh_badge()
        self._update_banner(None)
        self._render_chart()
        hello = ("Hello! I am your TradeMatrix assistant. Every order needs your explicit "
                 "confirmation. Say help to see commands.")
        if not self.broker:
            hello += " Open SETTINGS to add the Zerodha Kite API key and access token."
        elif not self.cfg.get("live_orders"):
            hello += " Live orders are OFF, so orders are dry runs."
        if self.position:
            hello += " I restored your tracked position from last time."
        self.add_chat("TradeMatrix AI", hello)
        self._poll_event = Clock.schedule_interval(self.poll_ltp, POLL_SECONDS)
        Clock.schedule_once(lambda dt: self.poll_ltp(0), 0.4)
        return root

    # ------------------------------------------------------------- basics
    def _cfg_path(self):
        return os.path.join(self.user_data_dir, "tradematrix_config.json")

    def _save_cfg(self):
        try:
            save_config(self._cfg_path(), self.cfg)
            return True
        except OSError as exc:
            log.warning("config save failed: %s", exc)
            return False

    def _rebuild_broker(self):
        key = str(self.cfg.get("api_key", "")).strip()
        tok = str(self.cfg.get("access_token", "")).strip()
        self.broker = KiteBroker(key, tok, str(self.cfg.get("order_proxy", ""))) if key and tok else None
        self.auth_warned = False

    def _live(self):
        return bool(self.cfg.get("live_orders"))

    def _refresh_badge(self):
        live = self._live()
        self.badge.set_bg(DOWN if live else AMBER)
        self.lb_badge.text = "LIVE" if live else "DRY-RUN"
        self.lb_badge.color = (1, 1, 1, 1) if live else (0, 0, 0, 1)

    def _set_status(self, text, color=MUTED):
        self.lb_status.text = text
        self.lb_status.color = hexc(color)

    def run_bg(self, fn, done):
        """Run fn() in a worker thread; call done(result, error) on the Kivy thread."""
        def worker():
            try:
                res, err = fn(), None
            except BrokerError as exc:
                res, err = None, exc
            except Exception as exc:  # noqa: BLE001
                res, err = None, BrokerError(f"{exc.__class__.__name__}: {exc}")
            Clock.schedule_once(lambda dt: done(res, err))

        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------------------------------------------- chat
    def add_chat(self, who, text):
        self.chat_box.add_widget(Bubble(who, text))
        while len(self.chat_box.children) > MAX_CHAT_BUBBLES:
            self.chat_box.remove_widget(self.chat_box.children[-1])
        Clock.schedule_once(lambda dt: setattr(self.sv_chat, "scroll_y", 0), 0.08)

    def ai_say(self, text):
        """Show an assistant message and speak it if AUTO-SPEAK is ON."""
        self.add_chat("TradeMatrix AI", text)
        if self.auto_speak:
            self.speech.speak(text)

    def _speak_toggled(self, _w, state):
        self.auto_speak = state == "down"
        self.tg_speak.text = f"AUTO-SPEAK: {'ON' if self.auto_speak else 'OFF'}"
        self.tg_speak.background_color = hexc(UP if self.auto_speak else NEUTRAL)
        if not self.auto_speak:
            self.speech.stop()
        self._set_status("Voice replies ON" if self.auto_speak else "Voice replies muted")

    def on_send(self):
        text = self.ti_chat.text
        self.ti_chat.text = ""
        self.submit(text)

    def on_mic(self):
        if self.mic_busy:
            return
        self.mic_busy = True
        self._set_status("Listening...", ACCENT)
        MicInput.listen("en-IN", self._heard)

    def _heard(self, text, err):
        self.mic_busy = False
        if text:
            self._set_status(f'Heard: "{text[:60]}"')
            self.submit(text)
        else:
            self._set_status(err or "Voice input failed.", DOWN)

    def submit(self, text):
        text = (text or "").strip()
        if not text:
            return
        self.add_chat("You", text)
        try:
            reply = self.handle_command(text)
        except Exception:  # noqa: BLE001
            log.exception("command failed")
            reply = "Sorry, something went wrong with that command."
        if reply:
            self.ai_say(reply)

    # ------------------------------------------------------- command brain
    def handle_command(self, text):
        t = text.lower().strip()
        words = set(re.findall(r"[a-z]+", t))

        tp_m = re.search(r"(?:take\s*profit|\btp\b)\D{0,12}?(\d+(?:\.\d+)?)", t)
        sl_m = re.search(r"(?:stop\s*loss|\bsl\b)\D{0,12}?(\d+(?:\.\d+)?)", t)
        if tp_m or sl_m:
            msgs = []
            for match, name, attr in ((tp_m, "take profit", "tp_pct"),
                                      (sl_m, "stop loss", "sl_pct")):
                if match:
                    value = float(match.group(1))
                    if 0.1 <= value <= 50:
                        setattr(self, attr, value)
                        self.cfg[attr] = value
                        msgs.append(f"{name} is now {value:g} percent")
                    else:
                        msgs.append(f"{name} must be between 0.1 and 50 percent")
            self._save_cfg()
            return ". ".join(msgs) + ". This applies to your next order."

        if len(words) <= 3 and words & YES_WORDS:
            return self.confirm_pending()
        if len(words) <= 3 and words & NO_WORDS:
            return self.cancel_pending()

        side, _qty, _tok = parse_trade(t)
        if side:
            return self.start_order(t)

        if words & {"forget", "clear", "reset"} and words & {"position", "trade"}:
            self.position = None
            self.cfg["position"] = None
            self._save_cfg()
            self.alerted = None
            self._update_banner(self.last_price)
            self._render_chart()
            return "Okay, I stopped tracking the position. This does not sell anything."
        if words & {"watch", "show", "chart", "switch"}:
            return self.start_watch(t)
        if words & {"status", "position", "pnl", "profit", "trades"}:
            return self.status_text()
        if "rsi" in words:
            return self.rsi_text()
        if words & {"ema", "trend"}:
            return self.trend_text()
        if words & {"price", "rate", "ltp", "bhav", "bhaav", "kitna"}:
            return (f"{self._name(self.watch_symbol)} is at {inr(self.last_price)}."
                    if self.last_price else "I do not have a live price yet.")
        if words & {"analysis", "analyze", "analyse", "market", "outlook", "summary", "update"}:
            return self.analysis_text()
        if words & {"help", "commands", "options"}:
            return ("You can say: buy reliance, buy 5 TCS, sell, sell 3 infy, yes or no to "
                    "confirm, price, status, RSI, trend, analysis, watch infy, set take profit 3, "
                    "set stop loss 1, or forget position.")
        return "I did not understand that. Say help to hear what I can do."

    @staticmethod
    def _name(instrument):
        return instrument.split(":", 1)[-1]

    def rsi_text(self):
        if self.rsi is None:
            return f"RSI needs 15 one-minute candles; I have {len(self.candles)} so far."
        zone = "oversold" if self.rsi < 30 else "overbought" if self.rsi > 70 else "neutral"
        return f"RSI 14 is {self.rsi:.0f}, which is {zone}."

    def trend_text(self):
        if not self.ema_values or self.last_price is None:
            return "I need more candles before I can describe the trend."
        ema = self.ema_values[-1]
        above = self.last_price > ema
        return (f"Price {inr(self.last_price)} is {'above' if above else 'below'} the EMA 20 at "
                f"{inr(ema)}, so the short-term trend is {'up' if above else 'down'}.")

    def analysis_text(self):
        if not self.ema_values or self.rsi is None or self.last_price is None:
            return (f"I am still collecting data: {len(self.candles)} of 15 candles needed "
                    f"for RSI.")
        price, ema = self.last_price, self.ema_values[-1]
        score = (1 if price > ema else -1) + (1 if self.rsi < 30 else -1 if self.rsi > 70 else 0)
        bias = "bullish" if score > 0 else "bearish" if score < 0 else "neutral"
        return (f"{self._name(self.watch_symbol)} is at {inr(price)}, "
                f"{'above' if price > ema else 'below'} its EMA 20. RSI is {self.rsi:.0f}. "
                f"Short-term bias looks {bias}. This is not financial advice.")

    def status_text(self):
        pos = self.position
        if not pos:
            return "No position is being tracked. Say for example: buy reliance."
        price = self.last_price if pos["instrument"] == self.watch_symbol and self.last_price else None
        pnl = f" Now {(price - pos['entry']) / pos['entry'] * 100:+.2f} percent." if price else ""
        return (f"{'Dry run ' if pos.get('dry') else ''}long {pos['qty']} "
                f"{self._name(pos['instrument'])} from {inr(pos['entry'])}.{pnl} "
                f"Target {inr(pos['tp'])}, stop loss {inr(pos['sl'])}.")

    # -------------------------------------------------------------- watch
    def start_watch(self, t):
        tokens = [x for x in re.findall(r"[a-z0-9&\-:\.]+", t)
                  if x not in {"watch", "show", "chart", "switch", "me", "the", "to"} | STOP_WORDS]
        if not tokens:
            return "Which stock? For example: watch infy."
        if tokens[0] in CRYPTO_WORDS:
            return "Crypto cannot be traded through Zerodha. Try an NSE stock like TCS."
        instrument = resolve_symbol(tokens[0])
        if not instrument:
            return "I did not recognise that symbol."
        if not self.broker:
            return "Add the Kite API key and access token in SETTINGS first."
        self.add_chat("TradeMatrix AI", f"Checking {instrument}...")

        def done(price, err):
            if err:
                self.ai_say(f"Could not switch: {err.message}")
                return
            self.watch_symbol = instrument
            self.cfg["watch_symbol"] = instrument
            self._save_cfg()
            self.lb_symbol.text = instrument
            self.candles, self.ema_values, self.rsi = [], [], None
            self.last_price = self.prev_price = None
            self.alerted = None
            self._on_new_price(price)
            self.ai_say(f"Now watching {self._name(instrument)} at {inr(price)}.")

        self.run_bg(lambda: self.broker.ltp(instrument), done)
        return None

    # ------------------------------------------------------ order: prepare
    def start_order(self, t):
        side, qty, tok = parse_trade(t)
        if tok in CRYPTO_WORDS:
            return ("Crypto such as BTC cannot be traded through Zerodha Kite - it only trades "
                    "Indian exchange instruments. Try: buy reliance.")
        if not self.broker:
            return "Add the Kite API key and access token in SETTINGS first."
        if self.order_busy:
            return "An order is being sent. Please wait for its result."
        if self.pending:
            return "Please answer the pending order first: say yes to place it or no to cancel."
        if self._live() and not market_open_now():
            return ("The market is closed (regular session is Monday to Friday, 9:15 to 15:30 "
                    "India time). Regular orders would be rejected, so I did not prepare one.")

        if side == "BUY" and self.position:
            return (f"I am already tracking a position in {self._name(self.position['instrument'])}. "
                    f"Sell it first, or say forget position if you already closed it elsewhere.")
        if side == "SELL" and tok is None:
            if not self.position:
                return "No position is tracked. Say for example: sell 5 infy."
            instrument = self.position["instrument"]
            qty = qty or self.position["qty"]
        elif tok is None:
            instrument = self.watch_symbol
        else:
            instrument = resolve_symbol(tok)
            if not instrument:
                return "I did not recognise that symbol. Try the NSE name, like TCS."
        qty = qty or int(self.cfg.get("default_qty", DEFAULT_QTY))
        if qty < 1 or qty > 100000:
            return "Quantity must be between 1 and 100000."

        self.add_chat("TradeMatrix AI", f"Fetching the latest price for {instrument}...")

        def done(ltp, err):
            if err:
                self.ai_say(f"I could not prepare the order: {err.message}")
                return
            self._prepared(side, instrument, qty, ltp)

        self.run_bg(lambda: self.broker.ltp(instrument), done)
        return None

    def _prepared(self, side, instrument, qty, ltp):
        if self.pending or self.order_busy:
            return
        value = qty * ltp
        cap = float(self.cfg.get("max_order_value", MAX_ORDER_VALUE))
        if value > cap:
            self.ai_say(f"Order refused: {qty} x {self._name(instrument)} is worth {inr(value)}, "
                        f"above your limit of {inr(cap)}. Lower the quantity or raise the limit "
                        f"in SETTINGS.")
            return
        product = str(self.cfg.get("product", PRODUCT))
        tp = ltp * (1 + self.tp_pct / 100.0)
        sl = ltp * (1 - self.sl_pct / 100.0)
        pos = self.position
        # a dry-run position must never turn into a real order (a real SELL could short-sell)
        dry_pos = bool(side == "SELL" and pos and pos["instrument"] == instrument and pos.get("dry"))
        live_mode = self._live() and not dry_pos
        self.pending = {"side": side, "instrument": instrument, "qty": qty, "ltp": ltp,
                        "product": product, "tp": tp, "sl": sl, "reason": "USER",
                        "dry_pos": dry_pos, "expires": time.time() + PENDING_SECONDS}
        holder = str(self.cfg.get("account_holder", "")).strip()
        acct = f" on {holder}'s account" if (holder and live_mode) else ""
        mode = "LIVE ORDER" if live_mode else "DRY RUN, no real order"
        text = (f"Please confirm {mode}{acct}: {side} {qty} x {self._name(instrument)} on "
                f"{instrument.split(':')[0]}, market order, product {product}. Latest price "
                f"{inr(ltp)}, about {inr(value)}.")
        if side == "BUY":
            text += f" Target {inr(tp)}, stop loss {inr(sl)}."
        text += " Say yes to place it, or no to cancel."
        self._show_confirm(True, side)
        self.ai_say(text)

    # -------------------------------------------------- order: confirm/cancel
    def _show_confirm(self, show, side="BUY"):
        self.confirm_row.height = dp(44) if show else 0
        self.confirm_row.opacity = 1 if show else 0
        self.btn_confirm.disabled = self.btn_cancel.disabled = not show
        self.btn_confirm.text = "CONFIRM " + side if show else "CONFIRM ORDER"
        self.btn_confirm.background_color = hexc(UP if side == "BUY" else DOWN)

    def _ui_confirm(self):
        reply = self.confirm_pending()
        if reply:
            self.ai_say(reply)

    def _ui_cancel(self):
        reply = self.cancel_pending()
        if reply:
            self.ai_say(reply)

    def cancel_pending(self):
        if not self.pending:
            return "There is no pending order to cancel."
        self.pending = None
        self._show_confirm(False)
        return "Okay, the order is cancelled. Nothing was sent."

    def confirm_pending(self):
        p = self.pending
        if not p:
            return "There is no pending order to confirm."
        if time.time() > p["expires"]:
            self.pending = None
            self._show_confirm(False)
            return "That confirmation expired. Nothing was sent. Please ask again."
        if self.order_busy:
            return "An order is already being sent."
        self.pending = None
        self._show_confirm(False)
        self.order_busy = True
        broker = self.broker
        live = self._live() and not p.get("dry_pos")
        self.add_chat("TradeMatrix AI",
                      "Sending order to the broker..." if live else "Running dry run...")
        self._set_status("Sending order...", ACCENT)

        def work():
            return self._execute_order(broker, live, p)

        def done(res, err):
            self.order_busy = False
            self._set_status("")
            if err:
                self.ai_say(f"Order failed: {err.message}")
                return
            self._order_done(res, p)

        self.run_bg(work, done)
        return None

    # ------------------------------------------------------ order: execute
    def _execute_order(self, broker, live, p):
        """Runs in a worker thread. Returns a result dict; raises BrokerError on failure."""
        exchange, tradingsymbol = p["instrument"].split(":", 1)
        if not live:
            self.dry_counter += 1
            return {"order_id": f"DRYRUN-{self.dry_counter:03d}", "status": "COMPLETE",
                    "avg": p["ltp"], "filled": p["qty"], "message": "", "dry": True}
        if broker is None:
            raise BrokerError("No broker connection. Add credentials in SETTINGS.")
        order_id = broker.place_market_order(exchange, tradingsymbol, p["side"], p["qty"],
                                             p["product"])
        last, history_error = None, None
        for _ in range(EXIT_POLL_TRIES):
            time.sleep(1.0)
            try:
                hist = broker.order_history(order_id)
            except BrokerError as exc:
                history_error = exc.message
                continue
            if hist:
                last = hist[-1]
                if str(last.get("status", "")).upper() in ("COMPLETE", "REJECTED", "CANCELLED"):
                    break
        if last is None:
            return {"order_id": order_id, "status": "UNKNOWN", "avg": 0.0, "filled": 0,
                    "message": history_error or "status unavailable", "dry": False}
        return {"order_id": order_id, "status": str(last.get("status", "")).upper(),
                "avg": float(last.get("average_price") or 0.0),
                "filled": int(last.get("filled_quantity") or 0),
                "message": str(last.get("status_message") or ""), "dry": False}

    def _order_done(self, res, p):
        sym = self._name(p["instrument"])
        oid, status = res["order_id"], res["status"]
        tag = "Dry run" if res.get("dry") else "Order"

        if status in ("REJECTED", "CANCELLED"):
            reason = f" Reason: {res['message']}." if res["message"] else ""
            self.ai_say(f"{tag} {oid} was {status.lower()} by the broker.{reason}")
            return
        if status != "COMPLETE":
            self.ai_say(f"Order {oid} was sent, but I could not confirm it filled "
                        f"(status {status.lower()}). Check the Kite app order book before doing "
                        f"anything else.")
            return

        price = res["avg"] if res["avg"] > 0 else p["ltp"]
        qty = res["filled"] or p["qty"]
        if p["side"] == "BUY":
            self.position = {"instrument": p["instrument"], "qty": qty, "entry": price,
                             "tp": price * (1 + self.tp_pct / 100.0),
                             "sl": price * (1 - self.sl_pct / 100.0),
                             "order_id": oid, "dry": bool(res.get("dry")),
                             "product": p["product"]}
            self.alerted = None
            if p["instrument"] != self.watch_symbol:
                self.watch_symbol = p["instrument"]
                self.cfg["watch_symbol"] = p["instrument"]
                self.lb_symbol.text = p["instrument"]
                self.candles, self.ema_values, self.rsi = [], [], None
            self.cfg["position"] = None if res.get("dry") else self.position
            self._save_cfg()
            self._update_banner(price)
            self._render_chart()
            self.ai_say(f"{tag} {oid} complete. Bought {qty} {sym} at {inr(price)}. "
                        f"Target {inr(self.position['tp'])}, stop loss {inr(self.position['sl'])}.")
        else:
            pos, pnl_text = self.position, ""
            if res.get("dry") and pos and not pos.get("dry"):
                # a dry run must never erase tracking of a real position
                self.ai_say(f"Dry run {oid} complete. Nothing was sold, and your real position "
                            f"is still tracked.")
                return
            if pos and pos["instrument"] == p["instrument"]:
                pnl_text = f" Result {(price - pos['entry']) / pos['entry'] * 100:+.2f} percent."
                remaining = pos["qty"] - qty
                if remaining > 0:
                    pos["qty"] = remaining
                    self.cfg["position"] = None if pos.get("dry") else pos
                else:
                    self.position = None
                    self.cfg["position"] = None
                self._save_cfg()
            self.alerted = None
            self._update_banner(price)
            self._render_chart()
            self.ai_say(f"{tag} {oid} complete. Sold {qty} {sym} at {inr(price)}.{pnl_text}")

    # ------------------------------------------------------- live data loop
    def poll_ltp(self, _dt):
        # expire stale confirmations even when nothing else is happening
        if self.pending and time.time() > self.pending["expires"]:
            self.pending = None
            self._show_confirm(False)
            self.add_chat("TradeMatrix AI", "The order confirmation expired. Nothing was sent.")
        if self.ltp_busy or not self.broker:
            if not self.broker:
                self.chart.hint = "Add credentials in SETTINGS"
                self.chart._redraw()
            return
        now = time.time()
        if (not market_open_now() and self.last_price is not None
                and now - self.last_poll < CLOSED_POLL_SECONDS):
            return
        self.last_poll = now
        self.ltp_busy = True
        instrument, broker = self.watch_symbol, self.broker

        def done(price, err):
            self.ltp_busy = False
            if instrument != self.watch_symbol:
                return
            if err:
                self._set_status(err.message[:80], DOWN)
                if err.kind == "auth" and not self.auth_warned:
                    self.auth_warned = True
                    self.ai_say("The broker access token is invalid or has expired. "
                                "Open SETTINGS and generate a new one.")
                return
            self._set_status("")
            self._on_new_price(price)

        self.run_bg(lambda: broker.ltp(instrument), done)

    def _on_new_price(self, price):
        self.prev_price, self.last_price = self.last_price, price
        is_open = market_open_now()
        bucket = int(time.time() // CANDLE_SECONDS)
        if not self.candles:
            self.candles.append({"b": bucket, "o": price, "h": price, "l": price, "c": price})
        else:
            c = self.candles[-1]
            if c["b"] == bucket or not is_open:
                c["h"], c["l"], c["c"] = max(c["h"], price), min(c["l"], price), price
            else:
                self.candles.append({"b": bucket, "o": c["c"], "h": max(c["c"], price),
                                     "l": min(c["c"], price), "c": price})
                if len(self.candles) > MAX_CANDLES:
                    del self.candles[0]
        closes = [c["c"] for c in self.candles]
        self.ema_values = ema_series(closes, 20)
        self.rsi = rsi_last(closes, 14)

        colour = TEXT
        if self.prev_price is not None:
            colour = UP if price > self.prev_price else DOWN if price < self.prev_price else TEXT
        self.lb_price.text = inr(price)
        self.lb_price.color = hexc(colour)
        self.lb_ema.text = f"EMA 20\n{self.ema_values[-1]:,.2f}"
        if self.rsi is not None:
            self.lb_rsi.text = f"RSI 14\n{self.rsi:.1f}"
            self.lb_rsi.color = hexc(DOWN if self.rsi > 70 else UP if self.rsi < 30 else TEXT)
        else:
            self.lb_rsi.text = f"RSI 14\n{len(self.candles)}/15"
            self.lb_rsi.color = hexc(MUTED)

        self._check_exit_alert(price)
        self._update_banner(price)
        self._render_chart()

    def _render_chart(self):
        pos = self.position
        levels = None
        if pos and pos["instrument"] == self.watch_symbol:
            levels = {"entry": pos["entry"], "tp": pos["tp"], "sl": pos["sl"]}
        hint = "Waiting for market data..." if self.broker else "Add credentials in SETTINGS"
        self.chart.set_data(self.candles, self.ema_values, self.last_price, levels, hint)

    def _update_banner(self, price):
        pos = self.position
        if not pos:
            self.banner.set_bg(PANEL)
            self.lb_banner1.text = "NO TRACKED POSITION"
            self.lb_banner1.color = hexc(MUTED)
            self.lb_banner2.text = "Say or type 'buy reliance' to prepare an order."
            return
        tag = "DRY-RUN " if pos.get("dry") else ""
        name = self._name(pos["instrument"])
        show = price if (price and pos["instrument"] == self.watch_symbol) else None
        pnl = (show - pos["entry"]) / pos["entry"] * 100.0 if show else 0.0
        self.banner.set_bg("#12302D" if pnl >= 0 else "#3A1A1C")
        self.lb_banner1.text = (f"{tag}LONG {name} x{pos['qty']}  Entry {inr(pos['entry'])}"
                                + (f"  P&L {pnl:+.2f}%" if show else ""))
        self.lb_banner1.color = hexc(UP if pnl >= 0 else DOWN)
        self.lb_banner2.text = (f"TP {inr(pos['tp'])}    SL {inr(pos['sl'])}    "
                                f"Order {pos.get('order_id', '-')}")
        self.lb_banner2.color = hexc(TEXT)

    # ------------------------------------------------- exit alert (asks first)
    def _check_exit_alert(self, price):
        pos = self.position
        if not pos or pos["instrument"] != self.watch_symbol:
            return
        reason = ("TAKE_PROFIT" if price >= pos["tp"]
                  else "STOP_LOSS" if price <= pos["sl"] else None)
        if reason is None:
            self.alerted = None
            return
        if reason == self.alerted or self.pending or self.order_busy:
            return
        self.alerted = reason
        dry_pos = bool(pos.get("dry"))
        pnl = (price - pos["entry"]) / pos["entry"] * 100.0
        self.pending = {"side": "SELL", "instrument": pos["instrument"], "qty": pos["qty"],
                        "ltp": price, "product": pos.get("product", PRODUCT),
                        "tp": pos["tp"], "sl": pos["sl"], "reason": reason,
                        "dry_pos": dry_pos, "expires": time.time() + 2 * PENDING_SECONDS}
        name = self._name(pos["instrument"])
        if reason == "TAKE_PROFIT":
            head = f"Take profit reached for {name} at {inr(price)}, {abs(pnl):.2f} percent profit."
        else:
            head = f"Stop loss reached for {name} at {inr(price)}, {abs(pnl):.2f} percent loss."
        self._show_confirm(True, "SELL")
        mode = " (dry run)" if (dry_pos or not self._live()) else ""
        self.ai_say(f"{head} Say yes to sell {pos['qty']} now{mode}, or no to keep holding.")

    # ------------------------------------------------------------- settings
    def open_settings(self):
        if self.order_busy:
            self._set_status("Wait for the current order to finish.", AMBER)
            return
        cfg = self.cfg
        body = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(8))
        sv = ScrollView(do_scroll_x=False)
        form = BoxLayout(orientation="vertical", spacing=dp(8), size_hint_y=None)
        form.bind(minimum_height=form.setter("height"))

        def field(hint, value="", password=False):
            return TextInput(hint_text=hint, text=str(value), password=password,
                             multiline=False, size_hint_y=None, height=dp(44),
                             font_size=sp(13), write_tab=False, background_color=hexc(BG),
                             foreground_color=hexc(TEXT), hint_text_color=hexc(MUTED))

        def note(text, color=MUTED, h=dp(36)):
            return make_label(text, 11, color, halign="left", size_hint_y=None, height=h)

        ti_holder = field("Account holder name (shown in order confirmations)",
                          cfg.get("account_holder", ""))
        ti_key = field("Kite API key", cfg["api_key"])
        ti_secret = field("Kite API secret (token generation only)", cfg["api_secret"], True)
        ti_token = field("Access token (valid for one day)", cfg["access_token"], True)
        ti_req = field("Paste the redirect URL or request_token here")
        lb_err = make_label("", 12, DOWN, size_hint_y=None, height=dp(34))

        tok_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        btn_login = flat_button("OPEN LOGIN PAGE", NEUTRAL)
        btn_gen = flat_button("GENERATE TOKEN", ACCENT)
        btn_login.font_size = btn_gen.font_size = sp(12)
        tok_row.add_widget(btn_login)
        tok_row.add_widget(btn_gen)

        sw_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        sw_row.add_widget(make_label("LIVE orders (real money)", 14, TEXT, bold=True, halign="left"))
        sw_live = Switch(active=bool(cfg["live_orders"]), size_hint_x=None, width=dp(100))
        sw_row.add_widget(sw_live)
        live_note = note("", MUTED, dp(48))
        ti_live = field("To switch LIVE on, type the word LIVE here")

        ti_qty = field("Default quantity (shares)", cfg["default_qty"])
        ti_max = field("Max order value in Rs", cfg["max_order_value"])
        prod_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        prod_row.add_widget(make_label("Product (CNC delivery / MIS intraday)", 12, TEXT,
                                       halign="left"))
        sp_prod = Spinner(text=str(cfg["product"]), values=["CNC", "MIS"], size_hint_x=0.4,
                          font_size=sp(14), background_normal="", background_color=hexc(BG),
                          color=hexc(TEXT))
        prod_row.add_widget(sp_prod)
        ti_proxy = field("Optional static-IP proxy for orders (http://host:port)", cfg["order_proxy"])

        def paint_live(*_):
            if sw_live.active:
                live_note.text = ("LIVE: orders go to the real Zerodha account (each one still "
                                  "needs a confirmation).")
                live_note.color = hexc(DOWN)
            else:
                live_note.text = "DRY-RUN: the full flow works but nothing is sent to the broker."
                live_note.color = hexc(MUTED)

        sw_live.bind(active=paint_live)
        paint_live()

        def do_login(*_):
            key = ti_key.text.strip()
            if not key:
                lb_err.text = "Enter your API key first."
                return
            ok = open_url(f"https://kite.zerodha.com/connect/login?v=3&api_key={key}")
            lb_err.color = hexc(MUTED if ok else DOWN)
            lb_err.text = ("The account holder logs in on Zerodha's page, then copy the full "
                           "address of the page you land on and paste it above." if ok
                           else "Could not open the browser. Open the Kite login URL manually.")

        def do_generate(*_):
            key, secret = ti_key.text.strip(), ti_secret.text.strip()
            req = parse_request_token(ti_req.text)
            if not (key and secret):
                lb_err.color = hexc(DOWN)
                lb_err.text = "API key and API secret are required to generate a token."
                return
            if not req:
                lb_err.color = hexc(DOWN)
                lb_err.text = "Paste the redirect URL (or request_token) from the login step."
                return
            lb_err.color = hexc(ACCENT)
            lb_err.text = "Generating access token..."
            btn_gen.disabled = True

            def done(token, err):
                btn_gen.disabled = False
                if err:
                    lb_err.color = hexc(DOWN)
                    lb_err.text = err.message[:140]
                    return
                ti_token.text = token
                ti_req.text = ""
                lb_err.color = hexc(UP)
                lb_err.text = "Token generated. Press SAVE to use it."

            self.run_bg(lambda: generate_session(key, secret, req), done)

        btn_login.bind(on_release=do_login)
        btn_gen.bind(on_release=do_generate)

        for w in (note("Zerodha Kite Connect credentials of the ACCOUNT HOLDER. Stored only in "
                       "this app's private folder. This app never sees their Zerodha password "
                       "or TOTP: they log in on Zerodha's own page.", MUTED, dp(62)),
                  ti_holder, ti_key, ti_secret, ti_token,
                  note("Access tokens expire daily: log in, paste the redirect URL, generate.",
                       MUTED, dp(34)),
                  ti_req, tok_row, lb_err, sw_row, live_note, ti_live, ti_qty, ti_max,
                  prod_row, ti_proxy):
            form.add_widget(w)
        sv.add_widget(form)
        body.add_widget(sv)

        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(10))
        pop = Popup(title="Settings", content=body, size_hint=(0.96, 0.94),
                    auto_dismiss=False, background_color=hexc(PANEL))

        def save(*_):
            try:
                qty = int(ti_qty.text.strip())
                cap = float(ti_max.text.strip())
                if qty < 1 or qty > 100000 or cap <= 0:
                    raise ValueError
            except ValueError:
                lb_err.color = hexc(DOWN)
                lb_err.text = "Quantity must be a whole number and max value a positive number."
                return
            turning_live = bool(sw_live.active) and not bool(cfg["live_orders"])
            if turning_live and ti_live.text.strip().upper() != "LIVE":
                lb_err.color = hexc(DOWN)
                lb_err.text = "To enable LIVE orders, type the word LIVE in the box above SAVE."
                return
            new = dict(self.cfg)
            new.update({"account_holder": ti_holder.text.strip(),
                        "api_key": ti_key.text.strip(), "api_secret": ti_secret.text.strip(),
                        "access_token": ti_token.text.strip(), "live_orders": bool(sw_live.active),
                        "default_qty": qty, "max_order_value": cap, "product": sp_prod.text,
                        "order_proxy": ti_proxy.text.strip()})
            self.cfg = new
            if not self._save_cfg():
                lb_err.color = hexc(DOWN)
                lb_err.text = "Could not save settings to storage."
                return
            self._rebuild_broker()
            self._refresh_badge()
            self.candles, self.ema_values, self.rsi = [], [], None
            self._render_chart()
            self._set_status("Settings saved.", UP)
            pop.dismiss()
            Clock.schedule_once(lambda dt: self.poll_ltp(0), 0.2)

        b_cancel = flat_button("CANCEL", NEUTRAL)
        b_save = flat_button("SAVE", UP)
        b_cancel.bind(on_release=lambda *_: pop.dismiss())
        b_save.bind(on_release=save)
        row.add_widget(b_cancel)
        row.add_widget(b_save)
        body.add_widget(row)
        pop.open()

    # ---------------------------------------------------------------- lifecycle
    def on_pause(self):
        return True                                  # keep the app alive on Android

    def on_stop(self):
        try:
            if self._poll_event is not None:
                self._poll_event.cancel()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.speech.shutdown()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    TradeMatrixApp().run()
