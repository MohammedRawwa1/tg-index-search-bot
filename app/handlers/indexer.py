"""Indexer handlers for live and history indexing."""
import os
from pyrogram import Client
from pyrogram.types import Message

from app.services.tokenizer import tokenize_filename
from app.models.file_index_impl import FileIndex
from app.services.rate_limiter import RateLimiter


def register_indexer(client: Client, mongo):
    if mongo is None or getattr(mongo, "db", None) is None:
        from app.utils.logger import logger

        logger.warning("Skipping indexer registration: no MongoDB available")
        return

    file_index = FileIndex(mongo.db)
    limiter = RateLimiter()
    # Config: allow indexing based on env, default to video-oriented
    skip_sender_chat = os.getenv("SKIP_SENDER_CHAT", "true").lower() in ("1", "true", "yes")

    @client.on_message()
    async def _on_message(client: Client, message: Message):
        # Skip messages from bots
        try:
            if getattr(message, "from_user", None) and getattr(message.from_user, "is_bot", False):
                return
        except Exception:
            pass
        # Optionally skip posts from sender_chat (channel posts)
        try:
            if skip_sender_chat and getattr(message, "sender_chat", None):
                return
        except Exception:
            pass
        # Minimal example: prefer video then document and extract file_name
        media = None
        if getattr(message, "video", None):
            media = message.video
        elif getattr(message, "document", None):
            media = message.document
        if not media:
            return
        filename = getattr(media, "file_name", None)
        if not filename:
            return
        # capture topic/thread id for forum topics (may be None)
        thread_id = getattr(message, "message_thread_id", None)
        token_struct = tokenize_filename(filename)
        # extract available media metadata from Telegram objects
        file_id = getattr(media, "file_id", None)
        file_size = getattr(media, "file_size", None)
        duration = getattr(media, "duration", None)
        width = getattr(media, "width", None)
        height = getattr(media, "height", None)
        codec = getattr(media, "mime_type", None) or getattr(media, "mime", None)
        # attempt to capture thumbnail file_id when available
        thumbnail = None
        thumb_obj = getattr(media, "thumb", None) or getattr(media, "thumbnail", None)
        if thumb_obj:
            try:
                thumbnail = getattr(thumb_obj, "file_id", None)
            except Exception:
                try:
                    # sometimes thumb is a list
                    thumbnail = thumb_obj[0].file_id
                except Exception:
                    thumbnail = None
        added_by = None
        if getattr(message, "from_user", None):
            try:
                added_by = message.from_user.id
            except Exception:
                added_by = None

        await file_index.upsert_file(
            chat_id=message.chat.id,
            message_id=message.message_id,
            message_thread_id=thread_id,
            filename=filename,
            extension=(filename.split(".")[-1] if "." in filename else ""),
            token_struct=token_struct,
            timestamp=message.date,
            file_id=file_id,
            file_size=file_size,
            duration=duration,
            width=width,
            height=height,
            codec=codec,
            added_by=added_by,
        )
        limiter.reset()

    @client.on_edited_message()
    async def _on_edit(client: Client, message: Message):
        # Skip edits from bots
        try:
            if getattr(message, "from_user", None) and getattr(message.from_user, "is_bot", False):
                return
        except Exception:
            pass
        # Optionally skip edits from sender_chat (channel posts)
        try:
            if skip_sender_chat and getattr(message, "sender_chat", None):
                return
        except Exception:
            pass
        # Re-index edited messages (captions changed or file replaced)
        media = None
        if getattr(message, "video", None):
            media = message.video
        elif getattr(message, "document", None):
            media = message.document
        if not media:
            return
        filename = getattr(media, "file_name", None)
        if not filename:
            return
        # capture topic/thread id for forum topics (may be None)
        thread_id = getattr(message, "message_thread_id", None)
        token_struct = tokenize_filename(filename)
        file_id = getattr(media, "file_id", None)
        file_size = getattr(media, "file_size", None)
        duration = getattr(media, "duration", None)
        width = getattr(media, "width", None)
        height = getattr(media, "height", None)
        codec = getattr(media, "mime_type", None) or getattr(media, "mime", None)
        thumbnail = None
        thumb_obj = getattr(media, "thumb", None) or getattr(media, "thumbnail", None)
        if thumb_obj:
            try:
                thumbnail = getattr(thumb_obj, "file_id", None)
            except Exception:
                try:
                    thumbnail = thumb_obj[0].file_id
                except Exception:
                    thumbnail = None
        added_by = None
        if getattr(message, "from_user", None):
            try:
                added_by = message.from_user.id
            except Exception:
                added_by = None

        await file_index.upsert_file(
            chat_id=message.chat.id,
            message_id=message.message_id,
            message_thread_id=thread_id,
            filename=filename,
            extension=(filename.split(".")[-1] if "." in filename else ""),
            token_struct=token_struct,
            timestamp=message.date,
            file_id=file_id,
            file_size=file_size,
            duration=duration,
            width=width,
            height=height,
            codec=codec,
            added_by=added_by,
        )


