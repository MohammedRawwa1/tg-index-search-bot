from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse, PlainTextResponse, StreamingResponse, HTMLResponse, Response
import json
from fastapi.staticfiles import StaticFiles
import os
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import OperationFailure
import certifi
import ssl
from app.config.settings import settings
from app.services.tokenizer import tokenize_query
import sys
import httpx
import html
import re
from app.utils.logger import logger
from app.utils.helpers import unescape_for_plain_text, render_paginated_page, md_to_plain_text
import asyncio
from asyncio.subprocess import PIPE
import pathlib
import re
from datetime import datetime, timedelta
import uuid
import urllib.parse

# Enable per-line enrichment debug logging when set (ENRICH_DEBUG=1|true|yes)
ENRICH_DEBUG = os.getenv("ENRICH_DEBUG", "").lower() in ("1", "true", "yes")


async def _reject_if_not_owner(token: str, chat_id: int) -> bool:
    """
    Check if the sender (by chat_id) is the configured bot owner.

    If an owner is configured and the sender is not the owner, send
    "Unauthorized" and return True so the caller can return early.
    If no owner is configured, or the sender is the owner, return False.
    """
    try:
        owner = settings.BOT_OWNER or settings.OWNER_ID
        if owner and int(owner) != int(chat_id):
            await _send_tg(token, chat_id, "Unauthorized")
            return True
    except Exception:
        pass
    return False


async def _reject_callback_if_not_owner(token: str, cq: dict) -> bool:
    """
    Check if the callback sender (by from.id) is the configured bot owner.

    Uses _answer_callback to reply with "Unauthorized" (show_alert=True)
    and returns True so the caller can return early.
    """
    try:
        from_id = cq.get("from", {}).get("id")
        owner = settings.BOT_OWNER or settings.OWNER_ID
        if owner and int(from_id) != int(owner):
            try:
                await _answer_callback(token, cq.get("id"), text="Unauthorized", show_alert=True)
            except Exception:
                pass
            return True
    except Exception:
        pass
    return False


def _escape_markdown(text: str) -> str:
    """Escape Telegram Markdown v1 safely."""
    if not text:
        return ""
    return re.sub(r'([*_`\[\]])', r"\\\1", str(text))

def _escape_url(url: str) -> str:
    """Escape parentheses and spaces in URLs to avoid breaking Markdown links."""
    if not url:
        return ""
    return url.replace("(", "%28").replace(")", "%29").replace(" ", "%20")


def md_to_markdown(raw_text: str) -> str:
    """Convert [Title](URL) into safe Telegram Markdown."""
    if not raw_text:
        return ""

    pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    out = []
    last = 0

    for m in pattern.finditer(raw_text):
        start, end = m.span()

        # normal text
        out.append(_escape_markdown(raw_text[last:start]))

        label = _escape_markdown(m.group(1))
        url = _escape_url(m.group(2))

        out.append(f"[{label}]({url})")
        last = end

    out.append(_escape_markdown(raw_text[last:]))

    return "".join(out)

from pyrogram import Client as TGClient
from app.services.mongo import MongoService
from app.handlers.indexer import backfill_history
from app.models.file_index_impl import (
    TITLE_WEIGHT,
    QUALITY_WEIGHT,
    CODEC_WEIGHT,
    YEAR_WEIGHT,
    PREFIX_BOOST,
    TRIGRAM_WEIGHT,
    FILENAME_MATCH,
    FNAME_LEN_PENALTY_DIV,
)

import re
from bson import ObjectId
import uuid

app = FastAPI(title="TG File Index API")

# mount static for favicon
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


def _ensure_internal_page_store():
    """Ensure there is an `internal_page_store` on `app.state`.

    If startup wiring did not initialize a store (e.g., Mongo unavailable),
    create an in-memory fallback and attach it to `app.state` so GUI pages
    can be created at runtime.
    """
    store = getattr(app.state, "internal_page_store", None)
    if store:
        return store
    try:
        import importlib

        mod = importlib.import_module("app.services.internal_pages")
        cls = getattr(mod, "InMemoryInternalPageStore", None)
        if cls:
            store = cls()
            app.state.internal_page_store = store
            logger.info("Initialized InMemoryInternalPageStore at runtime")
            return store
        else:
            logger.error("InMemoryInternalPageStore class not found in module")
            return None
    except Exception:
        logger.exception("Failed to initialize InMemoryInternalPageStore at runtime")
        return None


def _get_db():
    uri = settings.MONGO_URI
    client = AsyncIOMotorClient(uri)
    db = client[settings.DB_NAME]
    return db


@app.on_event("startup")
async def startup():
    # Try to establish a working MongoDB connection during startup.
    uri = settings.MONGO_URI
    if not uri:
        logger.warning("No MONGO_URI configured; running without DB")
        app.state.db = None
        return

    # Create client with a short selection timeout so startup fails fast when DB unreachable
    def _uri_uses_tls(uri: str) -> bool:
        u = uri.lower()
        if u.startswith("mongodb+srv://"):
            return True
        if "tls=true" in u or "ssl=true" in u:
            return True
        if "mongodb.net" in u:
            return True
        return False

    ms = int(getattr(settings, "MONGO_CONNECT_TIMEOUT_MS", 2000) or 2000)

    if _uri_uses_tls(uri):
        # Use certifi CA bundle for TLS verification. Avoid passing an
        # ssl.SSLContext directly because some pymongo versions raise
        # "Unknown option ssl_context" when it's provided.
        ca = certifi.where()
        client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=ms, connectTimeoutMS=ms, tls=True, tlsCAFile=ca)
    else:
        client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=ms, connectTimeoutMS=ms)
    try:
        # attempt a lightweight ping to verify connectivity
        await client.admin.command("ping")
        app.state.db = client[settings.DB_NAME]
        # keep client accessible for shutdown (store client separately)
        app.state.mongo_client = client
        logger.info("Connected to MongoDB")
        # Ensure a compound index to support server-side sorting by
        # (message_thread_id, timestamp) within a chat. Without this,
        # large sorts can exceed MongoDB's in-memory sort limit and fail
        # on hosted providers that disallow external sorting.
        try:
            await app.state.db.get_collection("files").create_index(
                [("chat_id", 1), ("message_thread_id", 1), ("timestamp", 1)],
                background=True,
            )
            logger.info("Ensured compound index files(chat_id,message_thread_id,timestamp)")
        except Exception:
            logger.exception("Failed to create compound index on files collection")
            # best-effort: do not fail startup if index creation fails
            pass
        # Ensure indexes that speed up eager/normalized lookups
        try:
            try:
                await app.state.db.get_collection("files").create_index([("filename_norm", 1)], background=True)
                logger.info("Ensured index files(filename_norm)")
            except Exception:
                logger.exception("Failed to create index files(filename_norm)")
            try:
                # multikey index on trigrams array to accelerate trigram candidate queries
                await app.state.db.get_collection("files").create_index([("trigrams", 1)], background=True)
                logger.info("Ensured index files(trigrams)")
            except Exception:
                logger.exception("Failed to create index files(trigrams)")
            try:
                # text index on search_text used by $text queries
                await app.state.db.get_collection("files").create_index([("search_text", "text")], background=True)
                logger.info("Ensured text index files(search_text)")
            except OperationFailure as of:
                if of.code == 85:
                    logger.warning("Text index already exists (code 85): {} — using existing index", of)
                else:
                    logger.warning("Failed to create text index files(search_text): code={} {}", of.code, of)
            except Exception:
                logger.exception("Failed to create text index files(search_text)")
        except Exception:
            # ignore index creation errors (best-effort)
            pass
        # Initialize optional server-side search cache (in-memory + Mongo persistence)
        try:
            from app.services.cache import AsyncSearchCache

            cache_db = getattr(settings, "TELEGRAPH_DB", None) or getattr(settings, "DB_NAME", None)
            app.state.search_cache = AsyncSearchCache(app.state.mongo_client, db_name=cache_db, max_entries=getattr(settings, "SEARCH_CACHE_MAX_ENTRIES", 1024), default_ttl=getattr(settings, "SEARCH_CACHE_TTL", 3600))
            try:
                # ensure Mongo TTL/indexes (best-effort)
                await app.state.search_cache.ensure_indexes()
            except Exception:
                pass
        except Exception:
            app.state.search_cache = None
        # Initialize internal page store: prefer Mongo-backed store, fall back to in-memory
        try:
            from app.services.internal_pages import InternalPageStore, InMemoryInternalPageStore

            if getattr(app.state, "mongo_client", None):
                try:
                    app.state.internal_page_store = InternalPageStore(app.state.mongo_client)
                except Exception:
                    app.state.internal_page_store = InMemoryInternalPageStore()
            else:
                app.state.internal_page_store = InMemoryInternalPageStore()
        except Exception:
            app.state.internal_page_store = None
    except Exception as exc:
        logger.warning("MongoDB not reachable at startup, continuing without DB: {}", exc)
        try:
            client.close()
        except Exception:
            pass
        app.state.db = None

    # ── Auto webhook registration ──
    # For each configured bot token, register the webhook URL with Telegram
    # so updates are delivered to this service. This prevents issues where
    # a bot's webhook was previously set to a different URL.
    public_url = getattr(settings, "PUBLIC_URL", None)
    if not public_url:
        public_url = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if public_url:
        for cred in (settings.API_CREDENTIALS or []):
            bot_token = cred.get("bot_token")
            if not bot_token:
                continue
            webhook_url = f"{public_url.rstrip('/')}/webhook/{bot_token}"
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"https://api.telegram.org/bot{bot_token}/setWebhook",
                        params={"url": webhook_url},
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("ok"):
                            logger.info("Webhook registered for bot: url={} description={}", webhook_url, data.get("description"))
                        else:
                            logger.warning("Webhook registration returned error for bot: {}", data)
                    else:
                        logger.warning("Webhook registration HTTP {} for bot", resp.status_code)
            except Exception:
                logger.exception("Failed to register webhook for bot token ending with {}...", bot_token[-8:] if len(bot_token) > 8 else bot_token)
    else:
        logger.warning("PUBLIC_URL not set — skipping auto webhook registration")

    # Automatic background backfill worker (optional)
    if getattr(settings, "BACKFILL_AUTO", False):
        # derive chat list
        chat_list = []
        if getattr(settings, "BACKFILL_CHAT_IDS", None):
            try:
                chat_list = [int(x.strip()) for x in settings.BACKFILL_CHAT_IDS.split(",") if x.strip()]
            except Exception:
                chat_list = []
        elif getattr(settings, "TARGET_CHAT_ID", None):
            chat_list = [settings.TARGET_CHAT_ID]

        if chat_list:
            async def _generate_and_persist_md(chat_id: int):
                # Use the async Motor DB stored in app.state for reads and store markdown to a collection
                db = getattr(app.state, "db", None)
                if db is None:
                    return
                coll = db.get_collection("files")
                # Use an aggregation with allowDiskUse to permit server-side external
                # sorting when the matched set is large. This avoids the
                # "Sort exceeded memory limit" error on hosted MongoDB where
                # in-memory sorts are restricted.
                try:
                    pipeline = [
                        {"$match": {"chat_id": chat_id}},
                        {"$sort": {"message_thread_id": 1, "timestamp": 1}},
                    ]
                    docs = await coll.aggregate(pipeline, allowDiskUse=True).to_list(length=100000)
                except Exception:
                    logger.exception("Aggregation with allowDiskUse failed; falling back to cursor sort")
                    docs = await coll.find({"chat_id": chat_id}).sort([("message_thread_id", 1), ("timestamp", 1)]).to_list(length=100000)
                groups = {}
                for d in docs:
                    tid = d.get("message_thread_id")
                    groups.setdefault(tid, []).append(d)
                base = str(chat_id)
                if base.startswith("-100"):
                    base = base[4:]
                else:
                    base = base.lstrip("-")
                lines = [f"# Media index for chat {chat_id}\n"]
                for tid in sorted(groups.keys(), key=lambda x: (x is None, x)):
                    heading = f"## Topic {tid}" if tid is not None else "## No topic"
                    lines.append(heading)
                    for doc in groups[tid]:
                        fname = doc.get("filename") or "-"
                        mid = int(doc.get("message_id"))
                        url = f"https://t.me/c/{base}/{mid}"
                        meta = []
                        if doc.get("duration"):
                            meta.append(f"{int(doc.get('duration'))}s")
                        if doc.get("file_size"):
                            try:
                                mb = int(doc.get("file_size")) / (1024 * 1024)
                                meta.append(f"{mb:.1f}MB")
                            except Exception:
                                pass
                        meta_text = f" ({', '.join(meta)})" if meta else ""
                        lines.append(f"- [{fname}]({url}){meta_text}")
                    lines.append("")
                md_text = "\n".join(lines)
                # persist into MongoDB collection `media_indexes`
                try:
                    await db.get_collection("media_indexes").update_one({"chat_id": int(chat_id)}, {"$set": {"markdown": md_text, "updated_at": datetime.utcnow()}}, upsert=True)
                except Exception:
                    pass
                # write to disk (best-effort)
                try:
                    folder = settings.MEDIA_INDEX_FOLDER
                    pathlib.Path(folder).mkdir(parents=True, exist_ok=True)
                    out = pathlib.Path(folder) / f"{chat_id}.md"
                    out.write_text(md_text, encoding="utf-8")
                except Exception:
                    pass

            async def _auto_backfill_worker():
                # Build a pyrogram client from available credentials (first usable)
                creds = settings.API_CREDENTIALS or []
                pg_client = None
                for c in creds:
                    api_id = c.get("api_id")
                    api_hash = c.get("api_hash")
                    bot_token = c.get("bot_token")
                    try:
                        if api_id and api_hash:
                            pg_client = TGClient("backfill_remote", api_id=int(api_id), api_hash=str(api_hash), bot_token=bot_token if bot_token else None)
                            break
                        if bot_token:
                            pg_client = TGClient("backfill_remote", bot_token=bot_token)
                            break
                    except Exception:
                        continue

                # fallback: try BOT_TOKEN env
                if pg_client is None:
                    bt = os.getenv("BOT_TOKEN")
                    if bt:
                        pg_client = TGClient("backfill_remote", bot_token=bt)

                if pg_client is None:
                    logger.warning("Auto backfill enabled but no Pyrogram credentials found; skipping auto backfill")
                    return

                # connect synchronous MongoService for backfill operations
                mongo_sync = MongoService(settings.MONGO_URI, settings.DB_NAME)
                try:
                    mongo_sync.connect()
                    mongo_sync.ensure_indexes()
                except Exception:
                    logger.exception("Auto backfill: failed to initialise MongoService")
                    return

                try:
                    await pg_client.start()
                except Exception:
                    logger.exception("Auto backfill: failed to start Pyrogram client")
                    try:
                        await pg_client.stop()
                    except Exception:
                        pass
                    return

                # run loop
                interval = int(getattr(settings, "BACKFILL_INTERVAL_SECONDS", 21600) or 21600)
                try:
                    while True:
                        for chat_id in chat_list:
                            try:
                                logger.info("Auto backfill: processing chat {}", chat_id)
                                await backfill_history(pg_client, mongo_sync, chat_id)
                                # regenerate markdown index and persist
                                try:
                                    await _generate_and_persist_md(chat_id)
                                except Exception:
                                    logger.exception("Failed to persist media index for {}", chat_id)
                            except Exception:
                                logger.exception("Auto backfill: error processing chat {}", chat_id)
                        await asyncio.sleep(interval)
                finally:
                    try:
                        await pg_client.stop()
                    except Exception:
                        pass

            # store task for shutdown handling
            try:
                task = asyncio.create_task(_auto_backfill_worker())
                app.state.backfill_task = task
            except Exception:
                logger.exception("Failed to start auto backfill task")


@app.on_event("shutdown")
async def shutdown():
    mongo_client = getattr(app.state, "mongo_client", None)
    if mongo_client:
        try:
            mongo_client.close()
        except Exception:
            pass
    # stop background backfill task if running
    backfill_task = getattr(app.state, "backfill_task", None)
    if backfill_task:
        try:
            backfill_task.cancel()
            await backfill_task
        except Exception:
            pass


@app.get("/favicon.ico")
async def favicon():
    path = os.path.join(os.path.dirname(__file__), "static", "favicon.ico")
    if os.path.exists(path):
        return FileResponse(path)
    raise HTTPException(status_code=404)


@app.get("/")
async def root_get():
    return {"ok": True, "service": "tg-index-search-bot"}


@app.head("/")
async def root_head():
    return Response(status_code=200)


@app.post("/")
async def root_post(payload: dict = None):
    # If Telegram sends updates to the root path (no token in URL),
    # forward to the webhook handler using the configured bot token
    # when available. Otherwise, acknowledge.
    logger.debug("root_post received payload keys: {}", list(payload.keys()) if isinstance(payload, dict) else type(payload))
    if payload and ("message" in payload or "edited_message" in payload):
        # try to find a configured bot token
        token = None
        for c in settings.API_CREDENTIALS:
            if c.get("bot_token"):
                token = c.get("bot_token")
                break
        if token:
            try:
                logger.info("Forwarding root POST update to webhook handler using configured bot token (background)")
                # schedule the webhook handler in background and return immediately
                try:
                    asyncio.create_task(telegram_webhook(token, payload))
                except Exception:
                    # fallback: call without scheduling if loop unavailable
                    await telegram_webhook(token, payload)
                return {"ok": True}
            except Exception as exc:
                logger.exception("Error forwarding root POST to webhook: {}", exc)
                # swallow errors to ensure Telegram gets 200
                return {"ok": True}
    # Basic acknowledgement for other POSTs
    return {"ok": True}


@app.post("/webhook/{token}")
@app.post("/webhook/{token}/")
async def telegram_webhook_with_slash(token: str, update: dict):
    """Alias: accept /webhook/<token>/ (trailing slash) as well."""
    return await telegram_webhook(token, update)


# Compatibility endpoints: accept webhook POSTs at the token root path
# (e.g. https://host/<BOT_TOKEN>/). Many users set the webhook URL to
# the raw bot token path instead of /webhook/<token>. Accept both with and
# without trailing slash and forward to the canonical handler.
@app.post("/{token}")
@app.post("/{token}/")
async def telegram_webhook_token_root(token: str, update: dict):
    # Only handle obvious bot-token-like paths to avoid capturing other routes
    configured_tokens = [c.get("bot_token") for c in settings.API_CREDENTIALS if c.get("bot_token")]
    # Accept when token contains a colon (typical bot token format) or matches configured token
    if ":" not in token and token not in configured_tokens:
        return JSONResponse(status_code=404, content={"ok": False, "error": "unknown token"})
    # Forward to canonical handler
    return await telegram_webhook(token, update)


