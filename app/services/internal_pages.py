from datetime import datetime
from pydoc import doc
from typing import Optional, Dict, Any
import os

from app.config.settings import settings
from bson import ObjectId
import uuid
from app.utils.logger import logger


class InternalPageStore:
    """Store markdown exports internally in Mongo (no public publishing).

    Stores page metadata in a dedicated database (settings.TELEGRAPH_DB).
    Only internal metadata is returned: page_id, created_at, author, markdown.
    """

    def __init__(self, motor_client):
        self.client = motor_client

    async def save_markdown_page(self, query: str, md_text: str, tokens: list, created_by: Optional[int] = None) -> Dict[str, Any]:
        db_name = getattr(settings, "TELEGRAPH_DB", "course_bot")
        db = self.client[db_name]
        col = db.get_collection("telegraph_pages")
        # Defensive: ensure stored content refers only to video file types.
        # Accept a broad set of extensions and pick lines that reference them.
        VIDEO_EXTS = (".mp4", ".mkv", ".ts", ".webm", ".mov", ".avi", ".mpeg", ".mpg", ".flv", ".m4v", ".wmv")
        try:
            lines = (md_text or "").splitlines()
            vid_lines = []
            for ln in lines:
                low = ln.lower()
                if any(ext in low for ext in VIDEO_EXTS):
                    vid_lines.append(ln)
                    continue
                stripped = low.strip()
                if any(stripped.endswith(ext) for ext in VIDEO_EXTS):
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

        res = await col.insert_one(doc)
        page_id = str(res.inserted_id)
        return {
            "page_id": page_id,
            "created_at": doc["created_at"].isoformat(),
            "author": doc.get("created_by"),
            "markdown": doc.get("content"),
        }

    async def save_raw_page(
        self,
        query: str,
        md_text: str,
        tokens: list,
        created_by: Optional[int] = None,
        group: str | None = None,
        part_index: int | None = None,
        total_parts: int | None = None,
        total_results: int | None = None,
        page_header: str | None = None,
        line_refs: list | None = None,
    ) -> Dict[str, Any]:
        db_name = getattr(settings, "TELEGRAPH_DB", "course_bot")
        db = self.client[db_name]
        col = db.get_collection("telegraph_pages")

        # If caller did not supply explicit part_index/total_parts/group,
        # auto-split oversized raw pages into multiple parts here so that
        # stored pages are never a single oversized document.
        try:
            if group is None and part_index is None and total_parts is None:
                # Compute conservative per-part char limit from settings
                MAX_MSG = getattr(settings, "MAX_MSG", 4000)
                TELEGRAM_LIMIT = min(int(MAX_MSG), 4096)
                CHUNK_CHAR_LIMIT = max(800, TELEGRAM_LIMIT - 200)
                from app.utils.helpers import chunk_lines_by_char_limit, chunk_lines_with_refs

                lines = (md_text or "").splitlines()
                # Prefer chunking with refs when provided so we can persist
                # exact per-line mappings for each saved part.
                if line_refs:
                    chunks_with_refs = chunk_lines_with_refs(lines, line_refs, CHUNK_CHAR_LIMIT)
                    if len(chunks_with_refs) > 1:
                        group_id = str(uuid.uuid4())
                        total_parts_calc = len(chunks_with_refs)
                        total_results_calc = len(lines)
                        first_info = None
                        for idx, (chunk, refs_slice) in enumerate(chunks_with_refs):
                            doc_part = {
                                "query": query,
                                "tokens": tokens,
                                "content": chunk,
                                "created_at": datetime.utcnow(),
                                "created_by": int(created_by) if created_by is not None else None,
                                "meta_tags": tokens,
                                "group": group_id,
                                "part_index": int(idx),
                                "total_parts": int(total_parts_calc),
                                "total_results": int(total_results_calc),
                            }
                            # Persist page_header only on the first part so GUI
                            # renderers can display it once for the group.
                            if idx == 0 and page_header is not None:
                                doc_part["page_header"] = page_header
                            if refs_slice:
                                doc_part["line_refs"] = refs_slice
                            res = await col.insert_one(doc_part)
                            pid = str(res.inserted_id)
                            logger.info("SAVE PAGE | id=%s group=%s part=%s/%s results=%s", pid, group_id, idx, total_parts_calc, total_results_calc)
                            if idx == 0:
                                first_info = {
                                    "page_id": pid,
                                    "created_at": doc_part["created_at"].isoformat(),
                                    "author": doc_part.get("created_by"),
                                    "markdown": doc_part.get("content"),
                                    "group": doc_part.get("group"),
                                    "part_index": doc_part.get("part_index"),
                                    "total_parts": doc_part.get("total_parts"),
                                    "total_results": doc_part.get("total_results"),
                                }
                        if first_info:
                            return first_info
                else:
                    chunks = chunk_lines_by_char_limit(lines, CHUNK_CHAR_LIMIT)
                    if len(chunks) > 1:
                        group_id = str(uuid.uuid4())
                        total_parts_calc = len(chunks)
                        total_results_calc = len(lines)
                        first_info = None
                        for idx, chunk in enumerate(chunks):
                            doc_part = {
                                "query": query,
                                "tokens": tokens,
                                "content": chunk,
                                "created_at": datetime.utcnow(),
                                "created_by": int(created_by) if created_by is not None else None,
                                "meta_tags": tokens,
                                "group": group_id,
                                "part_index": int(idx),
                                "total_parts": int(total_parts_calc),
                                "total_results": int(total_results_calc),
                            }
                            # Persist header only on first part
                            if idx == 0 and page_header is not None:
                                doc_part["page_header"] = page_header
                            res = await col.insert_one(doc_part)
                            pid = str(res.inserted_id)
                            logger.info("SAVE PAGE | id=%s group=%s part=%s/%s results=%s", pid, group_id, idx, total_parts_calc, total_results_calc)
                            if idx == 0:
                                first_info = {
                                    "page_id": pid,
                                    "created_at": doc_part["created_at"].isoformat(),
                                    "author": doc_part.get("created_by"),
                                    "markdown": doc_part.get("content"),
                                    "group": doc_part.get("group"),
                                    "part_index": doc_part.get("part_index"),
                                    "total_parts": doc_part.get("total_parts"),
                                    "total_results": doc_part.get("total_results"),
                                }
                        if first_info:
                            return first_info
        except Exception:
            # On any failure during auto-splitting, fall back to single-document save
            logger.exception("auto-split failed; falling back to single save")

        doc = {
            "query": query,
            "tokens": tokens,
            "content": md_text,
            "created_at": datetime.utcnow(),
            "created_by": int(created_by) if created_by is not None else None,
            "meta_tags": tokens,
        }
        if page_header is not None:
            doc["page_header"] = page_header
        if line_refs is not None:
            doc["line_refs"] = line_refs
        if group:
            doc["group"] = group
        if part_index is not None:
            doc["part_index"] = int(part_index)
        if total_parts is not None:
            doc["total_parts"] = int(total_parts)
        if total_results is not None:
            doc["total_results"] = int(total_results)

        res = await col.insert_one(doc)
        page_id = str(res.inserted_id)
        logger.info("SAVE PAGE | id=%s group=%s part=%s/%s results=%s", page_id, doc.get("group"), doc.get("part_index"), doc.get("total_parts"), doc.get("total_results"))
        return {
            "page_id": page_id,
            "created_at": doc["created_at"].isoformat(),
            "author": doc.get("created_by"),
            "markdown": doc.get("content"),
            "group": doc.get("group"),
            "part_index": doc.get("part_index"),
            "total_parts": doc.get("total_parts"),
            "total_results": doc.get("total_results"),
        }

    async def get_page(self, page_id: str) -> Dict[str, Any] | None:
        """Fetch a page and its group pages with full content for GUI display."""
        db_name = getattr(settings, "TELEGRAPH_DB", "course_bot")
        db = self.client[db_name]
        col = db.get_collection("telegraph_pages")

        try:
            oid = ObjectId(page_id)
        except Exception:
            logger.error("Invalid page_id: %s", page_id)
            return None

        doc = await col.find_one({"_id": oid})
        if not doc:
            logger.error("Page not found: %s", page_id)
            return None

        out = {
            "page_id": str(doc["_id"]),
            "content": doc.get("content") or "",
            "created_at": doc.get("created_at"),
            "group": doc.get("group"),
            "part_index": int(doc.get("part_index") or 0),
            "total_parts": int(doc.get("total_parts") or 1),
            "query": doc.get("query") or "",
            "total_results": int(doc.get("total_results") or 0),
        }

        # Load all pages in the same group, ordered by part_index, with full data
        if out["group"]:
            cursor = col.find({"group": out["group"]}).sort("part_index", 1)
            group_pages = []
            async for p in cursor:
                group_pages.append({
                    "page_id": str(p["_id"]),
                    "content": p.get("content") or "",
                    "part_index": int(p.get("part_index") or 0),
                    "total_parts": int(p.get("total_parts") or 1),
                    "created_at": p.get("created_at"),
                    "query": p.get("query") or "",
                    "total_results": int(p.get("total_results") or 0),
                        "line_refs": p.get("line_refs") or [],
                        "page_header": p.get("page_header") or "",
                })

            out["group_pages"] = group_pages

            # Clamp part_index
            if out["part_index"] >= len(group_pages):
                out["part_index"] = max(0, len(group_pages) - 1)
        else:
            # Single-part page: return a single-entry group_pages list
            single_part = {
                "page_id": out.get("page_id"),
                "content": out.get("content"),
                "part_index": out.get("part_index"),
                "total_parts": out.get("total_parts"),
                "created_at": out.get("created_at"),
                "query": out.get("query"),
                "total_results": out.get("total_results"),
                "line_refs": out.get("line_refs") or [],
                "page_header": out.get("page_header") or "",
            }
            out["group_pages"] = [single_part]

        if doc.get("top_links"):
            out["top_links"] = doc.get("top_links")

        # Include any top-level line_refs stored on the primary document
        # include page header (if set on top-level doc)
        try:
            if doc.get("page_header"):
                out["page_header"] = doc.get("page_header")
        except Exception:
            pass

        try:
            if doc.get("line_refs"):
                out["line_refs"] = doc.get("line_refs")
        except Exception:
            pass

        logger.info(
            "LOAD PAGE | id=%s part=%s/%s results=%s",
            out["page_id"],
            out["part_index"],
            out["total_parts"],
            out["total_results"],
        )

        return out

    async def set_top_links(self, page_id: str, top_links: list) -> bool:
        """Persist `top_links` for a page in Mongo."""
        try:
            oid = ObjectId(page_id)
        except Exception:
            logger.error("set_top_links: invalid page_id=%s", page_id)
            return False
        try:
            db_name = getattr(settings, "TELEGRAPH_DB", "course_bot")
            db = self.client[db_name]
            col = db.get_collection("telegraph_pages")
            res = await col.update_one({"_id": oid}, {"$set": {"top_links": top_links}})
            if res.modified_count and res.matched_count:
                return True
            # If matched but not modified, still consider success (same content)
            if res.matched_count:
                return True
            return False
        except Exception as e:
            logger.exception("set_top_links: failed to set top_links for %s: %s", page_id, e)
            return False
        


