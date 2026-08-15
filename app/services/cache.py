import asyncio
import time
import hashlib
import json
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Optional
from app.config.settings import settings


class AsyncSearchCache:
    """Lightweight async in-memory LRU cache with optional Mongo persistence.

    Stores cached search results keyed by a SHA256 of query+page+per_page+flags.
    When a Motor client is provided the cache also persists entries into
    a `search_cache` collection with an `expires_at` field so documents are
    auto-removed by MongoDB TTL index.
    """

    def __init__(self, motor_client=None, db_name: Optional[str] = None, collection: str = "search_cache", max_entries: int = 1024, default_ttl: int = 3600):
        self.client = motor_client
        self.db_name = db_name or getattr(settings, "DB_NAME", "test")
        self.collection = collection
        self.max_entries = int(max_entries or 1024)
        self.default_ttl = int(default_ttl or 3600)
        self._cache = OrderedDict()
        self._meta = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def make_key(query: str, page: int = 1, per_page: int = 50, strict: bool = False, thread_id: Optional[int] = None, allow_broad: bool = False) -> str:
        s = f"q={query}|p={page}|r={per_page}|s={int(bool(strict))}|t={thread_id or ''}|b={int(bool(allow_broad))}"
        return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()

    async def ensure_indexes(self):
        if not self.client:
            return
        try:
            db = self.client[self.db_name]
            col = db.get_collection(self.collection)
            # TTL index on expires_at to allow Mongo to auto-delete expired entries
            await col.create_index("expires_at", expireAfterSeconds=0, background=True)
            # index on metadata.query to enable invalidation by query
            await col.create_index("metadata.query", background=True)
            # index on metadata.file_ids to enable targeted invalidation by file id
            try:
                await col.create_index("metadata.file_ids", background=True)
            except Exception:
                # best-effort, not critical
                pass
        except Exception:
            # best-effort
            pass

    async def get(self, key: str) -> Optional[Any]:
        now = time.time()
        async with self._lock:
            entry = self._cache.get(key)
            if entry:
                if entry.get("expires_at", 0) < now:
                    try:
                        del self._cache[key]
                    except Exception:
                        pass
                    return None
                # move to end (most recently used)
                try:
                    self._cache.move_to_end(key)
                except Exception:
                    pass
                return entry.get("value")

        # not in-memory, try Mongo
        if self.client:
            try:
                db = self.client[self.db_name]
                col = db.get_collection(self.collection)
                doc = await col.find_one({"_id": key})
                if not doc:
                    return None
                exp = doc.get("expires_at")
                if exp and isinstance(exp, datetime) and exp < datetime.utcnow():
                    try:
                        await col.delete_one({"_id": key})
                    except Exception:
                        pass
                    return None
                val = doc.get("value")
                # populate memory
                async with self._lock:
                    while len(self._cache) >= self.max_entries:
                        try:
                            self._cache.popitem(last=False)
                        except Exception:
                            break
                    self._cache[key] = {"value": val, "expires_at": (exp.timestamp() if exp and isinstance(exp, datetime) else time.time() + self.default_ttl), "metadata": doc.get("metadata")}
                return val
            except Exception:
                return None
        return None

    async def set(self, key: str, value: Any, metadata: Optional[dict] = None, ttl: Optional[int] = None) -> None:
        ttl = int(ttl or self.default_ttl)
        # For in-memory entries store epoch float timestamps to avoid
        # ambiguity with naive datetimes and local timezone handling.
        expires_at_ts = time.time() + ttl
        expires_at_dt = datetime.utcnow() + timedelta(seconds=ttl)

        async with self._lock:
            if key in self._cache:
                try:
                    del self._cache[key]
                except Exception:
                    pass
            while len(self._cache) >= self.max_entries:
                try:
                    self._cache.popitem(last=False)
                except Exception:
                    break
            # store numeric timestamp for in-memory checks
            self._cache[key] = {"value": value, "expires_at": expires_at_ts, "metadata": metadata}

        if self.client:
            try:
                db = self.client[self.db_name]
                col = db.get_collection(self.collection)
                # persist a timezone-naive UTC datetime for Mongo TTL index
                doc = {"_id": key, "value": value, "metadata": metadata, "expires_at": expires_at_dt}
                await col.replace_one({"_id": key}, doc, upsert=True)
            except Exception:
                pass

    async def delete(self, key: str) -> None:
        async with self._lock:
            try:
                if key in self._cache:
                    del self._cache[key]
            except Exception:
                pass
        if self.client:
            try:
                db = self.client[self.db_name]
                col = db.get_collection(self.collection)
                await col.delete_one({"_id": key})
            except Exception:
                pass

    async def invalidate_by_query(self, query: str) -> None:
        # remove memory entries matching metadata.query
        async with self._lock:
            keys = [k for k, v in list(self._cache.items()) if (v.get("metadata") or {}).get("query") == query]
            for k in keys:
                try:
                    del self._cache[k]
                except Exception:
                    pass
        if self.client:
            try:
                db = self.client[self.db_name]
                col = db.get_collection(self.collection)
                await col.delete_many({"metadata.query": query})
            except Exception:
                pass

    async def invalidate_by_file_ids(self, file_ids: list) -> None:
        """Invalidate cached entries that reference any of the provided file identifier strings.

        `file_ids` should be a list of strings in the same format the cache stores
        in `metadata.file_ids` (e.g. "<chat_id>:<message_id>"). This performs a
        best-effort removal from in-memory LRU entries and the persisted Mongo
        collection when available.
        """
        if not file_ids:
            return
        # normalize to string set for comparisons
        try:
            idset = set(str(x) for x in file_ids)
        except Exception:
            idset = set()

        # remove from in-memory cache where metadata.file_ids intersects
        async with self._lock:
            keys = []
            for k, v in list(self._cache.items()):
                try:
                    meta = v.get("metadata") or {}
                    fids = meta.get("file_ids") or []
                    for fid in (fids or []):
                        if str(fid) in idset:
                            keys.append(k)
                            break
                except Exception:
                    continue
            for k in keys:
                try:
                    del self._cache[k]
                except Exception:
                    pass

        # remove persisted docs in Mongo if available
        if self.client:
            try:
                db = self.client[self.db_name]
                col = db.get_collection(self.collection)
                await col.delete_many({"metadata.file_ids": {"$in": list(idset)}})
            except Exception:
                pass

    async def clear(self) -> None:
        async with self._lock:
            self._cache.clear()
        if self.client:
            try:
                db = self.client[self.db_name]
                col = db.get_collection(self.collection)
                await col.delete_many({})
            except Exception:
                pass

    async def stats(self) -> dict:
        mem = 0
        async with self._lock:
            mem = len(self._cache)
        mongo_count = None
        if self.client:
            try:
                db = self.client[self.db_name]
                col = db.get_collection(self.collection)
                mongo_count = await col.count_documents({})
            except Exception:
                mongo_count = None
        return {"mem_entries": mem, "mongo_count": mongo_count}
