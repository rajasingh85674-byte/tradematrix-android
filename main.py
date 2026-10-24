"""
main.py - TradeMatrix (single file, drop-in replacement for your current main.py)

Project folder (all three files MUST sit side by side):

    TradeMatrix/
        main.py
        risk_manager.py
        voice_assistant.py

Features
    * Title bar with mode badge (PAPER / TESTNET / LIVE) and SETTINGS button
    * Live price from Binance (public REST, polled in a background thread)
    * Entry / P&L / Take-Profit / Stop-Loss tiles driven by RiskManager (TP 2.0%, SL 1.5%)
    * BUY and SELL NOW buttons. Orders are:
          no API keys saved  -> PAPER (simulated, no exchange orders)
          keys + Testnet ON  -> Binance Spot Testnet market orders
          keys + Testnet OFF -> LIVE market orders (asks for confirmation)
    * Automatic exit (market SELL) when Take-Profit or Stop-Loss is hit
    * AI chat (Google AI Studio / Gemini, offline fallback) with SPEAK (mic on Android),
      SEND, and spoken replies through VoiceAssistant
    * Settings popup: Testnet switch, API key/secret, Gemini key, order size, Save/Cancel,
      persisted to a local JSON file

Run on desktop:   python main.py      (pip install kivy requests pyttsx3)
Android build:    buildozer.spec requirements must include:
                  python3,kivy==2.3.0,pyjnius,requests,urllib3,idna,charset-normalizer,certifi
"""
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode

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
from kivy.uix.switch import Switch
from kivy.uix.textinput import TextInput
from kivy.uix.togglebutton import ToggleButton
from kivy.utils import escape_markup
from kivy.utils import get_color_from_hex as hexc
from kivy.utils import platform

from risk_manager import RiskManager
from voice_assistant import VoiceAssistant

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
TARGET_PROFIT_PCT = 2.0
STOP_LOSS_PCT = 1.5
POLL_SECONDS = 3
MAX_PRICE_FAILS = 5
HTTP_TIMEOUT = 10
MIN_ORDER_USDT = 5.0

DATA_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]
TESTNET_HOST = "https://testnet.binance.vision"
LIVE_HOST = "https://api.binance.com"

DEFAULT_CONFIG = {
    "testnet": True,
    "testnet_api_key": "",
    "testnet_api_secret": "",
    "live_api_key": "",
    "live_api_secret": "",
    "gemini_api_key": "",
    "gemini_model": "gemini-2.5-flash",   # change here if Google retires this model
    "order_usdt": 20.0,
}


# ------------------------------------------------------------------- helpers
def fmt_price(p):
    if p is None:
        return "--"
    return f"{p:,.2f}" if abs(p) >= 1 else f"{p:.6f}"


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
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


def to_pair(symbol):
    return symbol.replace("/", "").upper()


