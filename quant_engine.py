"""
quant_engine.py - Rules-based quantitative engine + Binance execution + AI brief.

* Signals: EMA20/50 trend, RSI14 mean reversion, Bollinger Bands, candlestick
  patterns. BUY/SELL only when confidence >= MIN_CONFIDENCE (70%).
* Risk: ATR-based SL, TP at fixed 1:3 R:R, max 1% of equity risked per trade.
* Execution: Binance Spot REST (HMAC-signed, no ccxt needed) - Testnet or Live.
* Assistant: Google AI Studio (Gemini) with an offline English/Hindi fallback.
"""
import hashlib
import hmac
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from typing import List, Optional
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import requests

try:  # optional data feed on desktop; not needed on Android
    import ccxt  # type: ignore
except Exception:  # noqa: BLE001
    ccxt = None

log = logging.getLogger("quant_engine")

HTTP_TIMEOUT = 10
BINANCE_DATA_HOSTS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api.binance.us",
]
RISK_REWARD = 3.0
ATR_SL_MULT = 1.5
MAX_RISK_PCT = 0.01
MIN_CANDLES = 60
MIN_CONFIDENCE = 70.0          # BUY/SELL only at or above this
MAX_SCORE = 6.5                # 1 (trend) + 2 (RSI) + 1.5 (BB) + 2 (pattern)
WAIT_LABEL = "WAIT / NEUTRAL (Low Confidence)"


class DataFeedError(Exception):
    """Market data could not be fetched."""


class BinanceError(Exception):
    """Binance returned an error response."""


@dataclass
class Signal:
    symbol: str = ""
    timeframe: str = ""
    direction: str = "HOLD"          # BUY | SELL | HOLD
    label: str = "NO SIGNAL"
    lean: str = "NONE"               # raw bias before the confidence filter
    price: float = 0.0
    tp: float = 0.0
    sl: float = 0.0
    rr: float = 0.0
    pattern: str = "None"
    confidence: float = 0.0
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
    for host in BINANCE_DATA_HOSTS:
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
    """DataFrame[time, open, high, low, close, volume]; last row = forming candle."""
    rows = None
    if ccxt is not None:
        try:
            ex = ccxt.binance({"enableRateLimit": True, "timeout": HTTP_TIMEOUT * 1000})
            rows = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception as exc:  # noqa: BLE001
            log.warning("ccxt feed failed, using REST fallback: %s", exc)
            rows = None
    if rows is None:
        raw = _rest_get("/api/v3/klines", {"symbol": _to_binance_symbol(symbol),
                                           "interval": timeframe, "limit": limit})
        rows = [[k[0], k[1], k[2], k[3], k[4], k[5]] for k in raw]
    if not rows:
        raise DataFeedError("Empty candle data")
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    return df.astype(float)


