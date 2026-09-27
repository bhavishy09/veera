"""Vera — configuration (12-factor: everything from env, safe defaults).

Team identity fields are intentionally left as placeholders for the team to
fill before submission. No host/port assumptions beyond $PORT.
"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

# --- server ---
PORT = int(os.getenv("PORT", "8000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# --- identity (fill before submission) ---
TEAM_NAME = os.getenv("TEAM_NAME", "bot_champ")
TEAM_MEMBERS = os.getenv("TEAM_MEMBERS", "bhavishya katariya")
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "bhavishyakatariya954@gmail.com")
BOT_NAME = "Vera"
VERSION = "1.0.0"

# --- Gemini / LLM ---
def _parse_keys() -> list[str]:
    raw = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") or ""
    return [k.strip() for k in raw.split(",") if k.strip()]

GEMINI_API_KEYS: list[str] = _parse_keys()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
COMPOSER_TIMEOUT_S = float(os.getenv("COMPOSER_TIMEOUT_S", "14"))     # per attempt (fail fast in 14s)
COMPOSER_BUDGET_S = float(os.getenv("COMPOSER_BUDGET_S", "20"))       # whole ladder ceiling
COMPOSER_RETRY_BACKOFF_S = float(os.getenv("COMPOSER_RETRY_BACKOFF_S", "1.5"))
DISABLE_LLM = os.getenv("DISABLE_LLM", "0") in ("1", "true", "yes")
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.6"))
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "4096"))

# --- Layer 1 tuning (Decision Quality knobs) ---
SILENCE_BAR = float(os.getenv("SILENCE_BAR", "0.35"))          # below this score -> send nothing
MAX_ACTIONS_PER_TICK = int(os.getenv("MAX_ACTIONS_PER_TICK", "2"))
MAX_SENDS_PER_MERCHANT_WINDOW = int(os.getenv("MAX_SENDS_PER_MERCHANT_WINDOW", "1"))
MERCHANT_WINDOW_S = float(os.getenv("MERCHANT_WINDOW_S", "3600"))
URGENCY_WEIGHTS = {"high": 1.0, "medium": 0.7, "low": 0.30}    # substitutes base when payload carries urgency
RECENCY_HALF_LIFE_S = float(os.getenv("RECENCY_HALF_LIFE_S", str(6 * 3600)))
STALE_AFTER_S = float(os.getenv("STALE_AFTER_S", str(2 * 3600)))   # unshot trigger past this age: why-now has decayed

# --- conversation state machine ---
AUTO_REPLY_THRESHOLD = int(os.getenv("AUTO_REPLY_THRESHOLD", "2"))   # consecutive machine-like acks
HOSTILITY_THRESHOLD = int(os.getenv("HOSTILITY_THRESHOLD", "1"))

# --- LLM circuit breaker ---
BREAKER_THRESHOLD = int(os.getenv("BREAKER_THRESHOLD", "5"))   # consecutive failures before trip
BREAKER_COOLDOWN_S = float(os.getenv("BREAKER_COOLDOWN_S", "5")) # brief 5s cooldown so manual user testing always re-engages LLM

# --- grounding / anti-penalty validators ---
MAX_BODY_CHARS = int(os.getenv("MAX_BODY_CHARS", "900"))
URL_RE_STR = r"(https?://|www\.|bit\.ly|tinyurl|wa\.me|t\.me)"