def fetch_price(symbol, testnet_venue=False):
    """Latest price from the venue the trade is placed on (testnet prices differ
    from mainnet, so TP/SL must be judged against the same venue)."""
    hosts = [TESTNET_HOST] if testnet_venue else DATA_HOSTS
    last = "unknown"
    for host in hosts:
        try:
            r = requests.get(host + "/api/v3/ticker/price",
                             params={"symbol": to_pair(symbol)}, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                price = float(r.json()["price"])
                if price > 0:
                    return price
            last = f"HTTP {r.status_code}"
        except (requests.RequestException, ValueError, KeyError) as exc:
            last = exc.__class__.__name__
    raise RuntimeError(f"Price feed unavailable ({last})")


def floor_step(qty, step):
    if step <= 0:
        return qty
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


# ----------------------------------------------------------- Binance client
class BinanceError(Exception):
    """Binance (or the network) refused or failed a request."""


class BinanceClient:
    """Minimal signed Binance Spot REST client (testnet or live)."""

    def __init__(self, key, secret, testnet):
        self.key = key.strip()
        self.secret = secret.strip()
        self.base = TESTNET_HOST if testnet else LIVE_HOST
        self._offset = None

    def _sync(self):
        if self._offset is None:
            try:
                r = requests.get(self.base + "/api/v3/time", timeout=HTTP_TIMEOUT)
                self._offset = int(r.json()["serverTime"]) - int(time.time() * 1000)
            except Exception:  # noqa: BLE001
                self._offset = 0

    def _call(self, method, path, params=None, signed=False):
        params = dict(params or {})
        headers = {"X-MBX-APIKEY": self.key}
        try:
            if signed:
                self._sync()
                params["timestamp"] = int(time.time() * 1000) + self._offset
                params["recvWindow"] = 10000
                query = urlencode(params)
                sig = hmac.new(self.secret.encode(), query.encode(),
                               hashlib.sha256).hexdigest()
                r = requests.request(method, f"{self.base}{path}?{query}&signature={sig}",
                                     headers=headers, timeout=HTTP_TIMEOUT)
            else:
                r = requests.request(method, self.base + path, params=params,
                                     headers=headers, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            raise BinanceError(f"Network error ({exc.__class__.__name__}). If this was an "
                               f"order, check your exchange before retrying.")
        try:
            data = r.json()
        except ValueError:
            raise BinanceError(f"HTTP {r.status_code}: unreadable response")
        if r.status_code != 200:
            msg = data.get("msg", "error") if isinstance(data, dict) else "error"
            raise BinanceError(f"{msg} (HTTP {r.status_code})")
        return data

    def lot_rules(self, pair):
        info = self._call("GET", "/api/v3/exchangeInfo", {"symbol": pair})
        step, min_qty = Decimal("0"), Decimal("0")
        for f in info["symbols"][0].get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                step, min_qty = Decimal(f["stepSize"]), Decimal(f["minQty"])
        return step, min_qty

    def free_balance(self, asset):
        data = self._call("GET", "/api/v3/account", signed=True)
        for b in data.get("balances", []):
            if b.get("asset") == asset:
                return Decimal(b["free"])
        return Decimal("0")

    def market_buy_quote(self, pair, quote_amount):
        return self._call("POST", "/api/v3/order", {
            "symbol": pair, "side": "BUY", "type": "MARKET",
            "quoteOrderQty": f"{float(quote_amount):.2f}",
            "newOrderRespType": "FULL"}, signed=True)

    def market_sell(self, pair, qty):
        return self._call("POST", "/api/v3/order", {
            "symbol": pair, "side": "SELL", "type": "MARKET",
            "quantity": format(qty.normalize(), "f"),
            "newOrderRespType": "FULL"}, signed=True)


def parse_fill(resp):
    """(executed_qty Decimal, average_price float, order_id str) from an order response."""
    executed = Decimal(str(resp.get("executedQty", "0")))
    spent = Decimal(str(resp.get("cummulativeQuoteQty", "0")))
    if executed <= 0 or spent <= 0:
        raise BinanceError(f"Order was not filled (status {resp.get('status', '?')})")
    return executed, float(spent / executed), str(resp.get("orderId", "?"))


# ------------------------------------------------------------ AI assistant
def gemini_reply(question, ctx, cfg):
    key = cfg.get("gemini_api_key", "").strip()
    model = (cfg.get("gemini_model") or "gemini-2.5-flash").strip()
    system = ("You are the concise assistant inside the TradeMatrix trading app. Use ONLY "
              "the market data provided; never invent prices. Reply in English, in at most "
              "4 short sentences of plain text with no markdown (it will be read aloud). "
              "Never promise profit; mention risk briefly if you suggest any action.")
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [
            {"text": f"APP DATA:\n{json.dumps(ctx)}\n\nUSER QUESTION:\n{question}"}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 800},
    }
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        json=body, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        timeout=20)
    if r.status_code != 200:
        try:
            detail = r.json().get("error", {}).get("message", "")
        except ValueError:
            detail = ""
        raise RuntimeError(f"Gemini HTTP {r.status_code} {detail[:100]}")
    try:
        parts = r.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).replace("*", "").strip()
    except (KeyError, IndexError, ValueError):
        raise RuntimeError("Gemini returned no usable answer")
    if not text:
        raise RuntimeError("Gemini returned an empty answer")
    return text


def offline_reply(ctx):
    if ctx.get("price") is None:
        return "I do not have a live price yet. Please wait a few seconds and ask again."
    sym = ctx["symbol"].replace("/", " ")
    text = f"{sym} is trading at {fmt_price(ctx['price'])}."
    pos = ctx.get("position")
    if pos:
        text += (f" You have an open {pos['mode']} position from {fmt_price(pos['entry'])}, "
                 f"currently {pos['pnl_pct']:+.2f} percent. Take profit is "
                 f"{fmt_price(pos['take_profit'])} and stop loss is {fmt_price(pos['stop_loss'])}.")
    else:
        text += " No position is open."
    return text + " Add a Gemini key in settings for full AI chat."