def fetch_price(symbol: str = "BTC/USDT") -> float:
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
    """(name, bias) for the last closed candle. bias: +1 bullish, -1 bearish, 0 none."""
    if len(df) < 3:
        return "None", 0
    c1, c2, c3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]

    def body(c): return abs(c["close"] - c["open"])
    def rng(c): return max(c["high"] - c["low"], 1e-12)
    def upper(c): return c["high"] - max(c["open"], c["close"])
    def lower(c): return min(c["open"], c["close"]) - c["low"]
    def green(c): return c["close"] > c["open"]
    def red(c): return c["close"] < c["open"]

    if red(c2) and green(c3) and c3["open"] <= c2["close"] and c3["close"] >= c2["open"] \
            and body(c3) > body(c2):
        return "Bullish Engulfing", 1
    if green(c2) and red(c3) and c3["open"] >= c2["close"] and c3["close"] <= c2["open"] \
            and body(c3) > body(c2):
        return "Bearish Engulfing", -1

    small2 = body(c2) <= 0.35 * rng(c2)
    if red(c1) and body(c1) >= 0.5 * rng(c1) and small2 and green(c3) \
            and c3["close"] > (c1["open"] + c1["close"]) / 2:
        return "Morning Star", 1
    if green(c1) and body(c1) >= 0.5 * rng(c1) and small2 and red(c3) \
            and c3["close"] < (c1["open"] + c1["close"]) / 2:
        return "Evening Star", -1

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

        closed = raw.iloc[:-1]
        if len(closed) < MIN_CANDLES:
            raise DataFeedError("Not enough candles for indicators")
        df = add_indicators(closed).dropna().reset_index(drop=True)
        if len(df) < 3:
            raise DataFeedError("Not enough valid indicator rows")

        last = df.iloc[-1]
        name, bias = detect_pattern(df)

        score, reasons = 0.0, []
        if last["ema20"] > last["ema50"]:
            score += 1.0; reasons.append("EMA20 > EMA50 (uptrend)")
        else:
            score -= 1.0; reasons.append("EMA20 < EMA50 (downtrend)")

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

        if last["close"] < last["bb_low"]:
            score += 1.5; reasons.append("Close below lower Bollinger Band")
        elif last["close"] > last["bb_up"]:
            score -= 1.5; reasons.append("Close above upper Bollinger Band")

        if bias != 0:
            score += 2.0 * bias; reasons.append(f"Pattern: {name}")

        conf = round(min(99.0, abs(score) / MAX_SCORE * 100.0), 1)
        lean = "BUY" if score > 0 else "SELL" if score < 0 else "NONE"
        a = float(last["atr"])
        sl_dist = ATR_SL_MULT * a

        direction, sl, tp = "HOLD", 0.0, 0.0
        if lean != "NONE" and conf >= MIN_CONFIDENCE:
            direction = lean
            if lean == "BUY":
                sl, tp = live_price - sl_dist, live_price + RISK_REWARD * sl_dist
            else:
                sl, tp = live_price + sl_dist, live_price - RISK_REWARD * sl_dist
            if sl_dist <= 0 or sl <= 0 or tp <= 0:
                direction, sl, tp = "HOLD", 0.0, 0.0
                reasons.append("Invalid TP/SL - signal discarded")
        else:
            reasons.append(f"Confidence {conf:.0f}% is below the {MIN_CONFIDENCE:.0f}% minimum - WAIT")

        sig.direction = direction
        sig.label = ("BUY / UP" if direction == "BUY"
                     else "SELL / DOWN" if direction == "SELL" else WAIT_LABEL)
        sig.lean = lean
        sig.price = live_price
        sig.tp, sig.sl = tp, sl
        sig.rr = RISK_REWARD if direction != "HOLD" else 0.0
        sig.pattern = name
        sig.score = score
        sig.confidence = conf
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


