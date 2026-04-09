#!/usr/bin/env python3
"""
Test helper: force-create an internal page and exercise the GUI send path.

Usage examples:
  # Dry-run (prints constructed message and keyboard)
  python scripts/test_gui.py --query "my search" --count 6

  # Live send (requires BOT token and chat id)
  python scripts/test_gui.py --query "my search" --count 6 --live --token <BOT_TOKEN> --chat 123456789

The script will:
- Ensure an in-memory `internal_page_store` exists
- Save a generated raw markdown page (links to sample t.me URLs)
- Set `top_links` on the saved page
- Render the first page to MarkdownV2 via the app's `md_to_markdownv2`
- Attempt to send the single-message GUI using MarkdownV2, falling back to Markdown, then to plain text
"""

import argparse
import asyncio
import os
import sys
import json

import httpx

# Import helpers from the app package
try:
    from app.api import _ensure_internal_page_store
    from app.utils.helpers import md_to_markdown, escape_markdown
except Exception as e:
    print("Failed to import app helpers:", e)
    raise


def build_sample_lines(count: int, base_chat: str = "12345"):
    lines = []
    for i in range(1, count + 1):
        # sample t.me link; adjust as needed by the user
        # place numbering outside the link label: `1) [Sample video](url)`
        lines.append(f"{i}) [Sample video {i}](https://t.me/c/{base_chat}/{1000 + i})")
    return lines


async def try_send(token: str, chat_id: int, text: str, reply_markup: object | None = None, parse_modes=None):
    if parse_modes is None:
        parse_modes = ["MarkdownV2", "Markdown", None]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient() as client:
        for mode in parse_modes:
            payload = {"chat_id": int(chat_id), "text": text}
            if mode:
                payload["parse_mode"] = mode
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            try:
                resp = await client.post(url, json=payload, timeout=10)
            except Exception as exc:
                print(f"Request failed for parse_mode={mode}: {exc}")
                continue
            print(f"Attempt parse_mode={mode!r} -> status={resp.status_code}")
            body = resp.text or ""
            if resp.status_code == 200:
                print("Sent successfully with parse_mode=", mode)
                try:
                    print(resp.json())
                except Exception:
                    print(resp.text[:400])
                return True, resp
            # Detect Telegram parse errors and continue to fallback
            if resp.status_code == 400 and ("can't parse entities" in body.lower() or "parse entities" in body.lower() or "can't parse" in body.lower()):
                print("Parse error reported by Telegram for mode=", mode)
                continue
            # For other non-200 responses try next mode
            print("Non-200 response, body:", body[:400])
        return False, None


