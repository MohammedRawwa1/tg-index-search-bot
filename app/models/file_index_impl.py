from typing import List, Dict, Any, Set, Optional
from datetime import datetime
import re
import os
import asyncio
from app.services.search_utils import make_trigrams, TRIGRAM_MAX
from app.services.relevance import (
    DEFAULT_MIN_TIER,
    MIN_RESULTS,
    NONE_TIER,
    accepts_tier,
    classify_match,
    compute_search_score,
    log_search_quality,
    resolve_min_tier,
    tier_priority,
)
from app.utils.logger import logger

# Scoring weights (tunable)
TITLE_WEIGHT = 10
QUALITY_WEIGHT = 6
CODEC_WEIGHT = 5
YEAR_WEIGHT = 8
PREFIX_BOOST = 5
TRIGRAM_WEIGHT = 40
FILENAME_MATCH = 3
FNAME_LEN_PENALTY_DIV = 200.0
CANDIDATE_LIMIT = 800


class FileIndex:
    def __init__(self, db):
        self._coll = db.get_collection("files")
        # Load ranking weights from environment to allow remote tuning.
        def _envf(name, default):
            try:
                v = os.getenv(name)
                return float(v) if v is not None else float(default)
            except Exception:
                return float(default)

        self.TITLE_WEIGHT = _envf("RANK_TITLE_WEIGHT", TITLE_WEIGHT)
        self.QUALITY_WEIGHT = _envf("RANK_QUALITY_WEIGHT", QUALITY_WEIGHT)
        self.CODEC_WEIGHT = _envf("RANK_CODEC_WEIGHT", CODEC_WEIGHT)
        self.YEAR_WEIGHT = _envf("RANK_YEAR_WEIGHT", YEAR_WEIGHT)
        self.PREFIX_BOOST = _envf("RANK_PREFIX_BOOST", PREFIX_BOOST)
        self.TRIGRAM_WEIGHT = _envf("RANK_TRIGRAM_WEIGHT", TRIGRAM_WEIGHT)
        self.FILENAME_MATCH = _envf("RANK_FILENAME_MATCH", FILENAME_MATCH)
        self.FNAME_LEN_PENALTY_DIV = _envf("RANK_FNAME_LEN_PENALTY_DIV", FNAME_LEN_PENALTY_DIV)
        self.RECENCY_WEIGHT = _envf("RANK_RECENCY_WEIGHT", 2.0)
        try:
            self.CANDIDATE_LIMIT = int(os.getenv("RANK_CANDIDATE_LIMIT", CANDIDATE_LIMIT))
        except Exception:
            self.CANDIDATE_LIMIT = CANDIDATE_LIMIT

    def _normalize_filename(self, filename: str) -> str:
        """Return a normalized filename string used for deduplication/search.

        Normalization steps:
        - Lowercase
        - Strip extension
        - Replace common separators and non-alnum with single spaces
        - Collapse multiple spaces
        """
        if not filename:
            return ""
        try:
            s = str(filename).lower()
        except Exception:
            s = filename or ""
        # remove trailing extension
        if "." in s:
            s = s.rsplit(".", 1)[0]
        # replace underscores/dashes/dots with spaces
        s = re.sub(r"[_\-.–—]", " ", s)
        # remove any remaining non-alnum characters to spaces
        s = re.sub(r"[^a-z0-9]+", " ", s)
        s = s.strip()
        s = re.sub(r"\s+", " ", s)
        return s

    async def upsert_file(
        self,
        chat_id: int,
        message_id: int,
        filename: str,
        extension: str,
        token_struct: dict,
        timestamp: datetime,
        file_id: str | None = None,
        file_size: int | None = None,
        duration: float | None = None,
        width: int | None = None,
        height: int | None = None,
        codec: str | None = None,
        message_thread_id: int | None = None,
        path: str | None = None,
        thumbnail: str | None = None,
        added_by: int | None = None,
    ):
        """Upsert a file document with structured token fields.

        token_struct: { title_tokens, quality_tokens, codec_tokens, year, other }
        """
        # build a simple search_text field to enable Mongo text search
        search_parts: List[str] = []
        search_parts.extend([t for t in token_struct.get("title_tokens", [])])
        search_parts.extend([t for t in token_struct.get("quality_tokens", [])])
        search_parts.extend([t for t in token_struct.get("codec_tokens", [])])
        if filename:
            try:
                search_parts.append(str(filename).lower())
            except Exception:
                pass

        search_text = " ".join([str(x).lower() for x in search_parts if x])
        trigrams = make_trigrams(search_text or "", TRIGRAM_MAX)

        # normalized filename used for deduplication and consistent grouping
        # prefer token-based normalized title when available (removes copy suffixes)
        try:
            title_tokens = token_struct.get("title_tokens", []) if token_struct else []
            if title_tokens:
                norm_filename = " ".join([t.lower() for t in title_tokens if t])
            else:
                norm_filename = self._normalize_filename(filename)
        except Exception:
            norm_filename = self._normalize_filename(filename)

        doc: Dict[str, Any] = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "message_thread_id": int(message_thread_id) if message_thread_id is not None else None,
            "filename": filename,
            "extension": extension,
            "title_tokens": token_struct.get("title_tokens", []),
            "quality_tokens": token_struct.get("quality_tokens", []),
            "codec_tokens": token_struct.get("codec_tokens", []),
            "year": token_struct.get("year"),
            "other_tokens": token_struct.get("other", []),
            "timestamp": timestamp,
            "search_text": search_text,
            "trigrams": trigrams,
            # optional metadata fields
            "file_id": file_id,
            "file_size": int(file_size) if file_size is not None else None,
            "duration": float(duration) if duration is not None else None,
            "width": int(width) if width is not None else None,
            "height": int(height) if height is not None else None,
            "codec": codec,
            "path": path,
            "thumbnail": thumbnail,
            "added_by": int(added_by) if added_by is not None else None,
            "norm_filename": norm_filename,
            # full-title string for Atlas Search phrase queries (array fields
            # cannot be phrase-matched across elements)
            "title_phrase": norm_filename,
        }

        # Upsert
        try:
            self._coll.update_one(
                {"chat_id": doc["chat_id"], "message_id": doc["message_id"]},
                {"$set": doc},
                upsert=True,
            )
        except Exception:
            # best-effort upsert; if it fails ignore for now
            pass

        # mark duplicates (same chat_id + filename but different message_id)
        try:
            # mark duplicates using normalized filename / title tokens for more robust matching
            dup_query = {"chat_id": doc["chat_id"], "message_id": {"$ne": doc["message_id"]}}
            if norm_filename:
                dup_query["norm_filename"] = norm_filename
            else:
                dup_query["filename"] = filename
            dup = self._coll.find_one(dup_query)
            if dup:
                self._coll.update_one(
                    {"chat_id": doc["chat_id"], "message_id": doc["message_id"]},
                    {"$set": {"is_duplicate": True}},
                )
            else:
                self._coll.update_one(
                    {"chat_id": doc["chat_id"], "message_id": doc["message_id"]},
                    {"$set": {"is_duplicate": False}},
                )
        except Exception:
            pass

        # Targeted cache invalidation: attempt to remove any cached search
        # entries that reference this particular file. This is best-effort and
        # will not raise on failure.
        try:
            try:
                from app.api import app as main_app

                cache = getattr(main_app.state, "search_cache", None)
                if cache:
                    try:
                        file_ident = f"{int(doc['chat_id'])}:{int(doc['message_id'])}"
                        await cache.invalidate_by_file_ids([file_ident])
                    except Exception:
                        logger.debug("upsert_file: cache.invalidate_by_file_ids failed for {}", file_ident)
            except Exception:
                # running outside of web app or cache unavailable
                pass
        except Exception:
            pass

    def search_by_tokens(self, tokens: List[str], limit: int = 20) -> List[Dict[str, Any]]:
        if not tokens:
            return []
        # Use title_tokens and return documents that contain any of the tokens
        cursor = (
            self._coll.find({"title_tokens": {"$in": tokens}}).sort("timestamp", -1).limit(limit)
        )
        return list(cursor)

    def _prefix_fallback(self, tokens: List[str], limit: int = 50) -> List[Dict[str, Any]]:
        # fallback using prefix regex on title tokens or filename
        regexes = []
        for t in tokens:
            regexes.append(re.compile(rf'^{re.escape(t)}', re.IGNORECASE))
        # build $or query: any title_tokens matches regex OR filename contains prefix
        or_clauses = []
        for r in regexes:
            or_clauses.append(
                {"title_tokens": {"$elemMatch": {"$regex": r.pattern, "$options": "i"}}}
            )
            or_clauses.append({"filename": {"$regex": r.pattern, "$options": "i"}})
        cursor = self._coll.find({"$or": or_clauses}).limit(limit)
        return list(cursor)

    def search_with_ranking(
        self,
        tokens: List[str],
        query: str | None = None,
        page: int = 1,
        per_page: int = 5,
        filters: Optional[Dict[str, Any]] = None,
        strict: bool = False,
        allow_broad: bool = False,
    ) -> Dict[str, Any]:
        """Tiered, precision-first ranked search.

        Candidate stages run in precision order — exact token hits, then
        prefix (autocomplete), then 1-edit trigram tolerance — and the
        accepted minimum tier auto-broadens only when a tier returns fewer
        than SEARCH_MIN_RESULTS results. The regex/broad fallback is NOT
        part of the normal search path; it is only reachable via
        `allow_broad=True` (scripts/admin use, never the bot handler).
        """
        if not tokens:
            return {"results": [], "total": 0}

        tokens = [t.lower() for t in tokens if t]
        q_text = query if query else " ".join(tokens)
        q_tris = make_trigrams(q_text or "", TRIGRAM_MAX)

        # Apply optional filter clauses (chat/thread/ext/year/duration/size/resolution)
        f_clauses = []
        if filters:
            try:
                if "chat_id" in filters:
                    f_clauses.append({"chat_id": int(filters.get("chat_id"))})
                if "message_thread_id" in filters:
                    f_clauses.append({"message_thread_id": int(filters.get("message_thread_id"))})
                if "extension" in filters:
                    ext_val = filters.get("extension")
                    if isinstance(ext_val, dict):
                        # e.g. {"$nin": ["mp4", "mkv", ...]} from INDEX_VIDEO=false
                        f_clauses.append({"extension": ext_val})
                    else:
                        f_clauses.append({"extension": str(ext_val).lower()})
                if "year" in filters:
                    f_clauses.append({"year": int(filters.get("year"))})
                if "min_duration" in filters:
                    f_clauses.append({"duration": {"$gte": float(filters.get("min_duration"))}})
                if "max_duration" in filters:
                    f_clauses.append({"duration": {"$lte": float(filters.get("max_duration"))}})
                if "min_size" in filters:
                    f_clauses.append({"file_size": {"$gte": int(filters.get("min_size"))}})
                if "max_size" in filters:
                    f_clauses.append({"file_size": {"$lte": int(filters.get("max_size"))}})
                # resolution bucket: allow providing min_height
                if "min_height" in filters:
                    f_clauses.append({"height": {"$gte": int(filters.get("min_height"))}})
            except Exception:
                pass

        def _with_filters(candidate_part: dict) -> dict:
            if f_clauses:
                return {"$and": [candidate_part, {"$and": f_clauses}]}
            return candidate_part

        # start_tier: strict requires ALL tokens; otherwise the precision default
        start_tier = "all" if strict else DEFAULT_MIN_TIER

        classified: List[Dict[str, Any]] = []
        seen = set()

        def _classify(docs) -> None:
            for doc in docs:
                try:
                    key = (int(doc.get("chat_id") or 0), int(doc.get("message_id") or 0))
                except Exception:
                    key = (0, 0)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    match = classify_match(q_text, tokens, doc, q_tris, allow_broad=allow_broad)
                except Exception:
                    continue
                if match.tier == NONE_TIER:
                    continue
                doc["_tier"] = match.tier
                doc["_score"] = compute_search_score(doc, tokens, q_text, match, q_tris)
                classified.append(doc)

        def _count_at(tier: str) -> int:
            return sum(1 for d in classified if accepts_tier(d.get("_tier", NONE_TIER), tier))

        # Stage 1 — exact token candidates (title/quality/codec hits)
        try:
            candidate_filter = _with_filters(
                {"$or": [
                    {"title_tokens": {"$in": tokens}},
                    {"quality_tokens": {"$in": tokens}},
                    {"codec_tokens": {"$in": tokens}},
                ]}
            )
            _classify(self._coll.find(candidate_filter).limit(self.CANDIDATE_LIMIT))
        except Exception:
            pass

        # Stage 2 — prefix candidates (autocomplete), only when scarce
        if not strict and _count_at("prefix") < MIN_RESULTS:
            try:
                or_clauses = []
                for t in tokens:
                    or_clauses.append(
                        {"title_tokens": {"$elemMatch": {"$regex": f"^{re.escape(t)}", "$options": "i"}}}
                    )
                    or_clauses.append({"filename": {"$regex": f"^{re.escape(t)}", "$options": "i"}})
                _classify(self._coll.find(_with_filters({"$or": or_clauses})).limit(self.CANDIDATE_LIMIT))
            except Exception:
                pass

        # Stage 3 — trigram candidates (1-edit typo tolerance), only when still scarce
        if not strict and _count_at("typo") < MIN_RESULTS:
            try:
                if q_tris:
                    _classify(
                        self._coll.find(_with_filters({"trigrams": {"$in": q_tris}})).limit(self.CANDIDATE_LIMIT)
                    )
            except Exception:
                pass

        # Stage 4 — broad regex fallback (opt-in only; never in the normal path)
        if _count_at("typo") < MIN_RESULTS and allow_broad:
            try:
                _classify(self._prefix_fallback(tokens, limit=200))
            except Exception:
                pass

        # decide the accepted minimum tier (precision default, broaden when scarce)
        if strict:
            min_tier_used = "all"
        else:
            min_tier_used = resolve_min_tier(classified, start_tier=start_tier)
        accepted = [d for d in classified if accepts_tier(d.get("_tier", NONE_TIER), min_tier_used)]

        # deduplicate by filename (case-insensitive) keeping highest-scoring doc
        deduped = {}
        final_results = []
        try:
            for doc in accepted:
                key = None
                try:
                    # prefer normalized filename if present; fall back to computed norm
                    norm = doc.get("norm_filename") or self._normalize_filename(doc.get("filename") or "")
                    norm = (norm or "").strip().lower()
                    if norm:
                        key = norm
                    else:
                        # fallback to file id or chat+message
                        key = f"{doc.get('file_id') or ''}:{doc.get('chat_id')}:{doc.get('message_id')}"
                except Exception:
                    key = f"{doc.get('chat_id')}:{doc.get('message_id')}"

                prev = deduped.get(key)
                if prev is None or (doc.get("_score", 0) > prev.get("_score", 0)):
                    deduped[key] = doc

            final_results = list(deduped.values())
        except Exception:
            final_results = accepted

        # sort by (tier priority, score, timestamp) desc — tier dominates
        try:
            final_results.sort(
                key=lambda r: (tier_priority(r.get("_tier")), r.get("_score", 0.0), r.get("timestamp")),
                reverse=True,
            )
        except Exception:
            final_results.sort(key=lambda r: (r.get("_score", 0.0), r.get("timestamp")), reverse=True)

        total = len(final_results)
        start = (page - 1) * per_page
        end = start + per_page

        fuzzy_used = min_tier_used == "typo"
        broad_used = min_tier_used == "broad" or allow_broad
        log_search_quality(
            q_text,
            tokens,
            final_results,
            min_tier_used,
            source="bot",
            fuzzy_used=fuzzy_used,
            broad_used=broad_used,
        )

        return {"results": final_results[start:end], "total": total}

    def bulk_upsert_files(self, docs: List[Dict[str, Any]]):
        """Perform bulk upsert of multiple file documents.

        Each doc must contain keys: chat_id, message_id, filename, extension,
        title_tokens, quality_tokens, codec_tokens, year, other_tokens, timestamp
        """
        if not docs:
            return

        # ensure each doc has a `search_text` field for text search and trigrams
        for doc in docs:
            if "search_text" not in doc:
                parts: List[str] = []
                parts.extend([t for t in doc.get("title_tokens", [])])
                parts.extend([t for t in doc.get("quality_tokens", [])])
                parts.extend([t for t in doc.get("codec_tokens", [])])
                if doc.get("filename"):
                    try:
                        parts.append(str(doc.get("filename")).lower())
                    except Exception:
                        pass
                doc["search_text"] = " ".join([str(x).lower() for x in parts if x])
            # ensure trigrams exist for fuzzy matching
            if "trigrams" not in doc:
                try:
                    doc["trigrams"] = make_trigrams(doc.get("search_text", "") or doc.get("filename", ""), TRIGRAM_MAX)
                except Exception:
                    doc["trigrams"] = []
            # ensure normalized filename is present for bulk docs
            if "norm_filename" not in doc:
                try:
                    if doc.get("title_tokens"):
                        doc["norm_filename"] = " ".join([t.lower() for t in doc.get("title_tokens") if t])
                    else:
                        doc["norm_filename"] = self._normalize_filename(doc.get("filename") or "")
                except Exception:
                    doc["norm_filename"] = ""
            # full-title phrase string used by Atlas Search phrase queries
            if "title_phrase" not in doc:
                try:
                    doc["title_phrase"] = doc.get("norm_filename") or ""
                except Exception:
                    doc["title_phrase"] = ""

        # Use pymongo bulk_write via raw collection API
        try:
            from pymongo import UpdateOne

            bulk_ops = []
            for d in docs:
                bulk_ops.append(
                    UpdateOne(
                        {"chat_id": int(d["chat_id"]), "message_id": int(d["message_id"])},
                        {"$set": d},
                        upsert=True,
                    )
                )
            if bulk_ops:
                self._coll.bulk_write(bulk_ops, ordered=False)
        except Exception:
            # fallback to individual upserts
            for d in docs:
                try:
                    self._coll.update_one(
                        {"chat_id": int(d["chat_id"]), "message_id": int(d["message_id"])},
                        {"$set": d},
                        upsert=True,
                    )
                except Exception:
                    pass

        # Best-effort targeted invalidation: if this code is running in a
        # non-async context (e.g. a script), perform synchronous invalidation
        # by running the cache coroutine. If an asyncio loop is active in this
        # thread, skip invalidation here (caller may handle it) to avoid
        # attempting to drive the loop from the same thread.
        try:
            file_ids = []
            for d in docs:
                try:
                    file_ids.append(f"{int(d['chat_id'])}:{int(d['message_id'])}")
                except Exception:
                    continue

            if file_ids:
                try:
                    from app.api import app as main_app

                    cache = getattr(main_app.state, "search_cache", None)
                    if cache:
                        try:
                            # detect running loop
                            loop_running = True
                            try:
                                asyncio.get_running_loop()
                                loop_running = True
                            except RuntimeError:
                                loop_running = False

                            if loop_running:
                                logger.debug(
                                    "bulk_upsert_files: event loop running; skipping immediate cache invalidation; caller should invalidate"
                                )
                            else:
                                try:
                                    asyncio.run(cache.invalidate_by_file_ids(file_ids))
                                except Exception:
                                    logger.exception("bulk_upsert_files: synchronous cache.invalidate_by_file_ids failed")
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception:
            pass