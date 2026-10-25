"""
main.py - TradeMatrix (single file, fully self-contained)

SIMULATION ONLY: prices are generated locally. No exchange is contacted and no real
orders are ever placed.

Features
  1. Dashboard: simulated BTC/USDT 15m candles with EMA20 and RSI14, a TradingView-style
     candlestick chart, and a trade banner. Opening a trade draws ENTRY / TP / SL markers.
  2. Scrollable AI chat (bubbles). TradeMatrix AI answers typed or spoken commands.
  3. MIC button: Android RecognizerIntent.ACTION_RECOGNIZE_SPEECH through pyjnius.
  4. AUTO-SPEAK: ON/OFF toggle that unmutes / mutes ALL voice output.
  5. Text input + SEND button.
  6. Android TextToSpeech created directly: TextToSpeech(activity, None) - no
     PythonJavaClass, no OnInitListener, no custom Java interfaces anywhere.

Commands you can type or say:
  buy | sell / close | status | price | rsi | ema | analysis | mute | unmute | help

Run on desktop:   pip install kivy        (optional voice: pip install pyttsx3)
Android build:    buildozer.spec ->  requirements = python3,kivy==2.3.0,pyjnius
"""
import logging
import queue
import random
import re
import threading
import time
from typing import Optional

from kivy.app import App
from kivy.clock import Clock
from kivy.core.text import Label as CoreLabel
from kivy.core.window import Window
from kivy.graphics import Color, Line, Rectangle, RoundedRectangle
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.uix.togglebutton import ToggleButton
from kivy.uix.widget import Widget
from kivy.utils import escape_markup
from kivy.utils import get_color_from_hex as hexc
from kivy.utils import platform

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tradematrix")

# ------------------------------------------------------------------ constants
BG = "#0B0E14"
PANEL = "#131722"
PANEL2 = "#1B2030"
GRID = "#232838"
TEXT = "#D1D4DC"
MUTED = "#787B86"
UP = "#26A69A"
DOWN = "#EF5350"
UP_DARK = "#0F3B36"
DOWN_DARK = "#3D1A1C"
ACCENT = "#2962FF"
ACCENT_DARK = "#1E3A8A"
AMBER = "#F7B500"
NEUTRAL = "#434651"

SYMBOL = "BTC/USDT"
TIMEFRAME = "15m"
START_PRICE = 64000.0
TICK_SECONDS = 1.0
TICKS_PER_CANDLE = 15          # one simulated "15m" candle every 15 seconds (demo speed)
TICK_VOLATILITY = 0.0015       # std-dev of each simulated tick (0.15%)
INIT_CANDLES = 90
MAX_CANDLES = 150
VISIBLE_CANDLES = 50
EMA_PERIOD = 20
RSI_PERIOD = 14
TAKE_PROFIT_PCT = 2.0
STOP_LOSS_PCT = 1.5
NOTIONAL_USDT = 1000.0         # simulated position size
MAX_CHAT_BUBBLES = 60
MIC_REQUEST_CODE = 4242


# ---------------------------------------------------------------- indicators
def ema_series(values, n):
    if not values:
        return []
    k = 2.0 / (n + 1)
    e = values[0]
    out = []
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi_last(closes, n=14):
    if len(closes) <= n:
        return 50.0
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


def fmt(p):
    return f"{p:,.2f}"


# ------------------------------------------------------------ market simulator
class MarketSim:
    """Random-walk BTC/USDT candle generator (candle = [open, high, low, close])."""

    def __init__(self, start=START_PRICE):
        self.rng = random.Random()
        price = start
        self.candles = []
        for _ in range(INIT_CANDLES):
            o = h = l = price
            for _ in range(TICKS_PER_CANDLE):
                price = max(1.0, price * (1 + self.rng.gauss(0.0, TICK_VOLATILITY)))
                h, l = max(h, price), min(l, price)
            self.candles.append([o, h, l, price])
        self.price = price
        self.candles.append([price, price, price, price])      # the forming candle
        self.ticks = 0

    def tick(self):
        p = max(1.0, self.price * (1 + self.rng.gauss(0.0, TICK_VOLATILITY)))
        self.price = p
        c = self.candles[-1]
        c[3] = p
        c[1] = max(c[1], p)
        c[2] = min(c[2], p)
        self.ticks += 1
        if self.ticks % TICKS_PER_CANDLE == 0:
            self.candles.append([p, p, p, p])
            if len(self.candles) > MAX_CANDLES:
                del self.candles[0]
        return p

    def closes(self):
        return [c[3] for c in self.candles]


