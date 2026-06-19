import os
import time
from dotenv import load_dotenv

# Load env-specific file first (APP_ENV=development -> .env.development),
# then fall back to .env for anything not set.
_APP_ENV = os.getenv("APP_ENV", "").strip()
if _APP_ENV:
    load_dotenv(f".env.{_APP_ENV}", override=False)
load_dotenv()

# ── Timezone ──────────────────────────────────────────────────────────────
# The whole bot uses naive datetime.now()/the `schedule` lib, so all market-hours
# logic assumes the process clock is IST. Force it here so the bot is correct on
# any host (e.g. GCP defaults to UTC). Override with TZ in the env if needed.
TIMEZONE = os.getenv("TZ", "Asia/Kolkata")
os.environ["TZ"] = TIMEZONE
try:
    time.tzset()   # POSIX only — applies TZ to datetime.now()/localtime
except AttributeError:
    pass

# --- Angel One Credentials ---
ANGEL_API_KEY    = os.getenv("ANGEL_API_KEY")
ANGEL_CLIENT_ID  = os.getenv("ANGEL_CLIENT_ID")
ANGEL_PASSWORD   = os.getenv("ANGEL_PASSWORD")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

# --- Paper trading budget ---
# Starting (virtual) capital for the paper trader.
PAPER_CAPITAL = float(os.getenv("PAPER_CAPITAL", 500000))   # ₹5,00,000

# --- Risk Rules (all env-overridable) ---
# max_capital_per_trade is a FRACTION of capital allocated per trade.
#   0.5 of ₹5,00,000 = ₹2,50,000 max capital/margin per trade.
# Loss limits are scaled to the larger book; raise/lower via env as needed.
RISK_RULES = {
    "max_daily_loss":        float(os.getenv("MAX_DAILY_LOSS",      -25000)),
    "max_per_trade_loss":    float(os.getenv("MAX_PER_TRADE_LOSS",  -10000)),
    "max_open_positions":    int(os.getenv("MAX_OPEN_POSITIONS",         3)),
    "max_capital_per_trade": float(os.getenv("MAX_CAPITAL_PER_TRADE",  0.5)),
}

# --- Instruments ---
NIFTY_SYMBOL     = "Nifty 50"
NIFTY_TOKEN      = "99926000"

BANKNIFTY_SYMBOL = "NIFTY BANK"
BANKNIFTY_TOKEN  = "99926009"

INDIA_VIX_SYMBOL = "India VIX"
INDIA_VIX_TOKEN  = "99926017"

# --- Timing ---
MARKET_OPEN      = "09:15"
SAFE_ENTRY_START = "09:30"
MARKET_CLOSE     = "15:00"
NO_NEW_TRADES    = "15:30"

# --- LLM Provider ---
LLM_PROVIDER      = os.getenv("LLM_PROVIDER", "anthropic")
LLM_MODEL         = os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
GROQ_API_KEY      = os.getenv("GROQ_API_KEY")
GEMENI_API_KEY    = os.getenv("GEMENI_API_KEY")
OLLAMA_BASE_URL   = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL      = os.getenv("OLLAMA_MODEL", "llama3")

INDICES = {
    "NIFTY": {
        "name":         "NIFTY",
        "symbol":       "Nifty 50",
        "token":        "99926000",
        "scrip_name":   "NIFTY",
        "lot_size":     75,
        "strike_gap":   50,
        "expiry_day":   3,      # Thursday (0=Mon, 3=Thu)
    },
    "BANKNIFTY": {
        "name":         "BANKNIFTY",
        "symbol":       "NIFTY BANK",
        "token":        "99926009",
        "scrip_name":   "BANKNIFTY",
        "lot_size":     30,
        "strike_gap":   100,
        "expiry_day":   2,      # Wednesday
    },
    "FINNIFTY": {
        "name":         "FINNIFTY",
        "symbol":       "NIFTY FIN SERVICE",
        "token":        "26037",
        "scrip_name":   "FINNIFTY",
        "lot_size":     40,
        "strike_gap":   50,
        "expiry_day":   1,      # Tuesday
    },
}

# Indices to scan/trade (env-driven, comma-separated), validated against INDICES.
# The bot is fully multi-index: every index here is scanned and can hold its own
# position concurrently. There is no single "primary" index.
_env_indices = [s.strip().upper() for s in os.getenv("ACTIVE_INDICES", "NIFTY,BANKNIFTY").split(",") if s.strip()]
ACTIVE_INDICES = [i for i in _env_indices if i in INDICES] or ["NIFTY"]

INDIA_VIX_SYMBOL = "India VIX"
INDIA_VIX_TOKEN  = "99926017"