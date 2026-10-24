"""
main.py - TradeMatrix: Kivy app wired to RiskManager (TP/SL) and VoiceAssistant (TTS).

Project folder (all three files MUST sit side by side):

    TradeMatrix/
        main.py
        risk_manager.py
        voice_assistant.py

Run on desktop:   python main.py
Android build:    add `pyjnius` to `requirements` in buildozer.spec

The SIMULATE BUY button runs an offline simulation (no exchange calls): it fills
a BUY at a random price, then a random-walk "live price" updates on a Clock
interval until the Take-Profit (+2.0%) or Stop-Loss (-1.5%) level is hit.
Every event is shown on screen and announced by voice. Replace the simulated
fill and price feed with your real order and price source when you go live.
"""
import logging
import random

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.utils import get_color_from_hex as hexc
from kivy.utils import platform

from risk_manager import RiskManager
from voice_assistant import VoiceAssistant

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tradematrix")

# ---- theme -----------------------------------------------------------------
BG = "#0B0E14"
TEXT = "#D1D4DC"
MUTED = "#787B86"
UP = "#26A69A"
DOWN = "#EF5350"
ACCENT = "#2962FF"

# ---- simulation settings ---------------------------------------------------
SYMBOL = "BTC/USDT"
START_PRICE = 64000.0
TICK_SECONDS = 0.5          # live price check interval
VOLATILITY = 0.0035         # std-dev of each simulated tick (0.35%)
TARGET_PROFIT_PCT = 2.0
STOP_LOSS_PCT = 1.5
MAX_BAD_TICKS = 5           # abort monitoring after this many invalid prices in a row


def make_label(text="", size=14, color=TEXT, bold=False, halign="center", **kwargs):
    """Label whose text wraps/aligns to its own size."""
    label = Label(text=text, font_size=sp(size), color=hexc(color), bold=bold,
                  halign=halign, valign="middle", **kwargs)
    label.bind(size=lambda widget, size_: setattr(widget, "text_size", size_))
    return label


