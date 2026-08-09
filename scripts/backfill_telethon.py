#!/usr/bin/env python3
"""Telethon-based backfill script.

Iterates chat history using Telethon which exposes `reply_to.reply_to_top_id`
making topic/thread-scoped backfills reliable. Writes to Mongo using
`app.services.mongo.MongoService` and uses FileIndex.bulk_upsert_files for
efficient upserts.

Usage:
  python scripts/backfill_telethon.py <chat_id> [--thread-id THREAD_ID] [--limit N] [--dry-run]

Requires: API_ID, API_HASH (or TELETHON_API_ID/TELETHON_API_HASH)
or existing .session file.
"""

import argparse
import asyncio
import os
import sys
import json
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from telethon import TelegramClient
from telethon.tl.types import Message

from app.services.mongo import MongoService
from app.models.file_index_impl import FileIndex
from app.services.tokenizer import tokenize_filename
from app.config.settings import settings


def _choose_telethon_client():
    api_id = os.getenv("API_ID") or os.getenv("TELETHON_API_ID") or os.getenv("TELEGRAM_APP_ID")
    api_hash = os.getenv("API_HASH") or os.getenv("TELETHON_API_HASH") or os.getenv("TELEGRAM_API_HASH")
    session = os.getenv("TELETHON_SESSION") or os.getenv("SESSION_NAME") or "telethon_backfill"
    if not (api_id and api_hash):
        raise RuntimeError("API_ID and API_HASH must be set in env")
    try:
        api_id = int(api_id)
    except Exception:
        pass
    return TelegramClient(str(session), api_id, str(api_hash))


