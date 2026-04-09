from datetime import datetime
from typing import Optional, Dict, Any
import os
import httpx

# optional markdown converter
try:
    import markdown as _md
except Exception:
    _md = None

from app.config.settings import settings


class AsyncTelegraphStore:
    """Store markdown exports and optional remote telegra.ph pages.

    Stores page metadata in a dedicated database (settings.TELEGRAPH_DB).
    If TELEGRAPH_API_TOKEN is set, attempts to publish to telegra.ph and
    stores the resulting URL.
    """

    def __init__(self, motor_client):
        self.client = motor_client

    async def save_markdown_page(self, query: str, md_text: str, tokens: list, created_by: Optional[int] = None) -> Dict[str, Any]:
        db_name = getattr(settings, "TELEGRAPH_DB", "course_bot")
        db = self.client[db_name]
        col = db.get_collection("telegraph_pages")
        # Ensure md_text refers only to video files (broad set). Filter out
        # non-video items to avoid storing PDFs or other files accidentally.
        VIDEO_EXTS = (".mp4", ".mkv", ".ts", ".webm", ".mov", ".avi", ".mpeg", ".mpg", ".flv", ".m4v", ".wmv")
        try:
            lines = (md_text or "").splitlines()
            vid_lines = []
            for ln in lines:
                low = ln.lower()
                if any(ext in low for ext in VIDEO_EXTS):
                    vid_lines.append(ln)
                    continue
                if any(low.strip().endswith(ext) for ext in VIDEO_EXTS):
                    vid_lines.append(ln)
            if vid_lines:
                content = "\n".join(vid_lines)
            else:
                content = "_No video files found in provided content._"
        except Exception:
            content = md_text

        doc = {
            "query": query,
            "tokens": tokens,
            "content": content,
            "created_at": datetime.utcnow(),
            "created_by": int(created_by) if created_by is not None else None,
            "meta_tags": tokens,
        }

        # Persist page in Mongo only — do NOT publish to telegra.ph.
        res = await col.insert_one(doc)
        page_id = str(res.inserted_id)
        return {
            "page_id": page_id,
            "created_at": doc["created_at"].isoformat(),
            "author": doc.get("created_by"),
            "markdown": doc.get("content"),
        }