# peer_manager.py
from typing import List
from pymongo import MongoClient
import os
from app.config.settings import strip_quotes

MONGO_URI = strip_quotes(os.getenv("MONGO_URI", "mongodb://localhost:27017/tg_index"))
DB_NAME = os.getenv("DB_NAME", "tg_index")
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
peers_col = db['peers']

def add_peer(chat_id: int):
    """Add a new peer to the DB if it doesn't exist yet."""
    peers_col.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id}},
        upsert=True
    )

def get_all_peers() -> List[int]:
    """Return list of all stored peer chat IDs."""
    return [doc['chat_id'] for doc in peers_col.find()]