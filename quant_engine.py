"""
quant_engine.py - Rules-based quantitative engine (Simons-style mean reversion
+ trend + candlestick confirmation) with ATR-based TP/SL at a fixed 1:3 R:R.

Data feed: ccxt (if installed) -> Binance public REST fallback via requests.
Indicators are implemented with pandas/numpy so the engine also runs on Android.
"""
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd
import requests

try:  # optional: not available / not needed on Android
    import ccxt  # type: ignore
except Exception:  # noqa: BLE001
    ccxt = None

log = logging.getLogger("quant_engine")

HTTP_TIMEOUT = 10  # seconds
BINANCE_HOSTS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api.binance.us",
]
RISK_REWARD = 3.0        # TP distance = 3 x SL distance
ATR_SL_MULT = 1.5        # SL distance = 1.5 x ATR(14)
MAX_RISK_PCT = 0.01      # never risk more than 1% of balance per trade
MIN_CANDLES = 60
BUY_THRESHOLD = 3.0
MAX_SCORE = 6.5          # 1 (trend) + 2 (RSI) + 1.5 (BB) + 2 (pattern)


class DataFeedError(Exception):
    """Raised when market data cannot be fetched."""


@dataclass
class Signal:
    symbol: str = ""
    timeframe: str = ""
    direction: str = "HOLD"          # BUY | SELL | HOLD
    price: float = 0.0
    tp: float = 0.0
    sl: float = 0.0
    rr: float = 0.0
    pattern: str = "None"
    confidence: float = 0.0          # 0-100
    score: float = 0.0
    rsi: float = 0.0
    ema20: float = 0.0
    ema50: float = 0.0
    bb_upper: float = 0.0
    bb_lower: float = 0.0
    atr: float = 0.0
    reasons: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    error: Optional[str] = None


# ----------------------------------------------------------------- data feed
def _to_binance_symbol(symbol: str) -> str:
    return symbol.replace("/", "").upper()


def _rest_get(path: str, params: dict):
    last_err = None
    for host in BINANCE_HOSTS:
        try:
            r = requests.get(host + path, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            last_err = f"{host} HTTP {r.status_code}"
        except requests.RequestException as exc:
            last_err = f"{host}: {exc.__class__.__name__}"
    raise DataFeedError(f"All data hosts failed ({last_err})")


def fetch_ohlcv(symbol: str = "BTC/USDT", timeframe: str = "15m",
                limit: int = 200) -> pd.DataFrame:
    """Return DataFrame[time, open, high, low, close, volume]; last row = forming candle."""
    rows = None
    if ccxt is not None:
        try:
            ex = ccxt.binance({"enableRateLimit": True, "timeout": HTTP_TIMEOUT * 1000})
            rows = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception as exc:  # noqa: BLE001
            log.warning("ccxt feed failed, using REST fallback: %s", exc)
            rows = None
    if rows is None:
        raw = _rest_get("/api/v3/klines", {
            "symbol": _to_binance_symbol(symbol),
            "interval": timeframe, "limit": limit})
        rows = [[k[0], k[1], k[2], k[3], k[4], k[5]] for k in raw]
    if not rows:
        raise DataFeedError("Empty candle data")
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df = df.astype(float)
    return df


def fetch_price(symbol: str = "BTC/USDT") -> float:
    """Latest traded price."""
    if ccxt is not None:
        try:
            ex = ccxt.binance({"enableRateLimit": True, "timeout": HTTP_TIMEOUT * 1000})
            return float(ex.fetch_ticker(symbol)["last"])
        except Exception as exc:  # noqa: BLE001
            log.warning("ccxt ticker failed, using REST fallback: %s", exc)
    data = _rest_get("/api/v3/ticker/price", {"symbol": _to_binance_symbol(symbol)})
    return float(data["price"])


# --------------------------------------------------------------- indicators
def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(avg_loss != 0, 100.0).where(avg_loss.notna())


def ema(close: pd.Series, n: int) -> pd.Series:
    return close.ewm(span=n, adjust=False, min_periods=n).mean()


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    return mid + k * sd, mid, mid - k * sd


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["rsi"] = rsi(df["close"], 14)
    df["ema20"] = ema(df["close"], 20)
    df["ema50"] = ema(df["close"], 50)
    df["bb_up"], df["bb_mid"], df["bb_low"] = bollinger(df["close"], 20, 2.0)
    df["atr"] = atr(df, 14)
    return df


# ------------------------------------------------------- candlestick patterns
def detect_pattern(df: pd.DataFrame):
    """Return (name, bias) for the last closed candle. bias: +1 bullish, -1 bearish, 0 none."""
    if len(df) < 3:
        return "None", 0
    c1, c2, c3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]

    def body(c): return abs(c["close"] - c["open"])
    def rng(c): return max(c["high"] - c["low"], 1e-12)
    def upper(c): return c["high"] - max(c["open"], c["close"])
    def lower(c): return min(c["open"], c["close"]) - c["low"]
    def green(c): return c["close"] > c["open"]
    def red(c): return c["close"] < c["open"]

    # Engulfing
    if red(c2) and green(c3) and c3["open"] <= c2["close"] and c3["close"] >= c2["open"] \
            and body(c3) > body(c2):
        return "Bullish Engulfing", 1
    if green(c2) and red(c3) and c3["open"] >= c2["close"] and c3["close"] <= c2["open"] \
            and body(c3) > body(c2):
        return "Bearish Engulfing", -1

    # Morning / Evening Star
    small2 = body(c2) <= 0.35 * rng(c2)
    if red(c1) and body(c1) >= 0.5 * rng(c1) and small2 and green(c3) \
            and c3["close"] > (c1["open"] + c1["close"]) / 2:
        return "Morning Star", 1
    if green(c1) and body(c1) >= 0.5 * rng(c1) and small2 and red(c3) \
            and c3["close"] < (c1["open"] + c1["close"]) / 2:
        return "Evening Star", -1

    # Hammer / Shooting Star
    b = max(body(c3), 1e-12)
    if lower(c3) >= 2 * b and upper(c3) <= 0.5 * b and body(c3) <= 0.4 * rng(c3):
        return "Hammer", 1
    if upper(c3) >= 2 * b and lower(c3) <= 0.5 * b and body(c3) <= 0.4 * rng(c3):
        return "Shooting Star", -1

    return "None", 0