class MicInput:
    """Speech-to-text through Android's built-in recognizer dialog."""

    @staticmethod
    def listen(locale, on_result):
        """on_result(text_or_None, error_or_None) is always called on the Kivy thread."""
        if platform != "android":
            on_result(None, "Mic input works on Android. Type your question instead.")
            return
        try:
            from android import activity
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            RI = autoclass("android.speech.RecognizerIntent")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            request_code = 4242

            def _on_result(req, res, data):
                if req != request_code:
                    return
                try:
                    activity.unbind(on_activity_result=_on_result)
                except Exception:  # noqa: BLE001
                    pass
                heard = None
                try:
                    if res == -1 and data is not None:          # RESULT_OK
                        arr = data.getStringArrayListExtra(RI.EXTRA_RESULTS)
                        if arr is not None and arr.size() > 0:
                            heard = str(arr.get(0))
                except Exception:  # noqa: BLE001
                    heard = None
                Clock.schedule_once(lambda dt: on_result(
                    heard, None if heard else "Could not hear anything."))

            activity.bind(on_activity_result=_on_result)
            intent = Intent(RI.ACTION_RECOGNIZE_SPEECH)
            intent.putExtra(RI.EXTRA_LANGUAGE_MODEL, RI.LANGUAGE_MODEL_FREE_FORM)
            intent.putExtra(RI.EXTRA_LANGUAGE, locale)
            intent.putExtra(RI.EXTRA_PROMPT, "Speak now")
            PythonActivity.mActivity.startActivityForResult(intent, request_code)
        except Exception as exc:  # noqa: BLE001
            on_result(None, f"Voice input unavailable ({exc.__class__.__name__}).")


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
        super().__init__(orientation="vertical", padding=dp(8), spacing=dp(2), **kw)
        self.title = make_label(title, 11, MUTED)
        self.value = make_label("--", 17, TEXT, bold=True)
        self.add_widget(self.title)
        self.add_widget(self.value)

    def set(self, text, color=TEXT):
        self.value.text = text
        self.value.color = hexc(color)


