import logging
import numpy as np
import pandas as pd
import requests
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# ────────────────────────────────────────
#  HISTORICAL DATA FROM ANGEL ONE
# ────────────────────────────────────────

def fetch_candles(obj, token: str, interval: str = "FIVE_MINUTE",
                  days_back: int = 5) -> pd.DataFrame:
    """
    Fetch OHLCV candle data from Angel One.

    interval options:
        ONE_MINUTE, THREE_MINUTE, FIVE_MINUTE, TEN_MINUTE,
        FIFTEEN_MINUTE, THIRTY_MINUTE, ONE_HOUR, ONE_DAY

    Returns DataFrame with columns: datetime, open, high, low, close, volume
    """
    try:
        to_date   = datetime.now()
        from_date = to_date - timedelta(days=days_back)

        # ← KEY FIX: use market hours, not current time
        from_str = from_date.strftime("%Y-%m-%d") + " 09:15"
        to_str   = to_date.strftime("%Y-%m-%d") + " 15:30"

        params = {
            "exchange":    "NSE",
            "symboltoken": token,
            "interval":    interval,
            "fromdate":    from_str,
            "todate":      to_str,
        }

        logger.info(f"⏳ Fetching candles for {params}")

        response = obj.getCandleData(params)

        if not response or not response.get("status"):
            logger.error(f"❌ Candle data error: {response}")
            return pd.DataFrame()
        
        logger.info(f"📈 {response}")

        data = response.get("data", [])
        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=[
            "datetime", "open", "high", "low", "close", "volume"
        ])
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values("datetime").reset_index(drop=True)

        logger.info(f"✅ Fetched {len(df)} candles for token={token}")
        return df

    except Exception as e:
        logger.error(f"❌ fetch_candles error: {e}")
        return pd.DataFrame()


# ─────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────

def calculate_rsi(closes: pd.Series, period: int = 14) -> float:
    """
    RSI — Relative Strength Index.
    > 70 → Overbought (bearish signal)
    < 30 → Oversold  (bullish signal)
    40-60 → Neutral
    """
    if len(closes) < period + 1:
        return 50.0  # neutral default

    delta  = closes.diff()
    gain   = delta.where(delta > 0, 0.0)
    loss   = -delta.where(delta < 0, 0.0)

    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()

    rs  = avg_gain / avg_loss.replace(0, np.inf)
    rsi = 100 - (100 / (1 + rs))

    return round(float(rsi.iloc[-1]), 2)


def calculate_macd(closes: pd.Series,
                   fast: int = 12, slow: int = 26,
                   signal: int = 9) -> dict:
    """
    MACD — Moving Average Convergence Divergence.
    macd_line > signal_line → Bullish momentum
    macd_line < signal_line → Bearish momentum
    histogram crossing zero → Trend change
    """
    if len(closes) < slow + signal:
        return {"macd": 0.0, "signal": 0.0, "histogram": 0.0, "bias": "NEUTRAL"}

    ema_fast   = closes.ewm(span=fast,   adjust=False).mean()
    ema_slow   = closes.ewm(span=slow,   adjust=False).mean()
    macd_line  = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram  = macd_line - signal_line

    macd_val  = round(float(macd_line.iloc[-1]),  2)
    sig_val   = round(float(signal_line.iloc[-1]), 2)
    hist_val  = round(float(histogram.iloc[-1]),  2)

    # Bias
    if macd_val > sig_val and hist_val > 0:
        bias = "BULLISH"
    elif macd_val < sig_val and hist_val < 0:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    return {
        "macd":      macd_val,
        "signal":    sig_val,
        "histogram": hist_val,
        "bias":      bias,
    }


