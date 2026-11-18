"""Bilingual (English / Arabic) message catalog for the bot's user-facing text.

The bots were English-only: command descriptions, ``/start``, ``/help``, the
search hints and every status message. This module is the single place that
holds the Arabic side, so a string is never translated in one handler and left
English in another.

Language selection, in order:

1. an explicit language (``"ar"`` / ``"en"``);
2. the script of the text being handled - an Arabic query or caption gets an
   Arabic reply even on an English deployment;
3. ``BOT_LANG`` (``en`` by default, ``ar`` to flip the whole UI).

``bilingual()`` returns English then Arabic in one message; it is used for
``/start`` and ``/help``, where a mixed audience both needs to read the same
screen.

Only text lives here - no I/O, no Telegram imports.
"""

import os
import re
from typing import Dict, Optional

SUPPORTED = ("en", "ar")

# Arabic letters (incl. Persian/Urdu additions) vs Latin letters, used only to
# pick a reply language; matching itself never depends on this.
_AR_LETTER_RE = re.compile("[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")

MESSAGES: Dict[str, Dict[str, str]] = {
    "en": {
        # /start and /help
        "start": (
            "Hi - I can search indexed files.\n\n"
            "Send /search <query> to search.\n"
            "Example: /search breaking bad\n"
            "Example: /search \u0641\u064a\u0644\u0645"
        ),
        "help": (
            "Commands:\n"
            "/search <query> - Search files\n"
            "/export <query> - Export all results as a file\n"
            "/stats - Indexed file counts (owner only)\n"
            "/reindex <chat_id> - Backfill chat history (owner only)\n"
            "/health - DB health (owner only)\n"
            "/clear_all - Clear all indexed files (owner only)\n\n"
            "Search works in any language: Arabic, English or a mix of both.\n"
            "Examples: /search \u0641\u064a\u0644\u0645  \u00b7  /search breaking bad"
        ),
        # search / export flow
        "usage_search": "Usage: /search <query>",
        "usage_export": "Usage: /export <query>",
        "searching": "\U0001f50d Searching...",
        "no_results": "No results",
        "invalid_query": "Invalid query",
        "slow_down": "Slow down...",
        "export_start": "\U0001f4e4 Exporting full results... this may take a while",
        "export_failed": "Export failed.",
        "no_page_store": "No page store available",
        # result headers
        "results_header": "Search: {query} \u2014 {total} results",
        "results_header_plain": "Search: {query}",
        "results_part": "Search: {query} \u2014 part {part}/{parts}",
        "results_none_for": "*No results* for {query}",
        "results_title": "Search results",
        "part_suffix": "part {part}/{parts}",
        "open_results": "Open formatted results",
        "export_header": "Export: {query} \u2014 {total} results",
        # navigation
        "prev": "\u2b05\ufe0f Prev",
        "home": "\U0001f3e0 Home",
        "end": "End \u23ed",
        "next": "Next \u27a1\ufe0f",
        "page_indicator": "Page {page}/{pages}",
        "part_indicator": "Part {part}/{total}",
        # admin / misc
        "unauthorized": "Unauthorized",
        "admin_panel": "Admin panel",
        "page_id": "Page id: {page_id}",
        "db_unavailable": "DB unavailable",
    },
    "ar": {
        "start": (
            "\u0645\u0631\u062d\u0628\u064b\u0627 \u2014 \u0623\u0633\u062a\u0637\u064a\u0639 \u0627\u0644\u0628\u062d\u062b \u0641\u064a \u0627\u0644\u0645\u0644\u0641\u0627\u062a \u0627\u0644\u0645\u0641\u0647\u0631\u0633\u0629.\n\n"
            "\u0623\u0631\u0633\u0644 /search <\u0627\u0633\u062a\u0639\u0644\u0627\u0645> \u0644\u0644\u0628\u062d\u062b.\n"
            "\u0645\u062b\u0627\u0644: /search \u0641\u064a\u0644\u0645\n"
            "\u0645\u062b\u0627\u0644: /search breaking bad"
        ),
        "help": (
            "\u0627\u0644\u0623\u0648\u0627\u0645\u0631:\n"
            "/search <\u0627\u0633\u062a\u0639\u0644\u0627\u0645> \u2014 \u0627\u0644\u0628\u062d\u062b \u0641\u064a \u0627\u0644\u0645\u0644\u0641\u0627\u062a\n"
            "/export <\u0627\u0633\u062a\u0639\u0644\u0627\u0645> \u2014 \u062a\u0635\u062f\u064a\u0631 \u0643\u0644 \u0627\u0644\u0646\u062a\u0627\u0626\u062c \u0643\u0645\u0644\u0641\n"
            "/stats \u2014 \u0639\u062f\u062f \u0627\u0644\u0645\u0644\u0641\u0627\u062a \u0627\u0644\u0645\u0641\u0647\u0631\u0633\u0629 (\u0644\u0644\u0645\u0627\u0644\u0643 \u0641\u0642\u0637)\n"
            "/reindex <chat_id> \u2014 \u0625\u0639\u0627\u062f\u0629 \u0641\u0647\u0631\u0633\u0629 \u0633\u062c\u0644 \u0627\u0644\u062f\u0631\u062f\u0634\u0629 (\u0644\u0644\u0645\u0627\u0644\u0643 \u0641\u0642\u0637)\n"
            "/health \u2014 \u062d\u0627\u0644\u0629 \u0642\u0627\u0639\u062f\u0629 \u0627\u0644\u0628\u064a\u0627\u0646\u0627\u062a (\u0644\u0644\u0645\u0627\u0644\u0643 \u0641\u0642\u0637)\n"
            "/clear_all \u2014 \u0645\u0633\u062d \u062c\u0645\u064a\u0639 \u0627\u0644\u0645\u0644\u0641\u0627\u062a \u0627\u0644\u0645\u0641\u0647\u0631\u0633\u0629 (\u0644\u0644\u0645\u0627\u0644\u0643 \u0641\u0642\u0637)\n\n"
            "\u0627\u0644\u0628\u062d\u062b \u064a\u0639\u0645\u0644 \u0628\u0623\u064a \u0644\u063a\u0629: \u0627\u0644\u0639\u0631\u0628\u064a\u0629 \u0623\u0648 \u0627\u0644\u0625\u0646\u062c\u0644\u064a\u0632\u064a\u0629 \u0623\u0648 \u0645\u062e\u0644\u0648\u0637\u0629.\n"
            "\u0645\u062b\u0627\u0644: /search \u0641\u064a\u0644\u0645  \u00b7  /search breaking bad"
        ),
        "usage_search": "\u0627\u0644\u0627\u0633\u062a\u062e\u062f\u0627\u0645: /search <\u0627\u0633\u062a\u0639\u0644\u0627\u0645>",
        "usage_export": "\u0627\u0644\u0627\u0633\u062a\u062e\u062f\u0627\u0645: /export <\u0627\u0633\u062a\u0639\u0644\u0627\u0645>",
        "searching": "\U0001f50d \u062c\u0627\u0631\u064d \u0627\u0644\u0628\u062d\u062b...",
        "no_results": "\u0644\u0627 \u062a\u0648\u062c\u062f \u0646\u062a\u0627\u0626\u062c",
        "invalid_query": "\u0627\u0633\u062a\u0639\u0644\u0627\u0645 \u063a\u064a\u0631 \u0635\u0627\u0644\u062d",
        "slow_down": "\u062a\u0645\u0647\u0651\u0644 \u0642\u0644\u064a\u0644\u064b\u0627...",
        "export_start": "\U0001f4e4 \u062c\u0627\u0631\u064d \u062a\u0635\u062f\u064a\u0631 \u0643\u0644 \u0627\u0644\u0646\u062a\u0627\u0626\u062c... \u0642\u062f \u064a\u0633\u062a\u063a\u0631\u0642 \u0628\u0639\u0636 \u0627\u0644\u0648\u0642\u062a",
        "export_failed": "\u0641\u0634\u0644 \u0627\u0644\u062a\u0635\u062f\u064a\u0631.",
        "no_page_store": "\u0645\u062e\u0632\u0646 \u0627\u0644\u0635\u0641\u062d\u0627\u062a \u063a\u064a\u0631 \u0645\u062a\u0627\u062d",
        "results_header": "\u0627\u0644\u0628\u062d\u062b: {query} \u2014 \u0627\u0644\u0646\u062a\u0627\u0626\u062c: {total}",
        "results_header_plain": "\u0627\u0644\u0628\u062d\u062b: {query}",
        "results_part": "\u0627\u0644\u0628\u062d\u062b: {query} \u2014 \u0627\u0644\u062c\u0632\u0621 {part}/{parts}",
        "results_none_for": "*\u0644\u0627 \u062a\u0648\u062c\u062f \u0646\u062a\u0627\u0626\u062c* \u0644\u0640 {query}",
        "results_title": "\u0646\u062a\u0627\u0626\u062c \u0627\u0644\u0628\u062d\u062b",
        "part_suffix": "\u0627\u0644\u062c\u0632\u0621 {part}/{parts}",
        "open_results": "\u0639\u0631\u0636 \u0627\u0644\u0646\u062a\u0627\u0626\u062c \u0627\u0644\u0645\u0646\u0633\u0642\u0629",
        "export_header": "\u062a\u0635\u062f\u064a\u0631: {query} \u2014 \u0627\u0644\u0646\u062a\u0627\u0626\u062c: {total}",
        "prev": "\u2b05\ufe0f \u0627\u0644\u0633\u0627\u0628\u0642",
        "home": "\U0001f3e0 \u0627\u0644\u0631\u0626\u064a\u0633\u064a\u0629",
        "end": "\u0627\u0644\u0646\u0647\u0627\u064a\u0629 \u23ed",
        "next": "\u0627\u0644\u062a\u0627\u0644\u064a \u27a1\ufe0f",
        "page_indicator": "\u0635\u0641\u062d\u0629 {page}/{pages}",
        "part_indicator": "\u0627\u0644\u062c\u0632\u0621 {part}/{total}",
        "unauthorized": "\u063a\u064a\u0631 \u0645\u0635\u0631\u0651\u062d",
        "admin_panel": "\u0644\u0648\u062d\u0629 \u0627\u0644\u0625\u062f\u0627\u0631\u0629",
        "page_id": "\u0645\u0639\u0631\u0651\u0641 \u0627\u0644\u0635\u0641\u062d\u0629: {page_id}",
        "db_unavailable": "\u0642\u0627\u0639\u062f\u0629 \u0627\u0644\u0628\u064a\u0627\u0646\u0627\u062a \u063a\u064a\u0631 \u0645\u062a\u0627\u062d\u0629",
    },
}


