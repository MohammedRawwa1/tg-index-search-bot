import os
import sys
import json
from loguru import logger as _logger


def _collect_secrets() -> list:
    secrets = []
    for k in ("MONGO_URI", "BOT_TOKEN", "API_HASH", "API_ID", "DB_NAME"):
        v = os.getenv(k)
        if v:
            secrets.append(v)
    # also include API_CREDENTIALS raw JSON if present
    raw = os.getenv("API_CREDENTIALS")
    if raw:
        secrets.append(raw)
        try:
            parsed = json.loads(raw)
            # include nested token/hash values if present
            if isinstance(parsed, list):
                for item in parsed:
                    for key in ("api_hash", "bot_token", "api_id"):
                        if key in item and item[key]:
                            secrets.append(str(item[key]))
        except Exception:
            pass
    # dedupe and remove empties
    return [s for i, s in enumerate(dict.fromkeys(secrets)) if s]


_SECRETS = _collect_secrets()


def _mask_message(msg: str) -> str:
    out = msg
    for s in _SECRETS:
        if not s:
            continue
        out = out.replace(str(s), "[REDACTED]")
    return out


def _sink(msg):
    # msg is a loguru.message.Message object; cast to str to get rendered
    try:
        txt = str(msg)
    except Exception:
        txt = repr(msg)
    sys.stdout.write(_mask_message(txt))


def configure(level: str = "INFO"):
    _logger.remove()
    _logger.add(_sink, level=level)


configure(os.getenv("LOG_LEVEL", "INFO"))

# export logger
logger = _logger