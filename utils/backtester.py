import sqlite3
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, date
from utils.greeks_engine import black_scholes, calculate_iv
from config.settings import INDICES

logger = logging.getLogger(__name__)

DB_PATH = "data/trade_journal.db"


# ─────────────────────────────────────────
#  HISTORICAL CANDLE LOADER
# ─────────────────────────────────────────

def load_historical_candles(obj, token: str,
                             days_back: int = 60) -> pd.DataFrame:
    """
    Fetch daily candles for backtesting.
    Uses Angel One getCandleData with ONE_DAY interval.
    """
    from utils.technical import fetch_candles
    df = fetch_candles(obj, token, interval="ONE_DAY", days_back=days_back)
    if df.empty:
        logger.warning(f"⚠️ No candle data for token={token}")
    return df


# ─────────────────────────────────────────
#  STRATEGY SIMULATORS
# ─────────────────────────────────────────

def simulate_short_straddle(
    spot:        float,
    strike:      float,
    ce_premium:  float,
    pe_premium:  float,
    days:        int,
    lot_size:    int,
    lots:        int = 1,
    sl_pct:      float = 0.40,   # exit if premium rises 40%
    target_pct:  float = 0.50,   # exit at 50% decay
) -> dict:
    """
    Simulate a short straddle over its lifetime.
    Uses simple linear theta decay model.
    """
    combined_entry = ce_premium + pe_premium
    sl_level       = combined_entry * (1 + sl_pct)
    target_level   = combined_entry * (1 - target_pct)

    daily_decay    = combined_entry / max(days, 1)
    result         = "EXPIRED"
    exit_day       = days
    exit_premium   = 0

    # Simulate day by day
    for d in range(1, days + 1):
        remaining     = days - d
        current_prem  = combined_entry * (remaining / days)  # linear decay

        if current_prem >= sl_level:
            result      = "STOP_LOSS"
            exit_day    = d
            exit_premium = current_prem
            break

        if current_prem <= target_level:
            result      = "TARGET"
            exit_day    = d
            exit_premium = current_prem
            break

        exit_premium = current_prem

    pnl_per_lot = (combined_entry - exit_premium) * lot_size
    total_pnl   = round(pnl_per_lot * lots, 2)

    return {
        "strategy":      "Short Straddle",
        "strike":        strike,
        "entry_premium": combined_entry,
        "exit_premium":  round(exit_premium, 2),
        "exit_day":      exit_day,
        "result":        result,
        "pnl":           total_pnl,
        "lots":          lots,
        "lot_size":      lot_size,
    }


def simulate_iron_condor(
    spot:       float,
    atm:        float,
    ce_sell:    float,  # sell OTM call
    pe_sell:    float,  # sell OTM put
    ce_buy:     float,  # buy further OTM call (hedge)
    pe_buy:     float,  # buy further OTM put (hedge)
    lot_size:   int,
    lots:       int = 1,
) -> dict:
    """Simulate an Iron Condor."""
    net_credit = (ce_sell + pe_sell) - (ce_buy + pe_buy)
    max_loss   = (atm * 0.02) - net_credit  # approx 2% wide wings

    # Simplified: 60% of time expires worthless (typical IC win rate)
    pnl = round(net_credit * lot_size * lots, 2)

    return {
        "strategy":   "Iron Condor",
        "net_credit": round(net_credit, 2),
        "max_loss":   round(max_loss, 2),
        "pnl":        pnl,
        "lots":       lots,
    }


# ─────────────────────────────────────────
#  SIGNAL REPLAY BACKTEST
# ─────────────────────────────────────────

