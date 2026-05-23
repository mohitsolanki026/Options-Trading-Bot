import sqlite3
import logging
import json
from datetime import datetime, date
from tabulate import tabulate

logger = logging.getLogger(__name__)

DB_PATH = "data/trade_journal.db"


# ─────────────────────────────────────────
#  DATABASE SETUP
# ─────────────────────────────────────────

def init_db():
    """Create all tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    # Trades table
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            date          TEXT,
            time          TEXT,
            index_name    TEXT,
            strategy      TEXT,
            action        TEXT,
            symbol        TEXT,
            direction     TEXT,
            strike        REAL,
            option_type   TEXT,
            entry_price   REAL,
            exit_price    REAL,
            lots          INTEGER,
            lot_size      INTEGER,
            pnl           REAL,
            status        TEXT,
            expiry        TEXT,
            notes         TEXT
        )
    """)

    # Signals table (every scan logged here)
    c.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT,
            time            TEXT,
            index_name      TEXT,
            nifty_spot      REAL,
            vix             REAL,
            pcr             REAL,
            sentiment       TEXT,
            support         REAL,
            resistance      REAL,
            max_pain        REAL,
            avg_iv          REAL,
            days_to_expiry  INTEGER,
            theta           REAL,
            regime          TEXT,
            signal_score    INTEGER,
            overall_bias    TEXT,
            llm_action      TEXT,
            llm_confidence  TEXT,
            llm_strategy    TEXT,
            llm_reasoning   TEXT,
            raw_json        TEXT
        )
    """)

    # Daily summary table
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_summary (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT UNIQUE,
            total_trades    INTEGER,
            winning_trades  INTEGER,
            losing_trades   INTEGER,
            total_pnl       REAL,
            best_trade      REAL,
            worst_trade     REAL,
            notes           TEXT
        )
    """)

    conn.commit()
    conn.close()
    logger.info("✅ Trade journal database initialised.")


# ─────────────────────────────────────────
#  LOG FUNCTIONS
# ─────────────────────────────────────────

def log_signal(
    index_name:  str,
    summary:     dict,
    greeks:      dict,
    regime:      dict,
    confluence:  dict,
    decision:    dict,
    vix:         float,
):
    """Log every market scan with LLM decision."""
    now  = datetime.now()
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    c.execute("""
        INSERT INTO signals (
            date, time, index_name,
            nifty_spot, vix, pcr, sentiment,
            support, resistance, max_pain,
            avg_iv, days_to_expiry, theta,
            regime, signal_score, overall_bias,
            llm_action, llm_confidence, llm_strategy, llm_reasoning,
            raw_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        now.strftime("%Y-%m-%d"),
        now.strftime("%H:%M:%S"),
        index_name,
        summary["nifty_spot"],
        vix,
        float(summary["pcr"]),
        summary["sentiment"],
        float(summary["support"]),
        float(summary["resistance"]),
        float(summary["max_pain"]),
        float(greeks["avg_iv"]),
        greeks["days_to_exp"],
        float(greeks["theta"] or 0.0),
        regime["regime"],
        confluence["score"],
        confluence["overall_bias"],
        decision.get("action"),
        decision.get("confidence"),
        decision.get("strategy"),
        decision.get("reasoning"),
        json.dumps(decision),
    ))

    conn.commit()
    conn.close()
    logger.info(f"📝 Signal logged: {decision.get('action')} @ {now.strftime('%H:%M')}")


def log_trade_entry(
    index_name:  str,
    strategy:    str,
    symbol:      str,
    direction:   str,
    strike:      float,
    option_type: str,
    entry_price: float,
    lots:        int,
    lot_size:    int,
    expiry:      str,
    notes:       str = "",
) -> int:
    """Log a new trade entry. Returns trade ID."""
    now  = datetime.now()
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    c.execute("""
        INSERT INTO trades (
            date, time, index_name, strategy,
            action, symbol, direction, strike,
            option_type, entry_price, lots,
            lot_size, status, expiry, notes
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        now.strftime("%Y-%m-%d"),
        now.strftime("%H:%M:%S"),
        index_name,
        strategy,
        "ENTRY",
        symbol,
        direction,
        strike,
        option_type,
        entry_price,
        lots,
        lot_size,
        "OPEN",
        expiry,
        notes,
    ))

    trade_id = c.lastrowid
    conn.commit()
    conn.close()
    logger.info(f"📝 Trade entry logged: ID={trade_id} {symbol} {direction} @ ₹{entry_price}")
    return trade_id


