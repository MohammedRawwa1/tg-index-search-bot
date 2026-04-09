#!/usr/bin/env python3
"""Check if the provided session can access a target chat.

Usage: EXPORT SESSION_STRING then run:
  python scripts/check_access.py <chat_id_or_username>

This will attempt `get_chat`, then scan dialogs (limit 5000) to find the chat.
"""
import os
import sys
import asyncio
import pathlib
from pyrogram import Client

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


async def main(target):
    # Accept TELETHON_SESSION as an alternate env name for exported session strings
    session = os.getenv("SESSION_STRING") or os.getenv("TELETHON_SESSION")
    if not session:
        print("SESSION_STRING (or TELETHON_SESSION) env not set. Set it to your exported session string.")
        return 2
    async with Client("check_access", session_string=session) as c:
        try:
            chat = await c.get_chat(target)
            print("get_chat OK:", getattr(chat, "id", None), getattr(chat, "title", None), getattr(chat, "username", None))
            return 0
        except Exception as e:
            print("get_chat failed:", e)
        # dialog scan fallback
        print("Scanning dialogs (up to 5000)...")
        found = None
        try:
            async for d in c.get_dialogs(limit=5000):
                chat = d.chat
                try:
                    if str(getattr(chat, "id", "")) == str(target) or getattr(chat, "username", "") == str(target) or (getattr(chat, "title", "") and str(target).lower() in getattr(chat, "title", "").lower()):
                        found = chat
                        break
                except Exception:
                    continue
        except Exception as e:
            print("Dialog scan failed:", e)
            return 3

        if found:
            print("Found in dialogs:", getattr(found, "id", None), getattr(found, "title", None), getattr(found, "username", None))
            return 0
        print("Chat not found in dialogs. Session may not have access.")
        return 4


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/check_access.py <chat_id_or_username>")
        sys.exit(1)
    tgt = sys.argv[1]
    code = asyncio.run(main(tgt))
    sys.exit(code)
