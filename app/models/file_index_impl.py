from typing import List, Dict, Any, Set, Optional
from datetime import datetime
import re
import os
import asyncio
from app.services.search_utils import make_trigrams, trigram_similarity, TRIGRAM_MAX
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
            dup = self._coll.find_one(
                {
                    "chat_id": doc["chat_id"],
                    "filename": filename,
                    "message_id": {"$ne": doc["message_id"]},
                }
            )
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
                        logger.debug("upsert_file: cache.invalidate_by_file_ids failed for %s", file_ident)
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
    ) -> Dict[str, Any]:
        """Ranked search with optional strict mode and improved scoring.

        Adds phrase/exact boosts (useful for movie-title style queries), year
        detection, and deduplication by filename. `strict=True` requires all
        tokens to appear (higher precision).
        """
        if not tokens:
            return {"results": [], "total": 0}

        # Prepare lowercase token set and query trigrams for candidate selection
        token_set: Set[str] = set(t.lower() for t in tokens if t)
        q_text = query if query else " ".join(tokens)
        q_tris = make_trigrams(q_text or "", TRIGRAM_MAX)

        # detect a 4-digit year in the query (common in movie searches)
        year = None
        try:
            ym = re.search(r"\b(19|20)\d{2}\b", (q_text or ""))
            if ym:
                year = int(ym.group(0))
                if str(year) in token_set:
                    token_set.discard(str(year))
        except Exception:
            year = None

        # Candidate selection: prefer title token hits or trigram overlap
        if strict:
            candidate_filter = {"title_tokens": {"$all": [t for t in tokens if t]}}
        else:
            candidate_filter = {"$or": [{"title_tokens": {"$in": tokens}}, {"trigrams": {"$in": q_tris}}]}

        # Apply optional filter clauses (chat/thread/ext/year/duration/size/resolution)
        if filters:
            f_clauses = []
            try:
                if "chat_id" in filters:
                    f_clauses.append({"chat_id": int(filters.get("chat_id"))})
                if "message_thread_id" in filters:
                    f_clauses.append({"message_thread_id": int(filters.get("message_thread_id"))})
                if "extension" in filters:
                    f_clauses.append({"extension": str(filters.get("extension")).lower()})
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

            if f_clauses:
                candidate_filter = {"$and": [candidate_filter, {"$and": f_clauses}]}

        try:
            cursor = self._coll.find(candidate_filter).limit(self.CANDIDATE_LIMIT)
        except Exception:
            cursor = self._coll.find({}).limit(self.CANDIDATE_LIMIT)

        results: List[Dict[str, Any]] = []

        def _score_doc(doc: Dict[str, Any]) -> float:
            score = 0.0
            doc_titles = [t.lower() for t in doc.get("title_tokens", [])]
            doc_titles_set = set(doc_titles)
            # title exact/token matches
            title_matches = len(token_set & doc_titles_set)
            score += title_matches * self.TITLE_WEIGHT
            # quality/codec matches
            q_matches = token_set & set(t.lower() for t in doc.get("quality_tokens", []))
            score += len(q_matches) * self.QUALITY_WEIGHT
            c_matches = token_set & set(t.lower() for t in doc.get("codec_tokens", []))
            score += len(c_matches) * self.CODEC_WEIGHT

            # phrase / exact title matching (movie-style queries)
            try:
                doc_title_str = " ".join(doc_titles).strip()
                q_norm = re.sub(r"[^a-z0-9\s]", " ", (query or "").lower()).strip()
                if q_norm and doc_title_str:
                    if q_norm == doc_title_str:
                        # exact title match: strong boost
                        score += self.TITLE_WEIGHT * 6
                    elif doc_title_str.startswith(q_norm):
                        score += self.TITLE_WEIGHT * 3
                    elif q_norm in doc_title_str:
                        score += self.TITLE_WEIGHT * 2
            except Exception:
                pass

            # year match (boost when query contained a year)
            try:
                if year and doc.get("year") and int(doc.get("year")) == int(year):
                    score += self.YEAR_WEIGHT * 2
                else:
                    # fallback: if year token appears among tokens
                    if doc.get("year") and str(doc.get("year")) in token_set:
                        score += self.YEAR_WEIGHT
            except Exception:
                pass

            # filename substring / exact match
            qlower = re.sub(r"[^a-z0-9\s]", " ", (query or "").lower()).strip()
            try:
                fname = doc.get("filename", "") or ""
                fname_norm = re.sub(r"[^a-z0-9\s]", " ", fname.lower()).strip()
                if qlower and qlower and fname_norm and qlower == fname_norm:
                    # exact filename match
                    score += self.FILENAME_MATCH * 4
                elif qlower and qlower in fname.lower():
                    score += self.FILENAME_MATCH
            except Exception:
                pass

            # prefix boost
            prefix_matches = sum(1 for tok in token_set if any(tt.startswith(tok) for tt in doc_titles_set))
            score += prefix_matches * self.PREFIX_BOOST

            # trigram similarity (fuzzy match)
            try:
                doc_tris = doc.get("trigrams", []) or []
                tri_sim = trigram_similarity(q_tris, doc_tris)
                score += tri_sim * self.TRIGRAM_WEIGHT
            except Exception:
                pass

            # small length penalty for very long filenames
            try:
                fname_len = len(doc.get("filename", "") or "")
                score -= fname_len / float(self.FNAME_LEN_PENALTY_DIV)
            except Exception:
                pass

            # recency boost (recent items slightly preferred)
            try:
                ts = doc.get("timestamp")
                if ts:
                    if isinstance(ts, str):
                        try:
                            ts_dt = datetime.fromisoformat(ts)
                        except Exception:
                            ts_dt = None
                    else:
                        ts_dt = ts
                    if ts_dt:
                        age_seconds = (datetime.utcnow() - ts_dt).total_seconds()
                        window = 30 * 24 * 3600  # 30 days
                        recency = max(0.0, (window - age_seconds) / window)
                        score += recency * self.RECENCY_WEIGHT
            except Exception:
                pass
            return score

        for doc in cursor:
            try:
                s = _score_doc(doc)
                doc["_score"] = s
                results.append(doc)
            except Exception:
                continue

        # fallback to text search or prefix-based searches if no candidates found
        if not results:
            try:
                if query:
                    text_cursor = self._coll.find({"$text": {"$search": query}}).limit(self.CANDIDATE_LIMIT)
                else:
                    text_cursor = []
                for doc in text_cursor:
                    try:
                        s = _score_doc(doc)
                        doc["_score"] = s
                        results.append(doc)
                    except Exception:
                        continue
            except Exception:
                fallback = self._prefix_fallback(tokens, limit=200)
                for doc in fallback:
                    try:
                        s = _score_doc(doc)
                        doc["_score"] = s
                        results.append(doc)
                    except Exception:
                        continue

        # deduplicate by filename (case-insensitive) keeping highest-scoring doc
        deduped = {}
        final_results = []
        try:
            for doc in results:
                key = None
                try:
                    fname = (doc.get("filename") or "").strip().lower()
                    if fname:
                        key = fname
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
            final_results = results

        # sort by score desc then timestamp desc
        try:
            final_results.sort(key=lambda r: (r.get("_score", 0), r.get("timestamp")), reverse=True)
            total = len(final_results)
            start = (page - 1) * per_page
            end = start + per_page
            return {"results": final_results[start:end], "total": total}
        except Exception:
            results.sort(key=lambda r: (r.get("_score", 0), r.get("timestamp")), reverse=True)
            total = len(results)
            start = (page - 1) * per_page
            end = start + per_page
            return {"results": results[start:end], "total": total}

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