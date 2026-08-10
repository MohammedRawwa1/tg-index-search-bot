from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup
import asyncio
import time
import uuid

from app.services.tokenizer import tokenize_query
from app.models.file_index_impl import FileIndex
from app.utils.helpers import (
    escape_markdown,
    escape_url,
    render_paginated_page
)
from app.utils.logger import logger
from app.config.settings import settings
import re


async def send_markdown_chunks(client_obj: Client, chat_id: int, header_text: str | None, md_text: str, reply_markup_obj=None, edit_msg=None, max_msg: int | None = None):
    """Send large markdown text as multiple safe-sized messages.

    Converts Markdown links to HTML anchors when possible and sends the
    chunks with `parse_mode="HTML"`, falling back to plain text on errors.
    """
    try:
        MAX_MSG = int(max_msg) if max_msg is not None else int(getattr(settings, "MAX_MSG", 4000))
    except Exception:
        MAX_MSG = 4000

    try:
        from app.utils.helpers import chunk_lines_by_char_limit, md_to_plain_text, unescape_for_plain_text, md_to_html
        _md_to_html = md_to_html
    except Exception:
        chunk_lines_by_char_limit = None
        md_to_plain_text = None
        unescape_for_plain_text = None
        _md_to_html = None

    CHUNK_CHAR_LIMIT = max(800, int(MAX_MSG - 200))
    lines = md_text.splitlines()
    if chunk_lines_by_char_limit:
        chunks = chunk_lines_by_char_limit(lines, CHUNK_CHAR_LIMIT)
    else:
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

    for idx, chunk in enumerate(chunks):
        text_md = (header_text + "\n\n" + chunk) if (idx == 0 and header_text) else chunk
        try:
            text_html = None
            # Unescape any Markdown-v1 backslash escapes that may have been
            # introduced earlier in the pipeline so HTML labels are clean.
            try:
                from app.utils.helpers import unescape_for_plain_text
            except Exception:
                def unescape_for_plain_text(x):
                    return x

            try:
                text_md_clean = unescape_for_plain_text(text_md)
            except Exception:
                text_md_clean = text_md

            if _md_to_html:
                try:
                    text_html = _md_to_html(text_md_clean, one_per_line=True)
                except Exception as e:
                    logger.exception("md->html conversion failed: {}", e)
                    text_html = None

            if text_html:
                # try to edit original message with first chunk
                if idx == 0 and edit_msg:
                    try:
                        await edit_msg.edit_text(text_html, reply_markup=reply_markup_obj, parse_mode="HTML")
                        continue
                    except Exception as e:
                        logger.exception("edit_text HTML failed (attempting plain fallback): {}", e)
                        try:
                            fallback_plain = md_to_plain_text(text_md) if md_to_plain_text else (unescape_for_plain_text(text_md) if unescape_for_plain_text else text_md)
                            await edit_msg.edit_text(fallback_plain, reply_markup=reply_markup_obj)
                            continue
                        except Exception as e2:
                            logger.exception("edit_text plain fallback also failed: {}", e2)
                            pass

                # send as new message
                try:
                    if idx == 0:
                        await client_obj.send_message(chat_id, text_html, reply_markup=reply_markup_obj, parse_mode="HTML")
                    else:
                        await client_obj.send_message(chat_id, text_html, parse_mode="HTML")
                    try:
                        await asyncio.sleep(0.12)
                    except Exception:
                        pass
                except Exception as e:
                    logger.exception("send_message HTML failed; falling back to plain: {}", e)
                    try:
                        plain = md_to_plain_text(text_md) if md_to_plain_text else (unescape_for_plain_text(text_md) if unescape_for_plain_text else text_md)
                        if idx == 0:
                            await client_obj.send_message(chat_id, plain, reply_markup=reply_markup_obj)
                        else:
                            await client_obj.send_message(chat_id, plain)
                        try:
                            await asyncio.sleep(0.12)
                        except Exception:
                            pass
                    except Exception as e2:
                        logger.exception("plain send also failed: {}", e2)
            else:
                # plain-text path
                try:
                    if idx == 0 and edit_msg:
                        try:
                            await edit_msg.edit_text(text_md, reply_markup=reply_markup_obj)
                            continue
                        except Exception as e:
                            logger.exception("edit_text plain failed: {}", e)
                            pass
                    if idx == 0:
                        await client_obj.send_message(chat_id, text_md, reply_markup=reply_markup_obj)
                    else:
                        await client_obj.send_message(chat_id, text_md)
                    try:
                        await asyncio.sleep(0.12)
                    except Exception:
                        pass
                except Exception as e:
                    logger.exception("send_message plain failed: {}", e)
                    pass
        except Exception:
            pass