# Fallback: catch any POST and attempt to extract a bot token from the raw path
# This helps with percent-encoded tokens (e.g. colon encoded as %3A) or unusual
# gateway rewrites where the token path isn't matched by the explicit route.
@app.post("/{full_path:path}")
async def telegram_webhook_fallback(full_path: str, request: Request):
    raw = request.scope.get("raw_path")
    if raw:
        try:
            # raw_path is bytes; decode using latin-1 to preserve byte values
            raw_path = raw.decode("latin-1")
        except Exception:
            raw_path = request.url.path
    else:
        raw_path = request.url.path

    # split segments and inspect each for a bot token-looking value
    segments = [s for s in raw_path.strip("/").split("/") if s]
    configured_tokens = [c.get("bot_token") for c in settings.API_CREDENTIALS if c.get("bot_token")]

    for seg in reversed(segments):
        seg_dec = urllib.parse.unquote(seg)
        if ":" in seg_dec or seg_dec in configured_tokens:
            try:
                body = await request.json()
            except Exception:
                try:
                    raw_body = await request.body()
                    body = json.loads(raw_body.decode("utf-8", errors="ignore") if isinstance(raw_body, (bytes, bytearray)) else raw_body)
                except Exception:
                    body = {}
            return await telegram_webhook(seg_dec, body)

    return JSONResponse(status_code=404, content={"ok": False, "error": "unknown token"})