# ------------------------------------------------------------ decision matrix
def analyze(symbol: str = "BTC/USDT", timeframe: str = "15m") -> Signal:
    """Full scan. Never raises: errors are returned in Signal.error."""
    sig = Signal(symbol=symbol, timeframe=timeframe)
    try:
        raw = fetch_ohlcv(symbol, timeframe, 200)
        live_price = float(raw["close"].iloc[-1])
        try:
            live_price = fetch_price(symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("live price fallback to last close: %s", exc)

        closed = raw.iloc[:-1]                      # drop still-forming candle
        if len(closed) < MIN_CANDLES:
            raise DataFeedError("Not enough candles for indicators")
        df = add_indicators(closed).dropna().reset_index(drop=True)
        if len(df) < 3:
            raise DataFeedError("Not enough valid indicator rows")

        last = df.iloc[-1]
        name, bias = detect_pattern(df)

        score, reasons = 0.0, []
        # 1) Trend
        if last["ema20"] > last["ema50"]:
            score += 1.0; reasons.append("EMA20 > EMA50 (uptrend)")
        else:
            score -= 1.0; reasons.append("EMA20 < EMA50 (downtrend)")
        # 2) RSI mean reversion
        r = float(last["rsi"])
        if r < 30:
            score += 2.0; reasons.append(f"RSI {r:.1f} oversold")
        elif r < 45:
            score += 0.5; reasons.append(f"RSI {r:.1f} leaning oversold")
        elif r > 70:
            score -= 2.0; reasons.append(f"RSI {r:.1f} overbought")
        elif r > 55:
            score -= 0.5; reasons.append(f"RSI {r:.1f} leaning overbought")
        else:
            reasons.append(f"RSI {r:.1f} neutral")
        # 3) Bollinger Bands
        if last["close"] < last["bb_low"]:
            score += 1.5; reasons.append("Close below lower Bollinger Band")
        elif last["close"] > last["bb_up"]:
            score -= 1.5; reasons.append("Close above upper Bollinger Band")
        # 4) Candlestick pattern
        if bias != 0:
            score += 2.0 * bias; reasons.append(f"Pattern: {name}")

        a = float(last["atr"])
        sl_dist = ATR_SL_MULT * a
        if score >= BUY_THRESHOLD:
            direction = "BUY"
            sl, tp = live_price - sl_dist, live_price + RISK_REWARD * sl_dist
        elif score <= -BUY_THRESHOLD:
            direction = "SELL"
            sl, tp = live_price + sl_dist, live_price - RISK_REWARD * sl_dist
        else:
            direction, sl, tp = "HOLD", 0.0, 0.0
            reasons.append("Score below threshold - no edge")

        if direction != "HOLD" and (sl_dist <= 0 or sl <= 0 or tp <= 0):
            direction, sl, tp = "HOLD", 0.0, 0.0
            reasons.append("Invalid TP/SL - signal discarded")

        sig.direction = direction
        sig.price = live_price
        sig.tp, sig.sl = tp, sl
        sig.rr = RISK_REWARD if direction != "HOLD" else 0.0
        sig.pattern = name
        sig.score = score
        sig.confidence = round(min(99.0, abs(score) / MAX_SCORE * 100.0), 1)
        sig.rsi = r
        sig.ema20, sig.ema50 = float(last["ema20"]), float(last["ema50"])
        sig.bb_upper, sig.bb_lower = float(last["bb_up"]), float(last["bb_low"])
        sig.atr = a
        sig.reasons = reasons
    except Exception as exc:  # noqa: BLE001
        log.exception("analyze failed")
        sig.error = f"{exc.__class__.__name__}: {exc}"
        sig.reasons = [sig.error]
    sig.timestamp = time.time()
    return sig


# ------------------------------------------------------------------- trading
DEFAULT_CONFIG = {
    "live_enabled": False,     # must be set True by you to send real orders
    "api_key": "",
    "api_secret": "",
    "paper_balance": 10000.0,
    "risk_pct": 0.01,
}


def load_config(path: str) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                cfg.update(json.load(fh))
    except Exception as exc:  # noqa: BLE001
        log.warning("config load failed: %s", exc)
    return cfg


def position_size(balance: float, entry: float, sl: float,
                  risk_pct: float = MAX_RISK_PCT) -> float:
    """Units to trade so that hitting SL loses at most risk_pct of balance."""
    risk_pct = min(max(risk_pct, 0.0), MAX_RISK_PCT)
    dist = abs(entry - sl)
    if dist <= 0 or entry <= 0:
        return 0.0
    qty = (balance * risk_pct) / dist
    return min(qty, balance / entry)       # no leverage


def execute_trade(sig: Signal, cfg: dict) -> dict:
    """Paper trade by default. Live only if cfg['live_enabled'] and keys exist.
    Returns {"ok": bool, "mode": "PAPER"|"LIVE", "msg": str}."""
    if sig.error or sig.direction not in ("BUY", "SELL") or sig.sl <= 0 or sig.tp <= 0:
        return {"ok": False, "mode": "-", "msg": "No valid signal to execute."}

    if not cfg.get("live_enabled"):
        qty = position_size(float(cfg["paper_balance"]), sig.price, sig.sl, cfg["risk_pct"])
        return {"ok": True, "mode": "PAPER",
                "msg": (f"PAPER {sig.direction} {qty:.6f} {sig.symbol} @ {sig.price:,.2f} | "
                        f"SL {sig.sl:,.2f} | TP {sig.tp:,.2f}")}

    if ccxt is None:
        return {"ok": False, "mode": "LIVE", "msg": "ccxt is not installed in this build."}
    if not cfg.get("api_key") or not cfg.get("api_secret"):
        return {"ok": False, "mode": "LIVE", "msg": "API key/secret missing in live_config.json."}
    if sig.direction == "SELL":
        return {"ok": False, "mode": "LIVE",
                "msg": "Spot account cannot open shorts. SELL signal not executed."}
    try:
        ex = ccxt.binance({"apiKey": cfg["api_key"], "secret": cfg["api_secret"],
                           "enableRateLimit": True, "timeout": HTTP_TIMEOUT * 1000})
        ex.load_markets()
        usdt = float(ex.fetch_balance().get("USDT", {}).get("free", 0.0))
        qty = position_size(usdt, sig.price, sig.sl, cfg["risk_pct"])
        qty = float(ex.amount_to_precision(sig.symbol, qty))
        if qty <= 0:
            return {"ok": False, "mode": "LIVE", "msg": "Calculated size is zero / balance too low."}
        order = ex.create_order(sig.symbol, "market", "buy", qty)
        return {"ok": True, "mode": "LIVE",
                "msg": (f"LIVE BUY {qty} {sig.symbol} filled (id {order.get('id')}). "
                        f"SET SL {sig.sl:,.2f} / TP {sig.tp:,.2f} on the exchange NOW.")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "mode": "LIVE", "msg": f"Order failed: {exc.__class__.__name__}: {exc}"}
