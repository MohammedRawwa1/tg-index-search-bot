import os
import json
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

from dotenv import load_dotenv

load_dotenv()


def _parse_api_credentials() -> List[Dict[str, Any]]:
    # Prefer a JSON array in API_CREDENTIALS. Fallback to single vars.
    raw = os.getenv("API_CREDENTIALS")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return data
        except Exception:
            pass

    # fallback to single credentials
    # Accept multiple common env names for API id/hash (some users use TELEGRAM_APP_ID/API_HASH)
    # Accept Telethon-prefixed env names as well for compatibility with user envs
    api_id = os.getenv("API_ID") or os.getenv("TELETHON_API_ID") or os.getenv("TELEGRAM_APP_ID") or os.getenv("Telegram_APP_ID")
    api_hash = os.getenv("API_HASH") or os.getenv("TELETHON_API_HASH") or os.getenv("TELEGRAM_API_HASH") or os.getenv("Telegram_API_HASH")
    bot_token = os.getenv("BOT_TOKEN")
    if api_id and api_hash:
        return [{"name": "default", "api_id": api_id, "api_hash": api_hash, "bot_token": bot_token}]

    # Accept a single BOT_TOKEN (webhook-only deployments) as a minimal credential
    # entry so webhook handlers that only need the token can operate.
    if bot_token:
        return [{"name": "default", "bot_token": bot_token}]

    return []


@dataclass
class Settings:
    API_CREDENTIALS: List[Dict[str, Any]]
    MONGO_URI: str
    DB_NAME: str
    MONGO_CONNECT_TIMEOUT_MS: int
    TARGET_CHAT_ID: Optional[int]
    LOG_LEVEL: str
    OWNER_ID: Optional[int]
    BOT_OWNER: Optional[int]
    SEARCH_COOLDOWN: int
    MAX_QUERY_LEN: int
    DELETE_COMMANDS: bool
    TELEGRAPH_DB: str
    TELEGRAPH_API_TOKEN: Optional[str]
    DIAG_SECRET: Optional[str]
    # Backfill / indexing automation
    BACKFILL_AUTO: bool
    BACKFILL_CHAT_IDS: Optional[str]
    BACKFILL_INTERVAL_SECONDS: int
    MEDIA_INDEX_FOLDER: str
    # Maximum number of inline result buttons to show per page (others appear only in text)
    MAX_INLINE_RESULTS: int
    # Maximum characters to attempt to keep in a single Telegram message (safety cap)
    MAX_MSG: int
    # Maximum number of link lines to include per page before hiding extras behind navigation
    MAX_LINKS_PER_PAGE: int
    # Public URL for webhook self-registration (set to e.g. https://your-service.onrender.com)
    PUBLIC_URL: Optional[str]


_api_creds = _parse_api_credentials()
# Support BOT_OWNER (preferred) or legacy OWNER_ID env var
_owner_env = os.getenv("BOT_OWNER") or os.getenv("OWNER_ID") or os.getenv("owner_id")

settings = Settings(
    API_CREDENTIALS=_api_creds,
    # Accept either MONGO_URI or MONGODB_URL (common env name in user .env)
    MONGO_URI=os.getenv("MONGO_URI", os.getenv("MONGODB_URL", "mongodb://localhost:27017/tg_index")),
    DB_NAME=os.getenv("DB_NAME", os.getenv("MONGODB_NAME", os.getenv("MONGO_DB_NAME", "tg_index"))),
    TARGET_CHAT_ID=int(os.getenv("TARGET_CHAT_ID", "0")) if os.getenv("TARGET_CHAT_ID") else None,
    # Mongo connection timeout in milliseconds for server selection/ping
    MONGO_CONNECT_TIMEOUT_MS=int(os.getenv("MONGO_CONNECT_TIMEOUT_MS", "10000")),
    LOG_LEVEL=os.getenv("LOG_LEVEL", "INFO"),
    OWNER_ID=int(_owner_env) if _owner_env else None,
    BOT_OWNER=int(_owner_env) if _owner_env else None,
    SEARCH_COOLDOWN=int(os.getenv("SEARCH_COOLDOWN", "3")),
    MAX_QUERY_LEN=int(os.getenv("MAX_QUERY_LEN", "64")),
    DELETE_COMMANDS=(os.getenv("DELETE_COMMANDS", "false").lower() in ("1", "true", "yes")),
    TELEGRAPH_DB=os.getenv("TELEGRAPH_DB", "course_bot"),
    TELEGRAPH_API_TOKEN=os.getenv("TELEGRAPH_API_TOKEN", None),
    DIAG_SECRET=os.getenv("DIAG_SECRET", None),
    # Backfill automation: enable automatic background indexing
    BACKFILL_AUTO=(os.getenv("BACKFILL_AUTO", "false").lower() in ("1", "true", "yes")),
    # Comma-separated list of chat ids to backfill automatically (e.g. -100123,-100456)
    BACKFILL_CHAT_IDS=os.getenv("BACKFILL_CHAT_IDS", None),
    # Interval in seconds between automatic backfill runs
    BACKFILL_INTERVAL_SECONDS=int(os.getenv("BACKFILL_INTERVAL_SECONDS", "21600")),
    # Folder to write markdown exports (best-effort; may be ephemeral on some hosts)
    MEDIA_INDEX_FOLDER=os.getenv("MEDIA_INDEX_FOLDER", "media_indexes"),
    # Maximum number of inline result buttons to show per page (others appear only in text)
    MAX_INLINE_RESULTS=int(os.getenv("MAX_INLINE_RESULTS", "8")),
    # Character budget for messages (conservative default below Telegram's hard cap)
    MAX_MSG=int(os.getenv("MAX_MSG", "4000")),
    # Max number of link lines to display per page before requiring navigation
    MAX_LINKS_PER_PAGE=int(os.getenv("MAX_LINKS_PER_PAGE", "100")),
    # Public base URL for webhook self-registration (e.g. https://my-service.onrender.com)
    # Falls back to RENDER_EXTERNAL_URL (auto-set by Render) or custom PUBLIC_URL env var
    PUBLIC_URL=os.getenv("PUBLIC_URL", os.getenv("RENDER_EXTERNAL_URL", None)),
)