async def telegram_webhook(token: str, update: dict):
    """Process Telegram Bot API webhook updates for configured bot tokens.

    Supports a minimal subset: responds to `/search <query>` by running the
    same search logic and sending a message via the Bot HTTP API.
    """
    # validate token exists in configured credentials
    creds = [c for c in settings.API_CREDENTIALS if c.get("bot_token") == token]
    if not creds:
        logger.warning("Received webhook for unknown token")
        return JSONResponse(status_code=404, content={"ok": False, "error": "unknown token"})

    # callback_query handling: support internal page navigation (IP|<page_id>)
    try:
        if update.get("callback_query"):
            cq = update.get("callback_query")
            data = cq.get("data") or ""
            logger.info("webhook callback received data={} chat={}", data, cq.get("message", {}).get("chat", {}).get("id"))
            # quick responder for noop (empty data)
            if not data:
                try:
                    await _answer_callback(token, cq.get("id"), text="")
                except Exception:
                    pass
                return JSONResponse(status_code=200, content={"ok": True})

            # explicit noop callback (used as a page indicator button) should
            # be answered to clear the loading spinner but otherwise ignored.
            if data == "noop":
                try:
                    await _answer_callback(token, cq.get("id"), text="")
                except Exception:
                    pass
                return JSONResponse(status_code=200, content={"ok": True})

            # support internal page navigation callbacks like "IP|<page_id>"
            if data.startswith("IP|"):
                page_id = data.split("|", 1)[1]
                try:
                    store = _ensure_internal_page_store()
                    if not store:
                        await _answer_callback(token, cq.get("id"), text="Page not found")
                        return JSONResponse(status_code=200, content={"ok": True})

                    page = await store.get_page(page_id)
                    if not page:
                        await _answer_callback(token, cq.get("id"), text="Page not found")
                        return JSONResponse(status_code=200, content={"ok": True})
                    try:
                        logger.debug("webhook: fetched page id={} keys={}", page_id, list(page.keys()) if isinstance(page, dict) else None)
                        logger.debug("webhook: page preview={}", (page.get("content")[:200] if page.get("content") else None))
                        logger.debug("webhook: page query={} total_results={}", page.get("query"), page.get("total_results"))
                    except Exception:
                        pass

                    # Render stored page using shared helper (builds body + keyboard
                    # and applies truncation rules). Fallback to raw conversion on error.
                    try:
                        text_to_send, kb_rows = render_paginated_page(page, query_override=page.get("query"))
                    except Exception:
                        try:
                            page_query = page.get("query") or ""
                            content_md = md_to_markdown(page.get("content") or "")
                            header = f"*Search:* {_escape_markdown(page_query)} — {page.get('total_results') or 0} results\n\n" if page_query else ""
                            text_to_send = header + (content_md or "")
                            # minimal keyboard: show parts if available
                            gp = page.get("group_pages") or []
                            kb_rows = []
                            if gp:
                                row = []
                                for p in gp[:8]:
                                    pid = p.get("page_id") if isinstance(p, dict) else p
                                    row.append({"text": str(len(kb_rows) + len(row) + 1), "callback_data": f"IP|{pid}"})
                                if row:
                                    kb_rows.append(row)
                        except Exception:
                            text_to_send = page.get("content") or ""
                            kb_rows = []

                    # If the rendered page is too large for a single Telegram
                    # message and the callback originated from a chat message
                    # (not an inline_message_id), send the full content as
                    # chunked messages to the chat instead of attempting to
                    # edit the original message in-place. This preserves the
                    # full result set while avoiding editMessageText size
                    # limits.
                    try:
                        MAX_MSG_CFG = getattr(settings, "MAX_MSG", 4000)
                    except Exception:
                        MAX_MSG_CFG = 4000
                    try:
                        msg = cq.get("message")
                        chat_obj = msg.get("chat") if msg else None
                    except Exception:
                        chat_obj = None

                    if chat_obj and chat_obj.get("id") and len((text_to_send or "")) > MAX_MSG_CFG:
                        try:
                            # Send full Markdown content as separate chat messages
                            chat_id_target = chat_obj.get("id")
                            # Send each saved part as its own Markdown chunked message
                            try:
                                total_parts_gp = len(gp)
                                for pi, part in enumerate(gp, start=1):
                                    part_lines = (part.get("content") or "").splitlines()
                                    part_header = f"{header} — part {pi}/{total_parts_gp}"
                                    rm = reply_markup if pi == 1 else None
                                    await _send_markdown_full(token, chat_id_target, part_header, part_lines, reply_markup=rm)
                                return
                            except Exception:
                                logger.exception("api_search: failed while streaming saved parts per-page")
                            except Exception:
                                pass
                            return JSONResponse(status_code=200, content={"ok": True})
                        except Exception:
                            logger.exception("callback: failed to send full page chunks, will attempt edit fallback")

                    # edit the message in place
                    try:
                        edit_url = f"https://api.telegram.org/bot{token}/editMessageText"
                        payload = {"text": text_to_send, "parse_mode": "Markdown"}
                        # If message exists, target it; otherwise use inline_message_id
                        msg = cq.get("message")
                        if msg and msg.get("chat"):
                            payload["chat_id"] = msg.get("chat", {}).get("id")
                            payload["message_id"] = msg.get("message_id")
                        else:
                            imid = cq.get("inline_message_id")
                            if imid:
                                payload["inline_message_id"] = imid

                        if kb_rows:
                            payload["reply_markup"] = {"inline_keyboard": kb_rows}
                        async with httpx.AsyncClient() as client:
                            try:
                                # Log the outgoing payload (masked by logger sink)
                                try:
                                    logger.debug("callback editMessageText request payload: {}", json.dumps(payload, ensure_ascii=False)[:2000])
                                except Exception:
                                    logger.debug("callback editMessageText request payload (unserializable)")
                                resp = await client.post(edit_url, json=payload, timeout=10)
                            except Exception:
                                resp = None
                        try:
                            if resp is None:
                                logger.exception("callback edit request failed: no response")
                            else:
                                # Log full response body for diagnosis (truncated)
                                try:
                                    body_text = resp.text or ""
                                except Exception:
                                    body_text = ""
                                logger.info("callback editMessageText response: status={} body={}", resp.status_code, body_text[:800])
                                if resp.status_code == 200:
                                    pass
                                else:
                                    # Log edit response details for diagnosis
                                    try:
                                        logger.debug("callback editMessageText response: status={} body={} request_payload={}", resp.status_code, body_text[:2000], json.dumps(payload, ensure_ascii=False)[:2000])
                                    except Exception:
                                        logger.debug("callback editMessageText response: status={} body={}", resp.status_code, body_text[:2000])

                                    if payload.get("parse_mode") and resp.status_code == 400 and (
                                        "can't parse entities" in body_text.lower() or "parse entities" in body_text.lower()
                                    ):
                                        try:
                                            raw_text = payload.get("text", "")
                                            # First try converting Markdown to HTML and re-send as HTML
                                            try:
                                                from app.utils.helpers import md_to_html

                                                try:
                                                    html_text = md_to_html(raw_text, one_per_line=True)
                                                except Exception:
                                                    html_text = None
                                            except Exception:
                                                html_text = None

                                            if html_text:
                                                fallback_html = {}
                                                if "chat_id" in payload:
                                                    fallback_html["chat_id"] = payload.get("chat_id")
                                                if "message_id" in payload:
                                                    fallback_html["message_id"] = payload.get("message_id")
                                                if "inline_message_id" in payload:
                                                    fallback_html["inline_message_id"] = payload.get("inline_message_id")
                                                fallback_html["text"] = html_text
                                                fallback_html["parse_mode"] = "HTML"
                                                if "reply_markup" in payload:
                                                    fallback_html["reply_markup"] = payload.get("reply_markup")
                                                try:
                                                    try:
                                                        logger.debug("callback editMessageText HTML fallback payload: {}", json.dumps(fallback_html, ensure_ascii=False)[:2000])
                                                    except Exception:
                                                        logger.debug("callback editMessageText HTML fallback payload (unserializable)")
                                                    async with httpx.AsyncClient() as client2:
                                                        resp2 = await client2.post(edit_url, json=fallback_html, timeout=10)
                                                    try:
                                                        logger.debug("callback editMessageText HTML fallback response: status={} body={}", resp2.status_code, (resp2.text or "")[:2000])
                                                    except Exception:
                                                        logger.debug("callback editMessageText HTML fallback response logged")
                                                    if resp2 is not None and resp2.status_code == 200:
                                                        # success
                                                        pass
                                                    else:
                                                        # Fall back to plain-text label-only edit
                                                        raise Exception("HTML fallback failed")
                                                except Exception:
                                                    # proceed to plain-text fallback below
                                                    raise
                                            # If HTML fallback not available or failed, send plain-text labels
                                            try:
                                                unescaped = md_to_plain_text(raw_text)
                                            except Exception:
                                                unescaped = unescape_for_plain_text(raw_text)
                                        except Exception:
                                            try:
                                                unescaped = md_to_plain_text(payload.get("text", ""))
                                            except Exception:
                                                unescaped = payload.get("text", "")
                                        fallback = {}
                                        if "chat_id" in payload:
                                            fallback["chat_id"] = payload.get("chat_id")
                                        if "message_id" in payload:
                                            fallback["message_id"] = payload.get("message_id")
                                        if "inline_message_id" in payload:
                                            fallback["inline_message_id"] = payload.get("inline_message_id")
                                        fallback["text"] = unescaped
                                        if "reply_markup" in payload:
                                            fallback["reply_markup"] = payload.get("reply_markup")
                                            try:
                                                try:
                                                    logger.debug("callback editMessageText fallback payload: {}", json.dumps(fallback, ensure_ascii=False)[:2000])
                                                except Exception:
                                                    logger.debug("callback editMessageText fallback payload (unserializable)")
                                                async with httpx.AsyncClient() as client2:
                                                    resp2 = await client2.post(edit_url, json=fallback, timeout=10)
                                                try:
                                                    logger.debug("callback editMessageText fallback response: status={} body={}", resp2.status_code, (resp2.text or "")[:2000])
                                                except Exception:
                                                    logger.debug("callback editMessageText fallback response logged")
                                            except Exception:
                                                logger.exception("callback edit fallback failed")
                                    else:
                                        logger.error("editMessageText failed status={} body={}", resp.status_code, body_text[:400])
                        except Exception:
                            logger.exception("callback edit error handling response")
                        try:
                            await _answer_callback(token, cq.get("id"), text="")
                        except Exception:
                            pass
                    except Exception:
                        try:
                            await _answer_callback(token, cq.get("id"), text="Unable to load page")
                        except Exception:
                            pass
                    return JSONResponse(status_code=200, content={"ok": True})
                except Exception:
                    try:
                        await _answer_callback(token, cq.get("id"), text="Error")
                    except Exception:
                        pass
                    return JSONResponse(status_code=200, content={"ok": True})
            # Admin callbacks: handle clear_all actions from inline buttons
            # e.g. C|delete, C|drop, C|cancel
            if data.startswith("C|"):
                action = data.split("|", 1)[1]
                if await _reject_callback_if_not_owner(token, cq):
                    return JSONResponse(status_code=200, content={"ok": True})

                try:
                    db = getattr(app.state, "db", None)
                    msg = cq.get("message")
                    if action == "delete":
                        # Try to delete via app state DB if available
                        if db is not None:
                            try:
                                await db.get_collection("files").delete_many({})
                                await db.get_collection("index_state").delete_many({})
                            except Exception:
                                logger.exception("API clear_all: delete operation failed using app.state.db")
                        else:
                            # Fallback: create a temporary AsyncIOMotorClient to perform deletion
                            try:
                                tmp_client = AsyncIOMotorClient(settings.MONGO_URI)
                                tmp_db = tmp_client[settings.DB_NAME]
                                try:
                                    await tmp_db.get_collection("files").delete_many({})
                                    await tmp_db.get_collection("index_state").delete_many({})
                                except Exception:
                                    logger.exception("API clear_all: delete operation failed using fallback motor client")
                                try:
                                    tmp_client.close()
                                except Exception:
                                    pass
                            except Exception:
                                logger.exception("API clear_all: could not create fallback motor client")
                        edit_url = f"https://api.telegram.org/bot{token}/editMessageText"
                        payload = {"text": "Documents deleted."}
                        if msg and msg.get("chat"):
                            payload["chat_id"] = msg.get("chat", {}).get("id")
                            payload["message_id"] = msg.get("message_id")
                        else:
                            imid = cq.get("inline_message_id")
                            if imid:
                                payload["inline_message_id"] = imid
                        try:
                            async with httpx.AsyncClient() as client:
                                await client.post(edit_url, json=payload, timeout=10)
                        except Exception:
                            pass
                        try:
                            await _answer_callback(token, cq.get("id"), text="Deleted")
                        except Exception:
                            pass
                    elif action == "drop":
                        if db is not None:
                            try:
                                try:
                                    await db.drop_collection("files")
                                except Exception:
                                    pass
                                try:
                                    await db.drop_collection("index_state")
                                except Exception:
                                    pass
                            except Exception:
                                logger.exception("API clear_all: drop operation failed using app.state.db")
                        else:
                            # Fallback: create a temporary AsyncIOMotorClient
                            try:
                                tmp_client = AsyncIOMotorClient(settings.MONGO_URI)
                                tmp_db = tmp_client[settings.DB_NAME]
                                try:
                                    try:
                                        await tmp_db.drop_collection("files")
                                    except Exception:
                                        pass
                                    try:
                                        await tmp_db.drop_collection("index_state")
                                    except Exception:
                                        pass
                                except Exception:
                                    logger.exception("API clear_all: drop operation failed using fallback motor client")
                                try:
                                    tmp_client.close()
                                except Exception:
                                    pass
                            except Exception:
                                logger.exception("API clear_all: could not create fallback motor client for drop")
                        edit_url = f"https://api.telegram.org/bot{token}/editMessageText"
                        payload = {"text": "Collections dropped."}
                        if msg and msg.get("chat"):
                            payload["chat_id"] = msg.get("chat", {}).get("id")
                            payload["message_id"] = msg.get("message_id")
                        else:
                            imid = cq.get("inline_message_id")
                            if imid:
                                payload["inline_message_id"] = imid
                        try:
                            async with httpx.AsyncClient() as client:
                                await client.post(edit_url, json=payload, timeout=10)
                        except Exception:
                            pass
                        try:
                            await _answer_callback(token, cq.get("id"), text="Dropped")
                        except Exception:
                            pass
                    else:
                        edit_url = f"https://api.telegram.org/bot{token}/editMessageText"
                        payload = {"text": "Cancelled."}
                        if msg and msg.get("chat"):
                            payload["chat_id"] = msg.get("chat", {}).get("id")
                            payload["message_id"] = msg.get("message_id")
                        else:
                            imid = cq.get("inline_message_id")
                            if imid:
                                payload["inline_message_id"] = imid
                        try:
                            async with httpx.AsyncClient() as client:
                                await client.post(edit_url, json=payload, timeout=10)
                        except Exception:
                            pass
                        try:
                            await _answer_callback(token, cq.get("id"), text="Cancelled")
                        except Exception:
                            pass
                except Exception as e:
                    logger.exception("API clear callback failed: {}", e)
                    try:
                        await _answer_callback(token, cq.get("id"), text="Error", show_alert=True)
                    except Exception:
                        pass
                return JSONResponse(status_code=200, content={"ok": True})

    except Exception:
        # ensure we don't break webhook on callback handling errors
        logger.exception("callback handling error")

    # find message payload
    message = update.get("message") or update.get("edited_message") or {}
    update_id = update.get("update_id")
    text = (message.get("text") or message.get("caption") or "").strip()
    chat_id = (message.get("chat") or {}).get("id")
    text_trunc = (text[:120] + "...") if len(text) > 120 else text
    logger.info("webhook token={} update_id={} chat={} text={}", "[REDACTED]", update_id, chat_id, text_trunc)
    if not text:
        logger.debug("No text/caption in incoming update, ignoring")
        return {"ok": True}

    # handle /search command
    if text.startswith("/search"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            # Escape angle brackets to avoid Telegram HTML parse errors
            reply_text = "Usage: /search &lt;query&gt;"
        else:
            query = parts[1].strip()
            # schedule search+reply in background so webhook returns quickly
            try:
                asyncio.create_task(_process_search_and_send(token, chat_id, query))
            except Exception:
                # if loop not available, run inline (rare)
                await _process_search_and_send(token, chat_id, query)
            # acknowledge immediately
            return {"ok": True}

    # handle other commands
    # normalize command without botname suffix
    cmd = text.split(maxsplit=1)[0].split("@", 1)[0]
    if cmd == "/start":
        welcome = "Hi — I can search indexed files. Use /search <query> to search."
        await _send_tg(token, chat_id, welcome)
        return {"ok": True}

    if cmd == "/help":
        help_text = "Commands:\n/search <query> — Search files\n/stats — Indexed file counts (owner only)\n/reindex <chat_id> — Backfill chat history (owner only)\n/health — DB health (owner only)\n/clear_all — Clear all indexed files (owner only)"
        await _send_tg(token, chat_id, help_text)
        return {"ok": True}

    if cmd == "/stats":
        if await _reject_if_not_owner(token, chat_id):
            return {"ok": True}
        db = getattr(app.state, "db", None)
        if db is None:
            await _send_tg(token, chat_id, "Stats: DB unavailable")
        else:
            try:
                total = await db.get_collection("files").estimated_document_count()
                dups = await db.get_collection("files").count_documents({"is_duplicate": True})
                await _send_tg(token, chat_id, f"Total files: {total}\nDuplicates: {dups}")
            except Exception as exc:
                logger.exception("/stats handler failed: {}", exc)
                await _send_tg(token, chat_id, "Stats: error")
        return {"ok": True}

    if cmd == "/health":
        if await _reject_if_not_owner(token, chat_id):
            return {"ok": True}
        mongo_client = getattr(app.state, "mongo_client", None)
        db = getattr(app.state, "db", None)
        if mongo_client is None or db is None:
            await _send_tg(token, chat_id, "Health: DB unavailable")
        else:
            try:
                await mongo_client.admin.command("ping")
                await _send_tg(token, chat_id, "Health: OK")
            except Exception as exc:
                logger.exception("/health handler failed: {}", exc)
                await _send_tg(token, chat_id, f"Health: error: {exc}")
        return {"ok": True}

    if cmd == "/clear_all":
        if await _reject_if_not_owner(token, chat_id):
            return {"ok": True}

        kb = [
            [
                {"text": "Delete", "callback_data": "C|delete"},
                {"text": "Drop", "callback_data": "C|drop"},
            ],
            [{"text": "Cancel", "callback_data": "C|cancel"}],
        ]
        try:
            await _send_tg(token, chat_id, "Clear ALL indexed files?\nDelete = safer, Drop = faster.", reply_markup=kb)
        except Exception:
            logger.exception("Failed to send clear_all confirmation from API webhook")
        return {"ok": True}

    if cmd == "/reindex":
        if await _reject_if_not_owner(token, chat_id):
            return {"ok": True}

        # /reindex <chat_id> optionally. We'll spawn scripts/backfill.py as a subprocess
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await _send_tg(token, chat_id, "Usage: /reindex <chat_id>")
            return {"ok": True}
        target = parts[1].strip()
        try:
            target_chat_id = int(target)
        except Exception:
            await _send_tg(token, chat_id, "Invalid chat id")
            return {"ok": True}

        # spawn backfill script in background using same python executable
        try:
            cmd = [sys.executable, "scripts/backfill.py"]
            # pass target via env var TARGET_CHAT_ID for the script
            env = os.environ.copy()
            env["TARGET_CHAT_ID"] = str(target_chat_id)
            # Ensure subprocess can import local package `app`
            project_root = os.path.dirname(os.path.dirname(__file__))
            prev = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = project_root + (os.pathsep + prev if prev else "")

            async def _spawn_and_log(cmd, env, cwd):
                proc = await asyncio.create_subprocess_exec(*cmd, env=env, cwd=cwd, stdout=PIPE, stderr=PIPE)

                # notify owner immediately that the subprocess has started
                try:
                    owner = settings.OWNER_ID
                    if owner and int(owner) != int(chat_id):
                        try:
                            await _send_tg(token, int(owner), f"Backfill subprocess started for {target_chat_id} (requested by {chat_id})")
                        except Exception:
                            pass
                except Exception:
                    pass

                async def _drain(stream, level="info"):
                    try:
                        while True:
                            line = await stream.readline()
                            if not line:
                                break
                            text = line.decode(errors="replace").rstrip()
                            if not text:
                                continue
                            if level == "info":
                                logger.info("[backfill] {}", text)
                            else:
                                logger.error("[backfill] {}", text)
                    except Exception:
                        logger.exception("Error reading subprocess stream")

                # schedule draining stdout and stderr
                asyncio.create_task(_drain(proc.stdout, "info"))
                asyncio.create_task(_drain(proc.stderr, "error"))
                # don't await proc here; let it run independently

            asyncio.create_task(_spawn_and_log(cmd, env, project_root))
            await _send_tg(token, chat_id, f"Reindex scheduled for {target_chat_id}")
            # notify owner that reindex was scheduled
            try:
                owner = settings.OWNER_ID
                if owner and int(owner) != int(chat_id):
                    await _send_tg(token, int(owner), f"Reindex scheduled for {target_chat_id} by {chat_id}")
            except Exception:
                pass
        except Exception as exc:
            logger.exception("Failed to schedule reindex: {}", exc)
            await _send_tg(token, chat_id, "Failed to schedule reindex")
            try:
                owner = settings.OWNER_ID
                if owner:
                    await _send_tg(token, int(owner), f"Failed to schedule reindex for {target}: {exc}")
            except Exception:
                pass
        return {"ok": True}

    return {"ok": True}


async def _process_search_and_send(token: str, chat_id: int, query: str) -> None:
    """Background task: run search and send reply via Telegram HTTP API."""
    # Diagnostic: log entry and mask token for safety
    try:
        masked = None
        try:
            if token:
                masked = (token[:4] + "..." + token[-4:]) if len(token) > 8 else token
        except Exception:
            masked = "[unknown]"
        logger.info("_process_search_and_send start: token={} chat={} query={}", masked, chat_id, (query or "")[:200])
    except Exception:
        pass

    try:
        res = await api_search(q=query, page=1, per_page=50)
    except Exception as exc:
        logger.exception("background search failed: {}", exc)
        res = None

    # Ensure `results` and `total` are always defined to avoid NameError
    results = []
    total = 0
    if res is None:
        # simple escaped Markdown error
        reply_text = _escape_markdown("Temporary error: search backend unavailable")
    else:
        results = res.get("results", [])
        total = res.get("total", 0)
        if not results:
            reply_text = f"*No results* for {_escape_markdown(query)}"
            # Short-circuit: send a simple 'no results' reply immediately
            try:
                await _send_tg(token, chat_id, reply_text, parse_mode="Markdown")
                try:
                    logger.info("_process_search_and_send: sent no-results to chat {}", chat_id)
                except Exception:
                    pass
            except Exception:
                logger.exception("_process_search_and_send: failed to send no-results message")
            return
        else:
            lines = [f"*Search:* {_escape_markdown(query)} — {total} results"]
            from app.utils.helpers import sanitize_filename_for_display
            for i, r in enumerate(results, start=1):
                raw_fname = r.get("filename") or "-"
                display_raw = sanitize_filename_for_display(raw_fname)
                display = _escape_markdown(display_raw)
                lines.append(f"{i}) {display}")
            reply_text = "\n".join(lines)

    # Telegram message size guard: prefer Telegraph fallback for very long replies.
    # Meta: Telegram limit varies, but 4000 is a safe upper bound for message text.
    # We'll use 3000 chars to leave room for headers/formatting. If reply_text
    # exceeds TELEGRAPH_THRESHOLD we'll attempt to create a Telegraph page and
    # send the link instead of many messages.
    # Prepare a default header early so later branches can reference it
    # without causing UnboundLocalError (some paths assign `header` later).
    try:
        header = f"*Search:* {_escape_markdown(query)} — {total} results"
    except Exception:
        header = f"*Search:* {query} — {total} results"

    MAX_MSG = 3000
    TELEGRAPH_THRESHOLD = 8000
    # For large result sets prefer creating internal/Telegraph pages and
    # sending a compact link or paginated GUI. Fall back to inline sends
    # in other branches below.

    try:
        logger.info("_process_search_and_send: total={} results_len={}", total, len(results))
        if len(reply_text) <= MAX_MSG:
            # Build a HTML message body where each result is an inline <a> link
            # so users can click links directly in the message text.
            try:
                from urllib.parse import quote as _quote

                # Build a Markdown message body where each result is a
                # Markdown link [Title](URL). We keep the header as a bold
                # Markdown token and convert the result list via
                # `md_to_markdown` so links and labels are escaped properly.
                header = f"*Search:* {_escape_markdown(query)} — {total} results"
                md_lines = []
                for i, r in enumerate(results, start=1):
                    raw_fname = (r.get("filename") or "-").replace("\n", " ")
                    display_raw = raw_fname if len(raw_fname) <= 80 else raw_fname[:77] + "..."
                    url = ""
                    try:
                        chat_id_r = r.get("chat_id")
                        message_id = r.get("message_id")
                        if chat_id_r and message_id:
                            s = str(chat_id_r)
                            base = s[4:] if s.startswith("-100") else s.lstrip("-")
                            url = f"https://t.me/c/{base}/{message_id}"
                    except Exception:
                        url = ""

                    if url:
                        # keep raw label; md_to_markdown will escape it
                        md_lines.append(f"{i}) [{display_raw}]({url})")
                    else:
                        md_lines.append(f"{i}) {display_raw}")

                body_md_raw = "\n".join(md_lines)

                # If we have an internal page store, prefer saving the full
                # result set as paginated internal pages and send the first
                # page instead of attempting to inline a potentially oversized
                # Markdown message. This avoids Telegram "message is too long"
                # errors and keeps behavior consistent across branches.
                try:
                    store = _ensure_internal_page_store()
                except Exception:
                    store = None

                try:
                    max_inline = getattr(settings, "MAX_INLINE_RESULTS", 8)
                except Exception:
                    max_inline = 8

                if store and md_lines:
                    try:
                        toks = tokenize_query(query)
                        group = str(uuid.uuid4())
                        MAX_MSG_CFG = getattr(settings, "MAX_MSG", 4000)
                        CHUNK_CHAR_LIMIT = max(800, int(MAX_MSG_CFG - 200))
                        from app.utils.helpers import chunk_lines_with_refs

                        # Build per-line refs aligned to md_lines so saved parts
                        # can persist deterministic mapping back to original
                        # chat/message ids and include a match indicator.
                        md_line_refs = []
                        try:
                            from app.utils.helpers import normalize_filename_key
                            import os, urllib.parse

                            for r in results:
                                ref = {}
                                try:
                                    ref["chat_id"] = int(r.get("chat_id")) if r.get("chat_id") is not None else None
                                except Exception:
                                    ref["chat_id"] = None
                                try:
                                    ref["message_id"] = int(r.get("message_id")) if r.get("message_id") is not None else None
                                except Exception:
                                    ref["message_id"] = None
                                try:
                                    raw_fname = r.get("filename") or ""
                                    # keep only basename and URL-decode
                                    try:
                                        bf = raw_fname.replace("\\", "/")
                                        bf = os.path.basename(bf)
                                        bf = urllib.parse.unquote(bf)
                                    except Exception:
                                        bf = raw_fname
                                    ref["filename"] = bf
                                    ref["filename_norm"] = normalize_filename_key(bf)
                                except Exception:
                                    ref["filename"] = ""
                                    ref["filename_norm"] = ""
                                try:
                                    ref["match_score"] = float(r.get("_score")) if r.get("_score") is not None else None
                                except Exception:
                                    ref["match_score"] = None
                                # heuristic match type: prefer filename-based detection using normalized keys
                                try:
                                    qnorm = re.sub(r"[^a-z0-9\s]", " ", (query or "").lower()).strip()
                                    fname_norm = ref.get("filename_norm") or ""
                                    fname = (ref.get("filename") or "").lower()
                                    if qnorm and fname_norm and qnorm == fname_norm:
                                        ref["match_type"] = "filename_exact"
                                    elif qnorm and fname and qnorm in fname:
                                        ref["match_type"] = "filename_substring"
                                    else:
                                        tt = r.get("title_tokens") or []
                                        tt_lower = [t.lower() for t in tt if isinstance(t, str)]
                                        if any(tok in qnorm for tok in tt_lower):
                                            ref["match_type"] = "title_token"
                                        else:
                                            ref["match_type"] = "fuzzy"
                                except Exception:
                                    ref["match_type"] = "unknown"
                                md_line_refs.append(ref)
                        except Exception:
                            md_line_refs = []

                        chunks_with_refs = chunk_lines_with_refs(md_lines, md_line_refs, CHUNK_CHAR_LIMIT)
                        total_parts = len(chunks_with_refs)
                        total_results = len(md_lines)
                        # Bind a clean header string to the saved page parts
                        try:
                            page_header = f"Search: {query} — {total_results} results"
                        except Exception:
                            page_header = f"Search: {query}"
                        pages = []
                        for part_index, (chunk, chunk_refs) in enumerate(chunks_with_refs):
                            try:
                                saved = await store.save_raw_page(
                                    query,
                                    chunk,
                                    toks,
                                    created_by=None,
                                    group=group,
                                    part_index=part_index,
                                    total_parts=total_parts,
                                    total_results=total_results,
                                    page_header=page_header,
                                    line_refs=chunk_refs,
                                )
                                pages.append(saved.get("page_id"))
                            except Exception:
                                logger.exception("api_search: failed to save page part {}/{}", part_index + 1, total_parts)

                        # Build top_links from preview results and persist
                        top_links = []
                        try:
                            from app.utils.helpers import sanitize_filename_for_display, normalize_filename_key
                            seen_urls = set()
                            for ii, rr in enumerate(results[:max_inline], start=1):
                                raw_fname = rr.get("filename") or "-"
                                display_raw = sanitize_filename_for_display(raw_fname)
                                url = ""
                                try:
                                    chat_id_r = rr.get("chat_id")
                                    message_id = rr.get("message_id")
                                    if chat_id_r and message_id:
                                        s = str(chat_id_r)
                                        base = s[4:] if s.startswith("-100") else s.lstrip("-")
                                        url = f"https://t.me/c/{base}/{message_id}"
                                except Exception:
                                    url = ""
                                # dedupe by URL and normalized filename key
                                try:
                                    key = normalize_filename_key(raw_fname)
                                except Exception:
                                    key = ""
                                if url and url not in seen_urls:
                                    top_links.append({"text": f"{ii}) {display_raw}", "url": url})
                                    seen_urls.add(url)
                        except Exception:
                            top_links = []

                        try:
                            store_for_links = _ensure_internal_page_store()
                            logger.info("api_search: set_top_links store={} pages={} top_links={}", "present" if store_for_links else "absent", len(pages), len(top_links))
                            if store_for_links:
                                try:
                                    await store_for_links.set_top_links(pages[0], top_links)
                                    logger.info("api_search: set_top_links succeeded for page {}", pages[0])
                                except Exception:
                                    logger.exception("api_search: set_top_links failed for page {}", pages[0])
                        except Exception:
                            logger.exception("api_search: error checking store_for_links")

                        # fetch the first page and send it as a single message with
                        # nav + top-links + part buttons. If rendering or sending
                        # the full GUI fails, send a compact "View full list" button
                        # that opens the saved internal page via callback.
                        if pages:
                            try:
                                first = await store.get_page(pages[0])
                                if not first:
                                    logger.error("First page is None: {}", pages[0])
                                else:
                                    try:
                                        # Build full Markdown lines from group pages
                                        gp = first.get("group_pages") or [first]
                                        md_all_lines = []
                                        for part in gp:
                                            md_all_lines.extend((part.get("content") or "").splitlines())

                                        # Build top-links keyboard (two-per-row)
                                        tlinks = first.get("top_links") or []
                                        kb_rows = []
                                        row = []
                                        for tl in tlinks:
                                            try:
                                                btn = {"text": (tl.get("text") or "")[:60], "url": tl.get("url")}
                                                row.append(btn)
                                                if len(row) >= 2:
                                                    kb_rows.append(row)
                                                    row = []
                                            except Exception:
                                                continue
                                        if row:
                                            kb_rows.append(row)

                                        # (button removed) do not append 'Open formatted results' button

                                        reply_markup = {"inline_keyboard": kb_rows} if kb_rows else None

                                        # Send each saved part as its own Markdown chunked message
                                        try:
                                            total_parts_gp = len(gp)
                                            for pi, part in enumerate(gp, start=1):
                                                part_lines = (part.get("content") or "").splitlines()
                                                part_header = f"{header} — part {pi}/{total_parts_gp}"
                                                # attach reply markup only to the first part
                                                rm = reply_markup if pi == 1 else None
                                                await _send_markdown_full(token, chat_id, part_header, part_lines, reply_markup=rm)
                                            return
                                        except Exception:
                                            logger.exception("api_search: failed while streaming saved parts per-page")
                                    except Exception:
                                        logger.exception("api_search: failed to render/send saved full pages")
                                        # Best-effort compact fallback so users can open the saved page
                                        try:
                                            fallback_kb = {"inline_keyboard": [[{"text": "View full list", "callback_data": f"IP|{pages[0]}"}]]}
                                            await _send_tg(token, chat_id, "Full results available — tap to open.", parse_mode=None, reply_markup=fallback_kb)
                                            return
                                        except Exception:
                                            logger.exception("api_search: fallback button send failed")
                            except Exception:
                                logger.exception("api_search: failed to load first saved page")

                    except Exception:
                        logger.exception("api_search: failed to create/persist internal pages")

                # Fallback: build the inline Markdown body if we didn't use stored pages
                body_md = header + "\n\n" + md_to_markdown(body_md_raw)

                # build inline keyboard for top results (keep buttons alongside HTML links)
                try:
                    max_inline = getattr(settings, "MAX_INLINE_RESULTS", 8)
                except Exception:
                    max_inline = 8

                rows = []
                try:
                    from app.utils.helpers import sanitize_filename_for_display
                    seen_urls = set()
                    for i, r in enumerate(results[:max_inline], start=1):
                        raw_fname = r.get("filename") or "-"
                        display_raw = sanitize_filename_for_display(raw_fname)
                        url = ""
                        try:
                            chat_id_r = r.get("chat_id")
                            message_id = r.get("message_id")
                            if chat_id_r and message_id:
                                s = str(chat_id_r)
                                base = s[4:] if s.startswith("-100") else s.lstrip("-")
                                url = f"https://t.me/c/{base}/{message_id}"
                        except Exception:
                            url = ""
                        if url and url not in seen_urls:
                            rows.append([{"text": f"{i}) {display_raw}", "url": url}])
                            seen_urls.add(url)
                except Exception:
                    rows = []

                # If there are many results, persist a paginated internal page
                # set (stored as markdown) and expose a "Full list" callback
                pages = []
                try:
                    # Force usage of internal page store (create in-memory fallback if needed)
                    store = _ensure_internal_page_store()
                    logger.info("api_search: total={} max_inline={} internal_page_store={}", total, max_inline, "present" if store else "absent")
                    toks = tokenize_query(query) if store else []

                    # Determine the full set of results to include in pages
                    # Force creation of internal pages whenever a store is present
                    # so the single-message GUI is used for all result sizes.
                    if store:
                        try:
                            full_res = await api_search(q=query, page=1, per_page=5000)
                            full_results = full_res.get("results", []) if isinstance(full_res, dict) else []
                        except Exception:
                            full_results = results
                    else:
                        full_results = results

                    # build markdown lines for full results
                    md_lines = []
                    from app.utils.helpers import sanitize_filename_for_display, normalize_filename_key
                    seen_urls = set()
                    for i, r in enumerate(full_results, start=1):
                        raw_fname = r.get("filename") or "-"
                        display_raw = sanitize_filename_for_display(raw_fname)
                        url = ""
                        try:
                            chat_id_r = r.get("chat_id")
                            message_id = r.get("message_id")
                            if chat_id_r and message_id:
                                s = str(chat_id_r)
                                base = s[4:] if s.startswith("-100") else s.lstrip("-")
                                url = f"https://t.me/c/{base}/{message_id}"
                        except Exception:
                            url = ""
                        try:
                            # Only include preview link if we can create a unique URL
                            if url and url not in seen_urls:
                                md_lines.append(f"{i}) [{display_raw}]({url})")
                                seen_urls.add(url)
                            else:
                                md_lines.append(f"- {display_raw}")
                        except Exception:
                            md_lines.append(f"- {display_raw}")

                    if md_lines and store:
                        # Chunk pages by character length to avoid creating
                        # pages that exceed Telegram's message length and get
                        # truncated. Compute a conservative per-page char limit
                        # from the configured MAX_MSG and leave a margin.
                        group = str(uuid.uuid4())
                        MAX_MSG = getattr(settings, "MAX_MSG", 4000)
                        CHUNK_CHAR_LIMIT = max(800, int(MAX_MSG - 200))
                        from app.utils.helpers import chunk_lines_with_refs

                        # Build per-line refs from full_results so saved parts
                        # carry deterministic mappings back to original messages.
                        md_line_refs = []
                        try:
                            from app.utils.helpers import normalize_filename_key
                            import os, urllib.parse

                            for r in full_results:
                                ref = {}
                                try:
                                    ref["chat_id"] = int(r.get("chat_id")) if r.get("chat_id") is not None else None
                                except Exception:
                                    ref["chat_id"] = None
                                try:
                                    ref["message_id"] = int(r.get("message_id")) if r.get("message_id") is not None else None
                                except Exception:
                                    ref["message_id"] = None
                                try:
                                    raw_fname = r.get("filename") or ""
                                    try:
                                        bf = raw_fname.replace("\\", "/")
                                        bf = os.path.basename(bf)
                                        bf = urllib.parse.unquote(bf)
                                    except Exception:
                                        bf = raw_fname
                                    ref["filename"] = bf
                                    ref["filename_norm"] = normalize_filename_key(bf)
                                except Exception:
                                    ref["filename"] = ""
                                    ref["filename_norm"] = ""
                                try:
                                    ref["match_score"] = float(r.get("_score")) if r.get("_score") is not None else None
                                except Exception:
                                    ref["match_score"] = None
                                try:
                                    qnorm = re.sub(r"[^a-z0-9\s]", " ", (query or "").lower()).strip()
                                    fname_norm = ref.get("filename_norm") or ""
                                    fname = (ref.get("filename") or "").lower()
                                    if qnorm and fname_norm and qnorm == fname_norm:
                                        ref["match_type"] = "filename_exact"
                                    elif qnorm and fname and qnorm in fname:
                                        ref["match_type"] = "filename_substring"
                                    else:
                                        tt = r.get("title_tokens") or []
                                        tt_lower = [t.lower() for t in tt if isinstance(t, str)]
                                        if any(tok in qnorm for tok in tt_lower):
                                            ref["match_type"] = "title_token"
                                        else:
                                            ref["match_type"] = "fuzzy"
                                except Exception:
                                    ref["match_type"] = "unknown"
                                md_line_refs.append(ref)
                        except Exception:
                            md_line_refs = []

                        chunks_with_refs = chunk_lines_with_refs(md_lines, md_line_refs, CHUNK_CHAR_LIMIT)

                        total_parts = len(chunks_with_refs)
                        total_results = len(md_lines)
                        try:
                            page_header = f"Search: {query} — {total_results} results"
                        except Exception:
                            page_header = f"Search: {query}"
                        for part_index, (chunk, chunk_refs) in enumerate(chunks_with_refs):
                            try:
                                try:
                                    logger.debug("api_search: saving page part {}/{} query={} preview={}", part_index + 1, total_parts, query, (chunk[:200] if chunk else None))
                                except Exception:
                                    pass
                                saved = await store.save_raw_page(
                                    query,
                                    chunk,
                                    toks,
                                    created_by=None,
                                    group=group,
                                    part_index=part_index,
                                    total_parts=total_parts,
                                    total_results=total_results,
                                    page_header=page_header,
                                    line_refs=chunk_refs,
                                )
                                pid = saved.get("page_id")
                                pages.append(pid)
                                logger.info("api_search: saved internal page {} ({}/{})", pid, part_index + 1, total_parts)
                            except Exception:
                                logger.exception("api_search: failed to save page part {}/{}", part_index + 1, total_parts)
                except Exception:
                    pages = []

                # If pages were created, prefer sending a single paginated GUI
                # message that shows the first part and provides navigation plus
                # top-8 quick links. This avoids creating many messages.
                if pages:
                    try:
                        # build top-links from the full_results preview
                        top_links = []
                        try:
                            for ii, rr in enumerate(full_results[:max_inline], start=1):
                                raw_fname = (rr.get("filename") or "-").replace("\n", " ")
                                display_raw = raw_fname if len(raw_fname) <= 80 else raw_fname[:77] + "..."
                                url = ""
                                try:
                                    chat_id_r = rr.get("chat_id")
                                    message_id = rr.get("message_id")
                                    if chat_id_r and message_id:
                                        s = str(chat_id_r)
                                        base = s[4:] if s.startswith("-100") else s.lstrip("-")
                                        url = f"https://t.me/c/{base}/{message_id}"
                                except Exception:
                                    url = ""
                                if url:
                                    top_links.append({"text": f"{ii}) {display_raw}", "url": url})
                        except Exception:
                            top_links = []

                        # persist top_links into the first saved page doc for callback rendering
                        try:
                            store_for_links = _ensure_internal_page_store()
                            logger.info("api_search: set_top_links store={} pages={} top_links={}", "present" if store_for_links else "absent", len(pages), len(top_links))
                            if store_for_links:
                                try:
                                    await store_for_links.set_top_links(pages[0], top_links)
                                    logger.info("api_search: set_top_links succeeded for page {}", pages[0])
                                except Exception:
                                    logger.exception("api_search: set_top_links failed for page {}", pages[0])
                        except Exception:
                            logger.exception("api_search: error checking store_for_links")

                        # fetch the first page and send it as a single message with
                        # nav + top-links + part buttons. If rendering or sending
                        # the full GUI fails, send a compact "View full list" button
                        # that opens the saved internal page via callback.
                        try:
                            first = await store.get_page(pages[0])
                            if not first:
                                logger.error("First page is None: {}", pages[0])
                            else:
                                try:
                                    # Send all saved page parts as full Markdown (no truncation)
                                    gp = first.get("group_pages") or [first]
                                    md_all_lines = []
                                    for part in gp:
                                        md_all_lines.extend((part.get("content") or "").splitlines())

                                    # build top-links keyboard
                                    tlinks = first.get("top_links") or []
                                    kb_rows = []
                                    row = []
                                    for tl in tlinks:
                                        try:
                                            btn = {"text": (tl.get("text") or "")[:60], "url": tl.get("url")}
                                            row.append(btn)
                                            if len(row) >= 2:
                                                kb_rows.append(row)
                                                row = []
                                        except Exception:
                                            continue
                                    if row:
                                        kb_rows.append(row)

                                    # (button removed) do not append 'Open formatted results' button

                                    reply_markup = {"inline_keyboard": kb_rows} if kb_rows else None
                                    await _send_markdown_full(token, chat_id, header, md_all_lines, reply_markup=reply_markup)
                                    return
                                except Exception:
                                    logger.exception("api_search: failed to render/send saved full pages")
                                    # Best-effort compact fallback so users can open the saved page
                                    try:
                                        fallback_kb = {"inline_keyboard": [[{"text": "View full list", "callback_data": f"IP|{pages[0]}"}]]}
                                        await _send_tg(token, chat_id, "Full results available — tap to open.", parse_mode=None, reply_markup=fallback_kb)
                                        return
                                    except Exception:
                                        logger.exception("api_search: fallback button send failed")
                        except Exception:
                            logger.exception("api_search: failed to load first saved page")
                    except Exception:
                        logger.exception("api_search: failed while handling saved pages block")

                reply_markup = {"inline_keyboard": rows} if rows else None

                # Send the full Markdown list as chunked HTML messages so links
                # are preserved and Telegram entity parsing is less error-prone.
                try:
                    await _send_markdown_full(token, chat_id, header, md_lines, reply_markup=reply_markup)
                    return
                except Exception:
                    logger.exception("api_search: _send_markdown_full failed; falling back to multi-part Markdown send")

                # Fallback: chunk by lines to avoid splitting anchor tags.
                parts = []
                cur = []
                cur_len = 0
                for line in body_md.splitlines():
                    add_len = len(line) + 1
                    if cur_len + add_len > MAX_MSG:
                        parts.append("\n".join(cur))
                        cur = [line]
                        cur_len = len(line) + 1
                    else:
                        cur.append(line)
                        cur_len += add_len
                if cur:
                    parts.append("\n".join(cur))

                # Send parts sequentially; attach keyboard only to first part
                for idx, part in enumerate(parts, start=1):
                    try:
                        if idx == 1:
                            await _send_tg(token, chat_id, part, parse_mode="Markdown", reply_markup=reply_markup)
                        else:
                            await _send_tg(token, chat_id, part, parse_mode="Markdown")
                    except Exception:
                        # If any send fails, stop to avoid spamming
                        break
                return
            except Exception:
                # fallback to previous behavior if HTML build fails
                pass

        # If the reply is very long, prefer to publish it to Telegraph and
        # send a short link instead of many messages. This requires a
        # configured AsyncTelegraphStore (uses Mongo state to persist pages).
        # Prefer using the internal page GUI when a store is available.
        try:
            store = _ensure_internal_page_store()
            logger.info("api_search: telegraph/internal-page branch check; internal_page_store={} reply_len={}", "present" if store else "absent", len(reply_text))
            toks = tokenize_query(query)
            if store:
                # split reply_text into chunks (by lines) and save multiple pages
                lines = (reply_text or "").splitlines()
                # chunk by cumulative character size to avoid oversized pages
                MAX_MSG = getattr(settings, "MAX_MSG", 4000)
                CHUNK_CHAR_LIMIT = max(800, int(MAX_MSG - 200))
                pages = []
                group = str(uuid.uuid4())
                from app.utils.helpers import chunk_lines_by_char_limit

                chunks = chunk_lines_by_char_limit(lines, CHUNK_CHAR_LIMIT)

                total_parts = len(chunks)
                total_results = len(lines)
                for part_index, chunk in enumerate(chunks):
                    try:
                        logger.debug("api_search: saving reply_text page part {}/{} query={} preview={}", part_index + 1, total_parts, query, (chunk[:200] if chunk else None))
                    except Exception:
                        pass
                    saved = await store.save_raw_page(query, chunk, toks, created_by=None, group=group, part_index=part_index, total_parts=total_parts, total_results=total_results)
                    pages.append(saved.get("page_id"))

                # After saving all parts, attempt to stream the full result set back
                if pages:
                    try:
                        first = await store.get_page(pages[0])
                        if first:
                            # Build full Markdown lines from group pages
                            gp = first.get("group_pages") or [first]
                            md_all_lines = []
                            for part in gp:
                                md_all_lines.extend((part.get("content") or "").splitlines())

                            # Build top-links keyboard (two-per-row)
                            tlinks = first.get("top_links") or []
                            kb_rows = []
                            row = []
                            for tl in tlinks:
                                try:
                                    btn = {"text": (tl.get("text") or "")[:60], "url": tl.get("url")}
                                    row.append(btn)
                                    if len(row) >= 2:
                                        kb_rows.append(row)
                                        row = []
                                except Exception:
                                    continue
                            if row:
                                kb_rows.append(row)

                            # (button removed) do not append 'Open formatted results' button

                            reply_markup = {"inline_keyboard": kb_rows} if kb_rows else None
                            # Send each saved part as its own Markdown chunked message
                            try:
                                total_parts_gp = len(gp)
                                for pi, part in enumerate(gp, start=1):
                                    part_lines = (part.get("content") or "").splitlines()
                                    part_header = f"{header} — part {pi}/{total_parts_gp}"
                                    rm = reply_markup if pi == 1 else None
                                    try:
                                        logger.debug("saved-page send pi={} of {} reply_markup_present={} reply_markup_preview={}", pi, total_parts_gp, bool(rm), json.dumps(rm)[:400] if rm else None)
                                    except Exception:
                                        # best-effort preview, do not fail the send
                                        pass
                                    await _send_markdown_full(token, chat_id, part_header, part_lines, reply_markup=rm)
                                return
                            except Exception:
                                logger.exception("api_search: failed while streaming saved parts per-page")
                    except Exception:
                        logger.exception("api_search: failed to render/send saved full pages after saving all parts")
        except Exception:
            # If page creation fails, continue to multi-message split
            logger.exception("failed to create internal pages")
            pass

        # Split into chunks on line boundaries for readability.
        parts = []
        cur = []
        cur_len = 0
        for line in reply_text.splitlines():
            # +1 for the newline when joined
            add_len = len(line) + 1
            if cur_len + add_len > MAX_MSG:
                parts.append("\n".join(cur))
                cur = [line]
                cur_len = len(line) + 1
            else:
                cur.append(line)
                cur_len += add_len
        if cur:
            parts.append("\n".join(cur))

        total_parts = len(parts)
        # Send each part with a small header containing metadata
        for idx, part in enumerate(parts, start=1):
            header = f"*Search:* {_escape_markdown(query)} — part {idx}/{total_parts}"
            meta_txt = f"results:{total} query_len:{len(query)} ts:{datetime.utcnow().isoformat()}Z"
            meta = f"\n_{_escape_markdown(meta_txt)}_\n\n"
            text_to_send = header + meta + part
            try:
                # Prefer sending as HTML by converting Markdown to HTML anchors
                try:
                    from app.utils.helpers import md_to_html

                    try:
                        html_text = md_to_html(text_to_send, one_per_line=True)
                    except Exception:
                        html_text = html.escape(text_to_send)
                except Exception:
                    html_text = html.escape(text_to_send)

                rm = reply_markup if idx == 1 else None
                try:
                    logger.debug("chunked send idx={} of {} reply_markup_present={} reply_markup_preview={}", idx, total_parts, bool(rm), json.dumps(rm)[:400] if rm else None)
                except Exception:
                    pass
                await _send_tg(token, chat_id, html_text, parse_mode="HTML", reply_markup=rm)
            except Exception:
                # If sending fails for any chunk, attempt to send a truncated summary
                trunc = (part[:MAX_MSG - 100] + "\n\n[truncated]") if len(part) > (MAX_MSG - 100) else part
                try:
                    try:
                        try:
                            from app.utils.helpers import md_to_html
                            try:
                                trunc_html = md_to_html(f"{header}\n\n{trunc}", one_per_line=True)
                            except Exception:
                                trunc_html = html.escape(f"{header}\n\n{trunc}")
                        except Exception:
                            trunc_html = html.escape(f"{header}\n\n{trunc}")
                        rm = reply_markup if idx == 1 else None
                        try:
                            logger.debug("chunked fallback HTML send idx={} of {} reply_markup_present={} reply_markup_preview={}", idx, total_parts, bool(rm), json.dumps(rm)[:400] if rm else None)
                        except Exception:
                            pass
                        await _send_tg(token, chat_id, trunc_html, parse_mode="HTML", reply_markup=rm)
                    except Exception:
                        rm = reply_markup if idx == 1 else None
                        try:
                            logger.debug("chunked fallback MD send idx={} of {} reply_markup_present={} reply_markup_preview={}", idx, total_parts, bool(rm), json.dumps(rm)[:400] if rm else None)
                        except Exception:
                            pass
                        await _send_tg(token, chat_id, f"{header}\n\n{trunc}", parse_mode="Markdown", reply_markup=rm)
                except Exception:
                    # give up silently to avoid blocking the worker
                    return
        return
    except Exception:
        # Fallback: send a truncated single message
        try:
            # ensure we escape and use Markdown here
            truncated = _escape_markdown(reply_text[:MAX_MSG]) + "\n\n[truncated]"
            try:
                try:
                    logger.debug("fallback single send reply_markup_present={} reply_markup_preview={}", bool(reply_markup), json.dumps(reply_markup)[:400] if reply_markup else None)
                except Exception:
                    pass
                await _send_tg(token, chat_id, truncated, parse_mode="Markdown", reply_markup=reply_markup if reply_markup is not None else None)
            except Exception:
                await _send_tg(token, chat_id, truncated, parse_mode="Markdown")
        except Exception:
            pass
        return

    try:
        logger.info("_process_search_and_send: attempting simple send (len={})", len(reply_text))
    except Exception:
        pass

    tg_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": reply_text, "parse_mode": "Markdown"}
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(tg_url, json=payload, timeout=10)
            logger.info("Telegram sendMessage response: status={}, body={}", resp.status_code, resp.text[:400])
        except Exception as exc:
            logger.exception("Failed to POST to Telegram API (background): {}", exc)


async def _send_tg(token: str, chat_id: int, text: str, parse_mode: str | None = None, reply_markup: object | None = None) -> None:
    """Utility to send a message via Telegram Bot API."""
    if chat_id is None:
        logger.warning("_send_tg called with no chat_id")
        return
    tg_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    # Normalize reply_markup if provided so callers can pass Pyrogram/AIogram
    # style objects or simple lists/dicts. We only include it when it can be
    # converted to the Bot API compatible structure (inline_keyboard).
    if reply_markup is not None:
        try:
            rm = reply_markup
            normalized = None
            # JSON string
            if isinstance(rm, (str, bytes)):
                try:
                    normalized = json.loads(rm)
                except Exception:
                    normalized = None
            # already a dict (likely compatible)
            elif isinstance(rm, dict):
                if "inline_keyboard" in rm:
                    normalized = rm
                elif "rows" in rm and isinstance(rm.get("rows"), list):
                    normalized = {"inline_keyboard": rm.get("rows")}
                elif "buttons" in rm and isinstance(rm.get("buttons"), list):
                    normalized = {"inline_keyboard": rm.get("buttons")}
                else:
                    normalized = rm
            # list-of-rows: treat as inline_keyboard directly
            elif isinstance(rm, list):
                normalized = {"inline_keyboard": rm}
            else:
                # attempt common object conversions (pyrogram aiogram)
                try:
                    if hasattr(rm, "inline_keyboard"):
                        normalized = {"inline_keyboard": getattr(rm, "inline_keyboard")}
                    elif hasattr(rm, "rows"):
                        normalized = {"inline_keyboard": getattr(rm, "rows")}
                    elif hasattr(rm, "to_dict"):
                        d = rm.to_dict()
                        if "inline_keyboard" in d:
                            normalized = d
                        elif "rows" in d:
                            normalized = {"inline_keyboard": d.get("rows")}
                except Exception:
                    normalized = None

            if normalized is not None:
                payload["reply_markup"] = normalized
        except Exception:
            logger.exception("_send_tg: failed to normalize reply_markup")
    # simple retry loop for transient errors
    retries = 3
    backoff = 0.5
    async with httpx.AsyncClient() as client:
        for attempt in range(1, retries + 1):
            try:
                # Log full outgoing payload (best-effort). Masking of secrets is
                # handled by the logger sink configured in `app.utils.logger`.
                try:
                    logger.info("Telegram sendMessage (attempt {}) request payload: {}", attempt, json.dumps(payload, ensure_ascii=False))
                except Exception:
                    try:
                        logger.info("Telegram sendMessage (attempt {}) request payload (repr): {}", attempt, repr(payload)[:10000])
                    except Exception:
                        pass

                resp = await client.post(tg_url, json=payload, timeout=10)
                # Log the full response body for diagnosis
                try:
                    logger.info("Telegram sendMessage response: status={} body={}", resp.status_code, (resp.text or ""))
                except Exception:
                    logger.info("Telegram sendMessage response: status={}", resp.status_code)

                # Success
                if resp.status_code == 200:
                    return

                # If Telegram reports a parsing error for entities, retry once
                # without parse_mode so the message is delivered as plain text.
                try:
                    body_text = resp.text or ""
                except Exception:
                    body_text = ""

                # If Telegram reports a parsing error for entities, retry once
                # without parse_mode so the message is delivered as plain text.
                if payload.get("parse_mode") and resp.status_code == 400 and (
                    "can't parse entities" in body_text.lower() or "parse entities" in body_text.lower()
                ):
                    try:
                        # Log detailed debug info about the parse error and payload
                        try:
                            logger.warning("Telegram parse error (400) for parse_mode={}; body={}", payload.get("parse_mode"), (body_text or ""))
                            try:
                                logger.warning("Offending payload: {}", json.dumps(payload, ensure_ascii=False))
                            except Exception:
                                logger.warning("Offending payload (repr): {}", repr(payload)[:10000])
                        except Exception:
                            logger.warning("Telegram parse error (400) for parse_mode={}", payload.get("parse_mode"))
                        # Try an alternative: convert HTML anchors to MarkdownV2
                        # and retry sending with `parse_mode=MarkdownV2` before
                        # falling back to plain text. This often succeeds when
                        # Telegram's HTML parser rejects the entity set.
                        try:
                            try:
                                from app.utils.helpers import md_to_markdownv2

                                raw_text = payload.get("text", "") or ""
                                try:
                                    anchors_to_md = re.sub(r'<a\s+href="([^"]+)"[^>]*>(.*?)<\/a>', r'[\2](\1)', raw_text, flags=re.I)
                                    anchors_to_md = html.unescape(anchors_to_md)
                                except Exception:
                                    anchors_to_md = html.unescape(raw_text)

                                try:
                                    mdv2_text = md_to_markdownv2(anchors_to_md)
                                    alt_payload = {"chat_id": payload.get("chat_id"), "text": mdv2_text, "parse_mode": "MarkdownV2"}
                                    if "reply_markup" in payload:
                                        alt_payload["reply_markup"] = payload.get("reply_markup")
                                    try:
                                        logger.info("Telegram parse error: retrying with MarkdownV2 alternative payload preview: {}", (mdv2_text or "")[:320])
                                    except Exception:
                                        pass
                                    resp3 = await client.post(tg_url, json=alt_payload, timeout=10)
                                    try:
                                        logger.debug("Telegram MarkdownV2 retry response: status={} body={}", resp3.status_code, (resp3.text or "")[:2000])
                                    except Exception:
                                        pass
                                    if resp3.status_code == 200:
                                        return
                                except Exception:
                                    pass
                            except Exception:
                                pass
                        except Exception:
                            pass
                        # Prepare a plain-text fallback by removing any
                        # Markdown links and unescaping escapes so the
                        # resulting text is readable when sent without
                        # parse_mode. Prefer `md_to_plain_text` which strips
                        # link syntax; fall back to `unescape_for_plain_text`.
                        try:
                            raw_text = payload.get("text", "")
                            try:
                                plain = md_to_plain_text(raw_text)
                            except Exception:
                                plain = unescape_for_plain_text(raw_text)
                            # Ensure final plain text has backslash-escapes removed
                            try:
                                plain = unescape_for_plain_text(plain)
                            except Exception:
                                pass
                        except Exception:
                            plain = unescape_for_plain_text(payload.get("text", "") or "")

                        fallback = {"chat_id": payload.get("chat_id"), "text": plain}
                        if "reply_markup" in payload:
                            fallback["reply_markup"] = payload.get("reply_markup")

                        try:
                            logger.debug("Telegram fallback send payload: {}", json.dumps(fallback, ensure_ascii=False)[:2000])
                        except Exception:
                            logger.debug("Telegram fallback send payload (unserializable)")

                        resp2 = await client.post(tg_url, json=fallback, timeout=10)
                        try:
                            logger.info("Telegram fallback send (plain text) response: status={} body={}", resp2.status_code, (resp2.text or ""))
                        except Exception:
                            logger.info("Telegram fallback send response: status={}", resp2.status_code)
                        if resp2.status_code == 200:
                            # After successfully delivering the plain-text fallback,
                            # try to save a formatted copy into the internal/telegraph
                            # store so we can provide a `telegraph://` link that
                            # preserves anchors for users to open.
                            try:
                                try:
                                    store = _ensure_internal_page_store()
                                except Exception:
                                    store = None
                                if store:
                                    try:
                                        # Convert simple HTML anchors back to Markdown
                                        # e.g. <a href="url">Label</a> -> [Label](url)
                                        raw_html = payload.get("text", "") or ""
                                        try:
                                            anchors_to_md = re.sub(r'<a\s+href="([^"]+)"[^>]*>(.*?)<\/a>', r'[\2](\1)', raw_html, flags=re.I)
                                            # Unescape HTML entities to restore readable text
                                            anchors_to_md = html.unescape(anchors_to_md)
                                        except Exception:
                                            anchors_to_md = html.unescape(raw_html)

                                        saved = await store.save_raw_page("__fallback__", anchors_to_md, tokens=[], created_by=None)
                                        pid = saved.get("page_id")
                                        if pid:
                                            try:
                                                # Build a reasonable public URL for the saved page.
                                                api_base = (
                                                    os.getenv("API_URL")
                                                    or os.getenv("API_BASE_URL")
                                                    or os.getenv("APP_URL")
                                                    or os.getenv("RENDER_EXTERNAL_URL")
                                                    or f"http://localhost:{os.getenv('PORT','8000')}"
                                                )
                                                if api_base and not api_base.lower().startswith(("http://", "https://")):
                                                    api_base = f"https://{api_base}"
                                                try:
                                                    from urllib.parse import urlparse

                                                    parsed = urlparse(api_base)
                                                    if parsed.scheme == "http" and parsed.hostname and parsed.hostname not in ("localhost", "127.0.0.1"):
                                                        api_base = api_base.replace("http://", "https://", 1)
                                                except Exception:
                                                    pass

                                                public_url = api_base.rstrip("/") + f"/p/{pid}"

                                                # Avoid duplicating a public link when the original
                                                # payload already included a keyboard with a URL button
                                                # or already contained an explicit 'View formatted results' text.
                                                def _payload_has_url_button(pl):
                                                    try:
                                                        rm = pl.get("reply_markup") or {}
                                                        kb = rm.get("inline_keyboard")
                                                        if not kb:
                                                            return False
                                                        for row in kb:
                                                            for btn in row:
                                                                try:
                                                                    if isinstance(btn, dict):
                                                                        if btn.get("url"):
                                                                            return True
                                                                    else:
                                                                        if hasattr(btn, "url") and getattr(btn, "url"):
                                                                            return True
                                                                except Exception:
                                                                    continue
                                                        return False
                                                    except Exception:
                                                        return False

                                                text_already = False
                                                try:
                                                    text_already = "view formatted results" in (payload.get("text") or "").lower()
                                                except Exception:
                                                    text_already = False

                                                if not _payload_has_url_button(payload) and not text_already:
                                                    try:
                                                        tele_msg = f"View formatted results: {public_url}"
                                                        tele_payload = {"chat_id": payload.get("chat_id"), "text": tele_msg}
                                                        await client.post(tg_url, json=tele_payload, timeout=10)
                                                    except Exception:
                                                        logger.exception("_send_tg: failed to send public page link after fallback")
                                            except Exception:
                                                logger.exception("_send_tg: failed to construct public page link")
                                    except Exception:
                                        logger.exception("_send_tg: failed to save telegraph fallback page")
                            except Exception:
                                logger.exception("_send_tg: unexpected error while creating telegraph fallback")
                            return
                    except Exception:
                        logger.exception("Telegram fallback send failed")

                # For non-200 responses, either continue retrying or stop if
                # this was the last attempt.
                if attempt == retries:
                    try:
                        logger.error("Telegram sendMessage failed after {} attempts: status={} body={}", attempt, resp.status_code, (resp.text or ""))
                    except Exception:
                        logger.error("Telegram sendMessage failed after {} attempts: status={}", attempt, resp.status_code)
                    return
                else:
                    await asyncio.sleep(backoff * attempt)

            except Exception as exc:
                logger.warning("Telegram send attempt {} failed: {}", attempt, exc)
                if attempt == retries:
                    logger.exception("Failed to POST to Telegram API after retries: {}", exc)
                else:
                    await asyncio.sleep(backoff * attempt)


async def _answer_callback(token: str, callback_id: str, text: str | None = None, show_alert: bool = False) -> None:
    """Utility to answer a callback_query to remove the loading state."""
    if not callback_id:
        return
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    if show_alert:
        payload["show_alert"] = True
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload, timeout=5)
    except Exception:
        logger.exception("_answer_callback failed")