async def safe_edit_or_send(client_obj: Client, edit_msg, orig_message: Message, text: str, **kwargs):
    """Try to edit `edit_msg`; on failure send a new message as a reply to `orig_message`.

    This ensures the user always receives feedback when edit fails (for example
    if the original message was deleted or editing is not permitted).
    """
    try:
        if edit_msg:
            await edit_msg.edit_text(text, **kwargs)
            return
    except Exception:
        logger.exception("safe_edit_or_send: edit_text failed, will try sending new message")

    try:
        if orig_message:
            await orig_message.reply_text(text, **kwargs)
            return
    except Exception:
        logger.exception("safe_edit_or_send: reply_text failed, will try client.send_message")

    try:
        # fallback: send directly to chat id if available
        chat_id = None
        try:
            if orig_message and getattr(orig_message, "chat", None):
                chat_id = orig_message.chat.id
            elif edit_msg and getattr(edit_msg, "chat", None):
                chat_id = edit_msg.chat.id
        except Exception:
            chat_id = None

        if chat_id is not None:
            await client_obj.send_message(chat_id, text, **kwargs)
    except Exception:
        logger.exception("safe_edit_or_send: final fallback send_message failed")


DEFAULT_PER_PAGE = 5
_LAST_SEARCH = {}
_COOLDOWN = settings.SEARCH_COOLDOWN


# -------------------------------
# 🔥 STREAM SEARCH (NO LAG)
# -------------------------------
async def iter_results(file_index, tokens, query, batch=100):
    page = 1
    while True:
        res = file_index.search_with_ranking(
            tokens,
            query=query,
            page=page,
            per_page=batch
        )
        results = res.get("results", [])
        if not results:
            break

        for r in results:
            yield r

        if len(results) < batch:
            break
        page += 1


# -------------------------------
# 🧠 FORMAT RESULT → MARKDOWN
# -------------------------------
def result_to_md(i, r):
    fname = (r.get("filename") or "-").replace("\n", " ")
    fname = fname[:200]

    url = ""
    try:
        chat_id = r.get("chat_id")
        msg_id = r.get("message_id")
        if chat_id and msg_id:
            s = str(chat_id)
            base = s[4:] if s.startswith("-100") else s.lstrip("-")
            url = f"https://t.me/c/{base}/{msg_id}"
    except:
        pass

    # Store raw filename/URL in the internal page content. Do not pre-escape
    # here because the renderer will apply the correct escaping at send-time
    # (avoids double-escaping when pages are saved and later rendered).
    name = fname

    if url:
        return f"{i}) [{name}]({url})"
    return f"{i}) {name}"


def build_line_ref(r, query: str) -> dict:
    """Build a per-line reference object from a search result document.

    The returned dict contains keys: `chat_id`, `message_id`, `filename`,
    `match_score`, and `match_type` (heuristic).
    """
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
        ref["filename"] = r.get("filename") or ""
    except Exception:
        ref["filename"] = ""
    try:
        ref["match_score"] = float(r.get("_score")) if r.get("_score") is not None else None
    except Exception:
        ref["match_score"] = None
    # Heuristic match_type
    try:
        qnorm = re.sub(r"[^a-z0-9\s]", " ", (query or "").lower()).strip()
        fname = (r.get("filename") or "").lower()
        fname_norm = re.sub(r"[^a-z0-9\s]", " ", fname).strip()
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
    return ref


# -------------------------------
# ⚡ STREAM → CHUNKS
# -------------------------------
async def chunk_stream(line_iter, limit):
    chunk = []
    size = 0

    async for line in line_iter:
        l = len(line) + 1

        if size + l > limit:
            yield "\n".join(chunk)
            chunk = [line]
            size = l
        else:
            chunk.append(line)
            size += l

    if chunk:
        yield "\n".join(chunk)


