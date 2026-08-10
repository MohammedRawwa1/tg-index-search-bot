import base64
from typing import Tuple, Optional
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import re
import html
import os
import urllib.parse


# Patterns to redact
_email_re = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
_phone_re = re.compile(r"\+?\d[\d\- ]{6,}\d")
_long_num_re = re.compile(r"\d{7,}")


# Robust Markdown link pattern used across all markdown->markdown/html
# converters. The label may itself contain `[`/`]` or parentheses; the
# non-greedy `.*?` anchors on the LAST `](` that closes a URL, and the URL
# may contain balanced parentheses. No DOTALL flag, so labels never span
# multiple lines.
_MD_LINK_RE = re.compile(r"\[(.*?)\]\(((?:[^()]|\([^)]*\))*)\)")


def normalize_bracket_links(raw_text: str) -> str:
    """Normalize `[Label] <bare-url>` patterns into Markdown links.

    Converts lines like ``[Some Book] https://t.me/c/...`` into
    ``[Some Book](https://t.me/c/...)`` so downstream HTML converters can
    produce clickable anchors.

    - Already-normalized `[Label](url)` links are left untouched.
    - Labels that themselves contain `[`/`]` are supported by matching the
      LAST closing bracket that is immediately followed by a URL.
    """
    try:
        if not raw_text:
            return ""
        # Protect existing `[Label](url)` links so they are not re-processed.
        links = []

        def _protect(m):
            links.append(m.group(0))
            return f"\x00L{len(links) - 1}\x00"

        text = _MD_LINK_RE.sub(_protect, raw_text)

        def _restore(m):
            try:
                return links[int(m.group(1))]
            except Exception:
                return m.group(0)

        out_lines = []
        for ln in text.splitlines():
            # Scan from the end for `] <url>` so labels containing ']' work.
            idx = len(ln)
            converted = False
            while not converted:
                j = ln.rfind("]", 0, idx)
                if j == -1:
                    break
                rest = ln[j + 1:]
                # URL may contain balanced parentheses (mirrors _MD_LINK_RE) so
                # bare links like `https://example.com/a(b)c` are not truncated.
                m = re.match(r"\s*(https?://(?:[^\s())\]>]|\([^)]*\))+)", rest)
                if m:
                    k = ln.rfind("[", 0, j)
                    if k != -1:
                        label = ln[k + 1:j]
                        url = m.group(1)
                        ln = ln[:k] + f"[{label}]({url})" + rest[m.end():]
                        converted = True
                    else:
                        break
                else:
                    idx = j
            out_lines.append(ln)
        text = "\n".join(out_lines)
        text = re.sub(r"\x00L(\d+)\x00", _restore, text)
        return text
    except Exception:
        return raw_text or ""


def extract_md_link_label(line: str) -> str:
    """Return the visible label text of a Markdown link line.

    Extracts the label from `[Label](url)` (robust to labels containing
    `[`/`]` or parentheses) and strips common list markers like `1) `,
    `1. `, `- `, `* `, `• `. When no link is present the cleaned line is
    returned as-is.
    """
    try:
        if not line:
            return ""
        m = _MD_LINK_RE.search(line)
        label = m.group(1) if m else line
        label = re.sub(r"^\s*(?:\d+[\)\.]\s*|[-*\u2022]\s*)", "", label)
        return label.strip()
    except Exception:
        return (line or "").strip()


def md_links_to_clickable(text: str) -> str:
    """Convert Markdown links to readable `Label — URL` plain text.

    Backslash-escapes inserted for Telegram Markdown are removed so the
    result is human-friendly (e.g. an escaped underscore becomes a plain one).
    """
    def _rep(m):
        label = unescape_for_plain_text(m.group(1))
        return f"{label} — {m.group(2)}"

    try:
        return _MD_LINK_RE.sub(_rep, text or "")
    except Exception:
        return text or ""