# ----------------------------------------------------------------------- app
class TradeMatrixApp(App):
    title = "TradeMatrix"

    # ----------------------------------------------------------------- build
    def build(self):
        if platform not in ("android", "ios"):
            Window.size = (420, 820)
        else:
            Window.softinput_mode = "below_target"
        Window.clearcolor = hexc(BG)

        # core services
        self.cfg = load_config(self._cfg_path())
        self.voice = VoiceAssistant()
        self.risk = RiskManager(target_profit_pct=TARGET_PROFIT_PCT,
                                stop_loss_pct=STOP_LOSS_PCT)

        # state
        self.position = None            # dict while a trade is open
        self.order_busy = False
        self.price_busy = False
        self.price_fails = 0
        self.auto_exit_paused = False
        self.last_price = None
        self.prev_price = None
        self.chat = []
        self.chat_busy = False
        self._poll_event = None

        root = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))

        # --- title bar -----------------------------------------------------
        bar = Panel(size_hint_y=None, height=dp(52), padding=[dp(12), dp(6)], spacing=dp(8))
        bar.add_widget(make_label("TRADEMATRIX", 20, ACCENT, bold=True, halign="left"))
        self.badge = Panel(bg=AMBER, radius=6, size_hint_x=None, width=dp(86),
                           padding=dp(4))
        self.lb_badge = make_label("PAPER", 12, "#000000", bold=True)
        self.badge.add_widget(self.lb_badge)
        bar.add_widget(self.badge)
        btn_settings = flat_button("SETTINGS", NEUTRAL, size_hint_x=None, width=dp(96))
        btn_settings.bind(on_release=lambda *_: self.open_settings())
        bar.add_widget(btn_settings)
        root.add_widget(bar)

        # --- live price ----------------------------------------------------
        pp = Panel(orientation="vertical", size_hint_y=None, height=dp(118),
                   padding=dp(10), spacing=dp(2))
        top = BoxLayout(size_hint_y=None, height=dp(34), spacing=dp(8))
        self.sp_symbol = Spinner(text=SYMBOLS[0], values=SYMBOLS, font_size=sp(14),
                                 background_normal="", background_color=hexc(BG),
                                 color=hexc(TEXT))
        self.sp_symbol.bind(text=self._symbol_changed)
        top.add_widget(self.sp_symbol)
        top.add_widget(make_label("LIVE PRICE", 11, MUTED, halign="right"))
        pp.add_widget(top)
        self.lb_price = make_label("--", 34, TEXT, bold=True)
        pp.add_widget(self.lb_price)
        root.add_widget(pp)

        # --- P&L and levels ------------------------------------------------
        grid = GridLayout(cols=2, spacing=dp(8), size_hint_y=None,
                          height=dp(2 * 62 + 8))
        self.t_entry = StatTile("ENTRY")
        self.t_pnl = StatTile("P&L")
        self.t_tp = StatTile(f"TAKE PROFIT (+{TARGET_PROFIT_PCT:g}%)")
        self.t_sl = StatTile(f"STOP LOSS (-{STOP_LOSS_PCT:g}%)")
        for t in (self.t_entry, self.t_pnl, self.t_tp, self.t_sl):
            grid.add_widget(t)
        root.add_widget(grid)

        self.lb_status = make_label("Ready.", 13, MUTED, size_hint_y=None, height=dp(44))
        root.add_widget(self.lb_status)

        # --- trade buttons -------------------------------------------------
        row = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(10))
        self.btn_buy = flat_button("BUY", UP)
        self.btn_sell = flat_button("SELL NOW", DOWN)
        self.btn_buy.bind(on_release=lambda *_: self.on_buy())
        self.btn_sell.bind(on_release=lambda *_: self.on_sell_now())
        row.add_widget(self.btn_buy)
        row.add_widget(self.btn_sell)
        root.add_widget(row)

        # --- AI chat / voice assistant ------------------------------------
        root.add_widget(self._build_chat_panel())

        self._refresh_badge()
        self._sync_buttons()
        self._poll_event = Clock.schedule_interval(self.poll_price, POLL_SECONDS)
        Clock.schedule_once(lambda dt: self.poll_price(0), 0.3)
        Clock.schedule_once(
            lambda dt: self.voice.announce_status("TradeMatrix is ready"), 1.0)
        return root

    # -------------------------------------------------------------- chat UI
    def _build_chat_panel(self):
        p = Panel(orientation="vertical", size_hint_y=1, padding=dp(10), spacing=dp(6))
        p.add_widget(make_label("AI CHAT / VOICE ASSISTANT", 12, ACCENT, bold=True,
                                halign="left", size_hint_y=None, height=dp(20)))

        self.sv_chat = ScrollView(do_scroll_x=False, bar_width=dp(3))
        self.lb_chat = Label(markup=True, size_hint_y=None, halign="left", valign="top",
                             font_size=sp(13), color=hexc(TEXT))
        self.lb_chat.bind(
            width=lambda w, _v: setattr(w, "text_size", (w.width, None)),
            texture_size=lambda w, ts: setattr(w, "height", ts[1]))
        self.sv_chat.add_widget(self.lb_chat)
        p.add_widget(self.sv_chat)

        self.tg_voice = ToggleButton(text="VOICE REPLY: ON", state="down", font_size=sp(12),
                                     size_hint_y=None, height=dp(34), background_normal="",
                                     background_down="", background_color=hexc(UP),
                                     color=(1, 1, 1, 1))
        self.tg_voice.bind(state=self._voice_toggled)
        p.add_widget(self.tg_voice)

        self.ti_chat = TextInput(hint_text="Ask: should I hold? how is BTC doing?",
                                 multiline=False, size_hint_y=None, height=dp(42),
                                 font_size=sp(14), background_color=hexc(BG),
                                 foreground_color=hexc(TEXT), cursor_color=hexc(TEXT),
                                 hint_text_color=hexc(MUTED))
        self.ti_chat.bind(on_text_validate=lambda *_: self.send_chat())
        p.add_widget(self.ti_chat)

        b = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        btn_mic = flat_button("SPEAK", ACCENT)
        btn_send = flat_button("SEND", UP)
        btn_mic.bind(on_release=lambda *_: self.mic_chat())
        btn_send.bind(on_release=lambda *_: self.send_chat())
        b.add_widget(btn_mic)
        b.add_widget(btn_send)
        p.add_widget(b)
        self._chat_render()
        return p

    def _voice_toggled(self, _w, state):
        on = state == "down"
        self.voice.enabled = on
        self.tg_voice.text = "VOICE REPLY: ON" if on else "VOICE REPLY: OFF"
        self.tg_voice.background_color = hexc(UP if on else NEUTRAL)

    def _chat_add(self, who, text):
        self.chat.append((who, text))
        self.chat = self.chat[-30:]
        self._chat_render()

    def _chat_render(self):
        if not self.chat:
            self.lb_chat.text = ("Ask about the market or your position. Tap SPEAK to use "
                                 "your voice (Android), or type and tap SEND.")
            return
        lines = []
        for who, text in self.chat:
            col = (ACCENT if who == "You" else UP).lstrip("#")
            lines.append(f"[color={col}][b]{who}:[/b][/color] {escape_markup(text)}")
        self.lb_chat.text = "\n\n".join(lines)
        Clock.schedule_once(lambda dt: setattr(self.sv_chat, "scroll_y", 0), 0.05)

    def _chat_context(self):
        ctx = {"symbol": self.sp_symbol.text, "price": self.last_price,
               "mode": self._mode_name(), "position": None,
               "take_profit_pct": TARGET_PROFIT_PCT, "stop_loss_pct": STOP_LOSS_PCT}
        pos = self.position
        if pos:
            pnl = 0.0
            if self.last_price:
                pnl = (self.last_price - pos["entry"]) / pos["entry"] * 100.0
            ctx["position"] = {"mode": pos["mode"], "entry": pos["entry"],
                               "take_profit": pos["levels"]["target_price"],
                               "stop_loss": pos["levels"]["stop_price"],
                               "pnl_pct": round(pnl, 3)}
        return ctx

    def mic_chat(self):
        if self.chat_busy:
            return
        self._status("Listening...", ACCENT)
        MicInput.listen("en-IN", self._heard)

    def _heard(self, text, err):
        if text:
            self.send_chat(text)
        else:
            self._status(err or "Voice input failed.", DOWN)

    def send_chat(self, text=None):
        q = (text if text is not None else self.ti_chat.text).strip()
        if not q or self.chat_busy:
            return
        self.ti_chat.text = ""
        self.chat_busy = True
        self._chat_add("You", q)
        self._status("AI is thinking...", ACCENT)
        ctx, cfg = self._chat_context(), dict(self.cfg)

        def worker():
            note = ""
            if cfg.get("gemini_api_key", "").strip():
                try:
                    reply, note = gemini_reply(q, ctx, cfg), "AI: Gemini"
                except Exception as exc:  # noqa: BLE001
                    log.warning("gemini failed: %s", exc)
                    reply = offline_reply(ctx)
                    note = f"AI offline summary ({str(exc)[:60]})"
            else:
                reply, note = offline_reply(ctx), "AI offline summary (no Gemini key)"
            Clock.schedule_once(lambda dt: self._chat_reply(reply, note))

        threading.Thread(target=worker, daemon=True).start()

    def _chat_reply(self, reply, note):
        self.chat_busy = False
        self._chat_add("AI", reply)
        self._status(note, MUTED)
        self.voice.speak(reply)

    # -------------------------------------------------------------- helpers
    def _cfg_path(self):
        return os.path.join(self.user_data_dir, "tradematrix_config.json")

    def _status(self, text, color=MUTED):
        self.lb_status.text = text
        self.lb_status.color = hexc(color)

    def _mode_name(self):
        testnet = bool(self.cfg.get("testnet", True))
        key = (self.cfg.get("testnet_api_key" if testnet else "live_api_key") or "").strip()
        sec = (self.cfg.get("testnet_api_secret" if testnet else "live_api_secret") or "").strip()
        if not key or not sec:
            return "PAPER"
        return "TESTNET" if testnet else "LIVE"

    def _make_client(self):
        """(mode, BinanceClient or None) for the current settings."""
        mode = self._mode_name()
        if mode == "PAPER":
            return mode, None
        testnet = mode == "TESTNET"
        key = self.cfg["testnet_api_key" if testnet else "live_api_key"]
        sec = self.cfg["testnet_api_secret" if testnet else "live_api_secret"]
        return mode, BinanceClient(key, sec, testnet)

    def _refresh_badge(self):
        mode = self._mode_name()
        colour = {"PAPER": NEUTRAL, "TESTNET": AMBER, "LIVE": DOWN}[mode]
        self.badge.set_bg(colour)
        self.lb_badge.text = mode
        self.lb_badge.color = (0, 0, 0, 1) if mode == "TESTNET" else (1, 1, 1, 1)
        self.btn_buy.text = f"BUY ({mode})"

    def _sync_buttons(self):
        has_pos = self.position is not None
        self.btn_buy.disabled = has_pos or self.order_busy
        self.btn_sell.disabled = (not has_pos) or self.order_busy
        self.btn_buy.background_color = hexc(NEUTRAL if self.btn_buy.disabled else UP)
        self.btn_sell.background_color = hexc(NEUTRAL if self.btn_sell.disabled else DOWN)
        self.sp_symbol.disabled = has_pos or self.order_busy

    def _symbol_changed(self, *_):
        self.last_price = None
        self.prev_price = None
        self.lb_price.text = "--"
        self.lb_price.color = hexc(TEXT)
        Clock.schedule_once(lambda dt: self.poll_price(0), 0.1)

    def _show_position(self, pnl_pct=None):
        pos = self.position
        if not pos:
            self.t_entry.set("--")
            self.t_pnl.set("--")
            self.t_tp.set("--")
            self.t_sl.set("--")
            return
        lv = pos["levels"]
        self.t_entry.set(fmt_price(pos["entry"]))
        self.t_tp.set(fmt_price(lv["target_price"]), UP)
        self.t_sl.set(fmt_price(lv["stop_price"]), DOWN)
        if pnl_pct is not None:
            self.t_pnl.set(f"{pnl_pct:+.2f}%", UP if pnl_pct > 0 else DOWN if pnl_pct < 0 else TEXT)

    # ------------------------------------------------------ live price loop
    def poll_price(self, _dt):
        if self.price_busy:
            return
        self.price_busy = True
        symbol = self.sp_symbol.text
        pos = self.position
        testnet_venue = (pos["mode"] == "TESTNET") if pos else (self._mode_name() == "TESTNET")

        def worker():
            try:
                price, err = fetch_price(symbol, testnet_venue), None
            except Exception as exc:  # noqa: BLE001
                price, err = None, str(exc)
            Clock.schedule_once(lambda dt: self._price_done(symbol, price, err))

        threading.Thread(target=worker, daemon=True).start()

    def _price_done(self, symbol, price, err):
        self.price_busy = False
        if symbol != self.sp_symbol.text:
            return                                     # symbol changed meanwhile
        if err or price is None:
            self.price_fails += 1
            if self.price_fails == 1 or self.price_fails % 10 == 0:
                self._status(f"{err or 'Price error'} - retrying...", DOWN)
            if self.position and self.price_fails == MAX_PRICE_FAILS:
                self.voice.announce_status("Price feed lost. Check your position")
            return
        self.price_fails = 0

        self.prev_price = self.last_price
        self.last_price = price
        colour = TEXT
        if self.prev_price is not None:
            colour = UP if price > self.prev_price else DOWN if price < self.prev_price else TEXT
        self.lb_price.text = fmt_price(price)
        self.lb_price.color = hexc(colour)

        pos = self.position
        if not pos:
            return
        try:
            result = self.risk.evaluate_trade(pos["entry"], price)
        except ValueError as exc:
            log.warning("evaluate_trade skipped: %s", exc)
            return
        self._show_position(result["pnl_pct"])
        if (result["status"] == "TRIGGER_EXIT" and not self.order_busy
                and not self.auto_exit_paused):
            self.begin_exit(result["reason"])

    # ------------------------------------------------------------------ BUY
    def on_buy(self):
        if self.position or self.order_busy:
            return
        mode = self._mode_name()
        if mode != "LIVE":
            self._do_buy()
            return
        usdt = float(self.cfg.get("order_usdt", 20.0))
        body = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
        body.add_widget(make_label(
            f"LIVE ORDER - REAL MONEY\nBuy ~{usdt:g} USDT of {self.sp_symbol.text} at market.\n"
            f"Auto-exit: TP +{TARGET_PROFIT_PCT:g}% / SL -{STOP_LOSS_PCT:g}%", 15, DOWN))
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(10))
        pop = Popup(title="Confirm live order", content=body, size_hint=(0.92, 0.5),
                    auto_dismiss=False, background_color=hexc(PANEL))
        b_no = flat_button("CANCEL", NEUTRAL)
        b_yes = flat_button("CONFIRM", DOWN)
        b_no.bind(on_release=lambda *_: pop.dismiss())
        b_yes.bind(on_release=lambda *_: (pop.dismiss(), self._do_buy()))
        row.add_widget(b_no)
        row.add_widget(b_yes)
        body.add_widget(row)
        pop.open()

    def _do_buy(self):
        mode, client = self._make_client()
        symbol = self.sp_symbol.text
        try:
            usdt = float(self.cfg.get("order_usdt", 20.0))
        except (TypeError, ValueError):
            usdt = 20.0
        self.order_busy = True
        self._sync_buttons()
        self._status(f"Placing {mode} buy order...", ACCENT)

        def worker():
            try:
                price = fetch_price(symbol, mode == "TESTNET")
                if mode == "PAPER":
                    qty = Decimal(str(usdt)) / Decimal(str(price))
                    entry, oid = price, f"PAPER-{int(time.time())}"
                else:
                    qty, entry, oid = parse_fill(client.market_buy_quote(to_pair(symbol), usdt))
                res = {"ok": True, "entry": entry, "qty": qty, "order_id": oid}
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "msg": str(exc)}
            Clock.schedule_once(lambda dt: self._buy_done(res, symbol, mode, client))

        threading.Thread(target=worker, daemon=True).start()

    def _buy_done(self, res, symbol, mode, client):
        self.order_busy = False
        if not res["ok"]:
            self._status(f"Buy failed: {res['msg']}", DOWN)
            self.voice.announce_status("Buy order failed")
            self._sync_buttons()
            return
        try:
            levels = self.risk.calculate_levels(res["entry"])
        except ValueError as exc:
            self._status(f"Risk error: {exc}. Position opened but NOT monitored - "
                         f"check your exchange!", DOWN)
            self.voice.announce_status("Risk calculation error. Check your position")
            self._sync_buttons()
            return
        self.position = {"symbol": symbol, "mode": mode, "client": client,
                         "entry": res["entry"], "qty": res["qty"],
                         "order_id": res["order_id"], "levels": levels}
        self.auto_exit_paused = False
        self.last_price = res["entry"]
        self.lb_price.text = fmt_price(res["entry"])
        self._show_position(0.0)
        self._status(f"{mode} BUY filled @ {fmt_price(res['entry'])} "
                     f"(Order ID {res['order_id']}). Monitoring TP/SL...", UP)
        self._sync_buttons()
        self.voice.announce_buy(symbol, res["entry"])
        self.voice.announce_status("Monitoring take profit and stop loss")

    # ----------------------------------------------------------------- EXIT
    def on_sell_now(self):
        if not self.position or self.order_busy:
            return
        self.auto_exit_paused = False
        self.begin_exit("MANUAL")

    def begin_exit(self, reason):
        pos = self.position
        if not pos or self.order_busy:
            return
        self.order_busy = True
        self._sync_buttons()
        self._status(f"{reason.replace('_', ' ')}: placing SELL order...", ACCENT)

        def worker():
            try:
                pair = to_pair(pos["symbol"])
                if pos["mode"] == "PAPER":
                    exit_price, oid = fetch_price(pos["symbol"], False), f"PAPER-{int(time.time())}"
                else:
                    client = pos["client"]
                    base = pos["symbol"].split("/")[0]
                    step, min_qty = client.lot_rules(pair)
                    qty = floor_step(min(pos["qty"], client.free_balance(base)), step)
                    if qty <= 0 or qty < min_qty:
                        raise BinanceError(f"Sell size {qty} is below the exchange minimum "
                                           f"({min_qty}) - sell manually on the exchange")
                    _, exit_price, oid = parse_fill(client.market_sell(pair, qty))
                res = {"ok": True, "exit": exit_price, "order_id": oid}
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "msg": str(exc)}
            Clock.schedule_once(lambda dt: self._exit_done(res, pos, reason))

        threading.Thread(target=worker, daemon=True).start()

    def _exit_done(self, res, pos, reason):
        self.order_busy = False
        if not res["ok"]:
            self.auto_exit_paused = True               # no retry storm; user decides
            self._status(f"SELL FAILED: {res['msg']}\nAuto-exit paused - press SELL NOW "
                         f"to retry or close it on the exchange.", DOWN)
            self.voice.announce_status("Exit order failed. Check your position")
            self._sync_buttons()
            return

        pnl = (res["exit"] - pos["entry"]) / pos["entry"] * 100.0
        self.position = None
        self.auto_exit_paused = False
        sym = pos["symbol"]
        if reason == "TAKE_PROFIT":
            head, colour = "TAKE PROFIT hit", UP
            self.voice.announce_take_profit(sym, pnl)
        elif reason == "STOP_LOSS":
            head, colour = "STOP LOSS hit", DOWN
            self.voice.announce_stop_loss(sym, abs(pnl))
        else:
            head, colour = "Position closed", UP if pnl >= 0 else DOWN
            self.voice.announce_status(f"Position closed at {pnl:+.2f} percent")
        self._status(f"{head}. SOLD @ {fmt_price(res['exit'])} ({pnl:+.2f}%), "
                     f"Order ID {res['order_id']}.", colour)
        self.t_entry.set("--")
        self.t_tp.set("--")
        self.t_sl.set("--")
        self.t_pnl.set(f"{pnl:+.2f}%", colour)
        self._sync_buttons()

    # ------------------------------------------------------------- settings
    def open_settings(self):
        if self.order_busy:
            self._status("Wait for the current order to finish.", AMBER)
            return
        cfg = dict(self.cfg)
        pairs = {"testnet": [cfg["testnet_api_key"], cfg["testnet_api_secret"]],
                 "live": [cfg["live_api_key"], cfg["live_api_secret"]]}
        state = {"mode": "testnet" if cfg["testnet"] else "live"}

        body = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(8))
        sv = ScrollView(do_scroll_x=False)
        form = BoxLayout(orientation="vertical", spacing=dp(8), size_hint_y=None)
        form.bind(minimum_height=form.setter("height"))

        def field(hint, password=False, value=""):
            return TextInput(hint_text=hint, text=str(value), password=password,
                             multiline=False, size_hint_y=None, height=dp(44),
                             font_size=sp(13), write_tab=False, background_color=hexc(BG),
                             foreground_color=hexc(TEXT), hint_text_color=hexc(MUTED))

        sw_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        sw_row.add_widget(make_label("Binance Testnet", 15, TEXT, bold=True, halign="left"))
        sw = Switch(active=cfg["testnet"], size_hint_x=None, width=dp(100))
        sw_row.add_widget(sw)

        hint = make_label("", 12, MUTED, size_hint_y=None, height=dp(44))
        ti_key = field("Binance API key", value=pairs[state["mode"]][0])
        ti_sec = field("Binance API secret", True, pairs[state["mode"]][1])
        ti_gem = field("Google AI Studio (Gemini) API key", True, cfg["gemini_api_key"])
        ti_usdt = field(f"Order size in USDT (min {MIN_ORDER_USDT:g})", value=cfg["order_usdt"])
        lb_err = make_label("", 12, DOWN, size_hint_y=None, height=dp(24))

        def paint():
            if state["mode"] == "testnet":
                hint.text = ("Testnet keys from testnet.binance.vision. Leave empty = "
                             "simulated PAPER trades.")
                hint.color = hexc(MUTED)
            else:
                hint.text = "LIVE = real money. Use a key WITHOUT withdrawal rights."
                hint.color = hexc(DOWN)

        def on_switch(_w, active):
            pairs[state["mode"]] = [ti_key.text.strip(), ti_sec.text.strip()]
            state["mode"] = "testnet" if active else "live"
            ti_key.text, ti_sec.text = pairs[state["mode"]]
            paint()

        sw.bind(active=on_switch)
        paint()
        for w in (sw_row, hint, ti_key, ti_sec, ti_gem, ti_usdt, lb_err):
            form.add_widget(w)
        sv.add_widget(form)
        body.add_widget(sv)

        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(10))
        pop = Popup(title="Settings", content=body, size_hint=(0.95, 0.88),
                    auto_dismiss=False, background_color=hexc(PANEL))

        def save(*_):
            try:
                usdt = float(ti_usdt.text.strip())
                if usdt < MIN_ORDER_USDT or usdt > 1_000_000:
                    raise ValueError
            except ValueError:
                lb_err.text = f"Order size must be a number of at least {MIN_ORDER_USDT:g}."
                return
            pairs[state["mode"]] = [ti_key.text.strip(), ti_sec.text.strip()]
            new = dict(self.cfg)
            new.update({"testnet": state["mode"] == "testnet",
                        "testnet_api_key": pairs["testnet"][0],
                        "testnet_api_secret": pairs["testnet"][1],
                        "live_api_key": pairs["live"][0],
                        "live_api_secret": pairs["live"][1],
                        "gemini_api_key": ti_gem.text.strip(),
                        "order_usdt": usdt})
            try:
                save_config(self._cfg_path(), new)
            except OSError as exc:
                lb_err.text = f"Could not save settings: {exc}"
                return
            self.cfg = new
            self._refresh_badge()
            self._status("Settings saved.", UP)
            pop.dismiss()

        b_c = flat_button("CANCEL", NEUTRAL)
        b_s = flat_button("SAVE", UP)
        b_c.bind(on_release=lambda *_: pop.dismiss())
        b_s.bind(on_release=save)
        row.add_widget(b_c)
        row.add_widget(b_s)
        body.add_widget(row)
        pop.open()

    # ------------------------------------------------------------ lifecycle
    def on_pause(self):
        return True                                    # keep the app alive on Android

    def on_stop(self):
        """App is closing: stop the price timer and shut down the voice thread."""
        try:
            if self._poll_event is not None:
                self._poll_event.cancel()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.voice.shutdown()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    TradeMatrixApp().run()
