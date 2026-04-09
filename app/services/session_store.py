import os
import base64
import secrets
from typing import Optional, Dict, Any, List
from datetime import datetime

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.services.mongo import MongoService


def _load_enc_key() -> Optional[bytes]:
    v = os.getenv("SESSION_ENC_KEY")
    if not v:
        return None
    # Accept base64-encoded 32-byte key or raw hex
    try:
        return base64.b64decode(v)
    except Exception:
        try:
            return bytes.fromhex(v)
        except Exception:
            return None


def _make_session_key() -> str:
    return "s_" + secrets.token_urlsafe(8)


class SessionStore:
    """Encrypt and store Telegram user session strings in MongoDB.

    The store keeps only an encrypted blob in the `sessions` collection.
    Encryption key must be provided via `SESSION_ENC_KEY` environment variable
    (base64-encoded 32 bytes). If not set, the store will fall back to
    storing plaintext but will log a warning.
    """

    def __init__(self, mongo: MongoService, collection: str = "sessions"):
        if mongo is None or mongo.db is None:
            raise RuntimeError("SessionStore requires a connected MongoService")
        self.mongo = mongo
        self.col = mongo.db.get_collection(collection)
        self._key = _load_enc_key()

    def _encrypt(self, plaintext: str) -> str:
        if not self._key:
            # store as plain base64 for dev convenience (NOT recommended)
            return "PLAIN:" + base64.b64encode(plaintext.encode("utf-8")).decode("ascii")
        aes = AESGCM(self._key)
        nonce = secrets.token_bytes(12)
        ct = aes.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.b64encode(nonce + ct).decode("ascii")

    def _decrypt(self, blob: str) -> str:
        if blob.startswith("PLAIN:"):
            return base64.b64decode(blob.split(":", 1)[1]).decode("utf-8")
        if not self._key:
            raise RuntimeError("Encryption key not configured; cannot decrypt stored sessions")
        raw = base64.b64decode(blob)
        nonce = raw[:12]
        ct = raw[12:]
        aes = AESGCM(self._key)
        return aes.decrypt(nonce, ct, None).decode("utf-8")

    def store_session(
        self,
        session_string: str,
        api_id: Optional[int] = None,
        api_hash: Optional[str] = None,
        name: Optional[str] = None,
        owner: Optional[int] = None,
        allowed_chats: Optional[List[int]] = None,
        allowed_topics: Optional[Dict[int, List[int]]] = None,
    ) -> str:
        session_key = _make_session_key()
        doc = {
            "session_key": session_key,
            "session_string_enc": self._encrypt(session_string),
            "api_id": int(api_id) if api_id is not None else None,
            "api_hash": str(api_hash) if api_hash is not None else None,
            "name": name,
            "owner": int(owner) if owner is not None else None,
            "allowed_chats": allowed_chats or [],
            "allowed_topics": allowed_topics or {},
            "status": "active",
            "created_at": datetime.utcnow(),
            "last_used": None,
        }
        self.col.insert_one(doc)
        return session_key

    def get_session_doc(self, session_key: str) -> Optional[Dict[str, Any]]:
        return self.col.find_one({"session_key": session_key})

    def get_session_string(self, session_key: str) -> Optional[str]:
        doc = self.get_session_doc(session_key)
        if not doc:
            return None
        enc = doc.get("session_string_enc")
        if not enc:
            return None
        s = self._decrypt(enc)
        # update last used timestamp
        try:
            self.col.update_one({"session_key": session_key}, {"$set": {"last_used": datetime.utcnow()}})
        except Exception:
            pass
        return s

    def delete_session(self, session_key: str) -> bool:
        res = self.col.delete_one({"session_key": session_key})
        return res.deleted_count > 0

    def list_sessions(self, owner: Optional[int] = None) -> List[Dict[str, Any]]:
        q = {}
        if owner is not None:
            q["owner"] = int(owner)
        return list(self.col.find(q))