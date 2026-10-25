"""
main.py - TradeMatrix (self-contained single file)

  * Kivy dark UI (TradingView-style)
  * Live price + 24h stats from Binance public REST (requests, polled in a thread)
  * Gemini AI trading analysis (temperature 0.7) built from real indicators
    (EMA20/50, RSI14, ATR14, recent high/low) computed from Binance candles
  * Native Android Text-to-Speech via pyjnius (pyttsx3 on desktop, if installed)

No other project files are needed. Run on desktop:
    pip install kivy requests          (optional voice: pip install pyttsx3)

Android (buildozer.spec):
    requirements = python3,kivy==2.3.0,pyjnius,requests,urllib3,idna,charset-normalizer,certifi
    android.permissions = INTERNET

The Gemini key is entered in SETTINGS (stored in the app's private folder) or taken
from the GEMINI_API_KEY environment variable on desktop. This app only analyses and
speaks; it never places orders. Not financial advice.
"""
import json
import logging
import os
import re
import threading

import requests

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, RoundedRectangle
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput
from kivy.uix.togglebutton import ToggleButton
from kivy.utils import get_color_from_hex as hexc
from kivy.utils import platform

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tradematrix")

# ------------------------------------------------------------------ constants
BG = "#0B0E14"
PANEL = "#131722"
TEXT = "#D1D4DC"
MUTED = "#787B86"
UP = "#26A69A"
DOWN = "#EF5350"
ACCENT = "#2962FF"
AMBER = "#F7B500"
NEUTRAL = "#434651"

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
INTERVALS = ["5m", "15m", "1h", "4h", "1d"]
POLL_SECONDS = 3
HTTP_TIMEOUT = 10
MIN_CANDLES = 60

BINANCE_HOSTS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api.binance.us",
]

# Tried in order; the first model that exists is remembered for the session.
# NOTE: Google has retired gemini-1.5-flash, so the second entry is what actually answers.
MODEL_CHAIN = ["gemini-1.5-flash", "gemini-2.5-flash"]
GEMINI_TEMPERATURE = 0.7
GEMINI_TIMEOUT = 25

BIAS_COLORS = {"BULLISH": UP, "BEARISH": DOWN, "NEUTRAL": AMBER}


# -------------------------------------------------------------- config storage
def load_config(path):
    cfg = {"gemini_api_key": ""}
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


# ------------------------------------------------------------------ Binance data
class DataError(Exception):
    """Market data could not be fetched or parsed."""


def to_pair(symbol):
    return symbol.replace("/", "").upper()