def _b64_encode(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def _b64_decode(s: str) -> str:
    return base64.urlsafe_b64decode(s.encode()).decode()


def encode_search_callback(query: str, page: int, per_page: int) -> str:
    """Deprecated: older encoding which stores base64 of query inline.

    Prefer `encode_search_callback_token` which stores a short token in DB.
    """
    max_query_len = 40
    safe_q = query if len(query) <= max_query_len else query[:max_query_len]
    return f"S|{_b64_encode(safe_q)}|{page}|{per_page}"


def encode_search_callback_token(token: str, page: int, per_page: int) -> str:
    """Encode callback using a token stored server-side in a callback store.

    Format: S|T|<token>|<page>|<per_page>
    """
    # Short format: S|<token>|<page>|<per_page> (avoids the extra 'T' marker)
    return f"S|{token}|{page}|{per_page}"


def decode_search_callback(data: str, token_resolver: Optional[callable] = None) -> Tuple[str, int, int]:
    """Decode callback data.

    Supports two formats:
      - legacy: S|<b64_query>|<page>|<per_page>
      - token:  S|T|<token>|<page>|<per_page>  (requires `token_resolver(token)->query`)

    The `token_resolver` callable is used only for token format and should
    accept a single `token` string and return the original query or None.
    """
    try:
        parts = data.split("|")
        if parts[0] != "S":
            raise ValueError("invalid")
        if len(parts) < 4:
            raise ValueError("invalid callback data")

        # Support two compact token formats and the legacy base64 format.
        # 1) Compact token format: S|<token>|<page>|<per>
        # 2) Legacy token marker: S|T|<token>|<page>|<per>
        # 3) Legacy base64: S|<b64_query>|<page>|<per>

        # Old explicit token marker
        if parts[1] == "T":
            if token_resolver is None:
                raise ValueError("token resolver required")
            token = parts[2]
            page = int(parts[3])
            per = int(parts[4]) if len(parts) > 4 else 5
            if page < 1 or page > 1000 or per < 1 or per > 100:
                raise ValueError("invalid pagination")
            query = token_resolver(token)
            if not query:
                raise ValueError("unknown token")
            return query, page, per

        # Reconstruct presumed page/per from positions (both compact and legacy base64 share layout)
        try:
            token_or_b64 = parts[1]
            page = int(parts[2])
            per = int(parts[3])
        except Exception:
            raise ValueError("invalid callback data")

        if page < 1 or page > 1000 or per < 1 or per > 100:
            raise ValueError("invalid pagination")

        # If token_resolver is provided, prefer resolving token_or_b64 as a token
        if token_resolver is not None:
            try:
                q = token_resolver(token_or_b64)
                if q:
                    return q, page, per
            except Exception:
                pass

        # Fallback: treat token_or_b64 as a base64-encoded query
        if len(token_or_b64) > 512:
            raise ValueError("callback too large")
        query = _b64_decode(token_or_b64)
        if len(query) > 200:
            raise ValueError("query too large")
        return query, page, per
    except Exception:
        raise ValueError("invalid callback data")


def build_result_keyboard(results: list, start_index: int = 1) -> InlineKeyboardMarkup:
    # Each result will have a numbered button opening the message link and
    # action buttons underneath. `start_index` is used to prefix numbering
    # so message text doesn't need numeric prefixes (avoids regex breaking).
    rows = []
    for idx, r in enumerate(results, start=start_index):
        chat_id = r["chat_id"]
        msg_id = r["message_id"]
        if str(chat_id).startswith("-100"):
            short = str(chat_id)[4:]
            url = f"https://t.me/c/{short}/{msg_id}"
        else:
            url = f"https://t.me/{chat_id}/{msg_id}"
        text = r.get("filename", "-")
        # sanitize filename: redact emails, phones, long numbers and trim
        text = _email_re.sub("[REDACTED_EMAIL]", text)
        text = _phone_re.sub("[REDACTED_PHONE]", text)
        text = _long_num_re.sub("[REDACTED_NUM]", text)
        text = text.replace("\n", " ")
        label = f"{idx}) {text[:56]}"
        # first row: link to the original message (numbered)
        rows.append([InlineKeyboardButton(label, url=url)])
        # second row: action buttons (mark duplicate / ignore / report)
        action_row = [
            InlineKeyboardButton("Mark Duplicate", callback_data=f"R|dup|{chat_id}|{msg_id}"),
            InlineKeyboardButton("Ignore", callback_data=f"R|ignore|{chat_id}|{msg_id}"),
            InlineKeyboardButton("Report", callback_data=f"R|report|{chat_id}|{msg_id}"),
        ]
        # if this result belongs to a forum/topic (message_thread_id), add a reindex button
        try:
            tid = r.get("message_thread_id")
            if tid is not None:
                action_row.append(InlineKeyboardButton("Reindex Topic", callback_data=f"R|reindex|{chat_id}|{tid}"))
        except Exception:
            pass
        rows.append(action_row)
    return InlineKeyboardMarkup(rows)


def build_pagination_keyboard(query: str, page: int, per_page: int, total: int) -> InlineKeyboardMarkup:
    pages = (total + per_page - 1) // per_page

    def _make_cb(q: str, p: int, per: int) -> str:
        # Build a compact callback string preserving either compact token format
        # (S|<token>|p|per) or legacy base64 format (S|<b64>|p|per) for raw queries.
        if isinstance(q, str) and q.startswith("S|"):
            parts = q.split("|")
            if len(parts) >= 2:
                token_or_b64 = parts[1]
                return f"S|{token_or_b64}|{p}|{per}"
        # fallback: encode using base64 of query
        return encode_search_callback(q, p, per)

    buttons = []
    # Prev
    if page > 1:
        buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=_make_cb(query, page - 1, per_page)))
    # Home (go to first page)
    if page > 1:
        buttons.append(InlineKeyboardButton("🏠 Home", callback_data=_make_cb(query, 1, per_page)))
    # Page indicator
    buttons.append(InlineKeyboardButton(f"Page {page}/{max(1, pages)}", callback_data="noop"))
    # End (go to last page)
    if page < pages:
        buttons.append(InlineKeyboardButton("End ⏭", callback_data=_make_cb(query, pages, per_page)))
    # Next
    if page < pages:
        buttons.append(InlineKeyboardButton("Next ➡️", callback_data=_make_cb(query, page + 1, per_page)))

    return InlineKeyboardMarkup([buttons])