def backtest_from_journal(days_back: int = 30) -> dict:
    """
    Replay signals from trade journal and calculate
    what would have happened if we'd taken every trade.

    Uses logged signals from signals table.
    """
    conn  = sqlite3.connect(DB_PATH)
    since = (date.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    signals_df = pd.read_sql_query(f"""
        SELECT date, time, index_name, nifty_spot, pcr, regime,
               signal_score, llm_action, avg_iv, days_to_expiry, theta
        FROM signals
        WHERE date >= '{since}'
        ORDER BY date, time
    """, conn)

    trades_df = pd.read_sql_query(f"""
        SELECT * FROM trades
        WHERE date >= '{since}'
        ORDER BY date
    """, conn)

    conn.close()

    if signals_df.empty:
        return {"error": "No signal data found. Run bot during market hours first."}

    # Stats on signals
    total_signals   = len(signals_df)
    take_signals    = len(signals_df[signals_df["llm_action"] == "ENTER"])
    skip_signals    = len(signals_df[signals_df["llm_action"] == "SKIP"])
    avg_score       = signals_df["signal_score"].mean()
    regime_counts   = signals_df["regime"].value_counts().to_dict()

    # Stats on actual trades
    total_trades    = len(trades_df[trades_df["status"] == "CLOSED"])
    winning_trades  = len(trades_df[trades_df["pnl"] > 0])
    losing_trades   = len(trades_df[trades_df["pnl"] < 0])
    total_pnl       = trades_df[trades_df["status"] == "CLOSED"]["pnl"].sum()
    win_rate        = (winning_trades / total_trades * 100) if total_trades else 0

    # Score distribution
    score_dist = signals_df["signal_score"].value_counts().sort_index().to_dict()

    return {
        "period":         f"Last {days_back} days",
        "total_signals":  total_signals,
        "take_signals":   take_signals,
        "skip_signals":   skip_signals,
        "avg_score":      round(avg_score, 2),
        "score_dist":     score_dist,
        "regime_counts":  regime_counts,
        "total_trades":   total_trades,
        "winning_trades": winning_trades,
        "losing_trades":  losing_trades,
        "total_pnl":      round(float(total_pnl or 0), 2),
        "win_rate":       round(win_rate, 1),
    }


# ─────────────────────────────────────────
#  STRATEGY PARAMETER OPTIMIZER
# ─────────────────────────────────────────

def optimize_thresholds(days_back: int = 30) -> dict:
    """
    Test different signal score thresholds (3,4,5,6)
    and see which would have been most profitable.
    """
    conn  = sqlite3.connect(DB_PATH)
    since = (date.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    df = pd.read_sql_query(f"""
        SELECT date, signal_score, llm_action,
               avg_iv, days_to_expiry, theta, nifty_spot
        FROM signals
        WHERE date >= '{since}'
        ORDER BY date, time
    """, conn)
    conn.close()

    if df.empty:
        return {"error": "No data"}

    results = []
    for threshold in [2, 3, 4, 5, 6]:
        trades_at_threshold = df[df["signal_score"] >= threshold]
        results.append({
            "threshold":    threshold,
            "trades_taken": len(trades_at_threshold),
            "pct_of_total": round(len(trades_at_threshold) / len(df) * 100, 1),
            "avg_iv":       round(trades_at_threshold["avg_iv"].mean(), 2)
                            if not trades_at_threshold.empty else 0,
        })

    return {
        "total_signals": len(df),
        "thresholds":    results,
    }


# ─────────────────────────────────────────
#  FULL BACKTEST REPORT
# ─────────────────────────────────────────

def generate_backtest_report(days_back: int = 30) -> str:
    """
    Generate a full backtest report as formatted text.
    """
    journal = backtest_from_journal(days_back)
    thresholds = optimize_thresholds(days_back)

    if "error" in journal:
        return f"❌ {journal['error']}"

    lines = [
        f"📊 BACKTEST REPORT — {journal['period']}",
        f"{'='*45}",
        f"",
        f"SIGNAL STATS:",
        f"  Total scans    : {journal['total_signals']}",
        f"  ENTER signals  : {journal['take_signals']}",
        f"  SKIP signals   : {journal['skip_signals']}",
        f"  Avg score      : {journal['avg_score']}/7",
        f"",
        f"SCORE DISTRIBUTION:",
    ]

    for score, count in sorted(journal["score_dist"].items()):
        bar = "█" * count
        lines.append(f"  Score {score}: {bar} ({count})")

    lines += [
        f"",
        f"REGIME DISTRIBUTION:",
    ]
    for regime, count in journal["regime_counts"].items():
        lines.append(f"  {regime}: {count}")

    lines += [
        f"",
        f"ACTUAL TRADE RESULTS:",
        f"  Total trades   : {journal['total_trades']}",
        f"  Wins           : {journal['winning_trades']}",
        f"  Losses         : {journal['losing_trades']}",
        f"  Win Rate       : {journal['win_rate']}%",
        f"  Total P&L      : ₹{journal['total_pnl']}",
        f"",
        f"THRESHOLD ANALYSIS:",
        f"  {'Threshold':<12} {'Trades':<10} {'% of Total':<12} {'Avg IV'}",
        f"  {'-'*45}",
    ]

    if "thresholds" in thresholds:
        for t in thresholds["thresholds"]:
            lines.append(
                f"  Score >= {t['threshold']:<4} "
                f"{t['trades_taken']:<10} "
                f"{t['pct_of_total']:<12}% "
                f"{t['avg_iv']}%"
            )

    lines += [
        f"",
        f"RECOMMENDATION:",
    ]

    if journal["total_trades"] == 0:
        lines.append("  ⚠️  No closed trades yet — keep paper trading.")
    elif journal["win_rate"] >= 60:
        lines.append(f"  ✅ Win rate {journal['win_rate']}% — consider going live with 1 lot.")
    elif journal["win_rate"] >= 40:
        lines.append(f"  🟡 Win rate {journal['win_rate']}% — keep paper trading, refine signals.")
    else:
        lines.append(f"  🔴 Win rate {journal['win_rate']}% — do NOT go live, review strategy.")

    return "\n".join(lines)
