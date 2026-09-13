import os
import sqlite3
import logging
import json
from datetime import datetime, date
from tabulate import tabulate

logger = logging.getLogger(__name__)

DB_PATH = "data/trade_journal.db"

# Rows the dashboard reads back are written by the bot itself, but the web
# server reads them from a different thread. WAL lets a reader run while a
# writer holds the table, and the timeout absorbs the brief lock during a write.
_WAL_READY = False


def _connect():
    """Open a journal connection with WAL enabled (once) and a sane timeout."""
    global _WAL_READY
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    if not _WAL_READY:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            _WAL_READY = True
        except sqlite3.Error as e:
            logger.warning(f"\u26a0\ufe0f Could not enable WAL: {e}")
    return conn


# ─────────────────────────────────────────
#  DATABASE SETUP
# ─────────────────────────────────────────

def _ensure_column(conn, table: str, column: str, decl: str):
    """Add a column to an existing table if it is not there yet."""
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in have:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        logger.info(f"\U0001f5c3\ufe0f Added {table}.{column}")


def init_db():
    """Create all tables if they don't exist, and migrate older databases."""
    conn = _connect()
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


    # Events — every notable thing the bot did, in one place. This is what the
    # dashboard timeline and the Telegram feed both read from.
    c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT,
            date        TEXT,
            time        TEXT,
            level       TEXT,
            kind        TEXT,
            index_name  TEXT,
            title       TEXT,
            body        TEXT,
            meta        TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_date ON events(date, id)")

    # Gate log — the full ten-check verdict for every scan, not just the first
    # failing reason. This is what answers "why did nothing trade today".
    c.execute("""
        CREATE TABLE IF NOT EXISTS gate_log (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ts             TEXT,
            date           TEXT,
            time           TEXT,
            index_name     TEXT,
            allowed        INTEGER,
            blocking       TEXT,
            checks         TEXT,
            score          REAL,
            max_score      REAL,
            threshold      REAL,
            bias           TEXT,
            bias_confirmed INTEGER,
            iv_rank        REAL,
            spot           REAL,
            state          TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_gate_date ON gate_log(date, index_name, id)")

    # Older databases stored the exit reason by overwriting `notes`, which threw
    # away the leg description written at entry. Keep both.
    _ensure_column(conn, "trades", "exit_reason", "TEXT")
    _ensure_column(conn, "trades", "exit_time", "TEXT")

    # Closing account value per day, so the equity curve is exact rather than
    # re-derived from trade rows every time it is drawn.
    c.execute("""
        CREATE TABLE IF NOT EXISTS equity_daily (
            date            TEXT PRIMARY KEY,
            closing_capital REAL,
            realised_pnl    REAL,
            trades          INTEGER
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
    conn = _connect()
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
    conn = _connect()
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
    conn = _connect()
    c    = conn.cursor()

    c.execute("""
        UPDATE trades
        SET exit_price  = ?,
            pnl         = ?,
            status      = 'CLOSED',
            exit_reason = ?,
            exit_time   = ?
        WHERE id = ?
    """, (exit_price, pnl, notes, now.strftime("%H:%M:%S"), trade_id))

    conn.commit()
    conn.close()
    logger.info(f"📝 Trade exit logged: ID={trade_id} exit=₹{exit_price} pnl=₹{pnl}")


def update_daily_summary():
    """Recalculate and upsert today's summary."""
    today = date.today().strftime("%Y-%m-%d")
    conn  = _connect()
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
    conn  = _connect()
    c     = conn.cursor()
    c.execute("SELECT * FROM trades WHERE date = ?", (today,))
    rows  = c.fetchall()
    conn.close()
    return rows


def get_today_signals() -> list:
    """Fetch all signals logged today."""
    today = date.today().strftime("%Y-%m-%d")
    conn  = _connect()
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
    conn = _connect()
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


# ─────────────────────────────────────────
#  EVENT LOG
# ─────────────────────────────────────────

def log_event(level: str, kind: str, title: str, body: str = "",
              index_name: str = None, meta: dict = None) -> int:
    """Persist one event. Returns its id so callers can stream incrementally."""
    now  = datetime.now()
    conn = _connect()
    c    = conn.cursor()
    c.execute("""
        INSERT INTO events (ts, date, time, level, kind, index_name, title, body, meta)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (
        now.isoformat(timespec="seconds"),
        now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"),
        level, kind, index_name, title, body,
        json.dumps(meta, default=str) if meta else None,
    ))
    event_id = c.lastrowid
    conn.commit()
    conn.close()
    return event_id


def _event_row(r) -> dict:
    return {
        "id": r[0], "ts": r[1], "date": r[2], "time": r[3], "level": r[4],
        "kind": r[5], "index": r[6], "title": r[7], "body": r[8],
        "meta": json.loads(r[9]) if r[9] else None,
    }


def get_events(limit: int = 80, since_id: int = None, day: str = None) -> list:
    """Most recent events first. ``since_id`` returns only newer rows, ascending."""
    conn = _connect()
    c    = conn.cursor()
    cols = "id, ts, date, time, level, kind, index_name, title, body, meta"
    if since_id is not None:
        c.execute(f"SELECT {cols} FROM events WHERE id > ? ORDER BY id ASC LIMIT ?",
                  (since_id, limit))
        rows = c.fetchall()
    elif day:
        c.execute(f"SELECT {cols} FROM events WHERE date = ? ORDER BY id DESC LIMIT ?",
                  (day, limit))
        rows = c.fetchall()
    else:
        c.execute(f"SELECT {cols} FROM events ORDER BY id DESC LIMIT ?", (limit,))
        rows = c.fetchall()
    conn.close()
    return [_event_row(r) for r in rows]


def latest_event_id() -> int:
    conn = _connect()
    row  = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
    conn.close()
    return row[0] if row else 0


# ─────────────────────────────────────────
#  GATE LOG  (why a trade did or did not happen)
# ─────────────────────────────────────────

def log_gate(index_name: str, verdict: dict, confluence: dict = None,
             spot: float = None, state: str = "scanned"):
    """
    Record the full entry-gate verdict for one index on one scan.

    ``verdict`` is the dict from ``signal_engine.evaluate_entry`` and carries
    every check, not just the first failure, which is what lets the dashboard
    show the whole checklist.
    """
    now  = datetime.now()
    conf = confluence or {}
    conn = _connect()
    conn.execute("""
        INSERT INTO gate_log (ts, date, time, index_name, allowed, blocking, checks,
                              score, max_score, threshold, bias, bias_confirmed,
                              iv_rank, spot, state)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        now.isoformat(timespec="seconds"),
        now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), index_name,
        1 if verdict.get("allowed") else 0,
        verdict.get("blocking"),
        json.dumps(verdict.get("checks", []), default=str),
        conf.get("score"), conf.get("max_score"), conf.get("threshold"),
        conf.get("overall_bias"),
        1 if conf.get("bias_confirmed") else 0,
        conf.get("iv_rank"), spot, state,
    ))
    conn.commit()
    conn.close()