def human_readable_size(num: int | None) -> str:
    """Return a human-readable file size string (e.g. '12.3 MB')."""
    try:
        if num is None:
            return ""
        n = float(num)
    except Exception:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while n >= 1024 and idx < len(units) - 1:
        n /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(n)} {units[idx]}"
    return f"{n:.2f} {units[idx]}"


def format_duration(seconds: float | int | None) -> str:
    """Format seconds to H:MM:SS or M:SS."""
    try:
        if seconds is None:
            return ""
        s = int(round(float(seconds)))
    except Exception:
        return ""
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h > 0:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def format_bitrate(file_size_bytes: int | None, duration_seconds: float | None) -> str:
    """Format bitrate from file size and duration as human-readable string (e.g. '1.23 Mbps')."""
    try:
        if file_size_bytes is None or duration_seconds is None:
            return ""
        dur = float(duration_seconds)
        if dur <= 0:
            return ""
        bits_per_sec = float(file_size_bytes) * 8.0 / dur
    except Exception:
        return ""
    # represent in kbps or mbps
    kbps = bits_per_sec / 1000.0
    mbps = bits_per_sec / 1_000_000.0
    if mbps >= 1.0:
        return f"{mbps:.2f} Mbps"
    if kbps >= 1.0:
        return f"{kbps:.1f} kbps"
    return f"{int(bits_per_sec)} bps"