async def _send_markdown_full(token: str, chat_id: int, header: str, md_lines_or_text, reply_markup: object | None = None) -> None:
    """Send a large Markdown list by splitting into message-sized chunks.

    - `md_lines_or_text` may be a list of lines or a single string.
    - The first chunk will include `header` and attach `reply_markup` if provided.
    """
    try:
        from app.utils.helpers import chunk_lines_by_char_limit
    except Exception:
        chunk_lines_by_char_limit = None

    MAX_MSG = getattr(settings, "MAX_MSG", 4000)
    TELEGRAM_LIMIT = min(int(MAX_MSG), 4096)
    CHUNK_CHAR_LIMIT = max(800, int(MAX_MSG - 200))

    if md_lines_or_text is None:
        return

    if isinstance(md_lines_or_text, str):
        lines = md_lines_or_text.splitlines()
    else:
        lines = list(md_lines_or_text)

    if chunk_lines_by_char_limit:
        chunks = chunk_lines_by_char_limit(lines, CHUNK_CHAR_LIMIT)
    else:
        # fallback simple chunking by accumulated length
        chunks = []
        cur = []
        cur_len = 0
        for ln in lines:
            add = len(ln) + 1
            if cur and (cur_len + add > CHUNK_CHAR_LIMIT):
                chunks.append("\n".join(cur))
                cur = [ln]
                cur_len = add
            else:
                cur.append(ln)
                cur_len += add
        if cur:
            chunks.append("\n".join(cur))

    if not chunks:
        return

    # FORCE: always publish a formatted public page and send its link instead
    # of attempting to deliver large HTML/Markdown bodies inline. This keeps
    # behaviour consistent across environments and avoids Telegram parsing
    # differences. We attempt to save the full markdown into the internal
    # page store and send a single compact message with a URL/callback button.
    try:
        try:
            store = _ensure_internal_page_store()
        except Exception:
            store = None
        if store:
            try:
                try:
                    full_md = (header + "\n\n" + "\n".join(lines)) if header else "\n".join(lines)
                except Exception:
                    full_md = "\n".join(lines)
                # Save full content; auto-splitting handled by the store
                saved = await store.save_raw_page("__forced__", full_md, tokens=[], created_by=None)
                pid = saved.get("page_id") if isinstance(saved, dict) else None
                if pid:
                    try:
                        # Prefer explicit environment variables, then Render's
                        # external URL, then localhost fallback.
                        api_base = (
                            os.getenv("API_URL")
                            or os.getenv("API_BASE_URL")
                            or os.getenv("APP_URL")
                            or os.getenv("RENDER_EXTERNAL_URL")
                            or f"http://localhost:{os.getenv('PORT','8000')}"
                        )
                        # Ensure a scheme is present
                        if api_base and not api_base.lower().startswith(("http://", "https://")):
                            api_base = f"https://{api_base}"

                        # Promote http->https for public hosts (avoid changing localhost)
                        try:
                            from urllib.parse import urlparse

                            parsed = urlparse(api_base)
                            if parsed.scheme == "http" and parsed.hostname and parsed.hostname not in ("localhost", "127.0.0.1"):
                                api_base = api_base.replace("http://", "https://", 1)
                        except Exception:
                            pass

                        public_url = api_base.rstrip("/") + f"/p/{pid}"

                        # Create an 'Open formatted results' button and merge it
                        # with any incoming `reply_markup`. Prefer an HTTPS URL
                        # button when available; otherwise fall back to a
                        # callback button that opens the internal page.
                        try:
                            if str(public_url).lower().startswith("https://"):
                                btn = {"text": "Open formatted results", "url": public_url}
                            else:
                                btn = {"text": "Open formatted results", "callback_data": f"IP|{pid}"}
                        except Exception:
                            btn = {"text": "Open formatted results", "callback_data": f"IP|{pid}"}

                        try:
                            orig_rm = reply_markup
                            merged = None
                            if not orig_rm:
                                merged = {"inline_keyboard": [[btn]]}
                            else:
                                norm = None
                                if isinstance(orig_rm, (str, bytes)):
                                    try:
                                        norm = json.loads(orig_rm)
                                    except Exception:
                                        norm = None
                                elif isinstance(orig_rm, dict):
                                    norm = orig_rm
                                elif isinstance(orig_rm, list):
                                    norm = {"inline_keyboard": orig_rm}
                                else:
                                    try:
                                        if hasattr(orig_rm, "inline_keyboard"):
                                            norm = {"inline_keyboard": getattr(orig_rm, "inline_keyboard")}
                                        elif hasattr(orig_rm, "rows"):
                                            norm = {"inline_keyboard": getattr(orig_rm, "rows")}
                                        elif hasattr(orig_rm, "to_dict"):
                                            d = orig_rm.to_dict()
                                            if "inline_keyboard" in d:
                                                norm = d
                                            elif "rows" in d:
                                                norm = {"inline_keyboard": d.get("rows")}
                                    except Exception:
                                        norm = None

                                if norm and isinstance(norm, dict) and norm.get("inline_keyboard"):
                                    try:
                                        kb = list(norm.get("inline_keyboard"))
                                    except Exception:
                                        kb = norm.get("inline_keyboard") or []

                                    exists = False
                                    try:
                                        for row in kb:
                                            for b in row:
                                                try:
                                                    if isinstance(b, dict):
                                                        if btn.get("url") and b.get("url") == btn.get("url"):
                                                            exists = True
                                                        if btn.get("callback_data") and b.get("callback_data") == btn.get("callback_data"):
                                                            exists = True
                                                    else:
                                                        if hasattr(b, "url") and getattr(b, "url") == btn.get("url"):
                                                            exists = True
                                                        if hasattr(b, "callback_data") and getattr(b, "callback_data") == btn.get("callback_data"):
                                                            exists = True
                                                except Exception:
                                                    continue
                                    except Exception:
                                        exists = False

                                    if not exists:
                                        kb.append([btn])
                                    merged = {"inline_keyboard": kb}
                                else:
                                    merged = {"inline_keyboard": [[btn]]}
                        except Exception:
                            merged = {"inline_keyboard": [[btn]]}

                        reply_markup = merged

                        # send a clickable plaintext fallback replacing Markdown
                        # links with `Label — URL` and removing backslash escapes
                        try:
                            import re as _re
                            try:
                                from app.utils.helpers import chunk_lines_by_char_limit as _chunker, unescape_for_plain_text
                            except Exception:
                                _chunker = None
                                def unescape_for_plain_text(x):
                                    return x

                            pattern = _re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
                            try:
                                clickable_lines = [_re.sub(pattern, r"\1 — \2", ln) for ln in full_md.splitlines()]
                            except Exception:
                                clickable_lines = full_md.splitlines()

                            # Remove Markdown backslash-escapes so plaintext is readable
                            clickable_lines = [unescape_for_plain_text(ln) for ln in clickable_lines]

                            # Chunk these lines to avoid overly large messages
                            if _chunker:
                                try:
                                    clickable_chunks = _chunker(clickable_lines, CHUNK_CHAR_LIMIT)
                                except Exception:
                                    clickable_chunks = ["\n".join(clickable_lines)]
                            else:
                                # fallback simple chunking
                                clickable_chunks = []
                                _cur = []
                                _cur_len = 0
                                for ln in clickable_lines:
                                    add = len(ln) + 1
                                    if _cur and (_cur_len + add > CHUNK_CHAR_LIMIT):
                                        clickable_chunks.append("\n".join(_cur))
                                        _cur = [ln]
                                        _cur_len = add
                                    else:
                                        _cur.append(ln)
                                        _cur_len += add
                                if _cur:
                                    clickable_chunks.append("\n".join(_cur))

                            # If the first chunk contains no URLs/links (often
                            # only the header landed alone), merge it with the
                            # following chunk so the initial numbered item isn't
                            # split into the next message. This keeps numbering
                            # aligned with link targets for large results.
                            try:
                                if len(clickable_chunks) > 1:
                                    first_chunk = clickable_chunks[0]
                                    # consider it header-only if no http(s) URL or
                                    # label->URL pattern present. Avoid using a
                                    # bare em-dash as a signal because headers
                                    # themselves contain an em-dash (e.g. "Search: ... — part 1/3").
                                    if not (re.search(r'https?://', first_chunk) or re.search(r'—\s*https?://', first_chunk)):
                                        # merge first two chunks
                                        clickable_chunks[0] = first_chunk + "\n\n" + clickable_chunks[1]
                                        del clickable_chunks[1]
                            except Exception:
                                pass

                            # Send clickable plaintext chunks; attach the reply_markup
                            # (callback or HTTPS url) only to the first chunk. Prefer
                            # attempting a MarkdownV2 send when lines follow the
                            # `Label — https://...` pattern so users get inline
                            # clickable links in Telegram; fall back to plain text
                            # if MarkdownV2 conversion or send fails.
                            for ci, ctext in enumerate(clickable_chunks):
                                try:
                                    rm = reply_markup if ci == 0 else None
                                    try:
                                        import re as _re2
                                        try:
                                            from app.utils.helpers import md_to_markdownv2
                                        except Exception:
                                            md_to_markdownv2 = None

                                        # Convert lines like `Label — https://t.me/...` back to
                                        # markdown link form `[Label](URL)` before escaping
                                        # for MarkdownV2. Support hyphen, en-dash and em-dash.
                                        try:
                                            md_pattern = _re2.compile(r'(.+?)\s+[—–-]\s+(https?://\S+)', flags=_re2.U)
                                            anchors_md = md_pattern.sub(r'[\1](\2)', ctext)
                                        except Exception:
                                            anchors_md = ctext

                                        if md_to_markdownv2:
                                            try:
                                                mdv2_text = md_to_markdownv2(anchors_md)
                                                await _send_tg(token, chat_id, mdv2_text, parse_mode="MarkdownV2", reply_markup=rm)
                                                continue
                                            except Exception:
                                                # fall through to plain-text send
                                                pass

                                        # Fallback: send as plain text
                                        await _send_tg(token, chat_id, ctext, parse_mode=None, reply_markup=rm)
                                    except Exception:
                                        # Last-resort: send plain text
                                        await _send_tg(token, chat_id, ctext, parse_mode=None, reply_markup=rm)
                                except Exception:
                                    logger.exception("_send_markdown_full: failed to send clickable plaintext chunk {}/{}", ci + 1, len(clickable_chunks))
                        except Exception:
                            logger.exception("_send_markdown_full: failed to build/send clickable plaintext fallback")

                        # (intentional) do not send an explicit textual public URL here
                    except Exception:
                        logger.exception("_send_markdown_full: failed to send public page link")
                    return
            except Exception:
                logger.exception("_send_markdown_full: failed to save forced public page")
    except Exception:
        # Don't allow forced-public failures to stop normal chunk sending
        logger.exception("_send_markdown_full: unexpected error in forced-public block")

    # Convert each chunk to HTML (anchors) and send with parse_mode=HTML.
    try:
        from app.utils.helpers import md_to_html, unescape_for_plain_text
    except Exception:
        md_to_html = None
        try:
            from app.utils.helpers import unescape_for_plain_text
        except Exception:
            def unescape_for_plain_text(x):
                return x

    for idx, chunk in enumerate(chunks):
        try:
            text_md_raw = (header + "\n\n" + chunk) if (idx == 0 and header) else chunk
            try:
                text_md = unescape_for_plain_text(text_md_raw)
            except Exception:
                text_md = text_md_raw

            if md_to_html:
                try:
                    text_html = md_to_html(text_md, one_per_line=True)
                except Exception:
                    text_html = html.escape(text_md)
            else:
                text_html = html.escape(text_md)

            # Diagnostic: log a short HTML preview at INFO so it appears
            # in normal server logs (not just DEBUG). This helps determine
            # whether HTML anchors are actually being generated/sent.
            try:
                logger.info("_send_markdown_full: sending chunk {}/{} len={} preview={}", idx + 1, len(chunks), len(text_html or ""), (text_html or "")[:320])
            except Exception:
                pass

            if idx == 0:
                await _send_tg(token, chat_id, text_html, parse_mode="HTML", reply_markup=reply_markup)
            else:
                await _send_tg(token, chat_id, text_html, parse_mode="HTML")
        except Exception:
            logger.exception("_send_markdown_full: failed to send chunk {}/{}", idx + 1, len(chunks))
            # continue to next chunk
    return