def log_trade_exit(
    trade_id:   int,
    exit_price: float,
    pnl:        float,
    notes:      str = "",
):
    """Update trade record with exit details."""
    now  = datetime.now()
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    c.execute("""
        UPDATE trades
        SET exit_price = ?,
            pnl        = ?,
            status     = 'CLOSED',
            notes      = ?,
            time       = ?
        WHERE id = ?
    """, (exit_price, pnl, notes, now.strftime("%H:%M:%S"), trade_id))

    conn.commit()
    conn.close()
    logger.info(f"📝 Trade exit logged: ID={trade_id} exit=₹{exit_price} pnl=₹{pnl}")


def update_daily_summary():
    """Recalculate and upsert today's summary."""
    today = date.today().strftime("%Y-%m-%d")
    conn  = sqlite3.connect(DB_PATH)
    c     = conn.cursor()

    c.execute("""
        SELECT
            COUNT(*),
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END),
            SUM(pnl),
            MAX(pnl),
            MIN(pnl)
        FROM trades
        WHERE date = ? AND status = 'CLOSED'
    """, (today,))

    row = c.fetchone()
    total, wins, losses, total_pnl, best, worst = row

    c.execute("""
        INSERT INTO daily_summary
            (date, total_trades, winning_trades, losing_trades,
             total_pnl, best_trade, worst_trade)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(date) DO UPDATE SET
            total_trades   = excluded.total_trades,
            winning_trades = excluded.winning_trades,
            losing_trades  = excluded.losing_trades,
            total_pnl      = excluded.total_pnl,
            best_trade     = excluded.best_trade,
            worst_trade    = excluded.worst_trade
    """, (today, total or 0, wins or 0, losses or 0,
          total_pnl or 0, best or 0, worst or 0))

    conn.commit()
    conn.close()
    logger.info(f"📊 Daily summary updated: P&L=₹{total_pnl or 0}")


# ─────────────────────────────────────────
#  QUERY / REPORT FUNCTIONS
# ─────────────────────────────────────────

def get_today_trades() -> list:
    """Fetch all trades for today."""
    today = date.today().strftime("%Y-%m-%d")
    conn  = sqlite3.connect(DB_PATH)
    c     = conn.cursor()
    c.execute("SELECT * FROM trades WHERE date = ?", (today,))
    rows  = c.fetchall()
    conn.close()
    return rows


def get_today_signals() -> list:
    """Fetch all signals logged today."""
    today = date.today().strftime("%Y-%m-%d")
    conn  = sqlite3.connect(DB_PATH)
    c     = conn.cursor()
    c.execute("""
        SELECT time, nifty_spot, vix, pcr, regime,
               signal_score, llm_action, llm_confidence
        FROM signals WHERE date = ?
        ORDER BY time
    """, (today,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_weekly_summary() -> str:
    """Return last 7 days P&L as a formatted table."""
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("""
        SELECT date, total_trades, winning_trades,
               losing_trades, total_pnl
        FROM daily_summary
        ORDER BY date DESC
        LIMIT 7
    """)
    rows = c.fetchall()
    conn.close()

    if not rows:
        return "No trade history yet."

    headers = ["Date", "Trades", "Wins", "Losses", "P&L (₹)"]
    return tabulate(rows, headers=headers, tablefmt="grid")


def print_today_signals():
    """Print today's signal log to console."""
    rows = get_today_signals()
    if not rows:
        print("No signals logged today.")
        return
    headers = ["Time", "Spot", "VIX", "PCR",
               "Regime", "Score", "Action", "Confidence"]
    print(tabulate(rows, headers=headers, tablefmt="grid"))
