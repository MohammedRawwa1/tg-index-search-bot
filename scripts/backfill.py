#!/usr/bin/env python3
"""Bot-driven backfill script.

Usage:
  python scripts/backfill.py [<target_chat_id>]

Reads `BOT_TOKEN` from the environment or `settings.API_CREDENTIALS`.
Requires access to the target chat (bot must be a member).
"""

import argparse
import asyncio
import os
import sys
import json
from typing import Optional
import pathlib
import sqlite3
import struct
import traceback

# Ensure project root is on sys.path so `import app` works when running
# this script directly from the `scripts/` directory.
_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx
from pyrogram import Client

# AuthKeyUnregistered can appear in different pyrogram versions; try imports defensively
try:
    from pyrogram.errors import AuthKeyUnregistered
except Exception:
    try:
        from pyrogram.errors.exceptions.unauthorized_401 import AuthKeyUnregistered
    except Exception:
        AuthKeyUnregistered = None

from app.config.settings import settings
from app.services.mongo import MongoService
from app.handlers.indexer import backfill_history


def _choose_client() -> Client:
    """Return a Pyrogram Client configured with BOT_TOKEN."""
    # Prefer SESSION_STRING or explicit API creds from environment to avoid locking local .session files
    session_string = os.getenv("SESSION_STRING") or os.getenv("TELETHON_SESSION")
    api_id_env = os.getenv("API_ID") or os.getenv("TELETHON_API_ID") or os.getenv("TELEGRAM_APP_ID")
    api_hash_env = os.getenv("API_HASH") or os.getenv("TELETHON_API_HASH") or os.getenv("TELEGRAM_API_HASH")
    if session_string or (api_id_env and api_hash_env):
        params = {"name": "backfill_user"}
        if session_string:
            params["session_string"] = session_string
        if api_id_env and api_hash_env:
            try:
                params["api_id"] = int(api_id_env)
            except Exception:
                params["api_id"] = api_id_env
            params["api_hash"] = str(api_hash_env)
        print("Using API/user session for backfill (from SESSION_STRING or API_ID/API_HASH)")
        return Client(**params)

    # Project root session detection: prefer any local session file (*.session)
    try:
        project_root = pathlib.Path(__file__).resolve().parents[1]
        # Respect an explicit SESSION_NAME env var to avoid conflicts with remote sessions
        session_name = os.getenv("SESSION_NAME")
        if session_name:
            session_file = project_root / f"{session_name}.session"
            if session_file.exists():
                api_id = os.getenv("API_ID") or os.getenv("TELETHON_API_ID") or os.getenv("TELEGRAM_APP_ID")
                api_hash = os.getenv("API_HASH") or os.getenv("TELETHON_API_HASH") or os.getenv("TELEGRAM_API_HASH")
                if not (api_id and api_hash):
                    for c in settings.API_CREDENTIALS:
                        if c.get("api_id") and c.get("api_hash"):
                            api_id = api_id or c.get("api_id")
                            api_hash = api_hash or c.get("api_hash")
                            break
                params = {"name": session_name}
                if api_id and api_hash:
                    try:
                        params["api_id"] = int(api_id)
                    except Exception:
                        params["api_id"] = api_id
                    params["api_hash"] = str(api_hash)
                print(f"Using local Pyrogram session: {session_name}")
                return Client(**params)

        # fallback: if no explicit name, prefer any local session file but only if not ambiguous
        session_files = list(project_root.glob("*.session"))
        if session_files:
            # pick the first session file that isn't a known remote default (best-effort)
            session_name = session_files[0].stem
            api_id = os.getenv("API_ID") or os.getenv("TELETHON_API_ID") or os.getenv("TELEGRAM_APP_ID")
            api_hash = os.getenv("API_HASH") or os.getenv("TELETHON_API_HASH") or os.getenv("TELEGRAM_API_HASH")
            if not (api_id and api_hash):
                for c in settings.API_CREDENTIALS:
                    if c.get("api_id") and c.get("api_hash"):
                        api_id = api_id or c.get("api_id")
                        api_hash = api_hash or c.get("api_hash")
                        break
            params = {"name": session_name}
            if api_id and api_hash:
                try:
                    params["api_id"] = int(api_id)
                except Exception:
                    params["api_id"] = api_id
                params["api_hash"] = str(api_hash)
            print(f"Using local Pyrogram session: {session_name}")
            return Client(**params)
    except Exception:
        pass

    # fallback: session string (useful when deploying)
    session_string = os.getenv("SESSION_STRING") or os.getenv("TELETHON_SESSION")
    api_id = os.getenv("API_ID") or os.getenv("TELETHON_API_ID") or os.getenv("TELEGRAM_APP_ID")
    api_hash = os.getenv("API_HASH") or os.getenv("TELETHON_API_HASH") or os.getenv("TELEGRAM_API_HASH")
    if session_string or (api_id and api_hash):
        params = {"name": "backfill_user"}
        if session_string:
            params["session_string"] = session_string
        if api_id and api_hash:
            try:
                params["api_id"] = int(api_id)
            except Exception:
                params["api_id"] = api_id
            params["api_hash"] = str(api_hash)
        print("Using API/user session for backfill")
        return Client(**params)

    # final fallback: bot token
    bot_token = os.getenv("BOT_TOKEN") or next(
        (c.get("bot_token") for c in settings.API_CREDENTIALS if c.get("bot_token")),
        None,
    )
    if bot_token:
        # include API id/hash if present
        api_id = api_id or next((c.get("api_id") for c in settings.API_CREDENTIALS if c.get("api_id")), None)
        api_hash = api_hash or next((c.get("api_hash") for c in settings.API_CREDENTIALS if c.get("api_hash")), None)
        if api_id and api_hash:
            try:
                api_id = int(api_id)
            except Exception:
                pass
            return Client("backfill_bot", api_id=api_id, api_hash=str(api_hash), bot_token=bot_token)
        return Client("backfill_bot", bot_token=bot_token)

    raise RuntimeError("No usable Telegram credentials found: set SESSION_STRING, API_ID/API_HASH, a local .session file, or BOT_TOKEN")