def calculate_ema(closes: pd.Series,
                  period: int = 20) -> dict:
    """
    EMA — Exponential Moving Average.
    Price > EMA → Bullish
    Price < EMA → Bearish
    """
    if len(closes) < period:
        return {"ema": closes.iloc[-1], "bias": "NEUTRAL"}

    ema  = closes.ewm(span=period, adjust=False).mean()
    val  = round(float(ema.iloc[-1]), 2)
    price = float(closes.iloc[-1])

    bias = "BULLISH" if price > val else "BEARISH"
    return {"ema": val, "price": price, "bias": bias}


def calculate_supertrend(df: pd.DataFrame,
                         period: int = 7,
                         multiplier: float = 3.0) -> dict:
    """
    Supertrend — combines ATR with trend direction.
    direction = 1  → Uptrend  (buy signal)
    direction = -1 → Downtrend (sell signal)
    """
    if len(df) < period + 1:
        return {"supertrend": 0.0, "direction": 0, "bias": "NEUTRAL"}

    hl_avg = (df["high"] + df["low"]) / 2

    # ATR calculation
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(span=period, adjust=False).mean()

    upper = hl_avg + multiplier * atr
    lower = hl_avg - multiplier * atr

    supertrend = pd.Series(index=df.index, dtype=float)
    direction  = pd.Series(index=df.index, dtype=int)

    for i in range(1, len(df)):
        close = df["close"].iloc[i]
        prev_close = df["close"].iloc[i - 1]

        # Upper band
        if upper.iloc[i] < upper.iloc[i-1] or prev_close > upper.iloc[i-1]:
            upper.iloc[i] = upper.iloc[i]
        else:
            upper.iloc[i] = upper.iloc[i-1]

        # Lower band
        if lower.iloc[i] > lower.iloc[i-1] or prev_close < lower.iloc[i-1]:
            lower.iloc[i] = lower.iloc[i]
        else:
            lower.iloc[i] = lower.iloc[i-1]

        # Direction
        if i == 1:
            direction.iloc[i] = 1
        elif supertrend.iloc[i-1] == upper.iloc[i-1]:
            direction.iloc[i] = -1 if close > upper.iloc[i] else 1
        else:
            direction.iloc[i] = 1 if close < lower.iloc[i] else -1

        supertrend.iloc[i] = lower.iloc[i] if direction.iloc[i] == 1 else upper.iloc[i]

    last_dir = int(direction.iloc[-1])
    last_st  = round(float(supertrend.iloc[-1]), 2)
    bias     = "BULLISH" if last_dir == 1 else "BEARISH"

    return {"supertrend": last_st, "direction": last_dir, "bias": bias}


def calculate_vwap(df: pd.DataFrame) -> dict:
    """
    VWAP — Volume Weighted Average Price.
    Price > VWAP → Bullish (institutions buying)
    Price < VWAP → Bearish (institutions selling)
    Most relevant intraday.
    """
    if df.empty or "volume" not in df.columns:
        return {"vwap": 0.0, "bias": "NEUTRAL"}

    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    vwap = (typical_price * df["volume"]).cumsum() / df["volume"].cumsum()

    val   = round(float(vwap.iloc[-1]), 2)
    price = float(df["close"].iloc[-1])
    bias  = "BULLISH" if price > val else "BEARISH"

    return {"vwap": val, "price": price, "bias": bias}


# ─────────────────────────────────────────
#  MAIN FUNCTION — Run all indicators
# ─────────────────────────────────────────

