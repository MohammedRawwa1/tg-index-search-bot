#!/usr/bin/env python3
import json
import certifi
from pymongo import MongoClient
import pathlib, sys

# ensure project root is on sys.path so `app` imports resolve when run from scripts/
_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config.settings import settings

CHAT_ID = -1002347599837


def main():
    uri = settings.MONGO_URI
    db_name = settings.DB_NAME
    try:
        ca = None
        u = uri.lower()
        if u.startswith("mongodb+srv://") or "mongodb.net" in u or "tls=true" in u or "ssl=true" in u:
            ca = certifi.where()
        if ca:
            client = MongoClient(uri, tls=True, tlsCAFile=ca)
        else:
            client = MongoClient(uri)
        db = client[db_name]
        files = db.get_collection("files")
        state = db.get_collection("index_state")

        count = files.count_documents({"chat_id": CHAT_ID})
        print("files_count:", count)

        sample = list(files.find({"chat_id": CHAT_ID}).limit(5))
        print("sample_docs:")
        for d in sample:
            try:
                print(json.dumps(d, default=str, indent=2))
            except Exception:
                print(d)

        last = state.find_one({"chat_id": CHAT_ID})
        print("index_state:", json.dumps(last, default=str, indent=2))
    except Exception as exc:
        print("ERROR querying Mongo:", exc)
        import traceback; traceback.print_exc()

if __name__ == '__main__':
    main()