async def _run(target_chat_id: Optional[int] = None):
    client = _choose_client()

    # Use the client's async context manager which handles start/stop lifecycle
    # cleanly. This prevents background update-handling tasks from racing with a
    # manual stop and producing "Client has not been started yet" exceptions.
    try:
        try:
            async with client:
                try:
                    me = await client.get_me()
                except Exception:
                    # Any exceptions during start will be handled below by
                    # the specific except blocks (sqlite3, struct.error).
                    raise

                print(f"[backfill] Logged in as: @{me.username} (id={me.id})")
                print("Bot client started for backfill")

                # ---- Mongo ----
                mongo = MongoService(settings.MONGO_URI, settings.DB_NAME)
                mongo.connect()
                mongo.ensure_indexes()

                # ---- Target chat resolution ----
                env_target = os.getenv("TARGET_CHAT_ID")
                if env_target:
                    try:
                        target_chat_id = int(env_target)
                    except ValueError:
                        print("Invalid TARGET_CHAT_ID in env")
                        return

                if target_chat_id is None:
                    target_chat_id = settings.TARGET_CHAT_ID

                if target_chat_id is None:
                    print("Set TARGET_CHAT_ID in env or settings to backfill")
                    return

                # ---- Ensure bot can access chat ----
                # Try resolving chat by id first; if that fails, try username, then
                # perform an extended dialog scan to locate the chat in the session's dialogs.
                resolved = None
                try:
                    chat = await client.get_chat(target_chat_id)
                    resolved = chat
                    print(f"Resolved chat: {chat.title or chat.id}")
                except Exception as e:
                    print(f"get_chat() failed for {target_chat_id}: {e}. Trying username and dialog cache fallback...")
                    # Try treating target as username (strip @ if present)
                    try:
                        uname = str(target_chat_id)
                        if uname.startswith("@"):
                            uname = uname[1:]
                        chat = await client.get_chat(uname)
                        resolved = chat
                        print(f"Resolved chat by username: {chat.title or chat.id}")
                    except Exception:
                        # dialog scan fallback with a larger limit
                        try:
                            chat = None
                            async for d in client.get_dialogs(limit=5000):
                                try:
                                    cobj = d.chat
                                    if getattr(cobj, "id", None) == target_chat_id or getattr(cobj, "username", None) == str(target_chat_id) or (getattr(cobj, "title", "") and str(target_chat_id).lower() in getattr(cobj, "title", "").lower()):
                                        chat = cobj
                                        break
                                except Exception:
                                    continue
                        except Exception:
                            chat = None

                        if chat is None:
                            print(
                                f"❌ Cannot access chat {target_chat_id}. Make sure the provided session is a member and has access."
                            )
                            return
                        resolved = chat
                        print(f"Found chat in dialog cache, using: {chat.title or chat.id}")

                # ---- Backfill ----
                # Read runtime override options from env (set by CLI wrapper)
                try:
                    limit = int(os.getenv("BACKFILL_LIMIT", "100000"))
                except Exception:
                    limit = 100000
                dry_run = os.getenv("BACKFILL_DRY_RUN", "0") == "1"
                resume = os.getenv("BACKFILL_RESUME", "1") == "1"
                # allow limiting backfill to a specific topic/thread when requested
                thread_id_env = os.getenv("BACKFILL_THREAD_ID")
                try:
                    thread_id = int(thread_id_env) if thread_id_env else None
                except Exception:
                    thread_id = None

                stats = await backfill_history(client, mongo, chat.id, limit=limit, dry_run=dry_run, resume=resume, thread_id=thread_id)

                summary = {
                    "target": chat.id,
                    "scanned": stats.get("scanned", 0),
                    "indexed": stats.get("indexed", 0),
                    "skipped_no_media": stats.get("skipped_no_media", 0),
                    "skipped_no_filename": stats.get("skipped_no_filename", 0),
                }

                print("Backfill summary:", json.dumps(summary))

                # ---- Notify owner ----
                owner = settings.OWNER_ID
                bot_token = os.getenv("BOT_TOKEN") or next(
                    (c.get("bot_token") for c in settings.API_CREDENTIALS if c.get("bot_token")),
                    None,
                )

                if owner and bot_token:
                    tg_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                    payload = {"chat_id": int(owner), "text": f"Backfill finished for {chat.id}: {summary}"}
                    async with httpx.AsyncClient() as client_http:
                        try:
                            await client_http.post(tg_url, json=payload, timeout=10)
                        except Exception:
                            pass

                # Optionally notify the API to invalidate server-side cache.
                # Set `API_URL` (e.g. https://my-service.example) and `DIAG_SECRET`
                # in the environment to enable this. When present we'll POST to
                # /_admin/cache/invalidate with X-DIAG-TOKEN header.
                api_url = os.getenv("API_URL") or os.getenv("API_BASE_URL")
                diag_secret = os.getenv("DIAG_SECRET")
                if api_url and diag_secret and not dry_run:
                    try:
                        invalidate_url = api_url.rstrip("/") + "/_admin/cache/invalidate"
                        async with httpx.AsyncClient() as client_http:
                            await client_http.post(invalidate_url, headers={"X-DIAG-TOKEN": diag_secret}, json={"all": True}, timeout=10)
                    except Exception:
                        pass
        except sqlite3.OperationalError as e:
            if 'database is locked' in str(e).lower():
                print("ERROR: SQLite session database is locked. Another process may be using the same Pyrogram session file.")
                print("Recommendation: stop other processes using the same .session file, or set SESSION_STRING to use an exported session string.")
                return
            raise
        except struct.error as e:
            print("ERROR: Failed to read session data — the session file appears corrupt or incompatible.")
            project_root = pathlib.Path(__file__).resolve().parents[1]
            session_name = os.getenv("SESSION_NAME") or "backfill_user"
            session_file = project_root / f"{session_name}.session"
            if session_file.exists():
                print(f"Detected session file: {session_file}")
                print("Recommendation: backup and remove the session file, then re-run `scripts/create_user_session.py --export` to generate a SESSION_STRING.")
            else:
                print("No local session file detected; verify your SESSION_STRING or API_ID/API_HASH.")
            traceback.print_exc()
            return
    except Exception as exc:
        # Handle invalid/expired Pyrogram session keys (AuthKeyUnregistered) with a clearer message.
        is_auth_key_issue = False
        try:
            if AuthKeyUnregistered and isinstance(exc, AuthKeyUnregistered):
                is_auth_key_issue = True
        except Exception:
            pass
        if not is_auth_key_issue:
            # fallback: inspect exception name/message for known patterns
            name = type(exc).__name__
            if "AuthKeyUnregistered" in name or "AUTH_KEY_UNREGISTERED" in str(exc):
                is_auth_key_issue = True

        if is_auth_key_issue:
            print("ERROR: Telegram session/auth key is unregistered or invalid.")
            print("This usually means your session string or .session file is expired, revoked, or incompatible.")
            project_root = pathlib.Path(__file__).resolve().parents[1]
            session_name = os.getenv("SESSION_NAME") or "backfill_user"
            session_file = project_root / f"{session_name}.session"
            if session_file.exists():
                print(f"Detected local session file: {session_file}")
                print("Recommendation: backup and remove this .session file, then run:")
                print("  python scripts/create_user_session.py")
                print("Follow the prompts to re-login, or run with --export to get SESSION_STRING for remote deployments.")
            else:
                print("No local .session file detected; if you use SESSION_STRING, ensure it's a fresh exported session.")
                print("To create/export a session string locally, run:")
                print("  python scripts/create_user_session.py --export")
                print("Then set SESSION_STRING in your environment and re-run the backfill.")
            return

        # Unexpected top-level exception; print traceback for debugging.
        traceback.print_exc()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill Telegram chat history into Mongo index")
    parser.add_argument("target", nargs="?", help="Target chat id (optional)")
    parser.add_argument("--limit", type=int, default=100000, help="Number of messages to fetch from history")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to DB; run a dry run")
    parser.add_argument("--no-resume", action="store_true", help="Do not update resume checkpoint (index_state)")
    parser.add_argument("--thread-id", type=int, default=None, help="Only backfill messages in this topic/thread id (forum topics)")
    args = parser.parse_args()

    arg_target = None
    if args.target:
        try:
            arg_target = int(args.target)
        except ValueError:
            print("Invalid target chat id argument")
            sys.exit(1)

    # pass through options to backfill runner via environment vars
    os.environ["BACKFILL_LIMIT"] = str(args.limit)
    os.environ["BACKFILL_DRY_RUN"] = "1" if args.dry_run else "0"
    os.environ["BACKFILL_RESUME"] = "0" if args.no_resume else "1"
    if args.thread_id is not None:
        os.environ["BACKFILL_THREAD_ID"] = str(args.thread_id)

    asyncio.run(_run(arg_target))