class TradeMatrixApp(App):
    title = "TradeMatrix"

    # ------------------------------------------------------------------ build
    def build(self):
        if platform not in ("android", "ios"):
            Window.size = (420, 720)
        Window.clearcolor = hexc(BG)

        # --- core services -------------------------------------------------
        self.voice = VoiceAssistant()
        self.risk = RiskManager(target_profit_pct=TARGET_PROFIT_PCT,
                                stop_loss_pct=STOP_LOSS_PCT)

        # --- trade state ---------------------------------------------------
        self.symbol = SYMBOL
        self.sim_price = START_PRICE
        self.entry_price = None          # None = no open position
        self._tick_event = None
        self._bad_ticks = 0

        # --- UI ------------------------------------------------------------
        root = BoxLayout(orientation="vertical", padding=dp(16), spacing=dp(12))

        root.add_widget(make_label("TRADEMATRIX", 24, ACCENT, bold=True,
                                   size_hint_y=None, height=dp(40)))
        root.add_widget(make_label(self.symbol, 14, MUTED,
                                   size_hint_y=None, height=dp(22)))

        self.lb_price = make_label("--", 40, TEXT, bold=True,
                                   size_hint_y=None, height=dp(70))
        root.add_widget(self.lb_price)

        self.lb_pnl = make_label("P&L: --", 16, MUTED, size_hint_y=None, height=dp(30))
        root.add_widget(self.lb_pnl)

        self.lb_levels = make_label(
            f"Take-profit +{TARGET_PROFIT_PCT:g}%   |   Stop-loss -{STOP_LOSS_PCT:g}%",
            13, MUTED, size_hint_y=None, height=dp(44))
        root.add_widget(self.lb_levels)

        self.lb_status = make_label("Ready. Press SIMULATE BUY.", 16, TEXT, bold=True)
        root.add_widget(self.lb_status)

        self.btn_sim = Button(text="SIMULATE BUY", bold=True, font_size=sp(17),
                              size_hint_y=None, height=dp(60), background_normal="",
                              background_color=hexc(ACCENT), color=(1, 1, 1, 1))
        self.btn_sim.bind(on_release=self.start_simulation)
        root.add_widget(self.btn_sim)

        # greet by voice once the window is up (non-blocking, queued on the TTS thread)
        Clock.schedule_once(
            lambda _dt: self.voice.announce_status("TradeMatrix is ready"), 1.0)
        return root

    # ------------------------------------------------------------- helpers
    def set_status(self, text, color=TEXT):
        self.lb_status.text = text
        self.lb_status.color = hexc(color)

    def _cancel_ticks(self):
        if self._tick_event is not None:
            self._tick_event.cancel()
            self._tick_event = None

    def _reset_button(self, text="SIMULATE BUY"):
        self.btn_sim.disabled = False
        self.btn_sim.background_color = hexc(ACCENT)
        self.btn_sim.text = text

    # --------------------------------------------------------- buy simulation
    def start_simulation(self, *_):
        """BUY order 'filled': record entry, announce it, start the live price loop."""
        if self.entry_price is not None:
            return                                   # a trade is already running

        self.sim_price = START_PRICE * (1 + random.uniform(-0.02, 0.02))
        fill_price = round(self.sim_price, 2)

        try:
            levels = self.risk.calculate_levels(fill_price)
        except ValueError as exc:
            self.set_status(f"Risk error: {exc}", DOWN)
            self.voice.announce_status("Risk calculation error. Trade not started")
            log.error("calculate_levels failed: %s", exc)
            return

        self.entry_price = fill_price
        self._bad_ticks = 0
        self.lb_price.text = f"{fill_price:,.2f}"
        self.lb_price.color = hexc(TEXT)
        self.lb_pnl.text = "P&L: +0.00%"
        self.lb_pnl.color = hexc(MUTED)
        self.lb_levels.text = (f"Entry {levels['entry_price']:,.2f}\n"
                               f"TP {levels['target_price']:,.2f}   |   "
                               f"SL {levels['stop_price']:,.2f}")
        self.set_status(f"BUY filled @ {fill_price:,.2f}. Monitoring TP/SL...", UP)
        self.btn_sim.disabled = True
        self.btn_sim.background_color = hexc(MUTED)
        self.btn_sim.text = "TRADE RUNNING..."

        self.voice.announce_buy(self.symbol, fill_price)
        self.voice.announce_status("Monitoring take profit and stop loss")

        self._cancel_ticks()
        self._tick_event = Clock.schedule_interval(self.check_live_price, TICK_SECONDS)

    # ------------------------------------------------------- live price loop
    def get_live_price(self):
        """Simulated price feed (random walk). Replace with your real price source."""
        self.sim_price = max(0.01, self.sim_price * (1 + random.gauss(0.0, VOLATILITY)))
        return self.sim_price

    def check_live_price(self, _dt):
        """Runs every TICK_SECONDS: fetch the price, evaluate TP/SL, update the UI."""
        if self.entry_price is None:
            self._tick_event = None
            return False                             # False = stop this schedule

        try:
            price = self.get_live_price()
            result = self.risk.evaluate_trade(self.entry_price, price)
        except Exception as exc:  # noqa: BLE001  - a bad tick must never kill the loop
            self._bad_ticks += 1
            log.warning("Price check failed (%d/%d): %s",
                        self._bad_ticks, MAX_BAD_TICKS, exc)
            if self._bad_ticks >= MAX_BAD_TICKS:
                self.abort_monitoring("Price feed error")
                return False
            return True
        self._bad_ticks = 0

        pnl = result["pnl_pct"]
        colour = UP if pnl > 0 else DOWN if pnl < 0 else TEXT
        self.lb_price.text = f"{price:,.2f}"
        self.lb_price.color = hexc(colour)
        self.lb_pnl.text = f"P&L: {pnl:+.2f}%"
        self.lb_pnl.color = hexc(colour if colour != TEXT else MUTED)

        if result["status"] == "TRIGGER_EXIT":
            self.handle_exit(result)
            return False                             # stop ticking
        return True

    def handle_exit(self, result):
        """TP or SL hit: close the position, update the UI, speak the alert."""
        self.entry_price = None                      # first, so nothing can re-trigger
        self._tick_event = None                      # returning False cancels the schedule

        pnl = result["pnl_pct"]
        if result["reason"] == "TAKE_PROFIT":
            self.set_status(f"TAKE PROFIT hit ({pnl:+.2f}%). SELL executed.", UP)
            self.voice.announce_take_profit(self.symbol, pnl)
        else:
            self.set_status(f"STOP LOSS hit ({pnl:+.2f}%). SELL executed.", DOWN)
            self.voice.announce_stop_loss(self.symbol, abs(pnl))

        log.info("Exit: %s", result["message"])
        self._reset_button("SIMULATE NEW BUY")

    def abort_monitoring(self, reason):
        """Stop monitoring after repeated price errors and tell the user."""
        self.entry_price = None
        self._tick_event = None
        self.set_status(f"{reason}. Monitoring stopped - check your position manually!", DOWN)
        self.voice.announce_status(f"{reason}. Monitoring stopped")
        self._reset_button("SIMULATE NEW BUY")

    # ----------------------------------------------------------- lifecycle
    def on_stop(self):
        """App is closing: stop the price timer and shut down the voice thread."""
        try:
            self._cancel_ticks()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.voice.shutdown()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    TradeMatrixApp().run()