async def backfill_history(client: Client, mongo, target_chat_id: int, limit: int = 100000, dry_run: bool = False, resume: bool = True, thread_id: int | None = None):
    """Simple history backfill that yields messages and indexes them.

    This is resumable by keeping track of last processed message id in a separate collection.
    """
    file_index = FileIndex(mongo.db)
    limiter = RateLimiter()
    last = mongo.get_last_indexed(target_chat_id)
    # collect newer messages (get_chat_history yields newest first)
    pending = []
    # Pyrogram/MTProto may not accept Bot-API style chat ids like -100123... for resolving peers
    # Normalize supergroup/channel ids by stripping the -100 prefix when calling Pyrogram.
    peer_id = target_chat_id
    try:
        if isinstance(target_chat_id, int) and str(target_chat_id).startswith("-100"):
            peer_id = int(str(target_chat_id)[4:])
    except Exception:
        peer_id = target_chat_id

    # Try to proactively resolve/get the chat to provide a clearer error if the client
    # cannot access the peer (common: bot not a member, bot can't read history before joining,
    # or using a fresh user session that hasn't met the peer yet).
    try:
        await client.get_chat(peer_id)
    except Exception:
        try:
            await client.get_chat(target_chat_id)
        except Exception as exc:
            from app.utils.logger import logger

            # As a last resort, try to locate the chat in the client's dialog cache
            # and proceed using the cached chat object. This helps when a fresh
            # session hasn't resolved access_hash but the dialog cache contains
            # the chat (observed as PEER_ID_INVALID on get_chat).
            found = None
            try:
                async for d in client.get_dialogs(limit=1000):
                    try:
                        if getattr(d.chat, "id", None) == target_chat_id or getattr(d.chat, "id", None) == peer_id:
                            found = d.chat
                            break
                    except Exception:
                        continue
            except Exception:
                found = None

            if found is not None:
                # Use the cached dialog chat object. To avoid resolve_peer/storage
                # mismatches, keep using the original `target_chat_id` (the
                # -100... style id) when calling history APIs — Pyrogram will
                # accept the chat id or the Chat object. This avoids converting
                # to a stripped numeric id which may not be present in the
                # session storage and causes `PeerIdInvalid`.
                try:
                    peer_id = target_chat_id
                except Exception:
                    pass
            else:
                logger.error(
                    "Cannot access chat {} (peer probes failed). Possible causes: bot/account not a member, "
                    "insufficient permissions, or peer id invalid for this session. Exception: {}",
                    target_chat_id,
                    exc,
                )
                return

    # honor SKIP_SENDER_CHAT for backfill selection (defined early to filter during fetch)
    try:
        skip_sender_chat = os.getenv("SKIP_SENDER_CHAT", "true").lower() in ("1", "true", "yes")
    except Exception:
        skip_sender_chat = True

    try:
        # helper to inspect raw message dict for reply_to_top_id (Telethon/raw cases)
        def _find_reply_top_id(obj):
            try:
                if isinstance(obj, dict):
                    if "reply_to_top_id" in obj:
                        return obj.get("reply_to_top_id")
                    for v in obj.values():
                        r = _find_reply_top_id(v)
                        if r is not None:
                            return r
                if isinstance(obj, list):
                    for it in obj:
                        r = _find_reply_top_id(it)
                        if r is not None:
                            return r
            except Exception:
                return None
            return None

        async for msg in client.get_chat_history(peer_id, limit=limit):
            msg_id = getattr(msg, "message_id", getattr(msg, "id", None))
            if msg_id is None:
                continue
            if msg_id <= last:
                break
            # if thread_id filter provided, skip messages not in that thread
            if thread_id is not None:
                mt = getattr(msg, "message_thread_id", None)
                if mt is None:
                    # try raw dict inspection to find reply_to_top_id
                    try:
                        raw = msg.to_dict() if hasattr(msg, "to_dict") else None
                        if raw is not None:
                            rt = _find_reply_top_id(raw)
                            try:
                                if rt is not None:
                                    mt = int(rt)
                            except Exception:
                                mt = mt
                    except Exception:
                        mt = mt
                if mt != thread_id:
                    continue
            # skip sender_chat/channel posts early to avoid wasting memory
            try:
                if skip_sender_chat and getattr(msg, "sender_chat", None):
                    continue
            except Exception:
                pass
            pending.append(msg)
    except Exception:
        # If resolving the normalized peer id fails (fresh session/storage),
        # fall back to using the original target_chat_id form.
        try:
            async for msg in client.get_chat_history(target_chat_id, limit=limit):
                msg_id = getattr(msg, "message_id", getattr(msg, "id", None))
                if msg_id is None:
                    continue
                if msg_id <= last:
                    break
                if thread_id is not None:
                    mt = getattr(msg, "message_thread_id", None)
                    if mt is None:
                        try:
                            raw = msg.to_dict() if hasattr(msg, "to_dict") else None
                            if raw is not None:
                                rt = _find_reply_top_id(raw)
                                try:
                                    if rt is not None:
                                        mt = int(rt)
                                except Exception:
                                    mt = mt
                        except Exception:
                            mt = mt
                    if mt != thread_id:
                        continue
                # skip sender_chat/channel posts early to avoid wasting memory
                try:
                    if skip_sender_chat and getattr(msg, "sender_chat", None):
                        continue
                except Exception:
                    pass
                pending.append(msg)
        except Exception:
            from app.utils.logger import logger

            logger.exception("Failed to fetch chat history for {}", target_chat_id)
            return

    # process oldest-first and collect stats with batching
    stats = {
        "scanned": 0,
        "indexed": 0,
        "skipped_no_media": 0,
        "skipped_no_filename": 0,
        "skipped_bot": 0,
        "skipped_sender_chat": 0,
    }

    batch = []
    # collect identifiers of indexed docs for targeted cache invalidation
    indexed_ids = set()
    # Only index video-like files
    VIDEO_EXTS = {"mp4", "mkv", "ts", "webm", "mov", "avi", "mpeg", "mpg", "flv", "m4v", "wmv"}

    # Larger batches to speed up indexing; tuned for bulk_write
    BATCH_SIZE = 500

    async def _flush_batch():
        nonlocal batch
        if not batch:
            return
        # perform bulk upsert (skip if dry_run)
        try:
            if not dry_run:
                file_index.bulk_upsert_files(batch)
                stats["indexed"] += len(batch)
                # record indexed identifiers for cache invalidation
                try:
                    for d in batch:
                        try:
                            cid = int(d.get("chat_id"))
                            mid = int(d.get("message_id"))
                            indexed_ids.add(f"{cid}:{mid}")
                        except Exception:
                            continue
                except Exception:
                    pass
            else:
                # in dry run mode, just count what would be indexed
                stats["indexed"] += len(batch)
        except Exception:
            # fallback: try per-item upsert when not dry_run
            for d in batch:
                try:
                    if not dry_run:
                        await file_index.upsert_file(
                            chat_id=d["chat_id"],
                            message_id=d["message_id"],
                            filename=d["filename"],
                            extension=d.get("extension", ""),
                            token_struct={
                                "title_tokens": d.get("title_tokens", []),
                                "quality_tokens": d.get("quality_tokens", []),
                                "codec_tokens": d.get("codec_tokens", []),
                                "year": d.get("year"),
                                "other": d.get("other_tokens", []),
                            },
                            timestamp=d.get("timestamp"),
                            file_id=d.get("file_id"),
                            file_size=d.get("file_size"),
                            duration=d.get("duration"),
                            width=d.get("width"),
                            height=d.get("height"),
                            codec=d.get("codec"),
                            thumbnail=d.get("thumbnail"),
                        )
                    stats["indexed"] += 1
                except Exception:
                    pass
        # update last indexed to last doc's message_id (only when not dry_run and resume enabled)
        try:
            last_msg = batch[-1]
            if not dry_run and resume:
                mongo.set_last_indexed(target_chat_id, int(last_msg["message_id"]))
        except Exception:
            pass
        batch = []

    for msg in reversed(pending):
        stats["scanned"] += 1
        msg_id = getattr(msg, "message_id", getattr(msg, "id", None))
        chat_id_val = getattr(msg.chat, "id", None) if getattr(msg, "chat", None) else None
        # Skip messages from bots so we don't index chatbot media
        try:
            if getattr(msg, "from_user", None) and getattr(msg.from_user, "is_bot", False):
                stats["skipped_bot"] += 1
                continue
        except Exception:
            pass
        # Optionally skip sender_chat/channel posts
        try:
            if skip_sender_chat and getattr(msg, "sender_chat", None):
                stats["skipped_sender_chat"] += 1
                continue
        except Exception:
            pass
        media = msg.video if getattr(msg, "video", None) else getattr(msg, "document", None)
        if not media:
            stats["skipped_no_media"] += 1
            continue
        filename = getattr(media, "file_name", None)
        if not filename:
            stats["skipped_no_filename"] += 1
            continue
        ext = (filename.split(".")[-1] if "." in filename else "").lower()
        # skip non-video extensions
        if ext not in VIDEO_EXTS:
            stats.setdefault("skipped_non_video", 0)
            stats["skipped_non_video"] += 1
            continue
        token_struct = tokenize_filename(filename)
        # capture available media metadata for bulk docs
        file_id = getattr(media, "file_id", None)
        file_size = getattr(media, "file_size", None)
        duration = getattr(media, "duration", None)
        width = getattr(media, "width", None)
        height = getattr(media, "height", None)
        codec = getattr(media, "mime_type", None) or getattr(media, "mime", None)
        thumbnail = None
        thumb_obj = getattr(media, "thumb", None) or getattr(media, "thumbnail", None)
        if thumb_obj:
            try:
                thumbnail = getattr(thumb_obj, "file_id", None)
            except Exception:
                try:
                    thumbnail = thumb_obj[0].file_id
                except Exception:
                    thumbnail = None

        doc = {
            "chat_id": int(chat_id_val) if chat_id_val is not None else None,
            "message_thread_id": int(msg.message_thread_id) if getattr(msg, "message_thread_id", None) is not None else None,
            "message_id": int(msg_id) if msg_id is not None else None,
            "filename": filename,
            "extension": (filename.split(".")[-1] if "." in filename else ""),
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
            "codec": codec,
            "thumbnail": thumbnail,
        }
        batch.append(doc)
        if len(batch) >= BATCH_SIZE:
            await _flush_batch()
        await limiter.wait()

    # flush remaining
    await _flush_batch()

    from app.utils.logger import logger
    logger.info(
        "Backfill complete for {}: scanned={} indexed={} skipped_no_media={} skipped_no_filename={} "
        "skipped_bot={} skipped_sender_chat={} skipped_non_video={}",
        target_chat_id,
        stats["scanned"],
        stats["indexed"],
        stats["skipped_no_media"],
        stats["skipped_no_filename"],
        stats.get("skipped_bot", 0),
        stats.get("skipped_sender_chat", 0),
        stats.get("skipped_non_video", 0),
    )

    # Invalidate server-side search cache (if present) after a backfill/reindex
    # so subsequent searches reflect newly indexed data. Only do this for
    # non-dry-run backfills.
    try:
        if not dry_run:
            # Import app lazily to avoid circular imports at module load time
            try:
                from app.api import app as main_app

                cache = getattr(main_app.state, "search_cache", None)
                if cache:
                    try:
                        # Prefer targeted invalidation when we have indexed ids
                        if indexed_ids:
                            try:
                                await cache.invalidate_by_file_ids(list(indexed_ids))
                            except Exception:
                                # fallback to full clear if targeted fails
                                await cache.clear()
                        else:
                            await cache.clear()
                    except Exception:
                        pass
            except Exception:
                # If importing the running app fails (e.g., backfill running in a
                # separate process), skip in-process invalidation.
                pass
    except Exception:
        pass

    return stats