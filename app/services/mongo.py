from typing import Optional, List, Dict, Any, Generator
from pymongo import MongoClient, TEXT
from pymongo.collection import Collection
import certifi
from pymongo.errors import PyMongoError
from datetime import datetime
from app.utils.logger import logger


class MongoService:
    def __init__(self, uri: str, db_name: str = "tg_index", connect_timeout_ms: int = 2000):
        self.uri = uri
        self.db_name = db_name
        self.connect_timeout_ms = int(connect_timeout_ms or 2000)
        self.client: Optional[MongoClient] = None
        self.db = None

    def connect(self):
        import os
        def _uses_tls(uri: str) -> bool:
            u = uri.lower()
            return (
                u.startswith("mongodb+srv://")
                or "tls=true" in u
                or "ssl=true" in u
                or "mongodb.net" in u
            )
        try:
            kwargs = dict(serverSelectionTimeoutMS=self.connect_timeout_ms, maxPoolSize=200, minPoolSize=10)
            if _uses_tls(self.uri):
                kwargs.update({"tls": True, "tlsCAFile": certifi.where()})
            self.client = MongoClient(self.uri, **kwargs)
            self.client.admin.command("ping")
            self.db = self.client[self.db_name]
            logger.info("MongoDB connected")
        except PyMongoError as exc:
            logger.error("Mongo connect failed: {}", exc)
            self.client = None
            self.db = None
            raise RuntimeError(f"MongoDB connection failed: {exc}") from exc

    def _ensure_connected(self):
        if self.client is None or self.db is None:
            self.connect()

    def _build_md_link(self, doc: Dict[str, Any]) -> str:
        try:
            chat_id = str(doc.get("chat_id", ""))
            msg_id = doc.get("message_id")
            if chat_id.startswith("-100"):
                short = chat_id[4:]
                url = f"https://t.me/c/{short}/{msg_id}"
            else:
                return doc.get("filename", "-")
            name = (doc.get("filename") or "-").replace("\n", " ")
            return f"[{name}]({url})"
        except Exception:
            return doc.get("filename", "-")

    # -------------------------------
    # ⚡ CURSOR-BASED PAGINATION
    # -------------------------------
    def paginated_search(
        self,
        tokens: List[str],
        query: str,
        last_ts: Optional[int] = None,
        per_page: int = 20,
    ) -> Dict[str, Any]:
        """
        Cursor-based pagination for large datasets:
        - last_ts: timestamp of the last seen item (from previous page)
        - per_page: number of items per page
        """
        self._ensure_connected()
        col = self.db.get_collection("files")

        token_query = {"$or": [
            {"title_tokens": {"$in": tokens}},
            {"quality_tokens": {"$in": tokens}},
            {"codec_tokens": {"$in": tokens}},
        ]}
        text_query = {"$text": {"$search": query}}
        final_query = {"$or": [token_query, text_query]}

        if last_ts:
            # cursor pagination: only fetch docs older than last_ts
            final_query["timestamp"] = {"$lt": last_ts}

        projection = {
            "_id": 0,
            "filename": 1,
            "chat_id": 1,
            "message_id": 1,
            "timestamp": 1,
        }

        cursor = col.find(final_query, projection).sort("timestamp", -1).limit(per_page)
        results = []
        for doc in cursor:
            doc["md"] = self._build_md_link(doc)
            results.append(doc)

        # next cursor: last item's timestamp
        next_cursor = results[-1]["timestamp"] if results else None

        return {"results": results, "next_cursor": next_cursor}

    # -------------------------------
    # 🚀 STREAM SEARCH (INFINITE SCROLL)
    # -------------------------------
    def stream_search(
        self,
        tokens: List[str],
        query: str,
        batch_size: int = 100,
    ) -> Generator[Dict[str, Any], None, None]:
        self._ensure_connected()
        col = self.db.get_collection("files")

        query_filter = {
            "$or": [
                {"title_tokens": {"$in": tokens}},
                {"$text": {"$search": query}},
            ]
        }
        projection = {"_id": 0, "filename": 1, "chat_id": 1, "message_id": 1}

        cursor = col.find(query_filter, projection).batch_size(batch_size)
        for doc in cursor:
            doc["md"] = self._build_md_link(doc)
            yield doc

    # -------------------------------
    # 📊 INDEX STATE
    # -------------------------------
    def get_index_state(self, chat_id: int) -> dict:
        self._ensure_connected()
        return self.db.index_state.find_one({"chat_id": chat_id}) or {}

    def set_last_indexed(self, chat_id: int, message_id: int):
        self._ensure_connected()

        self.db.index_state.update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "last_message_id": int(message_id),
                    "updated_at": datetime.utcnow(),
                }
            },
            upsert=True,
        )

    def ensure_indexes(self) -> None:
        """Ensure common MongoDB indexes used by the application.

        This is a synchronous helper used by scripts and startup paths
        that rely on the presence of particular indexes for efficient
        queries and sorting.
        """
        try:
            self._ensure_connected()
        except Exception:
            logger.exception("ensure_indexes: cannot connect to MongoDB")
            return

        try:
            col = self.db.get_collection("files")
            # Compound index to support sorting within a chat by thread/timestamp
            try:
                col.create_index([("chat_id", 1), ("message_thread_id", 1), ("timestamp", -1)], background=True)
                logger.info("Ensured compound index files(chat_id,message_thread_id,timestamp)")
            except Exception:
                logger.exception("Failed to create compound index on files collection")

            # Unique constraint for chat_id + message_id to make upserts efficient
            try:
                col.create_index([("chat_id", 1), ("message_id", 1)], unique=True, background=True)
                logger.info("Ensured unique index files(chat_id,message_id)")
            except Exception:
                logger.exception("Failed to create unique index on files(chat_id,message_id)")

            # Text index on search_text for $text searches
            try:
                # If any text index already exists on this collection, skip
                # creating a new one (MongoDB allows only a single text index
                # per collection and attempting to create another will raise
                # IndexOptionsConflict). Use list_indexes to detect existing
                # text indexes.
                existing_text_index = None
                try:
                    for idx in col.list_indexes():
                        if idx is None:
                            continue
                        # presence of 'textIndexVersion' indicates a text index
                        if "textIndexVersion" in idx:
                            existing_text_index = idx
                            break
                except Exception:
                    existing_text_index = None

                if existing_text_index:
                    try:
                        logger.info("Text index already present (name={}), skipping creation", existing_text_index.get("name"))
                    except Exception:
                        logger.info("Text index already present, skipping creation")
                else:
                    # No existing text index: create one on `search_text`.
                    col.create_index([("search_text", TEXT)], background=True, name="search_text_text", weights={"search_text": 1})
                    logger.info("Ensured text index on files(search_text)")
            except Exception as exc:
                # If an equivalent index exists with different options we may
                # get an IndexOptionsConflict; log info and continue.
                try:
                    from pymongo.errors import OperationFailure

                    if isinstance(exc, OperationFailure) and getattr(exc, "code", None) == 85:
                        logger.info("Text index creation skipped: equivalent index already exists")
                    else:
                        logger.exception("Failed to create text index on files(search_text): {}", exc)
                except Exception:
                    logger.exception("Failed to create text index on files(search_text): {}", exc)

            # Indexes to speed up trigram / token queries
            try:
                col.create_index("trigrams", background=True)
            except Exception:
                logger.exception("Failed to create index on files(trigrams)")
            try:
                col.create_index("title_tokens", background=True)
            except Exception:
                logger.exception("Failed to create index on files(title_tokens)")

            # Ensure index_state has unique chat_id index
            try:
                idx_col = self.db.get_collection("index_state")
                idx_col.create_index("chat_id", unique=True, background=True)
            except Exception:
                logger.exception("Failed to create index on index_state(chat_id)")

        except Exception:
            logger.exception("ensure_indexes: unexpected error")

    def get_last_indexed(self, chat_id: int) -> int:
        """Return the last indexed message id for a chat (0 if none).

        This is a synchronous helper used by backfill scripts and
        backfill workers which call it without awaiting.
        """
        try:
            self._ensure_connected()
        except Exception:
            logger.exception("get_last_indexed: cannot connect to MongoDB")
            return 0

        try:
            doc = self.db.index_state.find_one({"chat_id": chat_id})
            if not doc:
                return 0
            last = doc.get("last_message_id")
            if last is None:
                return 0
            try:
                return int(last)
            except Exception:
                return 0
        except Exception:
            logger.exception("get_last_indexed: unexpected error")
            return 0