@app.get("/health")
async def health():
    mongo_client = getattr(app.state, "mongo_client", None)
    db = getattr(app.state, "db", None)
    try:
        if mongo_client is None or db is None:
            raise Exception("MongoDB not configured")
        await mongo_client.admin.command("ping")
        files = await db.get_collection("files").estimated_document_count()
        return {"ok": True, "files": files}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/_diag/mongo_tls")
async def diag_mongo_tls(request: Request, host: str | None = None):
    """Protected diagnostic endpoint to probe MongoDB TLS handshake from the running service.

    Requires the `DIAG_SECRET` env var to be set and the same value provided in the
    `X-DIAG-TOKEN` header or `token` query parameter. Returns OpenSSL version and
    whether a TLS handshake to the Mongo host succeeds (or the exception).
    """
    diag_secret = getattr(settings, "DIAG_SECRET", None)
    if not diag_secret:
        return JSONResponse(status_code=404, content={"ok": False, "error": "diagnostics disabled"})

    token = request.headers.get("X-DIAG-TOKEN") or request.query_params.get("token")
    if not token or token != diag_secret:
        return JSONResponse(status_code=403, content={"ok": False, "error": "unauthorized"})

    uri = settings.MONGO_URI or ""
    # derive a reasonable host to probe from the URI
    import re

    host_candidate = None
    m = re.match(r"mongodb\+srv://(?:[^@]+@)?([^/]+)", uri or "", re.I)
    if m:
        host_candidate = m.group(1).split(",")[0]
    else:
        m = re.match(r"mongodb://(?:[^@]+@)?([^/]+)", uri or "", re.I)
        if m:
            hosts_part = m.group(1)
            host_candidate = hosts_part.split(",")[0].split(":" )[0]

    target = host or host_candidate or "localhost"

    loop = asyncio.get_running_loop()

    def _probe(h):
        import ssl, certifi, socket

        out = {"open_ssl": ssl.OPENSSL_VERSION}
        try:
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.load_verify_locations(cafile=certifi.where())

        try:
            with ctx.wrap_socket(socket.socket(), server_hostname=h) as s:
                s.settimeout(10)
                s.connect((h, 27017))
                out["tls_ok"] = True
                out["tls_version"] = s.version()
        except Exception as e:
            out["tls_ok"] = False
            out["error"] = repr(e)
        return out

    result = await loop.run_in_executor(None, _probe, target)
    return JSONResponse(status_code=200, content={"ok": True, "host": target, **result})


