from datetime import datetime, timedelta
from typing import Optional
import uuid


class CallbackStore:
    """Simple Mongo-backed store for long search queries to keep callback_data small.

    Documents stored in collection `callback_store`:
      { token: str, query: str, created_at: datetime, expires_at: datetime }

    Token is a short hex string (12 chars) derived from uuid4.
    """

    def __init__(self, db, collection_name: str = "callback_store"):
        self._col = db.get_collection(collection_name)

    def _make_token(self) -> str:
        return uuid.uuid4().hex[:12]

    def store_query(self, query: str, ttl_seconds: int = 86400) -> str:
        token = self._make_token()
        now = datetime.utcnow()
        doc = {"token": token, "query": query, "created_at": now, "expires_at": now + timedelta(seconds=ttl_seconds)}
        # upsert in case of collision (unlikely)
        self._col.insert_one(doc)
        return token

    def get_query(self, token: str) -> Optional[str]:
        doc = self._col.find_one({"token": token})
        if not doc:
            return None
        # check expiry
        if doc.get("expires_at") and doc["expires_at"] < datetime.utcnow():
            try:
                self._col.delete_one({"token": token})
            except Exception:
                pass
            return None
        return doc.get("query")

    def cleanup_expired(self) -> int:
        res = self._col.delete_many({"expires_at": {"$lt": datetime.utcnow()}})
        return res.deleted_count