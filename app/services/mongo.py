from typing import Optional, List, Dict, Any, Generator
from pymongo import MongoClient, TEXT
from pymongo.collection import Collection
import certifi
from pymongo.errors import PyMongoError
from datetime import datetime
from app.utils.logger import logger


class MongoService:
    def __init__(
        self,
        uri: str,
        db_name: str = "tg_index",
        connect_timeout_ms: int = 15000,
    ):
        self.uri = uri
        self.db_name = db_name
        self.connect_timeout_ms = int(connect_timeout_ms or 15000)
        self.client: Optional[MongoClient] = None
        self.db = None

    def connect(self):
        def _uses_tls(uri: str) -> bool:
            u = uri.lower()
            return (
                u.startswith("mongodb+srv://")
                or "tls=true" in u
                or "ssl=true" in u
                or "mongodb.net" in u
            )

        try:
            if not self.uri:
                raise ValueError("MongoDB URI is empty")

            logger.info("Connecting to MongoDB...")

            kwargs = {
                "serverSelectionTimeoutMS": 15000,
                "connectTimeoutMS": 15000,
                "maxPoolSize": 50,
                "minPoolSize": 0,
            }

            if _uses_tls(self.uri):
                kwargs.update({
                    "tls": True,
                    "tlsCAFile": certifi.where(),
                })

            self.client = MongoClient(self.uri, **kwargs)

            result = self.client.admin.command("ping")
            logger.info("MongoDB ping successful: {}", result)

            self.db = self.client[self.db_name]

            logger.info(
                "MongoDB connected to database: {}",
                self.db_name,
            )

        except Exception as exc:
            self.client = None
            self.db = None

            logger.exception("MongoDB connection failed")

            raise RuntimeError(
                f"MongoDB connection failed: {exc}"
            ) from exc

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
        self._ensure_connected()

        col = self.db.get_collection("files")

        col.create_index([
            ("chat_id", 1),
            ("message_thread_id", 1),
            ("timestamp", -1),
        ])

        col.create_index(
            [("chat_id", 1), ("message_id", 1)],
            unique=True,
        )

        col.create_index(
            [("search_text", TEXT)],
            name="search_text_text",
        )

        col.create_index("trigrams")
        col.create_index("title_tokens")

        idx_col = self.db.get_collection("index_state")
        idx_col.create_index("chat_id", unique=True)

        logger.info("MongoDB indexes ensured")

    def get_last_indexed(self, chat_id: int) -> int:
        try:
            self._ensure_connected()
        except Exception:
            logger.exception(
                "get_last_indexed: cannot connect to MongoDB"
            )
            return 0

        try:
            doc = self.db.index_state.find_one(
                {"chat_id": chat_id}
            )

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
            logger.exception(
                "get_last_indexed: unexpected error"
            )
            return 0