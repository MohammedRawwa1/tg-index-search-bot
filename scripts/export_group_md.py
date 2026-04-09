#!/usr/bin/env python3
"""Export all indexed media for a supergroup into a single Markdown file.

Usage:
  python scripts/export_group_md.py <chat_id> [output.md]

Produces a Markdown file grouped by forum topic (`message_thread_id`) with
links to the Telegram message containing the media (t.me/c/...).
"""

import sys
import pathlib
import os
import sys
# Ensure project root is on sys.path so `import app` works when running
# this script directly from the `scripts/` directory.
_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
import os
from collections import defaultdict
from app.config.settings import settings
from app.services.mongo import MongoService


def chat_base_from_id(chat_id: int) -> str:
    s = str(chat_id)
    if s.startswith("-100"):
        return s[4:]
    return s.lstrip("-")


def main():
    if len(sys.argv) < 2:
        print("Usage: export_group_md.py <chat_id> [output.md]")
        sys.exit(1)

    chat_id = int(sys.argv[1])
    out_path = sys.argv[2] if len(sys.argv) >= 3 else None

    mongo = MongoService(settings.MONGO_URI, settings.DB_NAME)
    try:
        mongo.connect()
    except Exception as e:
        print("Failed to connect to MongoDB:", e)
        sys.exit(2)

    coll = mongo.db.get_collection("files")
    cursor = coll.find({"chat_id": chat_id}).sort([("message_thread_id", 1), ("timestamp", 1)])

    groups = defaultdict(list)
    for d in cursor:
        tid = d.get("message_thread_id")
        groups[tid].append(d)

    base = chat_base_from_id(chat_id)

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

    md = "\n".join(lines)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(md)
        print("Wrote:", out_path)
    else:
        print(md)


if __name__ == "__main__":
    main()