# ------------------------------------------------------------ text to speech
class _AndroidTTS:
    """Native Android TextToSpeech via pyjnius. Direct instantiation, NO Java listener."""
    name = "android-tts"
    READY_TIMEOUT = 6.0
    SPEAK_RETRIES = 8
    RETRY_DELAY = 0.25

    def __init__(self):
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

        self._tts = TextToSpeech(activity, None)       # listener intentionally None
        # No init callback exists, so poll setLanguage(): it returns a negative code
        # until the engine has bound, then a code >= 0.
        deadline = time.monotonic() + self.READY_TIMEOUT
        while True:
            try:
                result = int(self._tts.setLanguage(Locale.US))
            except Exception:  # noqa: BLE001
                result = -2
            if result >= 0 or time.monotonic() >= deadline:
                break
            time.sleep(0.2)

    def say(self, text, urgent=False):
        if urgent:
            self.interrupt()
        java_text = self._String(text)                 # real java.lang.String (CharSequence)
        last = None
        for _ in range(self.SPEAK_RETRIES):
            self._counter += 1
            utterance_id = f"tradematrix-{self._counter}-{int(time.time() * 1000)}"
            result = self._tts.speak(java_text, self._queue_add, self._Bundle(), utterance_id)
            if result == self._success:
                return
            last = result
            time.sleep(self.RETRY_DELAY)               # engine may still be starting
        raise RuntimeError(f"TextToSpeech.speak failed (code {last}); "
                           f"is a TTS engine installed?")

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
    """pyttsx3 fallback for desktop testing."""
    name = "pyttsx3"

    def __init__(self):
        import pyttsx3                                   # optional dependency
        self._engine = pyttsx3.init()

    def say(self, text, urgent=False):
        self._engine.say(text)
        self._engine.runAndWait()

    def interrupt(self):
        try:
            self._engine.stop()
        except Exception:  # noqa: BLE001
            pass

    def close(self):
        self.interrupt()


class Speaker:
    """Thread-safe, non-blocking speech queue. Every method is safe to call from the UI."""
    _STOP = object()

    def __init__(self, on_error=None):
        self._on_error = on_error
        self._queue = queue.Queue(maxsize=8)
        self._thread = None
        self._lock = threading.Lock()
        self._backend = None
        self._failed = False
        self._closed = False
        self._reported = False
        self.last_error = None

    def speak(self, text, urgent=False):
        try:
            text = " ".join(str(text or "").split())[:300]
            if not text or self._failed or self._closed:
                return
            with self._lock:
                if self._thread is None or not self._thread.is_alive():
                    self._thread = threading.Thread(target=self._worker, daemon=True,
                                                    name="TradeMatrixTTS")
                    self._thread.start()
            if urgent:
                self._clear()
            try:
                self._queue.put_nowait((text, urgent))
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                self._queue.put_nowait((text, urgent))
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{exc.__class__.__name__}: {exc}"

    def _clear(self):
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    def stop(self):
        """Silence immediately: drop queued messages and cut off current speech."""
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
                self._queue.put_nowait(self._STOP)
                self._thread.join(timeout=2)
        except Exception:  # noqa: BLE001
            pass

    def _report(self, msg):
        self.last_error = msg
        if self._on_error and not self._reported:
            self._reported = True
            Clock.schedule_once(lambda dt: self._on_error(msg))

    def _worker(self):
        try:
            backend = _AndroidTTS() if platform == "android" else _DesktopTTS()
        except Exception as exc:  # noqa: BLE001
            self._failed = True
            self._clear()
            self._report(f"{exc.__class__.__name__}: {exc}")
            return
        self._backend = backend
        failures = 0
        while True:
            item = self._queue.get()
            if item is self._STOP:
                break
            text, urgent = item
            try:
                backend.say(text, urgent)
                failures = 0
            except Exception as exc:  # noqa: BLE001
                failures += 1
                self._report(f"{exc.__class__.__name__}: {exc}")
                if failures >= 3:
                    self._failed = True
                    self._clear()
                    break
        self._backend = None
        backend.close()