def get_latest_gate(index_name: str) -> dict:
    """The most recent gate verdict recorded for one index, or None."""
    conn = _connect()
    row = conn.execute("""
        SELECT ts, time, allowed, blocking, checks, score, max_score, threshold,
               bias, bias_confirmed, iv_rank, spot, state
        FROM gate_log WHERE index_name = ? ORDER BY id DESC LIMIT 1
    """, (index_name,)).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "ts": row[0], "time": row[1], "allowed": bool(row[2]), "blocking": row[3],
        "checks": json.loads(row[4]) if row[4] else [],
        "score": row[5], "maxScore": row[6], "threshold": row[7],
        "bias": row[8], "biasConfirmed": bool(row[9]), "ivRank": row[10],
        "spot": row[11], "state": row[12],
    }


# ─────────────────────────────────────────
#  EQUITY / PERFORMANCE
# ─────────────────────────────────────────

def record_equity(closing_capital: float, day: str = None):
    """Upsert the closing account value for a day."""
    day  = day or date.today().strftime("%Y-%m-%d")
    conn = _connect()
    row  = conn.execute("""
        SELECT COALESCE(SUM(pnl), 0), COUNT(*)
        FROM trades WHERE date = ? AND status = 'CLOSED'
    """, (day,)).fetchone()
    conn.execute("""
        INSERT INTO equity_daily (date, closing_capital, realised_pnl, trades)
        VALUES (?,?,?,?)
        ON CONFLICT(date) DO UPDATE SET
            closing_capital = excluded.closing_capital,
            realised_pnl    = excluded.realised_pnl,
            trades          = excluded.trades
    """, (day, round(float(closing_capital), 2), round(row[0] or 0, 2), row[1] or 0))
    conn.commit()
    conn.close()


def get_equity_series(limit: int = 60) -> list:
    """Oldest-first closing account value, for the equity curve."""
    conn = _connect()
    rows = conn.execute("""
        SELECT date, closing_capital, realised_pnl, trades
        FROM equity_daily ORDER BY date DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [{"date": r[0], "capital": r[1], "pnl": r[2], "trades": r[3]}
            for r in reversed(rows)]


def get_daily_pnl(limit: int = 10) -> list:
    """Oldest-first realised profit per day, for the daily bar chart."""
    conn = _connect()
    rows = conn.execute("""
        SELECT date, total_pnl, total_trades
        FROM daily_summary ORDER BY date DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [{"date": r[0], "pnl": r[1] or 0, "trades": r[2] or 0}
            for r in reversed(rows)]


def get_today_realised(day: str = None) -> tuple:
    """
    ``(realised_pnl, closed_trade_count)`` for a day, straight from the trade
    rows. The risk manager rehydrates from this on startup so a restart cannot
    reset a loss limit that was already hit.
    """
    day  = day or date.today().strftime("%Y-%m-%d")
    conn = _connect()
    row  = conn.execute("""
        SELECT COALESCE(SUM(pnl), 0), COUNT(*)
        FROM trades WHERE date = ? AND status = 'CLOSED'
    """, (day,)).fetchone()
    conn.close()
    return (round(row[0] or 0.0, 2), int(row[1] or 0))


def get_closed_trades(limit: int = 60) -> list:
    """Newest-first closed trades for the history table."""
    conn = _connect()
    rows = conn.execute("""
        SELECT id, date, time, exit_time, index_name, strategy, symbol, direction,
               entry_price, exit_price, lots, lot_size, pnl, expiry, notes, exit_reason
        FROM trades WHERE status = 'CLOSED' ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [{
        "id": r[0], "date": r[1], "entryTime": r[2], "exitTime": r[3],
        "index": r[4], "strategy": r[5], "symbol": r[6], "direction": r[7],
        "entry": r[8], "exit": r[9], "lots": r[10], "lotSize": r[11],
        "pnl": r[12], "expiry": r[13], "legs": r[14], "reason": r[15],
    } for r in rows]
