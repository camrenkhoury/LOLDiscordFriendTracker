import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # Allows pure syntax checks before dependencies are installed.
    load_dotenv = None


BASE_DIR = Path(__file__).resolve().parent

if load_dotenv:
    load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _optional_int(name: str) -> int | None:
    value = _env(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer channel ID") from exc


def _env_bool(name: str, default: str = "false") -> bool:
    return _env(name, default).lower() in {"1", "true", "yes", "on"}


DISCORD_TOKEN = _env("DISCORD_TOKEN")
COMMAND_PREFIX = _env("COMMAND_PREFIX", "!")

# ==============================
# Riot API
# ==============================

RIOT_API_KEY = _env("RIOT_API_KEY")

# Routing regions (for match + account APIs): americas, asia, europe, sea.
REGION = _env("RIOT_REGION", "americas").lower()

# Platform regions (for summoner + ranked APIs): na1, euw1, kr, etc.
PLATFORM = _env("RIOT_PLATFORM", "na1").lower()

# Optional Discord channel for hourly update summaries.
TEST_CHANNEL_ID = _optional_int("TEST_CHANNEL_ID")

# ==============================
# Scheduling
# ==============================

# Daily reset hour (24h clock, local server time)
DAILY_RESET_HOUR = int(_env("DAILY_RESET_HOUR", "3"))     # 3 AM


# ==============================
# Storage
# ==============================

DATA_FILE = str(BASE_DIR / "league.json")


# ==============================
# Match Processing
# ==============================

# Number of recent matches to fetch per update
MATCH_FETCH_LIMIT = int(_env("MATCH_FETCH_LIMIT", "50"))
ENABLE_SCHEDULED_UPDATES = _env_bool("ENABLE_SCHEDULED_UPDATES", "false")
SCHEDULED_UPDATE_INTERVAL_MINUTES = int(_env("SCHEDULED_UPDATE_INTERVAL_MINUTES", "360"))

# Queue IDs (Riot constants)
QUEUE_SOLO = 420
QUEUE_FLEX = 440
QUEUE_ARAM = 450


# ==============================
# Debug / Safety
# ==============================

# Log mismatched match counts without blocking updates
ENABLE_MATCH_COUNT_SANITY_CHECK = True

# Print warnings to console
DEBUG_LOGGING = _env("DEBUG_LOGGING", "true").lower() in {"1", "true", "yes", "on"}


def missing_required_settings() -> list[str]:
    missing = []
    if not DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")
    if not RIOT_API_KEY:
        missing.append("RIOT_API_KEY")
    return missing


def validate_runtime_config() -> None:
    missing = missing_required_settings()
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(
            f"Missing required environment setting(s): {names}. "
            "Create .env from .env.example or set them in your shell."
        )