# ------------------------------------------------------------------- widgets
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
        super().__init__(orientation="vertical", padding=dp(6), spacing=dp(1), **kw)
        self.title = make_label(title, 10, MUTED)
        self.value = make_label("--", 16, TEXT, bold=True)
        self.add_widget(self.title)
        self.add_widget(self.value)

    def set(self, text, color=TEXT):
        self.value.text = text
        self.value.color = hexc(color)


class Bubble(Panel):
    """One chat message bubble that sizes itself to its text."""

    def __init__(self, who, text, **kw):
        user = who == "You"
        super().__init__(bg=ACCENT_DARK if user else PANEL2, radius=12, orientation="vertical",
                         size_hint_x=0.86, size_hint_y=None, padding=dp(10), **kw)
        col = (ACCENT if user else UP).lstrip("#")
        body = f"[b][color={col}]{escape_markup(who)}[/color][/b]\n{escape_markup(text)}"
        self.lb = Label(text=body, markup=True, size_hint_y=None, halign="left",
                        valign="top", font_size=sp(13), color=hexc(TEXT))
        self.lb.bind(width=lambda w, _v: setattr(w, "text_size", (w.width, None)),
                     texture_size=self._fit)
        self.add_widget(self.lb)
        self.height = dp(60)

    def _fit(self, _w, size):
        self.lb.height = size[1]
        self.height = size[1] + dp(20)