def time_ago(dt) -> str:
    """Return a human-friendly 'time ago' string for a datetime or timestamp.

    Examples: 'just now', '5s ago', '3m ago', '2h ago', '3 days ago', '2 weeks ago', '3 months ago', '1 year ago'
    """
    try:
        from datetime import datetime

        if dt is None:
            return ""
        # if timestamp-like (int/float), convert
        if isinstance(dt, (int, float)):
            ts = float(dt)
            then = datetime.utcfromtimestamp(ts)
        else:
            then = dt
            # if timezone-aware, convert to UTC naive
            try:
                if hasattr(then, 'tzinfo') and then.tzinfo is not None:
                    then = then.astimezone(tz=None).replace(tzinfo=None)
            except Exception:
                pass
        now = datetime.utcnow()
        diff = now - then
        secs = int(diff.total_seconds())
        if secs < 10:
            return "just now"
        if secs < 60:
            return f"{secs}s ago"
        mins = secs // 60
        if mins < 60:
            return f"{mins}m ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        if days < 7:
            return f"{days} days ago"
        weeks = days // 7
        if weeks < 5:
            return f"{weeks} weeks ago"
        months = days // 30
        if months < 12:
            return f"{months} months ago"
        years = days // 365
        return f"{years} years ago"
    except Exception:
        return ""


def escape_md_v2(text: str) -> str:
    """Escape text for Telegram MarkdownV2."""
    try:
        if text is None:
            return ""
        # Escape characters required by MarkdownV2
        return re.sub(r'([_\*\[\]\(\)~`>#+\-=|{}\\.!])', r"\\\1", str(text))
    except Exception:
        return ""


def escape_md_v2_url(url: str) -> str:
    """Sanitize/escape URLs for use in MarkdownV2 links (minimal)."""
    try:
        if not url:
            return ""
        return str(url).replace("(", "%28").replace(")", "%29").replace(" ", "%20")
    except Exception:
        return ""


def md_to_markdownv2(raw_text: str) -> str:
    """Convert markdown-style links [Title](URL) into MarkdownV2 links.

    Escapes non-link text for MarkdownV2 and converts each markdown link
    into a MarkdownV2-compatible link while escaping the link label and
    sanitizing the URL.
    """
    try:
        if not raw_text:
            return ""
        # Normalize bare `[Label] https://...` lines into Markdown links so
        # they become clickable anchors (idempotent for existing links).
        raw_text = normalize_bracket_links(raw_text)
        out_parts = []
        last = 0
        for m in _MD_LINK_RE.finditer(raw_text):
            start, end = m.span()
            non = raw_text[last:start]
            out_parts.append(escape_md_v2(non))
            label = m.group(1)
            url = m.group(2)
            label_esc = escape_md_v2(label)
            url_esc = escape_md_v2_url(url)
            out_parts.append(f"[{label_esc}]({url_esc})")
            last = end
        out_parts.append(escape_md_v2(raw_text[last:]))
        return "".join(out_parts)
    except Exception:
        return escape_md_v2(raw_text or "")


def escape_markdown(text: str) -> str:
    """Escape text for Telegram Markdown (v1)."""
    try:
        if text is None:
            return ""
        # Do not escape parentheses here so numeric list markers like "1) "
        # remain human-friendly (avoid producing "1\) "). Keep escaping
        # characters that can break Markdown v1: asterisk, underscore,
        # backtick, backslash and square brackets.
        # Escape a single backslash before the special characters used by
        # Markdown v1. Use a single backslash in the replacement so that
        # Telegram receives the proper escape sequence (e.g. "\_").
        return re.sub(r'([*_`\[\]\\])', r"\\\1", str(text))
    except Exception:
        return ""