def detect_lang(text: Optional[str], *, threshold: float = 0.30) -> str:
    """``"ar"`` when *text* is Arabic (or a mix with enough Arabic), else ``"en"``.

    A bilingual title such as "مسلسل Breaking Bad" counts as Arabic when at
    least ``threshold`` of its letters are Arabic, so the reply matches the
    script the user actually led with instead of defaulting to English.
    """
    if not text:
        return "en"
    arabic = len(_AR_LETTER_RE.findall(str(text)))
    latin = len(_LATIN_LETTER_RE.findall(str(text)))
    if arabic == 0:
        return "en"
    total = arabic + latin
    return "ar" if (arabic / total) >= threshold else "en"


def env_lang() -> str:
    """UI language from ``BOT_LANG`` (falls back to English)."""
    lang = (os.getenv("BOT_LANG") or "en").strip().lower()
    return lang if lang in SUPPORTED else "en"


def resolve_lang(explicit: Optional[str] = None, text: Optional[str] = None) -> str:
    """Pick a reply language: explicit, then script of *text*, then `BOT_LANG`."""
    if explicit in SUPPORTED:
        return explicit
    if text and detect_lang(text) == "ar":
        return "ar"
    return env_lang()


def t(key: str, lang: Optional[str] = None, text: Optional[str] = None, **kwargs) -> str:
    """Translated message for *key* (English when a translation is missing)."""
    chosen = resolve_lang(lang, text)
    catalog = MESSAGES.get(chosen) or MESSAGES["en"]
    template = catalog.get(key) or MESSAGES["en"].get(key) or key
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template


