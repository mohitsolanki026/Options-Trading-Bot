import os
from dotenv import load_dotenv

load_dotenv()

# --- Angel One Credentials ---
ANGEL_API_KEY    = os.getenv("ANGEL_API_KEY")
ANGEL_CLIENT_ID  = os.getenv("ANGEL_CLIENT_ID")
ANGEL_PASSWORD   = os.getenv("ANGEL_PASSWORD")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

# --- Risk Rules ---
RISK_RULES = {
    "max_daily_loss":        -5000,
    "max_per_trade_loss":    -1500,
    "max_open_positions":    3,
    "max_capital_per_trade": 0.20,
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

ACTIVE_INDICES = ["NIFTY", "BANKNIFTY"]

# Primary index for single-index operations
ACTIVE_INDEX = "NIFTY"

INDIA_VIX_SYMBOL = "India VIX"
INDIA_VIX_TOKEN  = "99926017"