# -------------------------------
# 💾 SAVE PAGES IN STORE
# -------------------------------
async def save_pages(store, query, tokens, chunk_iter, user_id):
    group = str(uuid.uuid4())
    chunks = []
    # Collect all chunks first so we can record total_parts/total_results.
    # Support either an async iterator (yielding chunk strings) or a
    # precomputed list of (chunk_text, chunk_refs) tuples.
    try:
        if hasattr(chunk_iter, "__aiter__"):
            async for chunk in chunk_iter:
                chunks.append(chunk)
        else:
            # chunk_iter may already be a list
            chunks = list(chunk_iter)
    except Exception:
        # fallback: try iterating synchronously
        try:
            for chunk in chunk_iter:
                chunks.append(chunk)
        except Exception:
            pass

    total_parts = len(chunks)
    # approximate total results by counting lines across chunks
    total_results = 0
    for c in chunks:
        try:
            if isinstance(c, (list, tuple)):
                total_results += len((c[0] or "").splitlines())
            else:
                total_results += len((c or "").splitlines())
        except Exception:
            total_results += 0

    pages = []
    # Prepare a stored page header to bind to saved parts so renderers
    # can use a consistent header instead of attaching it separately.
    try:
        page_header = f"Search: {query} — {total_results} results"
    except Exception:
        page_header = f"Search: {query}"

    for part, chunk in enumerate(chunks):
        try:
            # chunk may be (text, refs) or raw text
            line_refs = None
            chunk_text = chunk
            if isinstance(chunk, (list, tuple)):
                chunk_text = chunk[0]
                line_refs = chunk[1]

            saved = await store.save_raw_page(
                query,
                chunk_text,
                tokens,
                created_by=user_id,
                group=group,
                part_index=part,
                total_parts=total_parts,
                total_results=total_results,
                page_header=page_header,
                line_refs=line_refs,
            )
            pages.append(saved["page_id"])
        except Exception:
            logger.exception("save_pages: failed to save part {}", part)
    logger.info("save_pages: query={} parts={} total_results={} user={}", query, total_parts, total_results, user_id)
    return pages


