"""
main.py - Kivy front-end, TradingView Pro dark theme (#0B0E14).
Run locally:  python main.py
"""
import os
import threading

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
from kivy.uix.widget import Widget
from kivy.utils import get_color_from_hex as hexc
from kivy.utils import platform

import quant_engine as qe

BG = "#0B0E14"
PANEL = "#131722"
BORDER = "#2A2E39"
TEXT = "#D1D4DC"
MUTED = "#787B86"
UP = "#26A69A"
DOWN = "#EF5350"
NEUTRAL = "#434651"
ACCENT = "#2962FF"

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d"]


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


class TradingApp(App):
    title = "Simons Quant Terminal"

    def build(self):
        if platform not in ("android", "ios"):
            Window.size = (420, 820)
        Window.clearcolor = hexc(BG)

        self.busy = False
        self.price_busy = False
        self.last_signal = None
        self.prev_price = None

        root = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))

        # ---- header: symbol + timeframe
        head = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        self.sp_symbol = Spinner(text=SYMBOLS[0], values=SYMBOLS, font_size=sp(15),
                                 background_normal="", background_color=hexc(PANEL),
                                 color=hexc(TEXT))
        self.sp_tf = Spinner(text="15m", values=TIMEFRAMES, size_hint_x=0.4, font_size=sp(15),
                             background_normal="", background_color=hexc(PANEL),
                             color=hexc(TEXT))
        head.add_widget(self.sp_symbol)
        head.add_widget(self.sp_tf)
        root.add_widget(head)

        # ---- scrollable content
        sv = ScrollView(do_scroll_x=False, bar_width=dp(2))
        content = BoxLayout(orientation="vertical", spacing=dp(8), size_hint_y=None)
        content.bind(minimum_height=content.setter("height"))
        sv.add_widget(content)
        root.add_widget(sv)

        # live price
        pp = Panel(orientation="vertical", size_hint_y=None, height=dp(92), padding=dp(10))
        pp.add_widget(make_label("LIVE PRICE", 11, MUTED))
        self.lb_price = make_label("--", 34, TEXT, bold=True)
        pp.add_widget(self.lb_price)
        content.add_widget(pp)

        # signal card
        self.card = Panel(orientation="horizontal", size_hint_y=None, height=dp(110),
                          padding=dp(14), spacing=dp(14))
        self.dot = Dot(size_hint_x=None, width=dp(64))
        self.card.add_widget(self.dot)
        right = BoxLayout(orientation="vertical")
        self.lb_dir = make_label("NO SIGNAL", 30, MUTED, bold=True, halign="left")
        self.lb_sub = make_label("Press SCAN MARKET", 13, MUTED, halign="left")
        right.add_widget(self.lb_dir)
        right.add_widget(self.lb_sub)
        self.card.add_widget(right)
        content.add_widget(self.card)

        # stats grid
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

        # confidence bar
        cp = Panel(orientation="vertical", size_hint_y=None, height=dp(50), padding=dp(10))
        self.bar = ProgressBar(max=100, value=0)
        cp.add_widget(self.bar)
        content.add_widget(cp)

        # reasons
        rp = Panel(orientation="vertical", size_hint_y=None, height=dp(130), padding=dp(10))
        rp.add_widget(make_label("SIGNAL LOGIC", 11, MUTED, halign="left", size_hint_y=None,
                                 height=dp(18)))
        self.lb_reasons = make_label("-", 13, TEXT, halign="left")
        self.lb_reasons.valign = "top"
        rp.add_widget(self.lb_reasons)
        content.add_widget(rp)

        # ---- status + buttons (fixed at bottom)
        self.lb_status = make_label("Ready.", 12, MUTED, size_hint_y=None, height=dp(24))
        root.add_widget(self.lb_status)

        btns = BoxLayout(size_hint_y=None, height=dp(56), spacing=dp(10))
        self.btn_scan = Button(text="SCAN MARKET", bold=True, font_size=sp(15),
                               background_normal="", background_color=hexc(ACCENT),
                               color=(1, 1, 1, 1))
        self.btn_exec = Button(text="EXECUTE LIVE TRADE", bold=True, font_size=sp(15),
                               background_normal="", background_color=hexc(NEUTRAL),
                               color=(1, 1, 1, 1))
        self.btn_scan.bind(on_release=lambda *_: self.scan())
        self.btn_exec.bind(on_release=lambda *_: self.confirm_execute())
        btns.add_widget(self.btn_scan)
        btns.add_widget(self.btn_exec)
        root.add_widget(btns)

        Clock.schedule_once(lambda dt: self.scan(), 0.5)
        Clock.schedule_interval(self.poll_price, 5)
        return root

    # ------------------------------------------------------------ helpers
    def _status(self, text, color=MUTED):
        self.lb_status.text = text
        self.lb_status.color = hexc(color)

    def _cfg_path(self):
        return os.path.join(self.user_data_dir, "live_config.json")

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

        if sig.direction == "BUY":
            col, txt = UP, "BUY / UP"
        elif sig.direction == "SELL":
            col, txt = DOWN, "SELL / DOWN"
        else:
            col, txt = NEUTRAL, "HOLD"
        self.dot.set_color(col)
        self.lb_dir.text = txt
        self.lb_dir.color = hexc(col if sig.direction != "HOLD" else MUTED)
        self.lb_sub.text = f"{sig.symbol}  |  {sig.timeframe}"
        self.card.set_bg(PANEL)

        active = sig.direction != "HOLD"
        self.t_tp.set(f"{sig.tp:,.2f}" if active else "--", UP if active else TEXT)
        self.t_sl.set(f"{sig.sl:,.2f}" if active else "--", DOWN if active else TEXT)
        self.t_pat.set(sig.pattern)
        self.t_conf.set(f"{sig.confidence:.0f}%", col if active else TEXT)
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
        symbol = self.sp_symbol.text
        threading.Thread(target=self._price_worker, args=(symbol,), daemon=True).start()

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
        if sig is None or sig.direction == "HOLD" or sig.error:
            self._status("No actionable signal. Scan first.", DOWN)
            return
        cfg = qe.load_config(self._cfg_path())
        mode = "LIVE (real money)" if cfg.get("live_enabled") else "PAPER (simulated)"
        body = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
        body.add_widget(make_label(
            f"{sig.direction} {sig.symbol}\nEntry {sig.price:,.2f}\n"
            f"SL {sig.sl:,.2f}   TP {sig.tp:,.2f}\nMode: {mode}", 15))
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(10))
        pop = Popup(title="Confirm trade", content=body, size_hint=(0.9, 0.5),
                    auto_dismiss=False, background_color=hexc(PANEL))
        b_no = Button(text="CANCEL", background_normal="", background_color=hexc(NEUTRAL))
        b_yes = Button(text="CONFIRM", background_normal="",
                       background_color=hexc(UP if sig.direction == "BUY" else DOWN))
        b_no.bind(on_release=lambda *_: pop.dismiss())
        b_yes.bind(on_release=lambda *_: (pop.dismiss(), self._execute(sig, cfg)))
        row.add_widget(b_no)
        row.add_widget(b_yes)
        body.add_widget(row)
        pop.open()

    def _execute(self, sig, cfg):
        self._status("Sending order...", ACCENT)
        self.btn_exec.disabled = True

        def worker():
            res = qe.execute_trade(sig, cfg)
            Clock.schedule_once(lambda dt, r=res: self._exec_done(r))

        threading.Thread(target=worker, daemon=True).start()

    def _exec_done(self, res):
        self.btn_exec.disabled = False
        self._status(res["msg"], UP if res["ok"] else DOWN)


if __name__ == "__main__":
    TradingApp().run()