async def main():
    p = argparse.ArgumentParser(description="Test GUI: create internal page and optionally send GUI message")
    p.add_argument("--query", required=True)
    p.add_argument("--count", type=int, default=6, help="Number of sample links to create")
    p.add_argument("--live", action="store_true", help="Actually POST to Telegram API (requires --token and --chat)")
    p.add_argument("--token", help="Bot token (alternatively set BOT_TOKEN env var)")
    p.add_argument("--chat", type=int, help="Target chat id (alternatively set TEST_CHAT_ID env var)")
    p.add_argument("--base-chat", default="12345", help="Numeric base used inside sample t.me links (for dry-run visualization)")

    args = p.parse_args()

    token = args.token or os.environ.get("BOT_TOKEN")
    chat_id = args.chat or os.environ.get("TEST_CHAT_ID")

    store = _ensure_internal_page_store()
    if not store:
        print("Failed to ensure internal page store")
        sys.exit(2)

    lines = build_sample_lines(args.count, base_chat=args.base_chat)
    raw_md = "\n".join(lines)

    # Save a single-part raw page
    try:
        saved = await store.save_raw_page(args.query, raw_md, tokens=[], created_by=None, group=None, part_index=0, total_parts=1, total_results=args.count)
    except Exception as exc:
        print("Failed to save page:", exc)
        raise

    page_id = saved.get("page_id")
    print("Saved page_id:", page_id)

    # Build top_links and persist
    top_links = []
    for i in range(1, min(9, args.count) + 1):
        top_links.append({"text": f"{i}) Sample video {i}", "url": f"https://t.me/c/{args.base_chat}/{1000 + i}"})

    try:
        await store.set_top_links(page_id, top_links)
        print("Set top_links on page")
    except Exception as exc:
        print("Failed to set top_links:", exc)

    # Retrieve page
    page = await store.get_page(page_id)
    if not page:
        print("Failed to retrieve saved page")
        sys.exit(3)

    content_md = md_to_markdown(page.get("content") or "")
    header = f"*Search:* {escape_markdown(args.query)} — {args.count} results\n\n"
    full_text = header + content_md

    # Build keyboard consistent with app/api.py (top-links first, then nav, then part buttons)
    kb_rows = []
    gp = page.get("group_pages") or []
    total_pg = len(gp) or 1
    cur_idx = int(page.get("part_index") or 0)

    # top_links rows (two per row)
    if top_links:
        row = []
        for tl in top_links:
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
    # Normalize gp entries into page_id strings (dicts or raw ids)
    gp_ids = [p.get("page_id") if isinstance(p, dict) else p for p in (gp or [])]
    nav = []
    if cur_idx > 0:
        prev_id = gp_ids[cur_idx - 1] if cur_idx - 1 < len(gp_ids) else None
        home_id = gp_ids[0] if gp_ids else None
        if prev_id:
            nav.append({"text": "⬅️ Prev", "callback_data": f"IP|{prev_id}"})
        if home_id:
            nav.append({"text": "🏠 Home", "callback_data": f"IP|{home_id}"})
    nav.append({"text": f"Part {cur_idx+1}/{max(1,total_pg)}", "callback_data": "noop"})
    if total_pg > 0 and cur_idx < total_pg - 1:
        next_id = gp_ids[cur_idx + 1] if cur_idx + 1 < len(gp_ids) else None
        end_id = gp_ids[-1] if gp_ids else None
        if next_id:
            nav.append({"text": "Next ➡️", "callback_data": f"IP|{next_id}"})
        if end_id:
            nav.append({"text": "End ⏭", "callback_data": f"IP|{end_id}"})
    if nav:
        kb_rows.append(nav)

    # part buttons (none for single-part)
    if total_pg > 1:
        part_row = []
        for i, p in enumerate(gp[:8]):
            pid = p.get("page_id") if isinstance(p, dict) else p
            part_row.append({"text": str(i + 1), "callback_data": f"IP|{pid}"})
        kb_rows.append(part_row)

    reply_markup = {"inline_keyboard": kb_rows} if kb_rows else None

    print("--- Constructed message (Markdown) ---")
    print(full_text)
    print("--- Reply markup ---")
    print(json.dumps(reply_markup, indent=2, ensure_ascii=False))

    if args.live:
        if not token or not chat_id:
            print("Missing BOT token or chat id for live send. Provide --token and --chat or set BOT_TOKEN/TEST_CHAT_ID env vars.")
            sys.exit(4)
        # Ensure the message fits Telegram limits before attempting to send.
        TELEGRAM_LIMIT = 4096
        if len(full_text) > TELEGRAM_LIMIT:
            # split header/body at first blank line (header contains metadata)
            header_part, sep, body_part = full_text.partition("\n\n")
            header = header_part + sep
            note = "\n\n_Showing truncated results due to size limits._"
            max_body_len = max(0, TELEGRAM_LIMIT - len(header) - len(note))
            lines = (body_part or "").splitlines()
            kept = []
            cur_len = 0
            for ln in lines:
                add = len(ln) + 1
                if cur_len + add > max_body_len:
                    break
                kept.append(ln)
                cur_len += add
            if kept:
                body_part = "\n".join(kept) + note
            else:
                body_part = "_Results too large to display. Use navigation to view parts._"
            full_text = header + body_part

        ok, resp = await try_send(token, chat_id, full_text, reply_markup, parse_modes=["Markdown", None])
        if not ok:
            print("All send attempts failed. See logs above.")
            sys.exit(5)
        print("Send succeeded")
    else:
        print("Dry-run complete. Use --live to actually POST to Telegram API.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