@app.get("/_diag/internal_page_store")
async def diag_internal_page_store(request: Request, create: bool | None = Query(False), token: str | None = None):
    """Diagnostic: report internal page store status and optionally create a test page.

    Protected by `DIAG_SECRET` (use `X-DIAG-TOKEN` header or `token` query param).
    """
    diag_secret = getattr(settings, "DIAG_SECRET", None)
    if not diag_secret:
        return JSONResponse(status_code=404, content={"ok": False, "error": "diagnostics disabled"})

    header_token = request.headers.get("X-DIAG-TOKEN")
    provided = header_token or token
    if not provided or provided != diag_secret:
        return JSONResponse(status_code=403, content={"ok": False, "error": "unauthorized"})

    # Ensure a store exists (creates in-memory fallback if needed)
    store = _ensure_internal_page_store()
    info = {"ok": True, "store_present": bool(store)}
    try:
        from app.services.internal_pages import InternalPageStore, InMemoryInternalPageStore

        if isinstance(store, InMemoryInternalPageStore):
            info["store_type"] = "InMemoryInternalPageStore"
            info["page_count"] = len(getattr(store, "pages", {}))
        elif isinstance(store, InternalPageStore):
            info["store_type"] = "InternalPageStore"
            try:
                db_name = getattr(settings, "TELEGRAPH_DB", "course_bot")
                coll = store.client[db_name].get_collection("telegraph_pages")
                cnt = await coll.estimated_document_count()
                info["page_count"] = int(cnt or 0)
            except Exception:
                info["page_count"] = None
        else:
            info["store_type"] = str(type(store))
    except Exception:
        info["store_type"] = str(type(store))

    # Optionally create a quick test page and return its id
    if create and store:
        try:
            toks = []
            saved = await store.save_raw_page("_diag_test_", "Test page content", toks, created_by=None, group=None, part_index=0, total_parts=1)
            info["created_test_page"] = saved.get("page_id")
        except Exception:
            info["created_test_page_error"] = True

    return JSONResponse(status_code=200, content=info)