def run_technical_analysis(obj, token: str,
                           interval: str = "FIFTEEN_MINUTE",
                           days_back: int = 5) -> dict:
    """
    Fetch candles and run all indicators.
    Returns a complete technical picture.
    """
    df = fetch_candles(obj, token, interval, days_back)

    if df.empty or len(df) < 30:
        logger.warning(f"⚠️ Not enough candle data for technical analysis")
        return _neutral_result()

    closes = df["close"]

    rsi        = calculate_rsi(closes)
    macd       = calculate_macd(closes)
    ema20      = calculate_ema(closes, 20)
    ema50      = calculate_ema(closes, 50)
    supertrend = calculate_supertrend(df)
    vwap       = calculate_vwap(df)

    # ── Bias scoring ──────────────────────
    bias_scores = {
        "BULLISH": 0,
        "BEARISH": 0,
        "NEUTRAL": 0,
    }

    signals = []

    logger.info(f"📊 Technical Analysis {rsi} {macd} {ema20} {ema50} {supertrend} {vwap}")

    # RSI
    if rsi > 70:
        bias_scores["BEARISH"] += 1
        signals.append({"name": "RSI", "value": rsi, "bias": "BEARISH",
                         "note": f"RSI={rsi} → Overbought"})
    elif rsi < 30:
        bias_scores["BULLISH"] += 1
        signals.append({"name": "RSI", "value": rsi, "bias": "BULLISH",
                         "note": f"RSI={rsi} → Oversold"})
    else:
        bias_scores["NEUTRAL"] += 1
        signals.append({"name": "RSI", "value": rsi, "bias": "NEUTRAL",
                         "note": f"RSI={rsi} → Neutral"})

    # MACD
    bias_scores[macd["bias"]] += 1
    signals.append({"name": "MACD", "value": macd["histogram"],
                     "bias": macd["bias"],
                     "note": f"MACD hist={macd['histogram']} → {macd['bias']}"})

    # EMA 20
    bias_scores[ema20["bias"]] += 1
    signals.append({"name": "EMA20", "value": ema20["ema"],
                     "bias": ema20["bias"],
                     "note": f"Price {'above' if ema20['bias']=='BULLISH' else 'below'} EMA20={ema20['ema']}"})

    # EMA 50
    bias_scores[ema50["bias"]] += 1
    signals.append({"name": "EMA50", "value": ema50["ema"],
                     "bias": ema50["bias"],
                     "note": f"Price {'above' if ema50['bias']=='BULLISH' else 'below'} EMA50={ema50['ema']}"})

    # Supertrend
    bias_scores[supertrend["bias"]] += 1
    signals.append({"name": "Supertrend", "value": supertrend["supertrend"],
                     "bias": supertrend["bias"],
                     "note": f"Supertrend → {supertrend['bias']}"})

    # VWAP
    bias_scores[vwap["bias"]] += 1
    signals.append({"name": "VWAP", "value": vwap["vwap"],
                     "bias": vwap["bias"],
                     "note": f"Price {'above' if vwap['bias']=='BULLISH' else 'below'} VWAP={vwap['vwap']}"})

    # Overall bias
    if bias_scores["BULLISH"] >= 4:
        overall = "STRONGLY BULLISH"
    elif bias_scores["BULLISH"] >= 3:
        overall = "BULLISH"
    elif bias_scores["BEARISH"] >= 4:
        overall = "STRONGLY BEARISH"
    elif bias_scores["BEARISH"] >= 3:
        overall = "BEARISH"
    else:
        overall = "NEUTRAL"

    return {
        "rsi":         rsi,
        "macd":        macd,
        "ema20":       ema20,
        "ema50":       ema50,
        "supertrend":  supertrend,
        "vwap":        vwap,
        "overall":     overall,
        "bull_count":  bias_scores["BULLISH"],
        "bear_count":  bias_scores["BEARISH"],
        "signals":     signals,
        "candles":     len(df),
        "interval":    interval,
    }


def _neutral_result() -> dict:
    return {
        "rsi":        50.0,
        "macd":       {"macd": 0, "signal": 0, "histogram": 0, "bias": "NEUTRAL"},
        "ema20":      {"ema": 0, "bias": "NEUTRAL"},
        "ema50":      {"ema": 0, "bias": "NEUTRAL"},
        "supertrend": {"supertrend": 0, "direction": 0, "bias": "NEUTRAL"},
        "vwap":       {"vwap": 0, "bias": "NEUTRAL"},
        "overall":    "NEUTRAL",
        "bull_count": 0,
        "bear_count": 0,
        "signals":    [],
        "candles":    0,
        "interval":   "N/A",
    }
