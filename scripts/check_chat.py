# scripts/check_chat.py
import asyncio, pathlib, sys
_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path: sys.path.insert(0, str(_ROOT))
from pyrogram import Client
import os
import sqlite3
import struct
import traceback


async def main():
    session_name = os.getenv("SESSION_NAME") or "user_session"
    # Accept TELETHON_SESSION as an alternate env name for exported session strings
    session_string = os.getenv("SESSION_STRING") or os.getenv("TELETHON_SESSION")

    # Ensure local .session files are created in the project root (not the current CWD).
    # You can override with SESSION_PATH env var to provide an explicit path.
    session_path_env = os.getenv("SESSION_PATH")
    if session_path_env:
        session_target = session_path_env
    else:
        session_target = str(_ROOT / session_name)

    # Optional: allow passing API credentials via env to ensure client can start
    # Accept TELETHON_* env names as aliases for API credentials
    api_id = os.getenv("API_ID") or os.getenv("TELETHON_API_ID")
    api_hash = os.getenv("API_HASH") or os.getenv("TELETHON_API_HASH")
    kwargs = {}
    if api_id:
        try:
            kwargs["api_id"] = int(api_id)
        except Exception:
            kwargs["api_id"] = api_id
    if api_hash:
        kwargs["api_hash"] = api_hash

    # Prefer SESSION_STRING to avoid creating/locking a local SQLite session file
    if session_string:
        c = Client(session_target, session_string=session_string, **kwargs)
    else:
        c = Client(session_target, **kwargs)

    try:
        await c.start()
    except sqlite3.OperationalError as e:
        # Provide a helpful message for the common 'database is locked' case
        if 'database is locked' in str(e).lower():
            print("ERROR: SQLite session database is locked. Another process may be using the same Pyrogram session file.")
            print("Recommendations:")
            print(" - Stop other processes that might be using the same session file (e.g., your deployed service).")
            print(" - Use an exported session string instead of a file: create one with `scripts/create_user_session.py` and set `SESSION_STRING` in your environment.")
            print(" - Or set a different SESSION_NAME for this run to use a separate session file (set SESSION_NAME env var).")
            print()
            print("Full exception follows for debugging:")
            traceback.print_exc()
            return
        raise
    except struct.error as e:
        # Corrupt or incompatible session file (binary unpack failed)
        print("ERROR: Failed to read session data — the session file appears corrupt or incompatible.")
        if session_string:
            print("You provided a SESSION_STRING, but the client failed when loading session data in memory. This may indicate an incompatible Pyrogram version or malformed session string.")
        else:
            # Determine the .session file path based on the resolved target we used
            try:
                st = session_target
            except NameError:
                st = None
            session_file = None
            if st:
                p = pathlib.Path(st)
                if not str(p).endswith('.session'):
                    p = p.with_suffix('.session')
                session_file = p
            if session_file and session_file.exists():
                print(f"Detected session file: {session_file}")
                print("Recommendations:")
                print(f" - Backup and remove the session file: Rename-Item '{session_file}' '{session_file}.bak' (PowerShell)")
                print(f" - Or delete it: Remove-Item '{session_file}'")
            else:
                print("No local .session file found; ensure your SESSION_STRING is correct or re-create the session locally with scripts/create_user_session.py.")
        print()
        print("You can re-create a session and export a session string with:")
        print("  python scripts/create_user_session.py --export")
        print()
        print("Full exception follows for debugging:")
        traceback.print_exc()
        return
    print("me:", await c.get_me())
    # try variations
    ids = ["-1002347599837", -1002347599837, 2347599837]
    for ident in ids:
        try:
            print("TRY", ident)
            chat = await c.get_chat(ident)
            print("OK", chat.id, getattr(chat, "title", None))
        except Exception as e:
            print("ERR", ident, repr(e))
    # list visible dialogs (first 200) — iterate the async generator
    async for d in c.get_dialogs(limit=200):
        print("D:", d.chat.id, d.chat.title if getattr(d.chat,'title',None) else d.chat.username)
    try:
        await c.stop()
    except ConnectionError as e:
        print("Client was already terminated during stop(): ignoring.")
    except Exception:
        traceback.print_exc()

asyncio.run(main())