def unescape_for_plain_text(text: str) -> str:
    """Remove backslash-escapes inserted for Markdown v1 so plain-text
    fallbacks show readable filenames.

    This intentionally does NOT unescape parentheses since we don't
    escape them for Markdown v1. It targets only the characters we
    escape in `escape_markdown`.
    """
    try:
        if text is None:
            return ""
        # Remove one-or-more backslashes used to escape Markdown v1
        # special characters (e.g. "\\_" or "\\\\_" -> "_").
        # Some inputs may be double-escaped; allow any number of
        # backslashes before the escaped character.
        return re.sub(r'\\+([*_`\[\]\\])', r"\1", str(text))
    except Exception:
        return text or ""


def md_to_plain_text(md: str) -> str:
    """Convert simple Markdown links and escaped characters into readable plain text.

    - Replaces Markdown links like `[Label](url)` with `Label`.
    - Removes backslash escapes inserted for Markdown.
    """
    try:
        if md is None:
            return ""
        # strip markdown links to labels
        out = _MD_LINK_RE.sub(r"\1", str(md))
        # remove remaining Markdown escapes
        out = unescape_for_plain_text(out)
        return out
    except Exception:
        return unescape_for_plain_text(md or "")


def sanitize_filename_for_display(s: str) -> str:
    """Return a safe, human-friendly filename for display in compact UIs.

    - Strips directory components (keeps basename).
    - URL-decodes percent-encoded names.
    - Replaces backslashes with slashes for Windows paths.
    - Truncates long names to 80 chars.
    """
    try:
        if not s:
            return ""
        t = str(s)
        t = t.replace("\\", "/")
        t = os.path.basename(t)
        try:
            t = urllib.parse.unquote(t)
        except Exception:
            pass
        t = t.replace("\n", " ").strip()
        if len(t) <= 80:
            return t
        return t[:77] + "..."
    except Exception:
        return str(s)[:80]


def normalize_filename_key(s: str) -> str:
    """Return a normalized key used for deduplication and fuzzy matching.

    - Uses the basename, lowercased, with non-alphanumeric sequences replaced by
      single spaces. Useful as a compact fingerprint for duplicate detection.
    """
    try:
        if not s:
            return ""
        t = str(s)
        t = t.replace("\\", "/")
        t = os.path.basename(t)
        try:
            t = urllib.parse.unquote(t)
        except Exception:
            pass
        t = re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()
        return t
    except Exception:
        return ""


def escape_url(url: str) -> str:
    """Sanitize/escape URLs for Markdown links (minimal)."""
    try:
        if not url:
            return ""
        return str(url).replace("(", "%28").replace(")", "%29").replace(" ", "%20")
    except Exception:
        return ""


def md_to_markdown(raw_text: str) -> str:
    """Convert markdown-style links [Title](URL) into Markdown links.

    Escapes non-link text for Markdown v1 and converts each markdown link
    into a Markdown-compatible link while escaping the link label and
    sanitizing the URL.
    """
    try:
        if not raw_text:
            return ""
        # Normalize bare `[Label] https://...` lines into Markdown links so
        # they become clickable anchors (idempotent for existing links).
        raw_text = normalize_bracket_links(raw_text)
        out_parts = []
        last = 0
        for m in _MD_LINK_RE.finditer(raw_text):
            start, end = m.span()
            non = raw_text[last:start]
            out_parts.append(escape_markdown(non))
            label = m.group(1)
            url = m.group(2)
            label_esc = escape_markdown(label)
            url_esc = escape_url(url)
            out_parts.append(f"[{label_esc}]({url_esc})")
            last = end
        out_parts.append(escape_markdown(raw_text[last:]))
        return "".join(out_parts)
    except Exception:
        return escape_markdown(raw_text or "")