# ------------------------------------------------------------------- config
DEFAULT_CONFIG = {
    "testnet": True,                 # True = Binance Spot Testnet (or local paper if no keys)
    "testnet_api_key": "",
    "testnet_api_secret": "",
    "live_api_key": "",
    "live_api_secret": "",
    "gemini_api_key": "",
    "gemini_model": "gemini-2.5-flash",   # change here if Google retires this model
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


def save_config(path: str, cfg: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


# --------------------------------------------------------- Binance execution
class BinanceClient:
    """Minimal signed Binance Spot REST client (testnet or live)."""

    def __init__(self, key: str, secret: str, testnet: bool = True):
        self.key = key.strip()
        self.secret = secret.strip()
        self.base = "https://testnet.binance.vision" if testnet else "https://api.binance.com"
        self._offset = None

    def _req(self, method: str, path: str, params: Optional[dict] = None, signed: bool = False):
        params = dict(params or {})
        headers = {"X-MBX-APIKEY": self.key} if self.key else {}
        if signed:
            self._sync()
            params["timestamp"] = int(time.time() * 1000) + self._offset
            params["recvWindow"] = 10000
            query = urlencode(params)
            sig = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            r = requests.request(method, f"{self.base}{path}?{query}&signature={sig}",
                                 headers=headers, timeout=HTTP_TIMEOUT)
        else:
            r = requests.request(method, self.base + path, params=params,
                                 headers=headers, timeout=HTTP_TIMEOUT)
        try:
            data = r.json()
        except ValueError:
            raise BinanceError(f"HTTP {r.status_code}: unreadable response")
        if r.status_code != 200:
            if isinstance(data, dict):
                raise BinanceError(f"{data.get('msg', 'error')} (code {data.get('code')})")
            raise BinanceError(f"HTTP {r.status_code}")
        return data

    def _sync(self):
        if self._offset is None:
            try:
                ms = int(self._req("GET", "/api/v3/time")["serverTime"])
                self._offset = ms - int(time.time() * 1000)
            except Exception:  # noqa: BLE001
                self._offset = 0

    def free_balance(self, asset: str) -> Decimal:
        data = self._req("GET", "/api/v3/account", signed=True)
        for b in data.get("balances", []):
            if b.get("asset") == asset:
                return Decimal(b["free"])
        return Decimal("0")

    def symbol_rules(self, symbol: str) -> dict:
        info = self._req("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
        s = info["symbols"][0]
        step, min_qty, min_not = Decimal("0"), Decimal("0"), Decimal("0")
        for f in s.get("filters", []):
            t = f.get("filterType")
            if t == "LOT_SIZE":
                step, min_qty = Decimal(f["stepSize"]), Decimal(f["minQty"])
            elif t in ("NOTIONAL", "MIN_NOTIONAL"):
                min_not = Decimal(f.get("minNotional", "0"))
        return {"step": step, "min_qty": min_qty, "min_notional": min_not}

    def market_order(self, symbol: str, side: str, quantity: str) -> dict:
        return self._req("POST", "/api/v3/order", {
            "symbol": symbol, "side": side, "type": "MARKET",
            "quantity": quantity, "newOrderRespType": "FULL"}, signed=True)


def position_size(balance: float, entry: float, sl: float,
                  risk_pct: float = MAX_RISK_PCT) -> float:
    """Units such that hitting SL loses at most risk_pct (capped at 1%) of balance."""
    risk_pct = min(max(risk_pct, 0.0), MAX_RISK_PCT)
    dist = abs(entry - sl)
    if dist <= 0 or entry <= 0:
        return 0.0
    return min((balance * risk_pct) / dist, balance / entry)   # no leverage


def _floor_step(q: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return q
    return (q / step).to_integral_value(rounding=ROUND_DOWN) * step


def _result(ok: bool, mode: str, msg: str, order_id: str = "") -> dict:
    return {"ok": ok, "mode": mode, "msg": msg, "order_id": order_id}


def _paper_order(sig: Signal, cfg: dict, risk: float) -> dict:
    qty = position_size(float(cfg.get("paper_balance", 10000.0)), sig.price, sig.sl, risk)
    oid = f"PAPER-{int(time.time())}"
    return _result(True, "PAPER",
                   f"PAPER {sig.direction} {qty:.6f} {sig.symbol} @ {sig.price:,.2f}\n"
                   f"Order ID: {oid}\nSL {sig.sl:,.2f} | TP {sig.tp:,.2f}\n"
                   f"(Simulated - add Testnet keys in settings for real testnet orders.)", oid)


def execute_trade(sig: Signal, cfg: dict) -> dict:
    """Market order on Binance Spot.
    Testnet ON : Spot Testnet order if testnet keys exist, else a local paper order.
    Testnet OFF: real order (keys required).
    Returns {"ok", "mode", "msg", "order_id"}."""
    if sig is None or sig.error or sig.direction not in ("BUY", "SELL") \
            or sig.sl <= 0 or sig.tp <= 0:
        return _result(False, "-", f"No actionable signal (needs confidence >= {MIN_CONFIDENCE:.0f}%).")

    testnet = bool(cfg.get("testnet", True))
    mode = "TESTNET" if testnet else "LIVE"
    key = cfg.get("testnet_api_key" if testnet else "live_api_key") or ""
    secret = cfg.get("testnet_api_secret" if testnet else "live_api_secret") or ""
    risk = min(float(cfg.get("risk_pct", MAX_RISK_PCT)), MAX_RISK_PCT)

    if not key.strip() or not secret.strip():
        if testnet:
            return _paper_order(sig, cfg, risk)
        return _result(False, mode, "Live API key/secret missing. Open settings (LIVE button) and add them.")

    try:
        base, quote = sig.symbol.split("/")
        bsym = _to_binance_symbol(sig.symbol)
        client = BinanceClient(key, secret, testnet)
        rules = client.symbol_rules(bsym)

        price = Decimal(str(sig.price))
        quote_free = client.free_balance(quote)
        base_free = client.free_balance(base)
        equity = quote_free + base_free * price

        qty = Decimal(str(position_size(float(equity), sig.price, sig.sl, risk)))
        if sig.direction == "BUY":
            qty = min(qty, quote_free / price * Decimal("0.995"))
        else:
            qty = min(qty, base_free)      # spot cannot short: sell only what you hold
        qty = _floor_step(qty, rules["step"])

        if qty <= 0 or qty < rules["min_qty"] or qty * price < rules["min_notional"]:
            need = "base asset balance" if sig.direction == "SELL" else f"{quote} balance"
            return _result(False, mode,
                           f"Order size {qty} {base} is below Binance minimums "
                           f"(min qty {rules['min_qty']}, min notional {rules['min_notional']}). "
                           f"Check your {need}.")

        resp = client.market_order(bsym, sig.direction, format(qty.normalize(), "f"))
        oid = str(resp.get("orderId", "?"))
        executed = Decimal(resp.get("executedQty", "0"))
        spent = Decimal(resp.get("cummulativeQuoteQty", "0"))
        avg = (spent / executed) if executed > 0 else price
        return _result(True, mode,
                       f"{mode} {sig.direction} {executed.normalize():f} {base} @ ~{float(avg):,.2f}\n"
                       f"Order ID: {oid}\nStatus: {resp.get('status', '?')}\n"
                       f"Set SL {sig.sl:,.2f} / TP {sig.tp:,.2f} on the exchange NOW "
                       f"(not placed automatically).", oid)
    except BinanceError as exc:
        return _result(False, mode, f"Binance rejected the request: {exc}")
    except requests.RequestException as exc:
        return _result(False, mode,
                       f"Network error ({exc.__class__.__name__}). Order NOT confirmed - "
                       f"check your exchange before retrying.")
    except Exception as exc:  # noqa: BLE001
        log.exception("execute_trade failed")
        return _result(False, mode, f"Unexpected error: {exc.__class__.__name__}: {exc}")


# ----------------------------------------------------------- AI assistant
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_HINGLISH = {"kya", "hai", "hain", "bazaar", "bazar", "bhav", "bhaav", "kaisa", "kaisi",
             "batao", "bata", "bataiye", "mujhe", "aaj", "khareed", "kharidna", "becho",
             "bechna", "kab", "kitna", "kitni", "abhi", "nahi", "haan", "ka", "ki", "ko"}


def has_devanagari(text: str) -> bool:
    return bool(_DEVANAGARI.search(text or ""))


def detect_lang(text: str) -> str:
    """Language code for TTS: 'hi' if the text contains Devanagari, else 'en'."""
    return "hi" if has_devanagari(text) else "en"


def resolve_lang(pref: str, user_text: str) -> str:
    if pref == "Hindi":
        return "hi"
    if pref == "English":
        return "en"
    if has_devanagari(user_text):
        return "hi"
    words = set(re.findall(r"[a-z]+", (user_text or "").lower()))
    return "hi" if len(words & _HINGLISH) >= 2 else "en"


def _num(x: float) -> str:
    return f"{x:.0f}" if abs(x) >= 100 else f"{x:.2f}"


def market_brief(sig: Optional[Signal], lang: str = "en") -> str:
    """Offline, speakable market summary in English or Hindi."""
    if sig is None or sig.error or not sig.price:
        return ("अभी मार्केट डेटा नहीं है। पहले स्कैन मार्केट दबाइए।" if lang == "hi"
                else "There is no market data yet. Press scan market first.")
    coin = sig.symbol.split("/")[0]
    up = sig.ema20 > sig.ema50
    c, price = f"{sig.confidence:.0f}", _num(sig.price)

    if lang == "hi":
        trend = "तेज़ी का ट्रेंड" if up else "मंदी का ट्रेंड"
        if sig.rsi < 30:
            zone = "आरएसआई ओवरसोल्ड है, यानी बिकवाली ज़्यादा हो चुकी है"
        elif sig.rsi > 70:
            zone = "आरएसआई ओवरबॉट है, यानी खरीदारी ज़्यादा हो चुकी है"
        else:
            zone = "आरएसआई सामान्य है"
        if sig.direction == "BUY":
            action = f"खरीद का सिग्नल है, कॉन्फिडेंस {c} प्रतिशत। टारगेट {_num(sig.tp)}, स्टॉप लॉस {_num(sig.sl)}।"
        elif sig.direction == "SELL":
            action = f"बिक्री का सिग्नल है, कॉन्फिडेंस {c} प्रतिशत। टारगेट {_num(sig.tp)}, स्टॉप लॉस {_num(sig.sl)}।"
        else:
            action = f"अभी इंतज़ार करना बेहतर है। कॉन्फिडेंस {c} प्रतिशत है, जो सत्तर प्रतिशत से कम है।"
        return f"{coin} का भाव {price} है, {sig.timeframe} चार्ट पर। {trend}। {zone}। {action} यह वित्तीय सलाह नहीं है।"

    trend = "uptrend" if up else "downtrend"
    if sig.rsi < 30:
        zone = "RSI is oversold"
    elif sig.rsi > 70:
        zone = "RSI is overbought"
    else:
        zone = "RSI is neutral"
    if sig.direction in ("BUY", "SELL"):
        word = "buy" if sig.direction == "BUY" else "sell"
        action = (f"The signal is {word} with {c} percent confidence. "
                  f"Target {_num(sig.tp)}, stop loss {_num(sig.sl)}.")
    else:
        action = (f"Better to wait. Confidence is {c} percent, below the "
                  f"{MIN_CONFIDENCE:.0f} percent minimum.")
    return (f"{coin} is trading at {price} on the {sig.timeframe} chart, in a {trend}. "
            f"{zone}. {action} This is not financial advice.")


def _gemini_reply(question: str, sig: Optional[Signal], cfg: dict, lang_pref: str) -> str:
    key = cfg.get("gemini_api_key", "").strip()
    model = cfg.get("gemini_model", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
    ctx = "No market scan has been run yet."
    if sig is not None and not sig.error and sig.price:
        ctx = json.dumps({
            "symbol": sig.symbol, "timeframe": sig.timeframe, "price": sig.price,
            "signal": sig.label, "confidence_pct": sig.confidence,
            "min_confidence_pct": MIN_CONFIDENCE, "rsi14": round(sig.rsi, 1),
            "ema20": sig.ema20, "ema50": sig.ema50, "bb_upper": sig.bb_upper,
            "bb_lower": sig.bb_lower, "atr14": sig.atr, "pattern": sig.pattern,
            "take_profit": sig.tp, "stop_loss": sig.sl, "reasons": sig.reasons})
    if lang_pref == "Hindi":
        lang_rule = "Reply in Hindi using Devanagari script."
    elif lang_pref == "English":
        lang_rule = "Reply in English."
    else:
        lang_rule = ("Reply in the user's language. If the user writes Hindi or Hinglish, "
                     "reply in Hindi using Devanagari script.")
    system = ("You are a concise trading-desk assistant inside a quant trading app. "
              "Use ONLY the market data provided; never invent prices. Answer in at most "
              "4 short sentences of plain text with no markdown, suitable to be read aloud. "
              "Never promise profit; if you suggest action, mention risk briefly. " + lang_rule)
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [
            {"text": f"MARKET DATA:\n{ctx}\n\nUSER QUESTION:\n{question}"}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 800},
    }
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        json=body, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        timeout=20)
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        raise RuntimeError(f"Gemini HTTP {r.status_code} {msg[:120]}")
    parts = r.json()["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts).replace("*", "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty answer")
    return text


def ask_assistant(question: str, sig: Optional[Signal], cfg: dict,
                  lang_pref: str = "Auto"):
    """Returns (reply_text, source_note). Never raises."""
    lang = resolve_lang(lang_pref, question)
    if cfg.get("gemini_api_key", "").strip():
        try:
            return _gemini_reply(question, sig, cfg, lang_pref), "gemini"
        except Exception as exc:  # noqa: BLE001
            log.warning("gemini failed: %s", exc)
            return market_brief(sig, lang), f"offline summary (AI error: {str(exc)[:80]})"
    return market_brief(sig, lang), "offline summary (add a Gemini key in settings for chat)"