def bilingual(key: str, **kwargs) -> str:
    """English and Arabic in one message (used by /start and /help)."""
    return f"{t(key, 'en', **kwargs)}\n\n{t(key, 'ar', **kwargs)}"


#: Command menu entries as ``(command, description)`` for both languages.
COMMANDS = [
    ("search", {"en": "Search indexed files", "ar": "\u0627\u0644\u0628\u062d\u062b \u0641\u064a \u0627\u0644\u0645\u0644\u0641\u0627\u062a \u0627\u0644\u0645\u0641\u0647\u0631\u0633\u0629"}),
    ("export", {"en": "Export all results as a file", "ar": "\u062a\u0635\u062f\u064a\u0631 \u0643\u0644 \u0627\u0644\u0646\u062a\u0627\u0626\u062c \u0643\u0645\u0644\u0641"}),
    ("stats", {"en": "Indexed file counts (owner)", "ar": "\u0639\u062f\u062f \u0627\u0644\u0645\u0644\u0641\u0627\u062a \u0627\u0644\u0645\u0641\u0647\u0631\u0633\u0629 (\u0627\u0644\u0645\u0627\u0644\u0643)"}),
    ("health", {"en": "Database health (owner)", "ar": "\u062d\u0627\u0644\u0629 \u0642\u0627\u0639\u062f\u0629 \u0627\u0644\u0628\u064a\u0627\u0646\u0627\u062a (\u0627\u0644\u0645\u0627\u0644\u0643)"}),
    ("reindex", {"en": "Backfill a chat (owner)", "ar": "\u0625\u0639\u0627\u062f\u0629 \u0641\u0647\u0631\u0633\u0629 \u062f\u0631\u062f\u0634\u0629 (\u0627\u0644\u0645\u0627\u0644\u0643)"}),
]


def command_menu(lang: Optional[str] = None) -> list:
    """``[(command, description)]`` for the resolved UI language."""
    chosen = resolve_lang(lang)
    return [(cmd, desc.get(chosen) or desc["en"]) for cmd, desc in COMMANDS]