class InMemoryInternalPageStore:
    """In-memory fallback store for internal pages used during development or
    when MongoDB is unavailable. Not persistent across restarts."""

    def __init__(self):
        self.pages: dict = {}
        self.groups: dict = {}

    async def save_markdown_page(self, query: str, md_text: str, tokens: list, created_by: Optional[int] = None) -> Dict[str, Any]:
        pid = str(uuid.uuid4())
        doc = {
            "page_id": pid,
            "query": query,
            "content": md_text,
            "tokens": tokens,
            "created_at": datetime.utcnow(),
            "created_by": int(created_by) if created_by is not None else None,
        }
        self.pages[pid] = doc
        return {"page_id": pid, "created_at": doc["created_at"].isoformat(), "author": doc.get("created_by"), "markdown": doc.get("content")}

    async def save_raw_page(self, query: str, md_text: str, tokens: list, created_by: Optional[int] = None, group: str | None = None, part_index: int | None = None, total_parts: int | None = None, total_results: int | None = None, page_header: str | None = None, line_refs: list | None = None) -> Dict[str, Any]:
        # Auto-split oversized content when caller didn't provide explicit parts
        try:
            if group is None and part_index is None and total_parts is None:
                MAX_MSG = getattr(settings, "MAX_MSG", 4000)
                TELEGRAM_LIMIT = min(int(MAX_MSG), 4096)
                CHUNK_CHAR_LIMIT = max(800, TELEGRAM_LIMIT - 200)
                from app.utils.helpers import chunk_lines_by_char_limit, chunk_lines_with_refs

                lines = (md_text or "").splitlines()
                if line_refs:
                    chunks_with_refs = chunk_lines_with_refs(lines, line_refs, CHUNK_CHAR_LIMIT)
                    if len(chunks_with_refs) > 1:
                        group_id = str(uuid.uuid4())
                        total_parts_calc = len(chunks_with_refs)
                        total_results_calc = len(lines)
                        first_info = None
                        for idx, (chunk, refs_slice) in enumerate(chunks_with_refs):
                            pid = str(uuid.uuid4())
                            doc_part = {
                                "page_id": pid,
                                "query": query,
                                "content": chunk,
                                "tokens": tokens,
                                "created_at": datetime.utcnow(),
                                "created_by": int(created_by) if created_by is not None else None,
                                "group": group_id,
                                "part_index": int(idx),
                                "total_parts": int(total_parts_calc),
                                "total_results": int(total_results_calc),
                            }
                            # attach page header only to first part
                            if idx == 0 and page_header is not None:
                                doc_part["page_header"] = page_header
                            if refs_slice:
                                doc_part["line_refs"] = refs_slice
                            self.pages[pid] = doc_part
                            self.groups.setdefault(group_id, []).append(pid)
                            logger.info("SAVE PAGE (mem) | id=%s group=%s part=%s/%s results=%s", pid, group_id, idx, total_parts_calc, total_results_calc)
                            if idx == 0:
                                first_info = {"page_id": pid, "created_at": doc_part["created_at"].isoformat(), "author": doc_part.get("created_by"), "markdown": doc_part.get("content"), "group": doc_part.get("group"), "part_index": doc_part.get("part_index"), "total_parts": doc_part.get("total_parts"), "total_results": doc_part.get("total_results")} 
                        if first_info:
                            return first_info
                else:
                    chunks = chunk_lines_by_char_limit(lines, CHUNK_CHAR_LIMIT)
                    if len(chunks) > 1:
                        group_id = str(uuid.uuid4())
                        total_parts_calc = len(chunks)
                        total_results_calc = len(lines)
                        first_info = None
                        for idx, chunk in enumerate(chunks):
                            pid = str(uuid.uuid4())
                            doc_part = {
                                "page_id": pid,
                                "query": query,
                                "content": chunk,
                                "tokens": tokens,
                                "created_at": datetime.utcnow(),
                                "created_by": int(created_by) if created_by is not None else None,
                                "group": group_id,
                                "part_index": int(idx),
                                "total_parts": int(total_parts_calc),
                                "total_results": int(total_results_calc),
                            }
                            # attach page header only to first part
                            if idx == 0 and page_header is not None:
                                doc_part["page_header"] = page_header
                            self.pages[pid] = doc_part
                            self.groups.setdefault(group_id, []).append(pid)
                            logger.info("SAVE PAGE (mem) | id=%s group=%s part=%s/%s results=%s", pid, group_id, idx, total_parts_calc, total_results_calc)
                            if idx == 0:
                                first_info = {"page_id": pid, "created_at": doc_part["created_at"].isoformat(), "author": doc_part.get("created_by"), "markdown": doc_part.get("content"), "group": doc_part.get("group"), "part_index": doc_part.get("part_index"), "total_parts": doc_part.get("total_parts"), "total_results": doc_part.get("total_results")} 
                        if first_info:
                            return first_info
        except Exception:
            logger.exception("In-memory auto-split failed; falling back to single save")

        pid = str(uuid.uuid4())
        doc = {
            "page_id": pid,
            "query": query,
            "content": md_text,
            "tokens": tokens,
            "created_at": datetime.utcnow(),
            "created_by": int(created_by) if created_by is not None else None,
            "group": group,
            "part_index": int(part_index) if part_index is not None else None,
            "total_parts": int(total_parts) if total_parts is not None else None,
        }
        if total_results is not None:
            doc["total_results"] = int(total_results)
        if page_header is not None:
            doc["page_header"] = page_header
        if line_refs is not None:
            doc["line_refs"] = line_refs
        self.pages[pid] = doc
        if group:
            self.groups.setdefault(group, []).append(pid)
        return {"page_id": pid, "created_at": doc["created_at"].isoformat(), "author": doc.get("created_by"), "markdown": doc.get("content"), "group": doc.get("group"), "part_index": doc.get("part_index"), "total_parts": doc.get("total_parts"), "total_results": doc.get("total_results")}

    async def get_page(self, page_id: str) -> Dict[str, Any] | None:
        doc = self.pages.get(page_id)
        if not doc:
            return None

        gp_ids = list(self.groups.get(doc.get("group"), []))
        group_pages = []
        for pid in gp_ids:
            p = self.pages.get(pid)
            if not p:
                continue
            group_pages.append({
                "page_id": p.get("page_id") or pid,
                "content": p.get("content") or "",
                "part_index": int(p.get("part_index") or 0),
                "total_parts": int(p.get("total_parts") or 1),
                "created_at": p.get("created_at"),
                "query": p.get("query") or "",
                "total_results": int(p.get("total_results") or 0),
                "line_refs": p.get("line_refs") or [],
                "page_header": p.get("page_header") or "",
            })

        out = {
            "page_id": doc.get("page_id"),
            "content": doc.get("content"),
            "created_at": doc.get("created_at"),
            "group": doc.get("group"),
            "part_index": doc.get("part_index"),
            "total_parts": doc.get("total_parts"),
            "query": doc.get("query"),
            "total_results": doc.get("total_results"),
        }
        if doc.get("top_links"):
            out["top_links"] = doc.get("top_links")
        out["group_pages"] = group_pages

        # include any top-level line_refs/page_header if present
        try:
            if doc.get("page_header"):
                out["page_header"] = doc.get("page_header")
        except Exception:
            pass

        try:
            if doc.get("line_refs"):
                out["line_refs"] = doc.get("line_refs")
        except Exception:
            pass

        return out

    async def set_top_links(self, page_id: str, top_links: list) -> bool:
        if page_id in self.pages:
            try:
                self.pages[page_id]["top_links"] = top_links
                return True
            except Exception:
                return False
        return False
        