def md_to_html(raw_text: str, one_per_line: bool = False) -> str:
    """Convert markdown-style links [Title](URL) into HTML anchor tags.

    - Escapes non-link text as HTML.
    - Percent-encodes unsafe URL characters and escapes for HTML attributes.
    - When `one_per_line` is True, each link is placed on its own line.
    """
    try:
        if not raw_text:
            return ""
        # Normalize bare `[Label] https://...` lines into Markdown links so
        # they become clickable anchors (idempotent for existing links).
        raw_text = normalize_bracket_links(raw_text)
        out_parts = []
        last = 0
        for m in _MD_LINK_RE.finditer(raw_text):
            start, end = m.span()
            non = raw_text[last:start]
            out_parts.append(html.escape(non))
            # Remove any stray Telegram-Markdown backslash-escapes so labels
            # render literally in HTML (e.g. `Tom\_Torero` -> `Tom_Torero`).
            label = unescape_for_plain_text(m.group(1))
            url = m.group(2)
            label_esc = html.escape(label)
            try:
                from urllib.parse import quote as _quote

                # Percent-encode unsafe characters but keep common URL syntax
                url_esc = _quote(url, safe=":/?&=#%+,-_~@[]")
            except Exception:
                url_esc = html.escape(url, quote=True)

            # Ensure the attribute value is HTML-escaped
            url_attr = html.escape(url_esc, quote=True)
            if one_per_line:
                out_parts.append(f'<a href="{url_attr}">{label_esc}</a>\n')
            else:
                out_parts.append(f'<a href="{url_attr}">{label_esc}</a>')
            last = end
        out_parts.append(html.escape(raw_text[last:]))
        return "".join(out_parts)
    except Exception:
        return html.escape(raw_text or "")


def chunk_lines_by_char_limit(lines: list, max_chars: int) -> list:
    """Chunk a list of text lines into pages where each page's total
    character length (including newlines) does not exceed `max_chars`.

    This is used to ensure generated pages fit within Telegram's message
    size limits rather than a fixed number of items per page.
    """
    if not lines:
        return []
    chunks = []
    cur = []
    cur_len = 0
    for ln in lines:
        # count newline when joining
        add = len(ln) + 1
        if cur and (cur_len + add > max_chars):
            chunks.append("\n".join(cur))
            cur = [ln]
            cur_len = add
        else:
            cur.append(ln)
            cur_len += add
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def chunk_lines_with_refs(lines: list, refs: list, max_chars: int) -> list:
    """Chunk a list of `lines` and aligned `refs` into pages.

    Returns a list of tuples: (chunk_text, chunk_refs_list), where
    `chunk_refs_list` is the slice of `refs` corresponding to lines in
    `chunk_text`.
    """
    if not lines:
        return []
    if not refs:
        # fallback to simple chunking without refs
        return [(c, []) for c in chunk_lines_by_char_limit(lines, max_chars)]

    chunks = []
    cur_lines = []
    cur_refs = []
    cur_len = 0
    # Iterate by index so we never drop lines if `refs` is shorter than `lines`.
    for idx, ln in enumerate(lines):
        rf = refs[idx] if idx < len(refs) else None
        add = len(ln) + 1
        if cur_lines and (cur_len + add > max_chars):
            chunks.append(("\n".join(cur_lines), list(cur_refs)))
            cur_lines = [ln]
            cur_refs = [rf]
            cur_len = add
        else:
            cur_lines.append(ln)
            cur_refs.append(rf)
            cur_len += add

    if cur_lines:
        chunks.append(("\n".join(cur_lines), list(cur_refs)))
    return chunks