def _binance_get(path, params):
    last = "unknown"
    for host in BINANCE_HOSTS:
        try:
            r = requests.get(host + path, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
        except (requests.RequestException, ValueError) as exc:
            last = exc.__class__.__name__
    raise DataError(f"Binance unavailable ({last})")


def fetch_ticker(symbol):
    """Live price and 24h stats for a symbol like 'BTC/USDT'."""
    d = _binance_get("/api/v3/ticker/24hr", {"symbol": to_pair(symbol)})
    try:
        return {"price": float(d["lastPrice"]), "change_pct": float(d["priceChangePercent"]),
                "high": float(d["highPrice"]), "low": float(d["lowPrice"])}
    except (KeyError, TypeError, ValueError):
        raise DataError("Unexpected ticker response from Binance")


def fetch_klines(symbol, interval, limit=120):
    rows = _binance_get("/api/v3/klines",
                        {"symbol": to_pair(symbol), "interval": interval, "limit": limit})
    try:
        return [(float(r[2]), float(r[3]), float(r[4])) for r in rows]   # high, low, close
    except (IndexError, TypeError, ValueError):
        raise DataError("Unexpected candle response from Binance")


# --------------------------------------------------------------------- indicators
def ema_last(values, n):
    k = 2.0 / (n + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


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


def atr_last(highs, lows, closes, n=14):
    if len(closes) <= n:
        return None
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
           for i in range(1, len(closes))]
    atr = sum(trs[:n]) / n
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return atr


def _r(x):
    return round(x, 6 if abs(x) < 1 else 2)


def fmt_price(p):
    if p is None:
        return "--"
    return f"{p:,.2f}" if abs(p) >= 1 else f"{p:.6f}"


def build_context(symbol, interval):
    """Fetch live data and compute the indicator snapshot sent to the AI."""
    tick = fetch_ticker(symbol)
    candles = fetch_klines(symbol, interval, 120)[:-1]      # drop the still-forming candle
    if len(candles) < MIN_CANDLES:
        raise DataError("Not enough candle history for analysis")
    highs = [c[0] for c in candles]
    lows = [c[1] for c in candles]
    closes = [c[2] for c in candles]
    rsi = rsi_last(closes, 14)
    atr = atr_last(highs, lows, closes, 14)
    if rsi is None or atr is None:
        raise DataError("Could not compute indicators")
    return {
        "symbol": symbol, "interval": interval,
        "price": _r(tick["price"]), "change_24h_pct": round(tick["change_pct"], 2),
        "high_24h": _r(tick["high"]), "low_24h": _r(tick["low"]),
        "rsi14": round(rsi, 1),
        "ema20": _r(ema_last(closes, 20)), "ema50": _r(ema_last(closes, 50)),
        "atr14": _r(atr),
        "recent_high_50": _r(max(highs[-50:])), "recent_low_50": _r(min(lows[-50:])),
        "last_5_closes": [_r(c) for c in closes[-5:]],
    }


# -------------------------------------------------------------------- AI analysis
class GeminiError(Exception):
    """Gemini request failed."""


def _gemini_http_error(r):
    try:
        detail = r.json().get("error", {}).get("message", "")
    except ValueError:
        detail = ""
    if r.status_code == 400 and "api key" in detail.lower():
        return "Invalid Gemini API key - check it in SETTINGS"
    if r.status_code in (401, 403):
        return "Gemini access denied - check the key and its permissions"
    if r.status_code == 429:
        return "Gemini rate limit reached - try again shortly"
    return f"Gemini HTTP {r.status_code} {detail[:100]}".strip()


def ask_gemini(ctx, key, preferred=None):
    """Returns (text, model_used). Falls through MODEL_CHAIN when a model is not found."""
    chain = list(MODEL_CHAIN)
    if preferred in chain:
        chain.remove(preferred)
        chain.insert(0, preferred)

    system = ("You are a crypto market analyst inside the TradeMatrix app. Use ONLY the data "
              "provided; never invent prices or news. The FIRST line must be exactly one of: "
              "BIAS: BULLISH, BIAS: BEARISH, BIAS: NEUTRAL. Then write at most 5 short "
              "sentences of plain text (no markdown, no bullet symbols) covering trend, "
              "momentum (RSI), key support and resistance taken from the data, and one risk. "
              "The last sentence must be: This is not financial advice.")
    user = f"MARKET DATA:\n{json.dumps(ctx)}\n\nGive the analysis."
    tried = []
    for model in chain:
        gen = {"temperature": GEMINI_TEMPERATURE, "maxOutputTokens": 2048}
        if model.startswith("gemini-2.5"):
            gen["thinkingConfig"] = {"thinkingBudget": 0}     # keep tokens for the answer
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": gen}
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                json=body, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                timeout=GEMINI_TIMEOUT)
        except requests.RequestException as exc:
            raise GeminiError(f"Network error ({exc.__class__.__name__})")
        if r.status_code == 404:                              # model retired / unknown
            tried.append(model)
            continue
        if r.status_code != 200:
            raise GeminiError(_gemini_http_error(r))
        try:
            parts = r.json()["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts).strip()
        except (KeyError, IndexError, TypeError, ValueError):
            raise GeminiError("Gemini returned no usable answer (blocked or empty)")
        if not text:
            raise GeminiError("Gemini returned an empty answer")
        return text, model
    raise GeminiError("No Gemini model available (" + ", ".join(tried) + " not found)")


def parse_analysis(text):
    """(bias, body) from the model output; tolerant of a missing BIAS line."""
    text = text.replace("*", "").strip()
    m = re.match(r"\s*BIAS:\s*(BULLISH|BEARISH|NEUTRAL)\b[^\n]*\n?", text, re.IGNORECASE)
    if m:
        return m.group(1).upper(), text[m.end():].strip() or text
    return "NEUTRAL", text


def rule_based_analysis(ctx):
    """Offline fallback used when there is no Gemini key or the AI call fails."""
    up = ctx["ema20"] > ctx["ema50"]
    rsi = ctx["rsi14"]
    score = (1 if up else -1) + (1 if rsi < 30 else -1 if rsi > 70 else 0)
    bias = "BULLISH" if score > 0 else "BEARISH" if score < 0 else "NEUTRAL"
    mom = ("oversold" if rsi < 30 else "overbought" if rsi > 70 else "neutral")
    body = (f"The {ctx['interval']} trend is {'up' if up else 'down'}, with EMA20 at "
            f"{fmt_price(ctx['ema20'])} against EMA50 at {fmt_price(ctx['ema50'])}. "
            f"RSI is {rsi:.0f}, which is {mom}. Support is near {fmt_price(ctx['recent_low_50'])} "
            f"and resistance near {fmt_price(ctx['recent_high_50'])}. Average range per candle "
            f"is about {fmt_price(ctx['atr14'])}, so keep stops wider than that. "
            f"This is not financial advice.")
    return bias, body


# ------------------------------------------------------------ text to speech
class TextToSpeechEngine:
    """Native Android TextToSpeech via pyjnius; pyttsx3 on desktop. Never raises."""

    def __init__(self):
        self._lock = threading.Lock()
        self._android = None
        self._flush = 0
        self._listener = None
        self.last_error = None

    def speak(self, text, on_error=None):
        text = " ".join((text or "").split())[:900]
        if not text:
            return
        threading.Thread(target=self._run, args=(text, on_error), daemon=True).start()

    def _run(self, text, on_error):
        with self._lock:
            try:
                if platform == "android":
                    self._speak_android(text)
                else:
                    self._speak_desktop(text)
                self.last_error = None
            except Exception as exc:  # noqa: BLE001
                msg = f"{exc.__class__.__name__}: {exc}"
                self.last_error = msg
                log.warning("TTS failed: %s", msg)
                if on_error:
                    Clock.schedule_once(lambda dt: on_error(msg))

    def _init_android(self):
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
        listener = _InitListener(ready)                 # keep a reference: avoids GC crash
        tts = TextToSpeech(activity, listener)
        # onInit is delivered on the UI thread; we wait on a worker thread, so no deadlock
        if not ready.wait(timeout=8) or listener.status != 0:
            raise RuntimeError("Android TextToSpeech engine did not start "
                               "(is a TTS engine installed?)")
        tts.setLanguage(Locale.US)
        self._listener = listener
        self._flush = TextToSpeech.QUEUE_FLUSH
        self._android = tts

    def _speak_android(self, text):
        if self._android is None:
            self._init_android()
        result = self._android.speak(text, self._flush, None, "tradematrix")
        if result != 0:                                 # TextToSpeech.SUCCESS
            raise RuntimeError(f"TextToSpeech.speak returned {result}")

    def _speak_desktop(self, text):
        import pyttsx3                                   # optional dependency
        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()

    def stop(self):
        try:
            if self._android is not None:
                self._android.stop()
        except Exception:  # noqa: BLE001
            pass

    def shutdown(self):
        self.stop()
        try:
            if self._android is not None:
                self._android.shutdown()
                self._android = None
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------- widgets
class Panel(BoxLayout):
    def __init__(self, bg=PANEL, radius=10, **kw):
        super().__init__(**kw)
        with self.canvas.before:
            self._color = Color(*hexc(bg))
            self._rect = RoundedRectangle(pos=self.pos, size=self.size,
                                          radius=[dp(radius)])
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


class StatTile(Panel):
    def __init__(self, title, **kw):
        super().__init__(orientation="vertical", padding=dp(6), spacing=dp(2), **kw)
        self.title = make_label(title, 10, MUTED)
        self.value = make_label("--", 15, TEXT, bold=True)
        self.add_widget(self.title)
        self.add_widget(self.value)

    def set(self, text, color=TEXT):
        self.value.text = text
        self.value.color = hexc(color)


# -------------------------------------------------------------------------- app
class TradeMatrixApp(App):
    title = "TradeMatrix"

    def build(self):
        if platform not in ("android", "ios"):
            Window.size = (420, 820)
        else:
            Window.softinput_mode = "below_target"
        Window.clearcolor = hexc(BG)

        self.cfg = load_config(self._cfg_path())
        self.tts = TextToSpeechEngine()
        self.model_cache = None
        self.last_price = None
        self.price_busy = False
        self.price_fails = 0
        self.analysis_busy = False
        self.spoken_text = ""
        self._poll_event = None

        root = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))

        # title bar
        bar = Panel(size_hint_y=None, height=dp(52), padding=[dp(12), dp(6)], spacing=dp(8))
        bar.add_widget(make_label("TRADEMATRIX", 20, ACCENT, bold=True, halign="left"))
        btn_settings = flat_button("SETTINGS", NEUTRAL, size_hint_x=None, width=dp(100))
        btn_settings.bind(on_release=lambda *_: self.open_settings())
        bar.add_widget(btn_settings)
        root.add_widget(bar)

        # symbol / interval
        sel = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(8))
        self.sp_symbol = Spinner(text=SYMBOLS[0], values=SYMBOLS, font_size=sp(15),
                                 background_normal="", background_color=hexc(PANEL),
                                 color=hexc(TEXT))
        self.sp_interval = Spinner(text="15m", values=INTERVALS, size_hint_x=0.35,
                                   font_size=sp(15), background_normal="",
                                   background_color=hexc(PANEL), color=hexc(TEXT))
        self.sp_symbol.bind(text=self._symbol_changed)
        sel.add_widget(self.sp_symbol)
        sel.add_widget(self.sp_interval)
        root.add_widget(sel)

        # live price
        pp = Panel(orientation="vertical", size_hint_y=None, height=dp(100), padding=dp(10))
        pp.add_widget(make_label("LIVE PRICE", 11, MUTED, size_hint_y=None, height=dp(18)))
        self.lb_price = make_label("--", 34, TEXT, bold=True)
        pp.add_widget(self.lb_price)
        self.lb_change = make_label("24h: --", 13, MUTED, size_hint_y=None, height=dp(20))
        pp.add_widget(self.lb_change)
        root.add_widget(pp)

        # stats
        grid = GridLayout(cols=3, spacing=dp(8), size_hint_y=None, height=dp(58))
        self.t_high = StatTile("24H HIGH")
        self.t_low = StatTile("24H LOW")
        self.t_rsi = StatTile("RSI 14")
        for t in (self.t_high, self.t_low, self.t_rsi):
            grid.add_widget(t)
        root.add_widget(grid)

        # AI analysis
        ap = Panel(orientation="vertical", padding=dp(10), spacing=dp(6))
        head = BoxLayout(size_hint_y=None, height=dp(30), spacing=dp(8))
        head.add_widget(make_label("AI ANALYSIS", 12, ACCENT, bold=True, halign="left"))
        self.badge = Panel(bg=NEUTRAL, radius=6, size_hint_x=None, width=dp(110), padding=dp(4))
        self.lb_badge = make_label("NO DATA", 12, "#FFFFFF", bold=True)
        self.badge.add_widget(self.lb_badge)
        head.add_widget(self.badge)
        ap.add_widget(head)

        sv = ScrollView(do_scroll_x=False, bar_width=dp(3))
        self.lb_analysis = Label(
            text="Press ANALYZE to get a live market analysis.", size_hint_y=None,
            halign="left", valign="top", font_size=sp(14), color=hexc(TEXT))
        self.lb_analysis.bind(
            width=lambda w, _v: setattr(w, "text_size", (w.width, None)),
            texture_size=lambda w, ts: setattr(w, "height", ts[1]))
        sv.add_widget(self.lb_analysis)
        ap.add_widget(sv)
        root.add_widget(ap)

        self.lb_status = make_label("Ready.", 12, MUTED, size_hint_y=None, height=dp(34))
        root.add_widget(self.lb_status)

        self.tg_auto = ToggleButton(text="AUTO-SPEAK: ON", state="down", font_size=sp(12),
                                    size_hint_y=None, height=dp(34), background_normal="",
                                    background_down="", background_color=hexc(UP),
                                    color=(1, 1, 1, 1))
        self.tg_auto.bind(state=self._auto_toggled)
        root.add_widget(self.tg_auto)

        btns = BoxLayout(size_hint_y=None, height=dp(54), spacing=dp(8))
        self.btn_analyze = flat_button("ANALYZE", ACCENT)
        btn_speak = flat_button("SPEAK", UP)
        btn_stop = flat_button("STOP", DOWN, size_hint_x=0.6)
        self.btn_analyze.bind(on_release=lambda *_: self.analyze())
        btn_speak.bind(on_release=lambda *_: self.speak_last())
        btn_stop.bind(on_release=lambda *_: self.tts.stop())
        for b in (self.btn_analyze, btn_speak, btn_stop):
            btns.add_widget(b)
        root.add_widget(btns)

        self._poll_event = Clock.schedule_interval(self.poll_price, POLL_SECONDS)
        Clock.schedule_once(lambda dt: self.poll_price(0), 0.3)
        return root

    # ---------------------------------------------------------------- helpers
    def _cfg_path(self):
        return os.path.join(self.user_data_dir, "tradematrix_config.json")

    def _gemini_key(self):
        return (self.cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY", "")).strip()

    def _status(self, text, color=MUTED):
        self.lb_status.text = text
        self.lb_status.color = hexc(color)

    def _auto_toggled(self, _w, state):
        on = state == "down"
        self.tg_auto.text = "AUTO-SPEAK: ON" if on else "AUTO-SPEAK: OFF"
        self.tg_auto.background_color = hexc(UP if on else NEUTRAL)

    def _symbol_changed(self, *_):
        self.last_price = None
        self.lb_price.text = "--"
        self.lb_price.color = hexc(TEXT)
        self.lb_change.text = "24h: --"
        self.lb_change.color = hexc(MUTED)
        self.t_high.set("--")
        self.t_low.set("--")
        self.t_rsi.set("--")
        Clock.schedule_once(lambda dt: self.poll_price(0), 0.1)

    # ---------------------------------------------------------- live price loop
    def poll_price(self, _dt):
        if self.price_busy:
            return
        self.price_busy = True
        symbol = self.sp_symbol.text

        def worker():
            try:
                tick, err = fetch_ticker(symbol), None
            except Exception as exc:  # noqa: BLE001
                tick, err = None, str(exc)
            Clock.schedule_once(lambda dt: self._price_done(symbol, tick, err))

        threading.Thread(target=worker, daemon=True).start()

    def _price_done(self, symbol, tick, err):
        self.price_busy = False
        if symbol != self.sp_symbol.text:
            return                                      # symbol changed meanwhile
        if err or tick is None:
            self.price_fails += 1
            if self.price_fails == 1 or self.price_fails % 10 == 0:
                self._status(f"{err or 'Price error'} - retrying...", DOWN)
            return
        self.price_fails = 0

        prev, price = self.last_price, tick["price"]
        self.last_price = price
        colour = TEXT
        if prev is not None:
            colour = UP if price > prev else DOWN if price < prev else TEXT
        self.lb_price.text = fmt_price(price)
        self.lb_price.color = hexc(colour)
        chg = tick["change_pct"]
        self.lb_change.text = f"24h: {chg:+.2f}%"
        self.lb_change.color = hexc(UP if chg > 0 else DOWN if chg < 0 else MUTED)
        self.t_high.set(fmt_price(tick["high"]), UP)
        self.t_low.set(fmt_price(tick["low"]), DOWN)

    # ------------------------------------------------------------------ analysis
    def analyze(self):
        if self.analysis_busy:
            return
        self.analysis_busy = True
        self.btn_analyze.disabled = True
        self._status("Fetching market data...", ACCENT)
        symbol, interval = self.sp_symbol.text, self.sp_interval.text
        key, preferred = self._gemini_key(), self.model_cache

        def worker():
            try:
                ctx = build_context(symbol, interval)
            except Exception as exc:  # noqa: BLE001
                msg = f"Market data error: {exc}"
                Clock.schedule_once(lambda dt: self._analysis_failed(msg))
                return
            model, note = None, ""
            if key:
                try:
                    text, model = ask_gemini(ctx, key, preferred)
                    bias, body = parse_analysis(text)
                    source = f"Gemini {model}"
                except Exception as exc:  # noqa: BLE001
                    note = str(exc)[:120]
                    bias, body = rule_based_analysis(ctx)
                    source = "Offline rule-based"
            else:
                bias, body = rule_based_analysis(ctx)
                source = "Offline rule-based"
                note = "Add a Gemini API key in SETTINGS for AI analysis"
            Clock.schedule_once(
                lambda dt: self._analysis_done(ctx, bias, body, source, model, note))

        threading.Thread(target=worker, daemon=True).start()

    def _analysis_failed(self, msg):
        self.analysis_busy = False
        self.btn_analyze.disabled = False
        self._status(msg, DOWN)

    def _analysis_done(self, ctx, bias, body, source, model, note):
        self.analysis_busy = False
        self.btn_analyze.disabled = False
        if model:
            self.model_cache = model
        self.lb_analysis.text = body
        self.badge.set_bg(BIAS_COLORS.get(bias, NEUTRAL))
        self.lb_badge.text = bias
        self.lb_badge.color = (0, 0, 0, 1) if bias == "NEUTRAL" else (1, 1, 1, 1)
        rsi = ctx["rsi14"]
        self.t_rsi.set(f"{rsi:.0f}", DOWN if rsi > 70 else UP if rsi < 30 else TEXT)

        coin = ctx["symbol"].replace("/", " ")
        self.spoken_text = f"{coin}. Bias is {bias.lower()}. {body}"
        self._status(f"Source: {source}" + (f" - {note}" if note else ""),
                     MUTED if not note else AMBER)
        if self.tg_auto.state == "down":
            self.tts.speak(self.spoken_text, on_error=self._tts_failed)

    def speak_last(self):
        if not self.spoken_text:
            self._status("Nothing to speak yet - press ANALYZE first.", AMBER)
            return
        self.tts.speak(self.spoken_text, on_error=self._tts_failed)

    def _tts_failed(self, msg):
        self._status(f"Voice unavailable: {msg[:90]}", DOWN)

    # ------------------------------------------------------------------ settings
    def open_settings(self):
        body = BoxLayout(orientation="vertical", spacing=dp(10), padding=dp(10))
        body.add_widget(make_label(
            "Google AI Studio (Gemini) API key.\nStored only in this app's private folder.",
            13, MUTED, size_hint_y=None, height=dp(48)))
        ti = TextInput(text=self.cfg.get("gemini_api_key", ""), password=True,
                       hint_text="Gemini API key", multiline=False, size_hint_y=None,
                       height=dp(46), font_size=sp(14), write_tab=False,
                       background_color=hexc(BG), foreground_color=hexc(TEXT),
                       cursor_color=hexc(TEXT), hint_text_color=hexc(MUTED))
        body.add_widget(ti)
        lb_err = make_label("", 12, DOWN, size_hint_y=None, height=dp(24))
        body.add_widget(lb_err)
        body.add_widget(Label())                         # spacer

        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(10))
        pop = Popup(title="Settings", content=body, size_hint=(0.92, 0.55),
                    auto_dismiss=False, background_color=hexc(PANEL))

        def save(*_):
            new = dict(self.cfg)
            new["gemini_api_key"] = ti.text.strip()
            try:
                save_config(self._cfg_path(), new)
            except OSError as exc:
                lb_err.text = f"Could not save: {exc}"
                return
            self.cfg = new
            self.model_cache = None
            self._status("Settings saved.", UP)
            pop.dismiss()

        b_cancel = flat_button("CANCEL", NEUTRAL)
        b_save = flat_button("SAVE", UP)
        b_cancel.bind(on_release=lambda *_: pop.dismiss())
        b_save.bind(on_release=save)
        row.add_widget(b_cancel)
        row.add_widget(b_save)
        body.add_widget(row)
        pop.open()

    # ----------------------------------------------------------------- lifecycle
    def on_pause(self):
        return True                                      # keep the app alive on Android

    def on_stop(self):
        try:
            if self._poll_event is not None:
                self._poll_event.cancel()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.tts.shutdown()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    TradeMatrixApp().run()
from voice_assistant import VoiceAssistant

self.voice = VoiceAssistant()           # in build()
self.voice.speak("Hello from TradeMatrix")
self.voice.stop()                       # e.g. for a STOP button
self.voice.shutdown()                   # in on_stop()