@app.get("/p/{page_id}", response_class=HTMLResponse)
async def public_page(request: Request, page_id: str):
    """Serve a saved internal page as a public HTML page.

    The endpoint renders stored Markdown-style links into HTML anchors
    so that links are clickable when opened in a browser. The base URL
    used for links is taken from the `API_URL` / `API_BASE_URL` / `APP_URL`
    environment variables when available; otherwise the page still renders
    the stored anchors as-is.
    """
    try:
        store = _ensure_internal_page_store()
    except Exception:
        store = None
    if not store:
        return PlainTextResponse("Internal page store unavailable", status_code=503)

    page = await store.get_page(page_id)
    if not page:
        return PlainTextResponse("Page not found", status_code=404)

    # Build combined markdown from group parts
    gp = page.get("group_pages") or [page]
    md_lines = []
    for part in gp:
        md_lines.extend((part.get("content") or "").splitlines())
    md_text = "\n".join(md_lines)

    try:
        from app.utils.helpers import md_to_html, normalize_bracket_links

        # Normalize bracketed-label + bare-URL patterns into Markdown links
        # so that md_to_html produces anchors and downstream enrichment
        # can rely on link-aware content. Update md_lines to match normalized text.
        try:
            md_text = normalize_bracket_links(md_text)
            md_lines = md_text.splitlines()
        except Exception:
            pass

        html_body = md_to_html(md_text, one_per_line=True)
    except Exception:
        html_body = html.escape(md_text)

    # Ensure anchor tags open in a new tab/window
    try:
        html_body = re.sub(r'<a\s+href="', '<a target="_blank" rel="noopener noreferrer" href="', html_body)
    except Exception:
        pass

    # If no anchors were produced, attempt a best-effort enrichment:
    # resolve lines that look like video filenames to t.me message links
    # by querying the `files` collection. This helps when stored pages
    # contained plain filenames (no Markdown links).
    try:
        if "<a " not in (html_body or ""):
            # If we have explicit per-line refs stored with the page, prefer
            # using them to create deterministic t.me anchors rather than
            # performing fuzzy DB lookups.
            try:
                md_line_refs = []
                for part in gp:
                    part_lines = (part.get("content") or "").splitlines()
                    part_refs = part.get("line_refs") or []
                    # pad refs to match lines count
                    if part_refs is None:
                        part_refs = []
                    while len(part_refs) < len(part_lines):
                        part_refs.append(None)
                    md_line_refs.extend(part_refs)
            except Exception:
                md_line_refs = []

            try:
                # If at least one per-line ref looks usable, render anchors from them
                has_refs = any((r and r.get("chat_id") and r.get("message_id")) for r in md_line_refs)
            except Exception:
                has_refs = False

            if md_line_refs and has_refs:
                try:
                    new_html_lines = []
                    for ln, ref in zip(md_lines, md_line_refs):
                        try:
                            # strip numeric prefix like '1) '
                            label = re.sub(r'^\s*\d+\)\s*', '', ln)
                            # remove markdown link if present
                            label = re.sub(r'\[([^\]]+)\]\([^)]+\)', r"\1", label)
                            label_esc = html.escape(label)
                            if ref and ref.get("chat_id") and ref.get("message_id"):
                                s = str(ref.get("chat_id"))
                                base = s[4:] if s.startswith("-100") else s.lstrip("-")
                                url = f"https://t.me/c/{base}/{int(ref.get('message_id'))}"
                                # include a compact match-type indicator when available
                                mt = ref.get("match_type")
                                if mt and mt not in ("unknown", None):
                                    mt_esc = html.escape(str(mt))
                                    new_html_lines.append(f'<a target="_blank" rel="noopener noreferrer" href="{url}">{label_esc}</a> <span style="color:#666;font-size:0.9em">({mt_esc})</span>')
                                else:
                                    new_html_lines.append(f'<a target="_blank" rel="noopener noreferrer" href="{url}">{label_esc}</a>')
                            else:
                                new_html_lines.append(label_esc)
                        except Exception:
                            new_html_lines.append(html.escape(ln))
                    html_body = '<br/>\n'.join(new_html_lines)
                except Exception:
                    # fallback to DB-based enrichment below
                    md_line_refs = []
                    pass
            else:
                try:
                    db = getattr(app.state, "db", None)
                except Exception:
                    db = None
            if db is not None:
                try:
                    coll = db.get_collection("files")
                    video_exts = "mp4|mkv|webm|mov|avi|flv|m4v|ts|mpeg|mpg"
                    # Match the last filename-like token ending with a known video extension.
                    # - Use greedy matching so we capture the final extension on the line.
                    # - Allow and ignore common trailing punctuation (])}).,;:"')
                    # - Use Unicode flag for broad character support.
                    pattern = re.compile(r'(.+\.(?:' + video_exts + r'))(?:[\)\]\}\.,;:\"\']*)\s*$', re.I | re.U)
                    try:
                        from app.utils.helpers import unescape_for_plain_text
                    except Exception:
                        def unescape_for_plain_text(x):
                            return x or ""
                    import urllib.parse

                    enriched_lines = []
                    # Limit resolution attempts to avoid excessive DB load
                    MAX_RESOLVE = 500
                    resolved = 0
                    async def _find_best_file_doc(filename: str):
                        """Find the best-matching file doc for `filename`.

                        Preference order:
                          1) exact (case-insensitive) filename
                          2) URL-decoded exact filename
                          3) suffix match (last 60 chars)
                          4) fuzzy token-ordered match
                          5) substring token matches (try top fragments)

                        Return policy:
                          - If a candidate's normalized filename key equals the
                            normalized input key, return it immediately.
                          - Otherwise keep a fallback candidate and accept it
                            only when there is meaningful normalized-token
                            overlap (or when input normalization is empty).

                        Tie-breaker: newest `timestamp` then highest `message_id`.
                        """
                        try:
                            proj = {"chat_id": 1, "message_id": 1, "timestamp": 1, "filename": 1}
                            sort_order = [("timestamp", -1), ("message_id", -1)]
                            from app.utils.helpers import normalize_filename_key

                            # normalized key for the requested filename
                            try:
                                fname_norm_input = normalize_filename_key(filename)
                            except Exception:
                                fname_norm_input = ""

                            # Aggregate candidate lists from various strategies then score
                            candidates = []

                            def _add_candidates(res_list):
                                try:
                                    for c in (res_list or []):
                                        # avoid duplicates by chat_id/message_id
                                        key = (int(c.get("chat_id") or 0), int(c.get("message_id") or 0))
                                        if key not in seen_keys:
                                            seen_keys.add(key)
                                            candidates.append(c)
                                except Exception:
                                    pass

                            seen_keys = set()

                            # Fast path: lookup by normalized filename key (eager-indexed)
                            if fname_norm_input:
                                try:
                                    res = await coll.find({"filename_norm": fname_norm_input}, proj).sort(sort_order).to_list(length=5)
                                    if res:
                                        # prefer newest match
                                        return res[0]
                                except Exception:
                                    pass

                            # 1) exact (case-insensitive)
                            try:
                                q = {"filename": {"$regex": f'^{re.escape(filename)}$', "$options": "i"}}
                                res = await coll.find(q, proj).sort(sort_order).to_list(length=10)
                                # if any exact normalized match, return immediately
                                for cand in res:
                                    try:
                                        if normalize_filename_key(cand.get("filename") or "") == fname_norm_input:
                                            return cand
                                    except Exception:
                                        pass
                                _add_candidates(res)
                            except Exception:
                                pass

                            # 2) URL-decoded exact
                            try:
                                filename_unq = urllib.parse.unquote(filename)
                            except Exception:
                                filename_unq = filename
                            if filename_unq and filename_unq != filename:
                                try:
                                    q = {"filename": {"$regex": f'^{re.escape(filename_unq)}$', "$options": "i"}}
                                    res = await coll.find(q, proj).sort(sort_order).to_list(length=10)
                                    for cand in res:
                                        try:
                                            if normalize_filename_key(cand.get("filename") or "") == fname_norm_input:
                                                return cand
                                        except Exception:
                                            pass
                                    _add_candidates(res)
                                except Exception:
                                    pass

                            # 3) suffix match (prefer most recent)
                            try:
                                short = filename[-60:]
                                q = {"filename": {"$regex": re.escape(short) + r'$', "$options": "i"}}
                                res = await coll.find(q, proj).sort(sort_order).to_list(length=10)
                                for cand in res:
                                    try:
                                        if normalize_filename_key(cand.get("filename") or "") == fname_norm_input:
                                            return cand
                                    except Exception:
                                        pass
                                _add_candidates(res)
                            except Exception:
                                pass

                            # 4) fuzzy token-ordered match
                            try:
                                norm = re.sub(r'[^a-z0-9]+', ' ', filename.lower()).strip()
                                tokens_norm = [t for t in norm.split() if t]
                                if tokens_norm:
                                    tokens_use = tokens_norm[:6]
                                    fuzzy_pattern = ".*".join([re.escape(t) for t in tokens_use]) + r'.*$'
                                    q = {"filename": {"$regex": fuzzy_pattern, "$options": "i"}}
                                    res = await coll.find(q, proj).sort(sort_order).to_list(length=50)
                                    for cand in res:
                                        try:
                                            if normalize_filename_key(cand.get("filename") or "") == fname_norm_input:
                                                return cand
                                        except Exception:
                                            pass
                                    _add_candidates(res)
                            except Exception:
                                pass

                            # 5) try by longer token fragments
                            try:
                                long_tokens = [t for t in (re.split(r'[^a-z0-9]+', filename.lower()) or []) if len(t) >= 4]
                                for tok in long_tokens[:3]:
                                    try:
                                        q = {"filename": {"$regex": re.escape(tok), "$options": "i"}}
                                        res = await coll.find(q, proj).sort(sort_order).to_list(length=10)
                                        for cand in res:
                                            try:
                                                if normalize_filename_key(cand.get("filename") or "") == fname_norm_input:
                                                    return cand
                                            except Exception:
                                                pass
                                        _add_candidates(res)
                                    except Exception:
                                        continue
                            except Exception:
                                pass

                            # If no candidates collected, nothing to do
                            if not candidates:
                                return None

                            # If filename seems like a video, prefer the most recent candidate
                            try:
                                file_ext = (filename.rsplit('.', 1)[1] or "").lower() if '.' in filename else ""
                            except Exception:
                                file_ext = ""
                            video_exts_set = {"mp4", "mkv", "webm", "mov", "avi", "flv", "m4v", "ts", "mpeg", "mpg"}
                            if file_ext in video_exts_set and candidates:
                                # prefer newest candidate (already sorted by timestamp desc)
                                return candidates[0]

                            # Score remaining candidates by multiple heuristics
                            best = None
                            best_score = 0.0
                            try:
                                # precompute input trigrams/tokens
                                try:
                                    fname_tris = make_trigrams((filename or "")[:200], TRIGRAM_MAX)
                                except Exception:
                                    fname_tris = []
                                in_tokens = set(t for t in fname_norm_input.split() if t)
                                for cand in candidates:
                                    try:
                                        score = 0.0
                                        cand_fname = (cand.get("filename") or "")
                                        cand_norm = normalize_filename_key(cand_fname)
                                        # normalized equality
                                        if cand_norm and fname_norm_input and cand_norm == fname_norm_input:
                                            score += 3.0
                                        # suffix match
                                        try:
                                            if cand_fname and filename and cand_fname.lower().endswith(filename.lower()):
                                                score += 1.5
                                        except Exception:
                                            pass
                                        # token overlap
                                        try:
                                            cand_tokens = set(t for t in cand_norm.split() if t)
                                            if in_tokens and cand_tokens:
                                                overlap = len(in_tokens & cand_tokens) / float(len(in_tokens | cand_tokens))
                                                score += overlap * 1.5
                                        except Exception:
                                            pass
                                        # trigram similarity
                                        try:
                                            tri_sim = trigram_similarity(fname_tris, cand.get("trigrams", []) or [])
                                            score += tri_sim * 1.5
                                        except Exception:
                                            pass
                                        # recency boost
                                        try:
                                            ts = cand.get("timestamp")
                                            ts_dt = None
                                            if isinstance(ts, str):
                                                try:
                                                    ts_dt = datetime.fromisoformat(ts)
                                                except Exception:
                                                    ts_dt = None
                                            elif isinstance(ts, datetime):
                                                ts_dt = ts
                                            if ts_dt:
                                                age_seconds = (datetime.utcnow() - ts_dt).total_seconds()
                                                window = 30 * 24 * 3600
                                                recency = max(0.0, (window - age_seconds) / window)
                                                score += recency * 0.5
                                        except Exception:
                                            pass

                                        if score > best_score:
                                            best_score = score
                                            best = cand
                                    except Exception:
                                        continue
                            except Exception:
                                pass

                            # Accept best candidate if it meets a conservative threshold
                            try:
                                if best and best_score >= 0.6:
                                    return best
                            except Exception:
                                pass

                        except Exception:
                            return None
                        return None

                    for ln in md_lines:
                        try:
                            if resolved >= MAX_RESOLVE:
                                enriched_lines.append(html.escape(ln))
                                continue
                            ln_un = unescape_for_plain_text(ln or "")
                            # Remove common list prefixes like '1) ', '1. ', '- ', '* ', '• '
                            ln_clean = re.sub(r'^\s*(?:\d+[\)\.]\s*|[-\*\u2022]\s*)', '', ln_un)
                            m = pattern.search(ln_clean)
                            if m:
                                filename = m.group(1).strip()
                                doc = await _find_best_file_doc(filename)
                                if doc and doc.get("chat_id") and doc.get("message_id"):
                                    cid = doc.get("chat_id")
                                    mid = doc.get("message_id")
                                    try:
                                        s = str(cid)
                                        if s.startswith("-100"):
                                            base = s[4:]
                                            url = f"https://t.me/c/{base}/{mid}"
                                        else:
                                            url = f"https://t.me/{s}/{mid}"
                                    except Exception:
                                        url = None
                                    if url:
                                        prefix = ln_un[: m.start(1)]
                                        prefix_html = html.escape(prefix)
                                        label_html = html.escape(filename)
                                        url_attr = html.escape(url, quote=True)
                                        line_html = f'{prefix_html}<a target="_blank" rel="noopener noreferrer" href="{url_attr}">{label_html}</a>'
                                        enriched_lines.append(line_html)
                                        resolved += 1
                                        continue
                            enriched_lines.append(html.escape(ln))
                        except Exception:
                            enriched_lines.append(html.escape(ln))
                    if enriched_lines:
                        html_body = "<br/>\n".join(enriched_lines)
                except Exception:
                    pass
    except Exception:
        pass

    page_query = html.escape(page.get("query") or "")
    title = f"Search: {page_query} — {page.get('total_results') or 0} results" if page_query else "Search results"

    # Render top links if present
    tlinks = page.get("top_links") or []
    top_html = ""
    if tlinks:
        parts = []
        for tl in tlinks:
            try:
                text = html.escape((tl.get("text") or "")[:80])
                url = html.escape(tl.get("url") or "")
                parts.append(f'<a href="{url}" target="_blank" rel="noopener noreferrer">{text}</a>')
            except Exception:
                continue
        if parts:
            top_html = "<div class=\"top-links\">" + " | ".join(parts) + "</div><hr/>"

    html_page = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>body{{font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif;padding:16px}}a{{color:#1a73e8}}</style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  {top_html}
  <div class="content">{html_body}</div>
  <hr/>
  <small>Page id: {html.escape(page_id)}</small>
</body>
</html>"""

    return HTMLResponse(content=html_page, status_code=200)


@app.get("/_admin/page/{page_id}")
async def admin_get_page(request: Request, page_id: str, token: str | None = None):
    """Admin endpoint to fetch an internally-stored page by `page_id`.

    Protected by the `DIAG_SECRET` value. Supply the secret via the
    `X-DIAG-TOKEN` header or the `token` query parameter.
    """
    diag_secret = getattr(settings, "DIAG_SECRET", None)
    if not diag_secret:
        return JSONResponse(status_code=404, content={"ok": False, "error": "admin endpoints disabled"})

    header_token = request.headers.get("X-DIAG-TOKEN")
    provided = header_token or token
    if not provided or provided != diag_secret:
        return JSONResponse(status_code=403, content={"ok": False, "error": "unauthorized"})

    db = getattr(app.state, "db", None)
    if db is None:
        return JSONResponse(status_code=503, content={"ok": False, "error": "DB unavailable"})

    try:
        coll = db.get_collection(getattr(settings, "TELEGRAPH_DB", "course_bot")).get_collection("telegraph_pages") if False else db.client[getattr(settings, "TELEGRAPH_DB", "course_bot")].get_collection("telegraph_pages")
        doc = await coll.find_one({"_id": __import__("bson").ObjectId(page_id)})
        if not doc:
            return JSONResponse(status_code=404, content={"ok": False, "error": "not found"})
        # normalize datetime
        try:
            if isinstance(doc.get("created_at"), __import__("datetime").datetime):
                doc["created_at"] = doc["created_at"].isoformat()
        except Exception:
            pass
        # convert ObjectId
        try:
            if "_id" in doc:
                doc["_id"] = str(doc["_id"])
        except Exception:
            pass
        return JSONResponse(status_code=200, content={"ok": True, "page": doc})
    except Exception as exc:
        logger.exception("admin_get_page failed: {}", exc)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@app.post("/_admin/cache/invalidate")
async def admin_cache_invalidate(request: Request, token: str | None = None):
    """Invalidate cached search entries.

    Accepts JSON body with either `query` to invalidate specific query cache,
    `key` to remove a specific cache key, or `all: true` to clear the cache.
    Protected by `DIAG_SECRET`.
    """
    diag_secret = getattr(settings, "DIAG_SECRET", None)
    if not diag_secret:
        return JSONResponse(status_code=404, content={"ok": False, "error": "admin endpoints disabled"})

    header_token = request.headers.get("X-DIAG-TOKEN")
    provided = header_token or token
    if not provided or provided != diag_secret:
        return JSONResponse(status_code=403, content={"ok": False, "error": "unauthorized"})

    try:
        body = await request.json()
    except Exception:
        body = {}

    cache = getattr(app.state, "search_cache", None)
    if cache is None:
        return JSONResponse(status_code=503, content={"ok": False, "error": "cache unavailable"})

    try:
        if body.get("all"):
            await cache.clear()
            return JSONResponse(status_code=200, content={"ok": True, "cleared": True})
        if body.get("key"):
            await cache.delete(body.get("key"))
            return JSONResponse(status_code=200, content={"ok": True, "deleted": True})
        if body.get("query"):
            await cache.invalidate_by_query(body.get("query"))
            return JSONResponse(status_code=200, content={"ok": True, "invalidated_query": body.get("query")})
        return JSONResponse(status_code=400, content={"ok": False, "error": "missing parameters"})
    except Exception as exc:
        logger.exception("cache invalidate failed: {}", exc)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@app.get("/_admin/cache/stats")
async def admin_cache_stats(request: Request, token: str | None = None):
    diag_secret = getattr(settings, "DIAG_SECRET", None)
    if not diag_secret:
        return JSONResponse(status_code=404, content={"ok": False, "error": "admin endpoints disabled"})
    header_token = request.headers.get("X-DIAG-TOKEN")
    provided = header_token or token
    if not provided or provided != diag_secret:
        return JSONResponse(status_code=403, content={"ok": False, "error": "unauthorized"})

    cache = getattr(app.state, "search_cache", None)
    if cache is None:
        return JSONResponse(status_code=503, content={"ok": False, "error": "cache unavailable"})

    try:
        stats = await cache.stats()
        return JSONResponse(status_code=200, content={"ok": True, "stats": stats})
    except Exception as exc:
        logger.exception("cache stats failed: {}", exc)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@app.get("/search")
async def api_search(
    q: str = Query(..., min_length=1, max_length=128),
    page: int = 1,
    per_page: int = 50,
    output_format: str = Query("json"),
    save_telegraph: bool = Query(False),
    thread_id: int | None = Query(None),
    strict: bool = Query(False),
):
    tokens = tokenize_query(q)
    if not tokens:
        return {"results": [], "total": 0}

    # Try server-side cache first
    cache = getattr(app.state, "search_cache", None)
    cache_key = None
    if cache:
        try:
            cache_key = cache.make_key(q, page, per_page, strict, thread_id)
            cached = await cache.get(cache_key)
            if cached and isinstance(cached, dict):
                return cached
        except Exception:
            logger.exception("search cache get failed")

    db = getattr(app.state, "db", None)
    if db is None:
        logger.warning("api_search: MongoDB unavailable, returning empty results for query={}", q)
        return {"results": [], "total": 0}

    try:
        coll = db.get_collection("files")
    except Exception as exc:
        logger.exception("api_search: failed to get collection: {}", exc)
        return {"results": [], "total": 0}

    # permissive match: return docs that contain any of the tokens (less strict than $all)
    flex_filter = {"title_tokens": {"$in": tokens}}
    projection = {
        "_id": 0,
        "chat_id": 1,
        "message_thread_id": 1,
        "message_id": 1,
        "filename": 1,
        "timestamp": 1,
        "title_tokens": 1,
        "quality_tokens": 1,
        "codec_tokens": 1,
        "year": 1,
        # metadata fields
        "file_id": 1,
        "file_size": 1,
        "duration": 1,
        "width": 1,
        "height": 1,
        "codec": 1,
        "thumbnail": 1,
        "path": 1,
    }
    # Candidate selection: prefer Atlas Search aggregation when available.
    from app.services.search_utils import make_trigrams, trigram_similarity, TRIGRAM_MAX

    start = (page - 1) * per_page

    def _uri_uses_tls_local(uri: str) -> bool:
        if not uri:
            return False
        u = uri.lower()
        if u.startswith("mongodb+srv://"):
            return True
        if "tls=true" in u or "ssl=true" in u:
            return True
        if "mongodb.net" in u:
            return True
        return False

    use_atlas = os.getenv("ENABLE_ATLAS_SEARCH", "").lower() in ("1", "true", "yes") or _uri_uses_tls_local(getattr(settings, "MONGO_URI", ""))

    docs = []
    total = 0

    if use_atlas:
        # Build Atlas $search aggregation pipeline using field boosts from env
        try:
            def _get_w(name, default):
                try:
                    return float(os.getenv(name, str(default)))
                except Exception:
                    return float(default)

            w_title = _get_w("RANK_TITLE_WEIGHT", TITLE_WEIGHT)
            w_quality = _get_w("RANK_QUALITY_WEIGHT", QUALITY_WEIGHT)
            w_codec = _get_w("RANK_CODEC_WEIGHT", CODEC_WEIGHT)
            w_year = _get_w("RANK_YEAR_WEIGHT", YEAR_WEIGHT)
            w_prefix = _get_w("RANK_PREFIX_BOOST", PREFIX_BOOST)
            w_trigram = _get_w("RANK_TRIGRAM_WEIGHT", TRIGRAM_WEIGHT)
            w_fname = _get_w("RANK_FILENAME_MATCH", FILENAME_MATCH)

            fuzzy_enabled = os.getenv("ATLAS_SEARCH_FUZZY", "true").lower() in ("1", "true", "yes")
            fuzzy_obj = {"maxEdits": 1, "prefixLength": 1} if fuzzy_enabled else None

            should = []
            # title tokens (high precision)
            should.append({
                "text": {"query": q, "path": "title_tokens", "score": {"boost": {"value": w_title}}}
            })
            # aggregated search_text (broad fuzzy match)
            should.append({
                "text": {"query": q, "path": "search_text", "score": {"boost": {"value": w_trigram}}}
            })
            # filename exact / substring
            should.append({
                "text": {"query": q, "path": "filename", "score": {"boost": {"value": w_fname}}}
            })
            # quality and codec
            should.append({"text": {"query": q, "path": "quality_tokens", "score": {"boost": {"value": w_quality}}}})
            should.append({"text": {"query": q, "path": "codec_tokens", "score": {"boost": {"value": w_codec}}}})

            # attach fuzzy where supported
            if fuzzy_obj:
                for s in should:
                    try:
                        tx = s.get("text")
                        if tx is not None:
                            tx["fuzzy"] = fuzzy_obj
                    except Exception:
                        pass

            # If strict query behavior requested, require more matches.
            try:
                if strict:
                    min_should = max(1, min(len(tokens), 5))
                else:
                    min_should = 1
            except Exception:
                min_should = 1

            search_stage = {"$search": {"compound": {"should": should, "minimumShouldMatch": min_should}}}

            pipeline = [search_stage, {"$addFields": {"score": {"$meta": "searchScore"}}}]

            # apply filters (thread_id) after searchStage
            if thread_id is not None:
                try:
                    tid = int(thread_id)
                    pipeline.append({"$match": {"message_thread_id": tid}})
                except Exception:
                    pass

            # projection stage: include requested projection fields and score
            proj_fields = {k: 1 for k in projection.keys()}
            proj_fields["score"] = 1
            pipeline.append({"$project": proj_fields})

            # sorting + pagination
            pipeline.append({"$sort": {"score": -1, "timestamp": -1}})
            if start:
                pipeline.append({"$skip": start})
            pipeline.append({"$limit": per_page})

            docs = await coll.aggregate(pipeline).to_list(length=per_page)

            # count total matches (separate, may be expensive)
            try:
                count_pipeline = [search_stage]
                if thread_id is not None:
                    try:
                        tid = int(thread_id)
                        count_pipeline.append({"$match": {"message_thread_id": tid}})
                    except Exception:
                        pass
                count_pipeline.append({"$count": "total"})
                cnt = await coll.aggregate(count_pipeline).to_list(length=1)
                total = int(cnt[0]["total"]) if cnt else 0
            except Exception:
                total = len(docs)
        except Exception:
            logger.exception("api_search: atlas aggregation failed, falling back to legacy search")
            docs = []

    if not docs:
        # Eager candidate selection: exact normalized filename -> token filters -> small fuzzy (trigrams)
        try:
            q_tris = make_trigrams(q or "", TRIGRAM_MAX)
            # helper to collect unique candidates preserving order
            candidates = []
            seen = set()
            def _add(res_list):
                try:
                    for c in (res_list or []):
                        try:
                            key = (int(c.get("chat_id") or 0), int(c.get("message_id") or 0))
                        except Exception:
                            key = (0, 0)
                        if key not in seen:
                            seen.add(key)
                            candidates.append(c)
                except Exception:
                    pass

            sort_order = [("timestamp", -1), ("message_id", -1)]
            # attempt eager normalized filename lookup first
            try:
                from app.utils.helpers import normalize_filename_key
                qnorm = normalize_filename_key(q or "")
            except Exception:
                qnorm = ""

            if qnorm:
                try:
                    qn = {"filename_norm": qnorm}
                    if thread_id is not None:
                        try:
                            qn["message_thread_id"] = int(thread_id)
                        except Exception:
                            pass
                    res = await coll.find(qn, projection).sort(sort_order).to_list(length=50)
                    _add(res)
                except Exception:
                    pass

            # token filters: strict/all then any
            try:
                if tokens:
                    q_all = {"title_tokens": {"$all": tokens}}
                    if thread_id is not None:
                        try:
                            q_all = {"$and": [q_all, {"message_thread_id": int(thread_id)}]}
                        except Exception:
                            pass
                    res_all = await coll.find(q_all, projection).sort(sort_order).to_list(length=500)
                    _add(res_all)
            except Exception:
                pass

            if not candidates:
                try:
                    if tokens:
                        q_any = {"title_tokens": {"$in": tokens}}
                        if thread_id is not None:
                            try:
                                q_any = {"$and": [q_any, {"message_thread_id": int(thread_id)}]}
                            except Exception:
                                pass
                        res_any = await coll.find(q_any, projection).sort(sort_order).to_list(length=2000)
                        _add(res_any)
                except Exception:
                    pass

            # small fuzzy/trigram fallback
            if not candidates:
                try:
                    q_tri = {"trigrams": {"$in": q_tris}} if q_tris else {}
                    if thread_id is not None:
                        try:
                            if q_tri:
                                q_tri = {"$and": [q_tri, {"message_thread_id": int(thread_id)}]}
                            else:
                                q_tri = {"message_thread_id": int(thread_id)}
                        except Exception:
                            pass
                    if q_tri:
                        res_tri = await coll.find(q_tri, projection).sort(sort_order).to_list(length=5000)
                        _add(res_tri)
                except Exception:
                    pass

            docs = candidates
        except Exception as exc:
            logger.exception("api_search: eager DB candidate query failed: {}", exc)
            docs = []

    # If no candidate results, try MongoDB text search on `search_text` field
    if not docs:
        try:
            # larger text search depth
            docs = await coll.find({"$text": {"$search": q}}, projection).to_list(length=2000)
        except Exception:
            docs = []

    # Score and rank results
    results = []
    qlower = q.lower()
    # If docs already contain a DB-computed `score` (Atlas Search), reuse it.
    if any(d.get("score") is not None for d in docs):
        for doc in docs:
            try:
                doc["_score"] = float(doc.get("score") or 0.0)
            except Exception:
                doc["_score"] = 0.0
            results.append(doc)
    else:
        # reuse query trigrams (already computed)
        # normalize query for phrase/filename matching
        try:
            q_norm = re.sub(r"[^a-z0-9\s]", " ", qlower).strip()
        except Exception:
            q_norm = qlower

        for doc in docs:
            score = 0.0
            doc_titles = [t.lower() for t in doc.get("title_tokens", [])]
            matched = sum(1 for t in tokens if t.lower() in doc_titles)
            score += matched * TITLE_WEIGHT
            # phrase / exact title boosts
            try:
                doc_title_str = " ".join(doc_titles).strip()
                if q_norm and doc_title_str:
                    if q_norm == doc_title_str:
                        score += TITLE_WEIGHT * 6
                    elif doc_title_str.startswith(q_norm):
                        score += TITLE_WEIGHT * 3
                    elif q_norm in doc_title_str:
                        score += TITLE_WEIGHT * 2
            except Exception:
                pass
            for qt in doc.get("quality_tokens", []):
                if qt and any(qt == t.lower() for t in tokens):
                    score += QUALITY_WEIGHT
            for cd in doc.get("codec_tokens", []):
                if cd and any(cd == t.lower() for t in tokens):
                    score += CODEC_WEIGHT
            if doc.get("year") and any(str(doc.get("year")) == t for t in tokens):
                score += YEAR_WEIGHT
            if qlower and qlower in doc.get("filename", "").lower():
                score += FILENAME_MATCH
            fname_len = len(doc.get("filename", ""))
            # prefix boost
            for t in tokens:
                if any(tt.startswith(t.lower()) for tt in doc_titles):
                    score += PREFIX_BOOST
            # trigram similarity
            try:
                tri_sim = trigram_similarity(q_tris, doc.get("trigrams", []))
                score += tri_sim * TRIGRAM_WEIGHT
            except Exception:
                pass
            score -= fname_len / FNAME_LEN_PENALTY_DIV
            doc["_score"] = score
            results.append(doc)

    # fallback: prefix/regex if still no results
    if not results:
        or_clauses = []
        for t in tokens:
            or_clauses.append({"title_tokens": {"$elemMatch": {"$regex": f'^{t}', "$options": "i"}}})
            or_clauses.append({"filename": {"$regex": f'{t}', "$options": "i"}})
        try:
            docs2 = await coll.find({"$or": or_clauses}, projection).to_list(length=500)
        except Exception as exc:
            logger.exception("api_search: fallback DB query failed: {}", exc)
            return {"results": [], "total": 0}
        for doc in docs2:
            score = 0
            doc_titles = [t.lower() for t in doc.get("title_tokens", [])]
            matched = sum(1 for t in tokens if any(tt.startswith(t.lower()) for tt in doc_titles))
            score += matched * (TITLE_WEIGHT - 2)
            if qlower and qlower in doc.get("filename", "").lower():
                score += 2
            fname_len = len(doc.get("filename", ""))
            score -= fname_len / 300.0
            doc["_score"] = score
            results.append(doc)

    results.sort(key=lambda r: (r.get("_score", 0), r.get("timestamp")), reverse=True)
    # If we computed `total` earlier (Atlas count), prefer it; otherwise derive from results
    total = total if isinstance(total, int) and total > 0 else len(results)
    # When using Atlas Search the pipeline already applied skip/limit; preserve results as-is.
    try:
        use_atlas
    except NameError:
        use_atlas = False

    if use_atlas:
        page_results = results
    else:
        start = (page - 1) * per_page
        end = start + per_page
        page_results = results[start:end]

    # Support markdown output for easy export/pasting into chats
    if str(output_format).lower() in ("md", "markdown"):
        def is_video_doc(doc: dict) -> bool:
            """Return True for video-like documents (.mp4/.mov/.webm etc.)."""
            mime = (doc.get("mime") or doc.get("content_type") or "").lower()
            fname = (doc.get("filename") or doc.get("file_name") or "").lower()
            # Accept based on MIME when available (video/*) or known extensions
            if mime and ("video/" in mime or "mp4" in mime):
                return True
            try:
                # check common video extensions
                for ext in (".mp4", ".mov", ".webm", ".mkv", ".avi", ".flv", ".m4v", ".ts", ".mpeg", ".mpg"):
                    if fname.endswith(ext):
                        return True
            except Exception:
                pass
            return False

        md_lines = []
        video_results = [r for r in page_results if is_video_doc(r)]
        if not video_results:
            md_text = "_No video files in results._"
        else:
            for r in video_results:
                fname = r.get("filename") or r.get("file_name") or "-"
                display = str(fname).replace("\n", " ").strip()
                url = ""
                try:
                    chat_id = r.get("chat_id")
                    message_id = r.get("message_id")
                    if chat_id and message_id:
                        s = str(chat_id)
                        base = s[4:] if s.startswith("-100") else s.lstrip("-")
                        url = f"https://t.me/c/{base}/{message_id}"
                except Exception:
                    url = ""
                if url:
                    md_lines.append(f"[{display}]({url})")
                else:
                    md_lines.append(f"- {display}")
            md_text = "\n".join(md_lines)

        # Optionally save the markdown page into the telegraph DB
        if save_telegraph:
            try:
                # prefer the configured internal page store (Mongo-backed or in-memory)
                store = _ensure_internal_page_store()
                if store:
                    # split md_text into multiple pages by cumulative char size
                    lines = (md_text or "").splitlines()
                    MAX_MSG = getattr(settings, "MAX_MSG", 4000)
                    CHUNK_CHAR_LIMIT = max(800, int(MAX_MSG - 200))
                    pages = []
                    group = str(uuid.uuid4())
                    from app.utils.helpers import chunk_lines_with_refs

                    # Build per-line refs for mp4_results so exported pages can
                    # link back deterministically to the original messages.
                    md_line_refs = []
                    try:
                        for r in mp4_results:
                            ref = {}
                            try:
                                ref["chat_id"] = int(r.get("chat_id")) if r.get("chat_id") is not None else None
                            except Exception:
                                ref["chat_id"] = None
                            try:
                                ref["message_id"] = int(r.get("message_id")) if r.get("message_id") is not None else None
                            except Exception:
                                ref["message_id"] = None
                            try:
                                ref["filename"] = r.get("filename") or r.get("file_name") or ""
                            except Exception:
                                ref["filename"] = ""
                            try:
                                ref["match_score"] = float(r.get("_score")) if r.get("_score") is not None else None
                            except Exception:
                                ref["match_score"] = None
                            ref["match_type"] = "export"
                            md_line_refs.append(ref)
                    except Exception:
                        md_line_refs = []

                    chunks = chunk_lines_with_refs(lines, md_line_refs, CHUNK_CHAR_LIMIT)
                    total_parts = len(chunks)
                    total_results = len(lines)
                    try:
                        page_header = f"Export: {q} — {total_results} results"
                    except Exception:
                        page_header = f"Export: {q}"
                    for part_index, (chunk, chunk_refs) in enumerate(chunks):
                        try:
                            logger.debug("api_search(md): saving telegraph page part {}/{} query={} preview={}", part_index + 1, total_parts, q, (chunk[:200] if chunk else None))
                        except Exception:
                            pass
                        saved = await store.save_raw_page(q, chunk, tokens, created_by=None, group=group, part_index=part_index, total_parts=total_parts, total_results=total_results, page_header=page_header, line_refs=chunk_refs)
                        pages.append(saved.get("page_id"))
                    # build a combined reference and prepend first page
                    if pages:
                        try:
                            api_base = (
                                os.getenv("API_URL")
                                or os.getenv("API_BASE_URL")
                                or os.getenv("APP_URL")
                                or os.getenv("RENDER_EXTERNAL_URL")
                                or f"http://localhost:{os.getenv('PORT','8000')}"
                            )
                            if api_base and not api_base.lower().startswith(("http://", "https://")):
                                api_base = f"https://{api_base}"
                            try:
                                from urllib.parse import urlparse

                                parsed = urlparse(api_base)
                                if parsed.scheme == "http" and parsed.hostname and parsed.hostname not in ("localhost", "127.0.0.1"):
                                    api_base = api_base.replace("http://", "https://", 1)
                            except Exception:
                                pass

                            public_first = api_base.rstrip("/") + f"/p/{pages[0]}"
                            # Only include an explicit textual public URL when it
                            # appears to be a public HTTPS host; otherwise prefer a
                            # telegraph token so we don't expose localhost URLs.
                            if str(public_first).lower().startswith("https://"):
                                md_text = f"View formatted results: {public_first} (parts: {len(pages)})\n\n" + md_text
                            else:
                                tele_url = f"telegraph://{pages[0]}"
                                md_text = f"Telegraph: {tele_url} (parts: {len(pages)})\n\n" + md_text

                            # if multiple pages, add parts list to md_text but prefer
                            # public HTTPS links; otherwise list telegraph tokens.
                            if len(pages) > 1:
                                parts_lines = [f"Parts:"]
                                for idx, pid in enumerate(pages, start=1):
                                    try:
                                        purl = api_base.rstrip("/") + f"/p/{pid}"
                                        if str(purl).lower().startswith("https://"):
                                            parts_lines.append(f"{idx}) {purl}")
                                        else:
                                            parts_lines.append(f"{idx}) telegraph://{pid}")
                                    except Exception:
                                        parts_lines.append(f"{idx}) telegraph://{pid}")
                                md_text = "\n".join(parts_lines) + "\n\n" + md_text
                        except Exception:
                            # fallback to telegraph-like token if env not present
                            tele_url = f"telegraph://{pages[0]}"
                            md_text = f"Telegraph: {tele_url} (parts: {len(pages)})\n\n" + md_text
                            if len(pages) > 1:
                                parts_lines = [f"Parts:"] + [f"{idx}) telegraph://{pid}" for idx, pid in enumerate(pages, start=1)]
                                md_text = "\n".join(parts_lines) + "\n\n" + md_text
                    # optionally mark matched docs with telegraph page id
                    try:
                        page_id = saved.get('page_id')
                        if page_id:
                            # attach page reference to matched documents
                            async def _attach():
                                try:
                                    await coll.update_many(
                                        {"chat_id": {"$exists": True}, "message_id": {"$exists": True}, "_id": {"$exists": True}},
                                        {"$addToSet": {"telegraph_pages": page_id}},
                                    )
                                except Exception:
                                    pass

                            try:
                                await _attach()
                            except Exception:
                                pass
                    except Exception:
                        pass
            except Exception:
                pass

        return PlainTextResponse(md_text, media_type="text/markdown")

    # persist page-level results into server-side cache (best-effort)
    if cache and cache_key:
        try:
            # record file identifiers for targeted invalidation later
            file_ids = []
            try:
                for r in page_results:
                    try:
                        cid = r.get("chat_id")
                        mid = r.get("message_id")
                        if cid is not None and mid is not None:
                            file_ids.append(f"{int(cid)}:{int(mid)}")
                    except Exception:
                        continue
            except Exception:
                file_ids = []

            metadata = {"query": q, "page": page, "per_page": per_page, "strict": bool(strict), "file_ids": file_ids}
            await cache.set(cache_key, {"results": page_results, "total": total, "page": page, "per_page": per_page}, metadata=metadata)
        except Exception:
            logger.exception("search cache set failed")

    return {"results": page_results, "total": total, "page": page, "per_page": per_page}


@app.get("/search/stream")
async def search_stream(
    q: str = Query(..., min_length=1, max_length=256),
    per_batch: int = Query(200, ge=1, le=2000),
    thread_id: int | None = Query(None),
):
    """Stream all matching results for a query as NDJSON using Atlas Search when available.

    This endpoint streams result documents in score-desc order. Use a reasonably
    small `per_batch` to control memory.
    """
    db = getattr(app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="DB unavailable")

    coll = db.get_collection("files")

    # Build Atlas searchStage similar to api_search
    def _get_w(name, default):
        try:
            return float(os.getenv(name, str(default)))
        except Exception:
            return float(default)

    w_title = _get_w("RANK_TITLE_WEIGHT", TITLE_WEIGHT)
    w_trigram = _get_w("RANK_TRIGRAM_WEIGHT", TRIGRAM_WEIGHT)
    w_fname = _get_w("RANK_FILENAME_MATCH", FILENAME_MATCH)
    w_quality = _get_w("RANK_QUALITY_WEIGHT", QUALITY_WEIGHT)
    w_codec = _get_w("RANK_CODEC_WEIGHT", CODEC_WEIGHT)

    fuzzy_enabled = os.getenv("ATLAS_SEARCH_FUZZY", "true").lower() in ("1", "true", "yes")
    fuzzy_obj = {"maxEdits": 1, "prefixLength": 1} if fuzzy_enabled else None

    should = []
    should.append({"text": {"query": q, "path": "title_tokens", "score": {"boost": {"value": w_title}}}})
    should.append({"text": {"query": q, "path": "search_text", "score": {"boost": {"value": w_trigram}}}})
    should.append({"text": {"query": q, "path": "filename", "score": {"boost": {"value": w_fname}}}})
    should.append({"text": {"query": q, "path": "quality_tokens", "score": {"boost": {"value": w_quality}}}})
    should.append({"text": {"query": q, "path": "codec_tokens", "score": {"boost": {"value": w_codec}}}})

    if fuzzy_obj:
        for s in should:
            try:
                tx = s.get("text")
                if tx is not None:
                    tx["fuzzy"] = fuzzy_obj
            except Exception:
                pass

    search_stage = {"$search": {"compound": {"should": should, "minimumShouldMatch": 1}}}

    proj_fields = {
        "chat_id": 1,
        "message_thread_id": 1,
        "message_id": 1,
        "filename": 1,
        "timestamp": 1,
        "title_tokens": 1,
        "quality_tokens": 1,
        "codec_tokens": 1,
        "year": 1,
        "file_id": 1,
        "file_size": 1,
        "duration": 1,
        "width": 1,
        "height": 1,
        "codec": 1,
        "thumbnail": 1,
        "path": 1,
    }

    pipeline = [search_stage, {"$addFields": {"score": {"$meta": "searchScore"}}}, {"$project": proj_fields}]
    if thread_id is not None:
        try:
            tid = int(thread_id)
            pipeline.append({"$match": {"message_thread_id": tid}})
        except Exception:
            pass

    pipeline.append({"$sort": {"score": -1, "timestamp": -1}})

    try:
        cursor = coll.aggregate(pipeline, allowDiskUse=True, batchSize=per_batch)

        async def gen():
            async for doc in cursor:
                # normalize fields for JSON streaming
                try:
                    if "_id" in doc:
                        doc["_id"] = str(doc["_id"])
                    ts = doc.get("timestamp")
                    if isinstance(ts, datetime):
                        doc["timestamp"] = ts.isoformat()
                except Exception:
                    pass
                # yield as newline-delimited JSON
                yield json.dumps(doc, default=str) + "\n"

        return StreamingResponse(gen(), media_type="application/x-ndjson")
    except Exception:
        # Fallback to legacy candidate find if $search isn't available
        try:
            from app.services.search_utils import make_trigrams, TRIGRAM_MAX

            q_tris = make_trigrams(q or "", TRIGRAM_MAX)
            candidate_filter = {"$or": [{"title_tokens": {"$in": [q]}}, {"trigrams": {"$in": q_tris}}]}
            if thread_id is not None:
                try:
                    tid = int(thread_id)
                    candidate_filter = {"$and": [candidate_filter, {"message_thread_id": tid}]}
                except Exception:
                    pass

            cursor2 = coll.find(candidate_filter, proj_fields).sort([("timestamp", -1)]).batch_size(per_batch)

            async def gen2():
                async for doc in cursor2:
                    try:
                        if "_id" in doc:
                            doc["_id"] = str(doc["_id"])
                        ts = doc.get("timestamp")
                        if isinstance(ts, datetime):
                            doc["timestamp"] = ts.isoformat()
                    except Exception:
                        pass
                    yield json.dumps(doc, default=str) + "\n"

            return StreamingResponse(gen2(), media_type="application/x-ndjson")
        except Exception:
            raise HTTPException(status_code=500, detail="Search streaming failed")