def render_paginated_page(page: dict, query_override: str | None = None, max_msg: int | None = None, max_links_per_page: int | None = None):
    """Render a stored internal page into a (text, inline_keyboard_rows) tuple.

    - `page` is the document returned by InternalPageStore.get_page(...).
    - Returns: (text_to_send, inline_keyboard_rows)
    - `inline_keyboard_rows` is a list of rows where each row is a list of button dicts
      with keys `text` and either `url` or `callback_data` so callers can pass it
      to the Bot API (`{"inline_keyboard": rows}`) or to Pyrogram's
      `InlineKeyboardMarkup(rows)`.

    The renderer enforces whole-line truncation so Markdown links are not broken
    mid-line and respects `MAX_MSG` / `MAX_LINKS_PER_PAGE` configuration.
    """
    try:
        from app.config.settings import settings
    except Exception:
        settings = None

    page_query = (page.get("query") or query_override or "")
    content_raw = page.get("content") or ""
    # Build both a Markdown-safe body (for attempts with parse_mode)
    # and a plain-text version used for fallbacks. We prefer sending
    # Markdown links so users get clickable anchors, but fall back to
    # plain text when Telegram rejects entity parsing.
    try:
        from app.utils.helpers import md_to_markdown
    except Exception:
        md_to_markdown = None

    try:
        # plain extraction (labels only) for truncation/fallback
        content_plain = _MD_LINK_RE.sub(r"\1", content_raw)
        content_plain = unescape_for_plain_text(content_plain)
    except Exception:
        content_plain = unescape_for_plain_text(content_raw)

    # Build Markdown-safe body using md_to_markdown if available
    try:
        if md_to_markdown:
            content_md_safe = md_to_markdown(content_raw)
        else:
            # fallback: use raw content
            content_md_safe = content_raw
    except Exception:
        content_md_safe = content_raw

    # Build keyboard rows: top-links, navigation, and part buttons
    kb_rows = []
    gp = page.get("group_pages") or []
    total = len(gp)
    try:
        cur_idx = int(page.get("part_index") or 0)
    except Exception:
        cur_idx = 0

    # top-links (two per row)
    tlinks = page.get("top_links") or []
    if tlinks:
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

    # navigation row
    gp_ids = [p.get("page_id") if isinstance(p, dict) else p for p in (gp or [])]
    nav = []
    if cur_idx > 0:
        prev_id = gp_ids[cur_idx - 1] if cur_idx - 1 < len(gp_ids) else None
        home_id = gp_ids[0] if gp_ids else None
        if prev_id:
            nav.append({"text": "⬅️ Prev", "callback_data": f"IP|{prev_id}"})
        if home_id:
            nav.append({"text": "🏠 Home", "callback_data": f"IP|{home_id}"})
    nav.append({"text": f"Part {cur_idx+1}/{max(1,total)}", "callback_data": "noop"})
    if total > 0 and cur_idx < total - 1:
        next_id = gp_ids[cur_idx + 1] if cur_idx + 1 < len(gp_ids) else None
        end_id = gp_ids[-1] if gp_ids else None
        if next_id:
            nav.append({"text": "Next ➡️", "callback_data": f"IP|{next_id}"})
        if end_id:
            nav.append({"text": "End ⏭", "callback_data": f"IP|{end_id}"})
    if nav:
        kb_rows.append(nav)

    # small direct part buttons (limit 8)
    if total > 1:
        part_row = []
        for i, p in enumerate(gp[:8]):
            pid = p.get("page_id") if isinstance(p, dict) else p
            part_row.append({"text": str(i + 1), "callback_data": f"IP|{pid}"})
        kb_rows.append(part_row)

    # Header (plain text)
    max_links = max_links_per_page if max_links_per_page is not None else (getattr(settings, "MAX_LINKS_PER_PAGE", 100) if settings is not None else 100)
    page_total = page.get("total_results") if page.get("total_results") is not None else (len(gp) * max_links)
    header = f"Search: {page_query} — {page_total} results\n\n" if page_query else ""

    # Build full Markdown body (do not truncate here; callers decide how to split)
    md_header = f"*Search:* {page_query} — {page_total} results\n\n" if page_query else ""
    # Prefer Markdown-safe content when available
    md_body = content_md_safe or content_plain or ""
    md_text_to_send = md_header + md_body

    return md_text_to_send, kb_rows
