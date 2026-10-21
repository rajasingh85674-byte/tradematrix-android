"""
main.py - Kivy front-end (TradingView Pro dark theme #0B0E14) with
voice/text AI assistant, 70% confidence filter display and Binance execution.
"""
import os
import threading
import time

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, Ellipse, RoundedRectangle
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.progressbar import ProgressBar
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput
from kivy.uix.togglebutton import ToggleButton
from kivy.uix.widget import Widget
from kivy.utils import escape_markup
from kivy.utils import get_color_from_hex as hexc
from kivy.utils import platform

import quant_engine as qe

BG = "#0B0E14"
PANEL = "#131722"
TEXT = "#D1D4DC"
MUTED = "#787B86"
UP = "#26A69A"
DOWN = "#EF5350"
NEUTRAL = "#434651"
ACCENT = "#2962FF"
AMBER = "#F7B500"

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d"]
LANGS = ["Auto", "English", "Hindi"]
MIC_LOCALE = {"Auto": "en-IN", "English": "en-IN", "Hindi": "hi-IN"}


def find_hindi_font():
    """Kivy's default font has no Devanagari. Look for one (drop
    NotoSansDevanagari-Regular.ttf into a 'fonts' folder to guarantee it)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "fonts", "NotoSansDevanagari-Regular.ttf"),
              "/system/fonts/NotoSansDevanagari-Regular.ttf",
              "/system/fonts/NotoSansDevanagariUI-Regular.ttf",
              "/system/fonts/NotoSerifDevanagari-Regular.ttf",
              "/system/fonts/Lohit-Devanagari.ttf",
              "C:/Windows/Fonts/Nirmala.ttf",
              "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf"):
        if os.path.exists(p):
            return p
    return None


HI_FONT = find_hindi_font()


# ------------------------------------------------------------------ widgets
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


class Dot(Widget):
    def __init__(self, **kw):
        super().__init__(**kw)
        with self.canvas:
            self._c = Color(*hexc(NEUTRAL))
            self._e = Ellipse(pos=self.pos, size=self.size)
        self.bind(pos=self._upd, size=self._upd)

    def _upd(self, *_):
        d = min(self.width, self.height)
        self._e.size = (d, d)
        self._e.pos = (self.center_x - d / 2, self.center_y - d / 2)

    def set_color(self, h):
        self._c.rgba = hexc(h)


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


# -------------------------------------------------------------- voice in/out
class VoiceIO:
    """Speech output: gTTS -> MediaPlayer (Android) / SoundLoader (desktop),
    pyttsx3 as desktop fallback. Speech input: Android RecognizerIntent."""

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self._mp = None
        self._snd = None

    def speak(self, text, lang):
        """Blocking - call from a worker thread. Returns True if audio started."""
        text = (text or "").strip()[:900]
        if not text:
            return False
        try:
            from gtts import gTTS
            path = os.path.join(self.cache_dir, f"tts_{int(time.time() * 1000)}.mp3")
            try:
                tts = gTTS(text=text, lang=lang, timeout=10)
            except TypeError:               # older gTTS without timeout argument
                tts = gTTS(text=text, lang=lang)
            tts.save(path)
            self._play(path)
            self._cleanup(path)
            return True
        except Exception:  # noqa: BLE001
            pass
        if platform not in ("android", "ios") and lang == "en":
            try:
                import pyttsx3
                eng = pyttsx3.init()
                eng.say(text)
                eng.runAndWait()
                return True
            except Exception:  # noqa: BLE001
                return False
        return False

    def _play(self, path):
        if platform == "android":
            from jnius import autoclass
            MediaPlayer = autoclass("android.media.MediaPlayer")
            if self._mp is not None:
                try:
                    self._mp.stop()
                    self._mp.release()
                except Exception:  # noqa: BLE001
                    pass
            mp = MediaPlayer()
            mp.setDataSource(path)
            mp.prepare()
            mp.start()
            self._mp = mp
        else:
            from kivy.core.audio import SoundLoader
            if self._snd is not None:
                try:
                    self._snd.stop()
                except Exception:  # noqa: BLE001
                    pass
            snd = SoundLoader.load(path)
            if snd is None:
                raise RuntimeError("no mp3 audio backend")
            snd.play()
            self._snd = snd

    def _cleanup(self, keep):
        try:
            for f in os.listdir(self.cache_dir):
                p = os.path.join(self.cache_dir, f)
                if f.startswith("tts_") and p != keep:
                    os.remove(p)
        except OSError:
            pass

    def listen(self, locale, on_text):
        """on_text(text_or_None, error_or_None) is called on the Kivy thread."""
        if platform != "android":
            on_text(None, "Mic input works on Android. Type your question instead.")
            return
        try:
            from android import activity
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            RI = autoclass("android.speech.RecognizerIntent")
            PA = autoclass("org.kivy.android.PythonActivity")
            REQ = 4242

            def _result(req, res, data):
                if req != REQ:
                    return
                try:
                    activity.unbind(on_activity_result=_result)
                except Exception:  # noqa: BLE001
                    pass
                heard = None
                try:
                    if res == -1 and data is not None:     # RESULT_OK
                        arr = data.getStringArrayListExtra(RI.EXTRA_RESULTS)
                        if arr is not None and arr.size() > 0:
                            heard = str(arr.get(0))
                except Exception:  # noqa: BLE001
                    heard = None
                Clock.schedule_once(
                    lambda dt: on_text(heard, None if heard else "Could not hear anything."))

            activity.bind(on_activity_result=_result)
            intent = Intent(RI.ACTION_RECOGNIZE_SPEECH)
            intent.putExtra(RI.EXTRA_LANGUAGE_MODEL, RI.LANGUAGE_MODEL_FREE_FORM)
            intent.putExtra(RI.EXTRA_LANGUAGE, locale)
            intent.putExtra(RI.EXTRA_PROMPT, "Speak now")
            PA.mActivity.startActivityForResult(intent, REQ)
        except Exception as exc:  # noqa: BLE001
            on_text(None, f"Voice input unavailable ({exc.__class__.__name__}).")


# ---------------------------------------------------------------------- app
class TradingApp(App):
    title = "Simons Quant Terminal"

    def build(self):
        if platform not in ("android", "ios"):
            Window.size = (420, 820)
        else:
            Window.softinput_mode = "below_target"
        Window.clearcolor = hexc(BG)

        self.busy = False
        self.price_busy = False
        self.chat_busy = False
        self.last_signal = None
        self.prev_price = None
        self.chat = []
        self.voice_on = True
        self.cfg = qe.load_config(self._cfg_path())
        self.voice = VoiceIO(os.path.join(self.user_data_dir, "tts"))

        root = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))

        head = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        self.sp_symbol = Spinner(text=SYMBOLS[0], values=SYMBOLS, font_size=sp(15),
                                 background_normal="", background_color=hexc(PANEL),
                                 color=hexc(TEXT))
        self.sp_tf = Spinner(text="15m", values=TIMEFRAMES, size_hint_x=0.3, font_size=sp(15),
                             background_normal="", background_color=hexc(PANEL),
                             color=hexc(TEXT))
        self.btn_mode = Button(size_hint_x=0.34, bold=True, font_size=sp(13),
                               background_normal="")
        self.btn_mode.bind(on_release=lambda *_: self.open_settings())
        for w in (self.sp_symbol, self.sp_tf, self.btn_mode):
            head.add_widget(w)
        root.add_widget(head)
        self._refresh_mode()

        sv = ScrollView(do_scroll_x=False, bar_width=dp(2))
        content = BoxLayout(orientation="vertical", spacing=dp(8), size_hint_y=None)
        content.bind(minimum_height=content.setter("height"))
        sv.add_widget(content)
        root.add_widget(sv)

        pp = Panel(orientation="vertical", size_hint_y=None, height=dp(92), padding=dp(10))
        pp.add_widget(make_label("LIVE PRICE", 11, MUTED))
        self.lb_price = make_label("--", 34, TEXT, bold=True)
        pp.add_widget(self.lb_price)
        content.add_widget(pp)

        self.card = Panel(orientation="horizontal", size_hint_y=None, height=dp(110),
                          padding=dp(14), spacing=dp(14))
        self.dot = Dot(size_hint_x=None, width=dp(64))
        self.card.add_widget(self.dot)
        right = BoxLayout(orientation="vertical")
        self.lb_dir = make_label("NO SIGNAL", 28, MUTED, bold=True, halign="left")
        self.lb_sub = make_label("Press SCAN MARKET", 13, MUTED, halign="left",
                                 size_hint_y=None, height=dp(24))
        right.add_widget(self.lb_dir)
        right.add_widget(self.lb_sub)
        self.card.add_widget(right)
        content.add_widget(self.card)

        grid = GridLayout(cols=2, spacing=dp(8), size_hint_y=None, height=dp(3 * 70 + 2 * 8))
        self.t_tp = StatTile("TARGET PRICE (TP)")
        self.t_sl = StatTile("STOP LOSS (SL)")
        self.t_pat = StatTile("PATTERN")
        self.t_conf = StatTile("CONFIDENCE")
        self.t_rsi = StatTile("RSI 14")
        self.t_rr = StatTile("RISK : REWARD")
        for t in (self.t_tp, self.t_sl, self.t_pat, self.t_conf, self.t_rsi, self.t_rr):
            grid.add_widget(t)
        content.add_widget(grid)

        cp = Panel(orientation="vertical", size_hint_y=None, height=dp(50), padding=dp(10))
        self.bar = ProgressBar(max=100, value=0)
        cp.add_widget(self.bar)
        content.add_widget(cp)

        rp = Panel(orientation="vertical", size_hint_y=None, height=dp(130), padding=dp(10))
        rp.add_widget(make_label("SIGNAL LOGIC", 11, MUTED, halign="left",
                                 size_hint_y=None, height=dp(18)))
        self.lb_reasons = make_label("-", 13, TEXT, halign="left")
        self.lb_reasons.valign = "top"
        rp.add_widget(self.lb_reasons)
        content.add_widget(rp)

        content.add_widget(self._build_chat_panel())

        self.lb_status = make_label("Ready.", 12, MUTED, size_hint_y=None, height=dp(24))
        root.add_widget(self.lb_status)

        btns = BoxLayout(size_hint_y=None, height=dp(56), spacing=dp(10))
        self.btn_scan = flat_button("SCAN MARKET", ACCENT)
        self.btn_exec = flat_button("EXECUTE LIVE TRADE", NEUTRAL)
        self.btn_scan.bind(on_release=lambda *_: self.scan())
        self.btn_exec.bind(on_release=lambda *_: self.confirm_execute())
        btns.add_widget(self.btn_scan)
        btns.add_widget(self.btn_exec)
        root.add_widget(btns)

        Clock.schedule_once(lambda dt: self.scan(), 0.5)
        Clock.schedule_interval(self.poll_price, 5)
        return root

    # ----------------------------------------------------------- chat panel
    def _build_chat_panel(self):
        p = Panel(orientation="vertical", size_hint_y=None, height=dp(420),
                  padding=dp(10), spacing=dp(6))
        p.add_widget(make_label("SPEAK / CHAT WITH AI ASSISTANT", 13, ACCENT, bold=True,
                                halign="left", size_hint_y=None, height=dp(22)))
        self.lb_chat = make_label("", 13, TEXT, halign="left", markup=True)
        self.lb_chat.valign = "top"
        p.add_widget(self.lb_chat)

        row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(8))
        self.sp_lang = Spinner(text="Auto", values=LANGS, font_size=sp(13),
                               background_normal="", background_color=hexc(BG),
                               color=hexc(TEXT))
        self.tg_voice = ToggleButton(text="VOICE REPLY: ON", state="down", font_size=sp(12),
                                     background_normal="", background_down="",
                                     background_color=hexc(UP), color=(1, 1, 1, 1))
        self.tg_voice.bind(state=self._voice_toggled)
        row.add_widget(self.sp_lang)
        row.add_widget(self.tg_voice)
        p.add_widget(row)

        self.ti_chat = TextInput(hint_text="Ask: Is BTC a buy now? / aaj market kaisa hai?",
                                 multiline=False, size_hint_y=None, height=dp(44),
                                 font_size=sp(14), background_color=hexc(BG),
                                 foreground_color=hexc(TEXT), cursor_color=hexc(TEXT),
                                 hint_text_color=hexc(MUTED))
        self.ti_chat.bind(on_text_validate=lambda *_: self.send_chat())
        p.add_widget(self.ti_chat)

        b = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        for text, color, fn in (("SPEAK", ACCENT, self.mic_chat),
                                ("MARKET BRIEF", NEUTRAL, self.brief_chat),
                                ("SEND", UP, lambda: self.send_chat())):
            btn = flat_button(text, color)
            btn.bind(on_release=lambda _b, f=fn: f())
            b.add_widget(btn)
        p.add_widget(b)
        self._chat_render()
        return p

    def _voice_toggled(self, _w, state):
        self.voice_on = state == "down"
        self.tg_voice.text = "VOICE REPLY: ON" if self.voice_on else "VOICE REPLY: OFF"
        self.tg_voice.background_color = hexc(UP if self.voice_on else NEUTRAL)

    def _fmt(self, who, text):
        t = escape_markup(text)
        if HI_FONT and qe.has_devanagari(text):
            t = f"[font={HI_FONT}]{t}[/font]"
        col = (ACCENT if who == "You" else UP).lstrip("#")
        return f"[color={col}][b]{who}:[/b][/color] {t}"

    def _chat_add(self, who, text):
        self.chat.append((who, text))
        self.chat = self.chat[-6:]
        self._chat_render()

    def _chat_render(self):
        parts = []
        for who, text in self.chat[-4:]:
            t = text if len(text) <= 280 else text[:277] + "..."
            parts.append(self._fmt(who, t))
        self.lb_chat.text = ("\n\n".join(parts) if parts else
                             "Ask about the market, tap SPEAK to use your voice, or tap "
                             "MARKET BRIEF for a spoken summary.")

    def mic_chat(self):
        if self.chat_busy:
            return
        self._status("Listening...", ACCENT)
        self.voice.listen(MIC_LOCALE.get(self.sp_lang.text, "en-IN"), self._heard)

    def _heard(self, text, err):
        if text:
            self.send_chat(text)
        else:
            self._status(err or "Voice input failed.", DOWN)

    def brief_chat(self):
        if self.last_signal is None:
            self._status("Press SCAN MARKET first.", DOWN)
            return
        self.send_chat("Give me a short spoken brief of the current market conditions "
                       "and whether I should act now.", show_as="Market brief")

    def send_chat(self, text=None, show_as=None):
        q = (text if text is not None else self.ti_chat.text).strip()
        if not q or self.chat_busy:
            return
        self.ti_chat.text = ""
        self.chat_busy = True
        self._chat_add("You", show_as or q)
        self._status("AI is thinking...", ACCENT)
        sig, cfg, pref = self.last_signal, dict(self.cfg), self.sp_lang.text

        def worker():
            reply, src = qe.ask_assistant(q, sig, cfg, pref)
            Clock.schedule_once(lambda dt: self._chat_reply(reply, src))

        threading.Thread(target=worker, daemon=True).start()

    def _chat_reply(self, reply, src):
        self.chat_busy = False
        self._chat_add("AI", reply)
        self._status(f"AI: {src}", MUTED if src == "gemini" else AMBER)
        if self.voice_on:
            lang = qe.detect_lang(reply)

            def talk():
                ok = self.voice.speak(reply, lang)
                if not ok:
                    Clock.schedule_once(lambda dt: self._status(
                        "Voice playback failed (gTTS needs internet).", DOWN))

            threading.Thread(target=talk, daemon=True).start()

    # ------------------------------------------------------------- helpers
    def _status(self, text, color=MUTED):
        self.lb_status.text = text
        self.lb_status.color = hexc(color)

    def _cfg_path(self):
        return os.path.join(self.user_data_dir, "live_config.json")

    def _refresh_mode(self):
        testnet = self.cfg.get("testnet", True)
        self.btn_mode.text = "TESTNET" if testnet else "LIVE"
        self.btn_mode.background_color = hexc(AMBER if testnet else DOWN)
        self.btn_mode.color = (0, 0, 0, 1) if testnet else (1, 1, 1, 1)

    def toast(self, title, body, ok=True, secs=8):
        content = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        content.add_widget(make_label(body, 14))
        pop = Popup(title=title, content=content, size_hint=(0.92, None), height=dp(280),
                    title_color=hexc(UP if ok else DOWN), background_color=hexc(PANEL))
        pop.open()
        Clock.schedule_once(lambda dt: pop.dismiss(), secs)

    # --------------------------------------------------------------- scan
    def scan(self):
        if self.busy:
            return
        self.busy = True
        self.btn_scan.disabled = True
        self._status("Scanning market...", ACCENT)
        symbol, tf = self.sp_symbol.text, self.sp_tf.text
        threading.Thread(target=self._scan_worker, args=(symbol, tf), daemon=True).start()

    def _scan_worker(self, symbol, tf):
        sig = qe.analyze(symbol, tf)
        Clock.schedule_once(lambda dt, s=sig: self._apply(s))

    def _apply(self, sig):
        self.busy = False
        self.btn_scan.disabled = False
        if sig.error:
            self._status("Network/data error - check connection and retry.", DOWN)
            self.lb_reasons.text = sig.error
            return
        self.last_signal = sig
        self._set_price(sig.price)

        col = UP if sig.direction == "BUY" else DOWN if sig.direction == "SELL" else NEUTRAL
        active = sig.direction != "HOLD"
        self.dot.set_color(col)
        self.lb_dir.text = sig.label
        self.lb_dir.font_size = sp(28 if active else 17)
        self.lb_dir.color = hexc(col if active else AMBER)
        self.lb_sub.text = f"{sig.symbol}  |  {sig.timeframe}"

        self.t_tp.set(f"{sig.tp:,.2f}" if active else "--", UP if active else TEXT)
        self.t_sl.set(f"{sig.sl:,.2f}" if active else "--", DOWN if active else TEXT)
        self.t_pat.set(sig.pattern)
        self.t_conf.set(f"{sig.confidence:.0f}%", col if active else AMBER)
        self.t_rsi.set(f"{sig.rsi:.1f}")
        self.t_rr.set(f"1 : {sig.rr:.0f}" if active else "--")
        self.bar.value = sig.confidence
        self.lb_reasons.text = "\n".join("- " + r for r in sig.reasons)
        self.btn_exec.background_color = hexc(col if active else NEUTRAL)
        self._status("Scan complete.", MUTED)

    # ---------------------------------------------------------- live price
    def poll_price(self, _dt):
        if self.busy or self.price_busy:
            return
        self.price_busy = True
        threading.Thread(target=self._price_worker, args=(self.sp_symbol.text,),
                         daemon=True).start()

    def _price_worker(self, symbol):
        try:
            p = qe.fetch_price(symbol)
        except Exception:  # noqa: BLE001
            p = None
        Clock.schedule_once(lambda dt, v=p: self._price_done(v))

    def _price_done(self, p):
        self.price_busy = False
        if p is not None:
            self._set_price(p)

    def _set_price(self, p):
        col = TEXT
        if self.prev_price is not None:
            col = UP if p > self.prev_price else DOWN if p < self.prev_price else TEXT
        self.prev_price = p
        self.lb_price.text = f"{p:,.2f}"
        self.lb_price.color = hexc(col)

    # ------------------------------------------------------------- execute
    def confirm_execute(self):
        sig = self.last_signal
        if sig is None or sig.error or sig.direction == "HOLD":
            self._status(f"{qe.WAIT_LABEL}: no trade allowed. Need confidence >= "
                         f"{qe.MIN_CONFIDENCE:.0f}%.", AMBER)
            return
        testnet = self.cfg.get("testnet", True)
        mode = "TESTNET / PAPER" if testnet else "LIVE - REAL MONEY"
        body = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
        body.add_widget(make_label(
            f"{sig.direction} {sig.symbol}  (confidence {sig.confidence:.0f}%)\n"
            f"Entry ~{sig.price:,.2f}\nSL {sig.sl:,.2f}   TP {sig.tp:,.2f}\nMode: {mode}", 15,
            DOWN if not testnet else TEXT))
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(10))
        pop = Popup(title="Confirm order", content=body, size_hint=(0.92, 0.55),
                    auto_dismiss=False, background_color=hexc(PANEL))
        b_no = flat_button("CANCEL", NEUTRAL)
        b_yes = flat_button("CONFIRM", UP if sig.direction == "BUY" else DOWN)
        b_no.bind(on_release=lambda *_: pop.dismiss())
        b_yes.bind(on_release=lambda *_: (pop.dismiss(), self._execute(sig)))
        row.add_widget(b_no)
        row.add_widget(b_yes)
        body.add_widget(row)
        pop.open()

    def _execute(self, sig):
        self._status("Sending order...", ACCENT)
        self.btn_exec.disabled = True
        cfg = dict(self.cfg)

        def worker():
            res = qe.execute_trade(sig, cfg)
            Clock.schedule_once(lambda dt, r=res: self._exec_done(r))

        threading.Thread(target=worker, daemon=True).start()

    def _exec_done(self, res):
        self.btn_exec.disabled = False
        self._status(res["msg"].split("\n")[0], UP if res["ok"] else DOWN)
        title = f"{res['mode']} ORDER PLACED" if res["ok"] else "ORDER NOT PLACED"
        self.toast(title, res["msg"], ok=res["ok"], secs=12 if res["ok"] else 8)

    # ------------------------------------------------------------ settings
    def open_settings(self):
        cfg = dict(self.cfg)
        tmp = {"testnet": [cfg["testnet_api_key"], cfg["testnet_api_secret"]],
               "live": [cfg["live_api_key"], cfg["live_api_secret"]]}
        state = {"mode": "testnet" if cfg["testnet"] else "live"}

        body = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(8))
        sv = ScrollView(do_scroll_x=False)
        form = BoxLayout(orientation="vertical", spacing=dp(8), size_hint_y=None)
        form.bind(minimum_height=form.setter("height"))

        def field(hint, password=False, value=""):
            return TextInput(hint_text=hint, text=value, password=password, multiline=False,
                             size_hint_y=None, height=dp(44), font_size=sp(13),
                             write_tab=False, background_color=hexc(BG),
                             foreground_color=hexc(TEXT), hint_text_color=hexc(MUTED))

        tg = ToggleButton(size_hint_y=None, height=dp(46), bold=True, background_normal="",
                          background_down="", color=(0, 0, 0, 1),
                          state="down" if cfg["testnet"] else "normal")
        hint = make_label("", 12, MUTED, size_hint_y=None, height=dp(48))
        ti_key = field("Binance API key", value=tmp[state["mode"]][0])
        ti_sec = field("Binance API secret", True, tmp[state["mode"]][1])
        ti_gem = field("Google AI Studio (Gemini) API key", True, cfg["gemini_api_key"])

        def paint():
            t = state["mode"] == "testnet"
            tg.text = "TESTNET MODE: ON (safe)" if t else "TESTNET MODE: OFF (LIVE)"
            tg.background_color = hexc(AMBER if t else DOWN)
            tg.color = (0, 0, 0, 1) if t else (1, 1, 1, 1)
            hint.text = ("Keys from testnet.binance.vision. Leave empty = local paper trades."
                         if t else "LIVE = real money. Use a key WITHOUT withdrawal rights.")
            hint.color = hexc(MUTED if t else DOWN)

        def toggled(_w, st):
            tmp[state["mode"]] = [ti_key.text.strip(), ti_sec.text.strip()]
            state["mode"] = "testnet" if st == "down" else "live"
            ti_key.text, ti_sec.text = tmp[state["mode"]]
            paint()

        tg.bind(state=toggled)
        paint()
        for w in (tg, hint, ti_key, ti_sec, ti_gem):
            form.add_widget(w)
        sv.add_widget(form)
        body.add_widget(sv)

        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(10))
        pop = Popup(title="Settings", content=body, size_hint=(0.95, 0.85),
                    auto_dismiss=False, background_color=hexc(PANEL))

        def save(*_):
            tmp[state["mode"]] = [ti_key.text.strip(), ti_sec.text.strip()]
            new = dict(self.cfg)
            new.update({"testnet": state["mode"] == "testnet",
                        "testnet_api_key": tmp["testnet"][0],
                        "testnet_api_secret": tmp["testnet"][1],
                        "live_api_key": tmp["live"][0],
                        "live_api_secret": tmp["live"][1],
                        "gemini_api_key": ti_gem.text.strip()})
            try:
                qe.save_config(self._cfg_path(), new)
                self.cfg = new
                self._refresh_mode()
                self._status("Settings saved.", UP)
            except OSError as exc:
                self._status(f"Could not save settings: {exc}", DOWN)
            pop.dismiss()

        b_c = flat_button("CANCEL", NEUTRAL)
        b_s = flat_button("SAVE", UP)
        b_c.bind(on_release=lambda *_: pop.dismiss())
        b_s.bind(on_release=save)
        row.add_widget(b_c)
        row.add_widget(b_s)
        body.add_widget(row)
        pop.open()


if __name__ == "__main__":
    TradingApp().run()