class CandleChart(Widget):
    """TradingView-style candlestick chart with EMA line and ENTRY/TP/SL markers."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.candles = []
        self.ema = []
        self.markers = {}
        self.last_price = None
        self.bind(pos=self.redraw, size=self.redraw)

    def set_data(self, candles, ema, markers, last_price):
        self.candles, self.ema = candles, ema
        self.markers, self.last_price = markers, last_price
        self.redraw()

    @staticmethod
    def _tex(text, size, color_hex):
        label = CoreLabel(text=text, font_size=sp(size), color=hexc(color_hex))
        label.refresh()
        return label.texture

    def redraw(self, *_):
        self.canvas.clear()
        x0, y0 = self.pos
        w, h = self.size
        with self.canvas:
            Color(*hexc(PANEL))
            RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(10)])
            if len(self.candles) < 2 or w <= dp(120) or h <= dp(60):
                return

            n = min(len(self.candles), VISIBLE_CANDLES)
            cs = self.candles[-n:]
            em = self.ema[-n:] if len(self.ema) >= n else []
            pad_l, pad_r, pad_t, pad_b = dp(6), dp(88), dp(8), dp(8)
            plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b

            hi = max(c[1] for c in cs)
            lo = min(c[2] for c in cs)
            for price, _col in self.markers.values():
                hi, lo = max(hi, price), min(lo, price)
            span = (hi - lo) or 1.0
            hi, lo = hi + span * 0.05, lo - span * 0.05
            span = hi - lo

            def y_of(p):
                return y0 + pad_b + (p - lo) / span * plot_h

            def tag(price, text, bg_hex):
                tex = self._tex(text, 10, "#FFFFFF")
                if tex is None:
                    return
                tw, th = tex.size
                y = y_of(price) - th / 2
                y = max(y0 + dp(1), min(y, y0 + h - th - dp(3)))
                x = x0 + w - pad_r + dp(3)
                Color(*hexc(bg_hex))
                Rectangle(pos=(x, y), size=(tw + dp(6), th + dp(2)))
                Color(1, 1, 1, 1)
                Rectangle(texture=tex, pos=(x + dp(3), y + dp(1)), size=tex.size)

            # grid + axis labels
            for i in range(5):
                gy = y0 + pad_b + plot_h * i / 4
                Color(*hexc(GRID))
                Line(points=[x0 + pad_l, gy, x0 + pad_l + plot_w, gy], width=1)
                tex = self._tex(fmt(lo + span * i / 4 * (plot_h / plot_h)), 9, MUTED)
                if tex is not None:
                    Color(1, 1, 1, 1)
                    Rectangle(texture=tex, pos=(x0 + w - pad_r + dp(3), gy - tex.size[1] / 2),
                              size=tex.size)

            # candles
            slot = plot_w / n
            body_w = max(dp(1.5), slot * 0.62)
            for i, (o, hh, ll, cc) in enumerate(cs):
                cx = x0 + pad_l + slot * (i + 0.5)
                Color(*hexc(UP if cc >= o else DOWN))
                Line(points=[cx, y_of(ll), cx, y_of(hh)], width=1)
                top, bot = y_of(max(o, cc)), y_of(min(o, cc))
                Rectangle(pos=(cx - body_w / 2, bot), size=(body_w, max(dp(1), top - bot)))

            # EMA line
            if len(em) == n:
                pts = []
                for i, v in enumerate(em):
                    pts += [x0 + pad_l + slot * (i + 0.5), y_of(v)]
                Color(*hexc(AMBER))
                Line(points=pts, width=1.2)

            # ENTRY / TP / SL markers (dashed lines + price tags)
            for name, (price, col) in self.markers.items():
                Color(*hexc(col))
                Line(points=[x0 + pad_l, y_of(price), x0 + pad_l + plot_w, y_of(price)],
                     width=1.2, dash_length=dp(6), dash_offset=dp(4))
                tag(price, f"{name} {fmt(price)}", col)

            # last price tag
            if self.last_price is not None:
                last_up = cs[-1][3] >= cs[-1][0]
                tag(self.last_price, fmt(self.last_price), UP if last_up else DOWN)


# --------------------------------------------------------------------- app
class TradeMatrixApp(App):
    title = "TradeMatrix"

    # ------------------------------------------------------------------ build
    def build(self):
        if platform not in ("android", "ios"):
            Window.size = (420, 820)
        else:
            Window.softinput_mode = "below_target"
        Window.clearcolor = hexc(BG)

        self.sim = MarketSim()
        self.speaker = Speaker(on_error=self._voice_error)
        self.auto_speak = True
        self.position = None
        self.session_pnl = 0.0
        self.trade_count = 0
        self.prev_price = None
        self.ema_now = self.sim.price
        self.rsi_now = 50.0
        self._mic_active = False
        self._mic_cb = None

        root = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))

        # --- title bar ------------------------------------------------------
        bar = Panel(size_hint_y=None, height=dp(44), padding=[dp(12), dp(4)], spacing=dp(8))
        bar.add_widget(make_label("TRADEMATRIX", 18, ACCENT, bold=True, halign="left"))
        bar.add_widget(make_label(f"{SYMBOL} | {TIMEFRAME} | SIMULATION", 11, AMBER,
                                  halign="right"))
        root.add_widget(bar)

        # --- dashboard ------------------------------------------------------
        dash = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(340),
                         spacing=dp(6))
        price_panel = Panel(size_hint_y=None, height=dp(56), padding=[dp(12), dp(4)])
        self.lb_price = make_label("--", 30, TEXT, bold=True, halign="left")
        price_panel.add_widget(self.lb_price)
        dash.add_widget(price_panel)

        tiles = GridLayout(cols=3, spacing=dp(8), size_hint_y=None, height=dp(50))
        self.t_ema = StatTile(f"EMA {EMA_PERIOD}")
        self.t_rsi = StatTile(f"RSI {RSI_PERIOD}")
        self.t_trend = StatTile("TREND")
        for t in (self.t_ema, self.t_rsi, self.t_trend):
            tiles.add_widget(t)
        dash.add_widget(tiles)

        self.chart = CandleChart(size_hint_y=1)
        dash.add_widget(self.chart)

        self.banner = Panel(size_hint_y=None, height=dp(48), padding=[dp(10), dp(4)])
        self.lb_banner = make_label("", 12, TEXT)
        self.banner.add_widget(self.lb_banner)
        dash.add_widget(self.banner)
        root.add_widget(dash)

        # --- chat history ---------------------------------------------------
        self.sv_chat = ScrollView(do_scroll_x=False, bar_width=dp(3))
        self.chat_box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6),
                                  padding=[0, dp(4)])
        self.chat_box.bind(minimum_height=self.chat_box.setter("height"))
        self.sv_chat.add_widget(self.chat_box)
        root.add_widget(self.sv_chat)

        # --- auto-speak toggle + status --------------------------------------
        trow = BoxLayout(size_hint_y=None, height=dp(34), spacing=dp(8))
        self.tg_speak = ToggleButton(text="AUTO-SPEAK: ON", state="down", font_size=sp(12),
                                     size_hint_x=0.5, background_normal="",
                                     background_down="", background_color=hexc(UP),
                                     color=(1, 1, 1, 1), bold=True)
        self.tg_speak.bind(state=self._speak_toggled)
        trow.add_widget(self.tg_speak)
        self.lb_status = make_label("", 11, MUTED, halign="right")
        trow.add_widget(self.lb_status)
        root.add_widget(trow)

        # --- input row ------------------------------------------------------
        irow = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        self.ti_chat = TextInput(hint_text="Type a command (try: help)", multiline=False,
                                 font_size=sp(14), background_color=hexc(PANEL),
                                 foreground_color=hexc(TEXT), cursor_color=hexc(TEXT),
                                 hint_text_color=hexc(MUTED), padding=[dp(10), dp(12)])
        self.ti_chat.bind(on_text_validate=lambda *_: self.send_text())
        btn_send = flat_button("SEND", UP, size_hint_x=None, width=dp(72))
        btn_mic = flat_button("MIC", ACCENT, size_hint_x=None, width=dp(66))
        btn_send.bind(on_release=lambda *_: self.send_text())
        btn_mic.bind(on_release=lambda *_: self.start_mic())
        irow.add_widget(self.ti_chat)
        irow.add_widget(btn_send)
        irow.add_widget(btn_mic)
        root.add_widget(irow)

        self.update_dashboard()
        Clock.schedule_once(lambda dt: self.ai_says(
            "TradeMatrix AI is ready. This is a SIMULATION - no real orders are placed. "
            "Say buy, sell, status, price, analysis or help.", speak_text="TradeMatrix is ready"),
            0.8)
        self._tick_event = Clock.schedule_interval(self.tick, TICK_SECONDS)
        return root

    # ------------------------------------------------------------ simulation
    def tick(self, _dt):
        try:
            price = self.sim.tick()
            pos = self.position
            if pos is not None:
                if price >= pos["tp"]:
                    self.close_position("TAKE PROFIT")
                elif price <= pos["sl"]:
                    self.close_position("STOP LOSS")
            self.update_dashboard()
        except Exception:  # noqa: BLE001  - never let a tick crash the app
            log.exception("tick failed")

    def update_dashboard(self):
        price = self.sim.price
        closes = self.sim.closes()
        ema = ema_series(closes, EMA_PERIOD)
        self.ema_now = ema[-1]
        self.rsi_now = rsi_last(closes, RSI_PERIOD)

        colour = TEXT
        if self.prev_price is not None:
            colour = UP if price > self.prev_price else DOWN if price < self.prev_price else TEXT
        self.prev_price = price
        self.lb_price.text = fmt(price)
        self.lb_price.color = hexc(colour)

        self.t_ema.set(fmt(self.ema_now), AMBER)
        rsi = self.rsi_now
        self.t_rsi.set(f"{rsi:.1f}", DOWN if rsi >= 70 else UP if rsi <= 30 else TEXT)
        above = price >= self.ema_now
        self.t_trend.set("UP" if above else "DOWN", UP if above else DOWN)

        markers = {}
        pos = self.position
        if pos is not None:
            markers = {"ENTRY": (pos["entry"], TEXT), "TP": (pos["tp"], UP),
                       "SL": (pos["sl"], DOWN)}
            pnl_pct = (price - pos["entry"]) / pos["entry"] * 100.0
            pnl_usdt = NOTIONAL_USDT * pnl_pct / 100.0
            self.banner.set_bg(UP_DARK if pnl_pct >= 0 else DOWN_DARK)
            self.lb_banner.text = (
                f"OPEN LONG {SYMBOL}  |  Entry {fmt(pos['entry'])}\n"
                f"TP {fmt(pos['tp'])}   SL {fmt(pos['sl'])}   |   "
                f"P&L {pnl_pct:+.2f}% ({pnl_usdt:+.2f} USDT)")
            self.lb_banner.color = hexc(UP if pnl_pct >= 0 else DOWN)
        else:
            self.banner.set_bg(PANEL)
            self.lb_banner.text = (f"NO OPEN POSITION\nSession P&L {self.session_pnl:+.2f} USDT "
                                   f"({self.trade_count} trades)")
            self.lb_banner.color = hexc(MUTED)

        self.chart.set_data(self.sim.candles, ema, markers, price)

    # --------------------------------------------------------------- trading
    def open_position(self):
        price = self.sim.price
        self.position = {"entry": price,
                         "tp": price * (1 + TAKE_PROFIT_PCT / 100.0),
                         "sl": price * (1 - STOP_LOSS_PCT / 100.0)}
        self.update_dashboard()
        pos = self.position
        return (f"Order placed for {SYMBOL.replace('/', ' ')} at {fmt(price)}. "
                f"Take profit {fmt(pos['tp'])}, stop loss {fmt(pos['sl'])}. (Simulated)")

    def close_position(self, reason):
        pos = self.position
        if pos is None:
            return
        price = self.sim.price
        pnl_pct = (price - pos["entry"]) / pos["entry"] * 100.0
        pnl_usdt = NOTIONAL_USDT * pnl_pct / 100.0
        self.position = None
        self.session_pnl += pnl_usdt
        self.trade_count += 1
        sym = SYMBOL.replace("/", " ")
        if reason == "TAKE PROFIT":
            shown = f"TAKE PROFIT hit at {fmt(price)}: {pnl_pct:+.2f}% ({pnl_usdt:+.2f} USDT)."
            spoken = f"Take profit triggered for {sym} with {abs(pnl_pct):.2f} percent profit"
        elif reason == "STOP LOSS":
            shown = f"STOP LOSS hit at {fmt(price)}: {pnl_pct:+.2f}% ({pnl_usdt:+.2f} USDT)."
            spoken = f"Stop loss triggered for {sym} with {abs(pnl_pct):.2f} percent loss"
        else:
            shown = f"Position closed at {fmt(price)}: {pnl_pct:+.2f}% ({pnl_usdt:+.2f} USDT)."
            spoken = f"Position closed at {abs(pnl_pct):.2f} percent {'profit' if pnl_pct >= 0 else 'loss'}"
        self.update_dashboard()
        self.ai_says(shown, speak_text=spoken, urgent=True)

    # --------------------------------------------------------------- commands
    def analysis_text(self):
        price, ema, rsi = self.sim.price, self.ema_now, self.rsi_now
        rel = "above" if price >= ema else "below"
        if rsi >= 70:
            mom = "overbought, so a pullback is possible"
        elif rsi <= 30:
            mom = "oversold, so a bounce is possible"
        else:
            mom = "neutral"
        if price >= ema and rsi < 70:
            bias = "bullish"
        elif price < ema and rsi > 30:
            bias = "bearish"
        else:
            bias = "neutral"
        return (f"{SYMBOL.replace('/', ' ')} is at {fmt(price)}, {rel} the EMA {EMA_PERIOD} at "
                f"{fmt(ema)}. RSI is {rsi:.0f}, which is {mom}. Bias: {bias}. "
                f"This is simulated data, not financial advice.")

    def status_text(self):
        pos = self.position
        if pos is None:
            return (f"No open position. Session P&L is {self.session_pnl:+.2f} USDT over "
                    f"{self.trade_count} trades. Say buy to open a simulated long trade.")
        price = self.sim.price
        pnl_pct = (price - pos["entry"]) / pos["entry"] * 100.0
        return (f"Long from {fmt(pos['entry'])}, now {fmt(price)} ({pnl_pct:+.2f}%). "
                f"Take profit {fmt(pos['tp'])}, stop loss {fmt(pos['sl'])}.")

    def handle_command(self, text):
        t = re.sub(r"[^a-z0-9\s%.]", " ", text.lower())
        t = " ".join(t.split())
        if not t:
            return "I did not catch that. Say help to see what I can do."

        # trade actions only when the command STARTS with the action word
        # (so "should I buy now" is treated as a question, not an order)
        if re.match(r"^(please |ok |okay |now )?(buy|long|enter)\b", t):
            if self.position is not None:
                return "You already have an open position. Say sell to close it first."
            return self.open_position()
        if re.match(r"^(please |ok |okay |now )?(sell|close|exit|square off)\b", t):
            if self.position is None:
                return "There is no open position to close. (This simulator is long-only.)"
            self.close_position("MANUAL")
            return None                         # close_position already told the user

        if re.search(r"\b(help|commands|what can you do)\b", t):
            return ("Commands: buy, sell or close, status, price, rsi, ema, analysis, mute, "
                    "unmute. Take profit is 2 percent and stop loss is 1.5 percent. "
                    "All trades are simulated.")
        if re.search(r"\b(unmute|voice on|speak up|talk to me)\b", t):
            self.tg_speak.state = "down"
            return "Voice is on."
        if re.search(r"\b(mute|silence|be quiet|voice off|stop talking)\b", t):
            self.tg_speak.state = "normal"
            return "Voice is muted. I will reply in text only."
        if re.search(r"\b(status|position|positions|pnl|profit|trade|trades)\b", t):
            return self.status_text()
        if re.search(r"\brsi\b", t):
            rsi = self.rsi_now
            zone = "overbought" if rsi >= 70 else "oversold" if rsi <= 30 else "neutral"
            return f"RSI {RSI_PERIOD} is {rsi:.1f}, which is {zone}."
        if re.search(r"\bema\b", t):
            rel = "above" if self.sim.price >= self.ema_now else "below"
            return (f"EMA {EMA_PERIOD} is {fmt(self.ema_now)}. Price {fmt(self.sim.price)} is "
                    f"{rel} it.")
        if re.search(r"\b(price|rate|btc|bitcoin|how much|quote)\b", t):
            return f"{SYMBOL.replace('/', ' ')} is trading at {fmt(self.sim.price)}."
        if re.search(r"\b(analysis|analyse|analyze|market|trend|outlook|advice|should|view)\b", t):
            return self.analysis_text()
        if re.search(r"\b(hi|hello|hey|namaste)\b", t):
            return "Hello! Ask for analysis, price or status, or say buy to open a simulated trade."
        return "Sorry, I did not understand that. Say help to see the commands."

    # ------------------------------------------------------------------ chat
    def _add_bubble(self, who, text):
        row = BoxLayout(size_hint_y=None, height=dp(60))
        bubble = Bubble(who, text)
        bubble.bind(height=lambda _w, h: setattr(row, "height", h))
        if who == "You":
            row.add_widget(Widget(size_hint_x=0.14))
            row.add_widget(bubble)
        else:
            row.add_widget(bubble)
            row.add_widget(Widget(size_hint_x=0.14))
        self.chat_box.add_widget(row)
        if len(self.chat_box.children) > MAX_CHAT_BUBBLES:
            self.chat_box.remove_widget(self.chat_box.children[-1])    # oldest message
        Clock.schedule_once(lambda dt: setattr(self.sv_chat, "scroll_y", 0), 0.1)

    def user_says(self, text):
        self._add_bubble("You", text)
        try:
            reply = self.handle_command(text)
        except Exception:  # noqa: BLE001
            log.exception("command failed")
            reply = "Sorry, something went wrong handling that command."
        if reply:
            self.ai_says(reply)

    def ai_says(self, text, speak_text=None, urgent=False):
        self._add_bubble("TradeMatrix AI", text)
        if self.auto_speak:
            self.speaker.speak(speak_text or text, urgent=urgent)

    def send_text(self):
        text = self.ti_chat.text.strip()
        if not text:
            return
        self.ti_chat.text = ""
        self.user_says(text)

    # --------------------------------------------------------- auto-speak toggle
    def _speak_toggled(self, _w, state):
        self.auto_speak = state == "down"
        self.tg_speak.text = "AUTO-SPEAK: ON" if self.auto_speak else "AUTO-SPEAK: OFF"
        self.tg_speak.background_color = hexc(UP if self.auto_speak else NEUTRAL)
        if not self.auto_speak:
            self.speaker.stop()                  # mute immediately, including current speech
        self.lb_status.text = "Voice on" if self.auto_speak else "Voice muted"

    def _voice_error(self, msg):
        self.lb_status.text = "Voice unavailable"
        self._add_bubble("System", f"Voice output failed: {msg[:140]}")

    # ------------------------------------------------------------------- mic
    def start_mic(self):
        if self._mic_active:
            return
        if platform != "android":
            self._add_bubble("System", "The MIC button uses Android's speech recognizer. "
                                       "On desktop, type your command instead.")
            return
        try:
            from android import activity            # python-for-android module
            from jnius import autoclass

            Intent = autoclass("android.content.Intent")
            RecognizerIntent = autoclass("android.speech.RecognizerIntent")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")

            def on_result(request_code, result_code, data):
                if request_code != MIC_REQUEST_CODE:
                    return
                self._mic_cb = None
                try:
                    activity.unbind(on_activity_result=on_result)
                except Exception:  # noqa: BLE001
                    pass
                heard = None
                try:
                    if result_code == -1 and data is not None:          # RESULT_OK
                        results = data.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
                        if results is not None and results.size() > 0:
                            heard = str(results.get(0))
                except Exception:  # noqa: BLE001
                    heard = None
                Clock.schedule_once(lambda dt: self._mic_done(heard))

            self._mic_cb = on_result
            self._mic_active = True
            self.lb_status.text = "Listening..."
            activity.bind(on_activity_result=on_result)

            intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                            RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE, "en-IN")
            intent.putExtra(RecognizerIntent.EXTRA_PROMPT, "Say a trading command")
            intent.putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
            PythonActivity.mActivity.startActivityForResult(intent, MIC_REQUEST_CODE)
        except Exception as exc:  # noqa: BLE001
            self._mic_active = False
            self.lb_status.text = ""
            try:
                if self._mic_cb is not None:
                    from android import activity as _act
                    _act.unbind(on_activity_result=self._mic_cb)
            except Exception:  # noqa: BLE001
                pass
            self._mic_cb = None
            self._add_bubble("System", f"Speech recognition is not available on this device "
                                       f"({exc.__class__.__name__}).")

    def _mic_done(self, heard):
        self._mic_active = False
        self.lb_status.text = ""
        if heard:
            self.user_says(heard)
        else:
            self._add_bubble("System", "I could not hear anything. Tap MIC and try again.")

    # -------------------------------------------------------------- lifecycle
    def on_pause(self):
        return True                                      # keep the app alive on Android

    def on_stop(self):
        try:
            self._tick_event.cancel()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._mic_cb is not None:
                from android import activity
                activity.unbind(on_activity_result=self._mic_cb)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.speaker.shutdown()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    TradeMatrixApp().run()