async def run_backfill(chat_id: int, thread_id: int | None, limit: int = 20000, dry_run: bool = False):
    client = _choose_telethon_client()

    mongo = MongoService(settings.MONGO_URI, settings.DB_NAME)
    try:
        mongo.connect()
        mongo.ensure_indexes()
    except Exception as exc:
        print("Mongo init failed:", exc)
        return

    file_index = FileIndex(mongo.db)

    stats = {
        "scanned": 0,
        "indexed": 0,
        "skipped_no_media": 0,
        "skipped_no_filename": 0,
        "skipped_non_video": 0,
    }

    batch = []
    BATCH_SIZE = int(os.getenv("BACKFILL_BATCH_SIZE", "500"))
    VIDEO_EXTS = {"mp4", "mkv", "ts", "webm", "mov", "avi", "mpeg", "mpg", "flv", "m4v", "wmv"}

    def _extract_filename_from_media(media, msg):
        # common direct attributes
        filename = getattr(media, "file_name", None) or getattr(media, "name", None)
        # Telethon Document often stores filename in attributes
        attrs = getattr(media, "attributes", None) or getattr(media, "attrs", None)
        if not filename and attrs:
            for a in attrs:
                fname = getattr(a, "file_name", None) or getattr(a, "fileName", None) or getattr(a, "filename", None)
                if fname:
                    filename = fname
                    break

        # try to find a filename-like token in caption/text
        if not filename:
            caption = getattr(msg, "message", None) or getattr(msg, "text", None) or getattr(msg, "caption", None)
            if caption:
                pattern = r'([\w\-\s\.]+\.(?:' + '|'.join(VIDEO_EXTS) + r'))'
                m = re.search(pattern, caption, flags=re.IGNORECASE)
                if m:
                    filename = m.group(1).strip()

        # fallback: derive a filename from mime type
        if not filename:
            mime = getattr(media, "mime_type", None) or getattr(media, "mime", None)
            ext = None
            if mime and '/' in mime:
                subtype = mime.split('/')[-1].lower()
                if subtype in VIDEO_EXTS:
                    ext = subtype
                elif subtype == 'x-matroska':
                    ext = 'mkv'
                elif subtype == 'quicktime':
                    ext = 'mov'
            if ext:
                mid = getattr(msg, "id", getattr(msg, "message_id", None))
                filename = f"{mid}.{ext}" if mid else f"media.{ext}"

        return filename

    async with client:
        print("Telethon client started")
        # Telethon's iter_messages is efficient and yields newest->oldest by default
        async for msg in client.iter_messages(chat_id, limit=limit):
            stats["scanned"] += 1
            try:
                # telethon Message: check reply_to.reply_to_top_id or message.top_id
                belongs = False
                if thread_id is None:
                    belongs = True
                else:
                    # prefer top-level id
                    rt = None
                    try:
                        if getattr(msg, "reply_to_msg_id", None):
                            # telethon exposes reply_to_msg_id but nested object may be present
                            if getattr(msg, "reply_to", None) and getattr(msg.reply_to, "reply_to_top_id", None):
                                rt = msg.reply_to.reply_to_top_id
                    except Exception:
                        rt = None
                    # also check msg.top_id attribute (older Telethon versions)
                    try:
                        if rt is None and getattr(msg, "top_id", None):
                            rt = msg.top_id
                    except Exception:
                        pass
                    # finally, some messages may have msg.reply_to_msg_id equal to thread root
                    try:
                        if rt is None and getattr(msg, "reply_to_msg_id", None):
                            rt = getattr(msg, "reply_to_msg_id")
                    except Exception:
                        pass

                    if rt is not None:
                        try:
                            if int(rt) == int(thread_id):
                                belongs = True
                        except Exception:
                            belongs = False

                if not belongs:
                    continue

                # extract media (video/document)
                media = None
                if getattr(msg, "video", None):
                    media = msg.video
                elif getattr(msg, "document", None):
                    media = msg.document
                if not media:
                    stats["skipped_no_media"] += 1
                    continue

                filename = _extract_filename_from_media(media, msg)
                if not filename:
                    stats["skipped_no_filename"] += 1
                    continue

                ext = (filename.split(".")[-1] if "." in filename else "").lower()
                if ext not in VIDEO_EXTS:
                    stats["skipped_non_video"] += 1
                    continue

                token_struct = tokenize_filename(filename)
                file_id = getattr(media, "file_id", None)
                file_size = getattr(media, "file_size", None)
                duration = getattr(media, "duration", None)
                width = getattr(media, "w", None) or getattr(media, "width", None)
                height = getattr(media, "h", None) or getattr(media, "height", None)

                doc = {
                    "chat_id": int(chat_id),
                    "message_thread_id": int(thread_id) if thread_id is not None else None,
                    "message_id": int(msg.id),
                    "filename": filename,
                    "extension": ext,
                    "title_tokens": token_struct.get("title_tokens", []),
                    "quality_tokens": token_struct.get("quality_tokens", []),
                    "codec_tokens": token_struct.get("codec_tokens", []),
                    "year": token_struct.get("year"),
                    "other_tokens": token_struct.get("other", []),
                    "timestamp": msg.date,
                    "file_id": file_id,
                    "file_size": file_size,
                    "duration": duration,
                    "width": width,
                    "height": height,
                }

                batch.append(doc)
                if len(batch) >= BATCH_SIZE:
                    if not dry_run:
                        file_index.bulk_upsert_files(batch)
                        stats["indexed"] += len(batch)
                    else:
                        stats["indexed"] += len(batch)
                    batch = []

            except Exception:
                continue

        if batch:
            if not dry_run:
                file_index.bulk_upsert_files(batch)
                stats["indexed"] += len(batch)
            else:
                stats["indexed"] += len(batch)

    print(
        f"Telethon backfill complete: scanned={stats['scanned']} indexed={stats['indexed']} "
        f"skipped_no_media={stats['skipped_no_media']} skipped_no_filename={stats['skipped_no_filename']} "
        f"skipped_non_video={stats['skipped_non_video']}"
    )


def main():
    parser = argparse.ArgumentParser(description="Telethon backfill for chat/topic")
    parser.add_argument("chat", help="Target chat id or username")
    parser.add_argument("--thread-id", type=int, default=None, help="Topic/thread id to limit to")
    parser.add_argument("--limit", type=int, default=20000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    chat = args.chat
    thread_id = args.thread_id
    limit = args.limit
    dry_run = args.dry_run

    # normalize chat id
    try:
        chat_id = int(chat)
    except Exception:
        chat_id = chat

    asyncio.run(run_backfill(chat_id, thread_id=thread_id, limit=limit, dry_run=dry_run))


if __name__ == "__main__":
    main()