# -------------------------------
# 🚀 MAIN SEARCH HANDLER
# -------------------------------
def register_search_handlers(client: Client, mongo):
    file_index = FileIndex(mongo.db)

    @client.on_message(filters.command("search"))
    async def search_handler(client: Client, message: Message):
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.reply_text("Usage: /search <query>")
            return

        query = args[1].strip()
        uid = message.from_user.id if message.from_user else None

        # cooldown
        now = time.time()
        if uid and uid in _LAST_SEARCH and now - _LAST_SEARCH[uid] < _COOLDOWN:
            await message.reply_text("Slow down...")
            return
        _LAST_SEARCH[uid] = now

        tokens = tokenize_query(query)
        if not tokens:
            await message.reply_text("Invalid query")
            return

        searching = await message.reply_text("🔍 Searching...")

        # -------------------------------
        # STREAM RESULTS → build lines + per-line refs
        # -------------------------------
        lines = []
        refs = []
        i = 1
        async for r in iter_results(file_index, tokens, query):
            ln = result_to_md(i, r)
            try:
                ref = build_line_ref(r, query)
            except Exception:
                ref = {"chat_id": None, "message_id": None, "filename": r.get("filename") if r else "", "match_score": None, "match_type": "unknown"}
            lines.append(ln)
            refs.append(ref)
            i += 1

        # -------------------------------
        # CHUNKING
        # -------------------------------
        MAX_MSG = getattr(settings, "MAX_MSG", 4000)
        CHUNK_LIMIT = MAX_MSG - 200
        from app.utils.helpers import chunk_lines_with_refs
        chunks = chunk_lines_with_refs(lines, refs, CHUNK_LIMIT)

        # -------------------------------
        # INTERNAL PAGE STORE
        # -------------------------------
        store = getattr(client, "_internal_page_store", None)

        if not store:
            await safe_edit_or_send(client, searching, message, "No page store available")
            return

        pages = await save_pages(
            store,
            query,
            tokens,
            chunks,
            uid
        )

        if not pages:
            await safe_edit_or_send(client, searching, message, "No results")
            return

        # -------------------------------
        # RENDER FIRST PAGE
        # -------------------------------
        first = await store.get_page(pages[0])
        text, kb = render_paginated_page(first, query)

        # Auto-stream full results if multiple saved page parts exist.
        try:
            if pages and len(pages) > 1:
                combined_parts = []
                for pid in pages:
                    try:
                        pdoc = await store.get_page(pid)
                        part_content = ""
                        if pdoc:
                            part_content = pdoc.get("content") or pdoc.get("markdown") or ""
                        combined_parts.append(part_content)
                    except Exception:
                        combined_parts.append("")

                combined_md = "\n".join([c for c in combined_parts if c])
                header = f"*Search:* {query} — {int(first.get('total_results', 0))} results\n\n" if query else ""
                try:
                    chat_id_target = message.chat.id if message and message.chat else None
                    reply_markup_obj = InlineKeyboardMarkup(kb) if kb else None
                    await send_markdown_chunks(client, chat_id_target, header, combined_md, reply_markup_obj, edit_msg=searching)
                    return
                except Exception as e:
                    logger.exception("auto stream failed: {}", e)
        except Exception:
            pass

        # If the rendered page is very large, send it as chunked HTML messages
        try:
            MAX_MSG = getattr(settings, "MAX_MSG", 4000)
        except Exception:
            MAX_MSG = 4000

        if text and len(text) > MAX_MSG:
            try:
                chat_id_target = message.chat.id if message and message.chat else None
                reply_markup_obj = InlineKeyboardMarkup(kb) if kb else None
                try:
                    await send_markdown_chunks(client, chat_id_target, None, text, reply_markup_obj, edit_msg=searching, max_msg=MAX_MSG)
                    return
                except Exception as e:
                    logger.exception("chunked send failed: {}", e)
            except Exception:
                pass

        try:
            await searching.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown"
            )
        except Exception:
            # If editing the temporary 'searching' message fails, send the
            # rendered page as a fresh message so the user still receives it.
            await safe_edit_or_send(client, searching, message, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

    @client.on_message(filters.command("export"))
    async def export_all_handler(client: Client, message: Message):
        """Export all search results for a query to a Markdown file and send as document.

        Usage: /export <query>
        """
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.reply_text("Usage: /export <query>")
            return

        query = args[1].strip()
        uid = message.from_user.id if message.from_user else None

        tokens = tokenize_query(query)
        if not tokens:
            await message.reply_text("Invalid query")
            return

        notice = await message.reply_text("📤 Exporting full results... this may take a while")

        try:
            lines = []
            count = 0
            async for i, r in _iter_with_index(file_index, tokens, query):
                fname = (r.get("filename") or "-").replace("\n", " ")[:400]
                url = ""
                try:
                    chat_id = r.get("chat_id")
                    msg_id = r.get("message_id")
                    if chat_id and msg_id:
                        s = str(chat_id)
                        base = s[4:] if s.startswith("-100") else s.lstrip("-")
                        url = f"https://t.me/c/{base}/{msg_id}"
                except Exception:
                    url = ""

                if url:
                    lines.append(f"{i}) [{fname}]({url})")
                else:
                    lines.append(f"{i}) {fname}")
                count = i

            content = "\n".join(lines)
            try:
                from app.utils.helpers import md_to_markdown
                md_safe = md_to_markdown(content)
            except Exception:
                md_safe = content

            header = f"*Search:* {query} — {count} results\n\n" if query else ""
            await send_markdown_chunks(client, message.chat.id, header, md_safe, reply_markup_obj=None, edit_msg=notice)
        except Exception as exc:
            logger.exception("export failed: {}", exc)
            await message.reply_text("Export failed.")
        finally:
            try:
                await notice.delete()
            except Exception:
                pass


    def _iter_with_index(file_index_obj, tokens, query, batch=200):
        """Helper: async generator that yields (index, result) starting at 1."""
        async def _gen():
            i = 1
            async for r in iter_results(file_index_obj, tokens, query, batch=batch):
                yield i, r
                i += 1

        return _gen()


    # -------------------------------
    # 📄 CALLBACK HANDLER
    # -------------------------------
    @client.on_callback_query()
    async def merged_callback_handler(client: Client, cq: CallbackQuery):
        data = cq.data or ""

        # Admin callbacks (C| and A|)
        if data.startswith("C|") or data.startswith("A|"):
            try:
                from app.handlers.admin import handle_admin_callback
                await handle_admin_callback(client, cq)
            except Exception as exc:
                logger.exception("admin callback dispatch failed: {}", exc)
                await cq.answer("Admin callback error", show_alert=True)
            return

        # Search internal page navigation (IP|)
        if data.startswith("IP|"):
            page_id = data.split("|", 1)[1]
            store = getattr(client, "_internal_page_store", None)

            if not store:
                await cq.answer("Missing store")
                return

            page = await store.get_page(page_id)
            if not page:
                await cq.answer("Not found")
                return

            text, kb = render_paginated_page(page, None)

            await cq.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown"
            )
            await cq.answer()
            return

        # Unknown callback
        await cq.answer("Unknown callback", show_alert=True)