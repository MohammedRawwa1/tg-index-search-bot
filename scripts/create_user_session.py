#!/usr/bin/env python3
"""
Interactive helper to create a Pyrogram user session locally.
Requirements:
- Set environment variables `API_ID` and `API_HASH` (or TELEGRAM_APP_ID/TELEGRAM_API_HASH)
- Run inside the venv created by `scripts/venv_setup.sh`.

This will start a Pyrogram client named `user_session` and will prompt for login (phone/code) if needed.
"""
import os
import asyncio
import sys
import pathlib

# Ensure project root is on sys.path so `import app` works when running
# this script directly from the `scripts/` directory.
_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from app.bot import BotManager

async def main():
    # Allow TELETHON_* env names as aliases for API credentials
    api_id = os.getenv("API_ID") or os.getenv("TELETHON_API_ID") or os.getenv("TELEGRAM_APP_ID") or os.getenv("Telegram_APP_ID")
    api_hash = os.getenv("API_HASH") or os.getenv("TELETHON_API_HASH") or os.getenv("TELEGRAM_API_HASH") or os.getenv("Telegram_API_HASH")
    if not api_id or not api_hash:
        print("Set API_ID and API_HASH in environment before running this script.")
        return

    # CLI parsing: optional [session_name] and optional --export flag
    args = sys.argv[1:]
    export = False
    if "--export" in args:
        export = True
        args = [a for a in args if a != "--export"]

    # Allow overriding the default session name to avoid conflicts with remote sessions
    session_name = os.getenv("SESSION_NAME") or (args[0] if len(args) > 0 else "user_session")
    creds = [{"name": session_name, "api_id": int(api_id), "api_hash": api_hash}]
    mgr = BotManager(creds)
    try:
        await mgr.start_all()
        print(f"Client started. Session name: {session_name}. If this is first run, follow prompts to authorize the user session.")

        # Optionally export the session string for remote use
        if export or os.getenv("EXPORT_SESSION_STRING"):
            for c in mgr.clients:
                export_fn = getattr(c, "export_session_string", None)
                if export_fn:
                    try:
                        res = export_fn()
                        if asyncio.iscoroutine(res):
                            res = await res
                        print("\nSESSION_STRING (copy this to remote env):")
                        print(res)
                    except Exception as e:
                        print("Failed to export session string:", e)
                else:
                    print("This Pyrogram client does not support exporting session strings in the installed version.")

        print("Press Ctrl+C to stop the client once session file is created or when finished.")
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        await mgr.stop_all()

if __name__ == "__main__":
    asyncio.run(main())