#!/usr/bin/env python3
import sys
import traceback
from pathlib import Path

# Ensure the repository root is on sys.path so `app` is importable when running
# this script from the project folder.
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

try:
    from app.config.settings import settings
except Exception as e:
    print("Failed to import project settings (run from repo root).", file=sys.stderr)
    print(e, file=sys.stderr)
    sys.exit(2)

try:
    from pymongo import MongoClient
except Exception as e:
    print("pymongo not installed. Install with: pip install pymongo", file=sys.stderr)
    print(e, file=sys.stderr)
    sys.exit(3)

uri = getattr(settings, "MONGO_URI", None) or "mongodb://localhost:27017/tg_index"
tele_db = getattr(settings, "TELEGRAPH_DB", "course_bot")
print("Using MONGO_URI:", uri)
print("TELEGRAPH_DB:", tele_db)

try:
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
except Exception as e:
    print("Mongo ping failed:", e, file=sys.stderr)
    traceback.print_exc()
    sys.exit(4)

db = client[tele_db]
coll = db.get_collection("telegraph_pages")

queries = [
    ("seduction", {"$or":[ {"content": {"$regex": "seduction", "$options":"i"}}, {"page_header":{"$regex":"seduction","$options":"i"}}, {"query":{"$regex":"seduction","$options":"i"}} ]}),
    ("seduction skills", {"$or":[ {"content": {"$regex": "seduction skills", "$options":"i"}}, {"page_header":{"$regex":"seduction skills","$options":"i"}}, {"query":{"$regex":"seduction skills","$options":"i"}} ]}),
    ("pdfcoffee", {"$or":[ {"content": {"$regex": "pdfcoffee", "$options":"i"}}, {"page_header":{"$regex":"pdfcoffee","$options":"i"}}, {"query":{"$regex":"pdfcoffee","$options":"i"}} ]}),
    ("algebra", {"$or":[ {"content": {"$regex": "algebra", "$options":"i"}}, {"page_header":{"$regex":"algebra","$options":"i"}}, {"query":{"$regex":"algebra","$options":"i"}} ]}),
    ("chat_2347599837", {"$or":[ {"content": {"$regex": "2347599837", "$options":"i"}}, {"page_header":{"$regex":"2347599837","$options":"i"}}, {"query":{"$regex":"2347599837","$options":"i"}} ]}),
]

found_any = False

for qname, q in queries:
    print(f"\n--- Searching telegraph_pages for: {qname} ---")
    cursor = coll.find(q).limit(200)
    found = False
    for doc in cursor:
        found = True
        found_any = True
        _id = str(doc.get("_id"))
        print("=== PAGE ID:", _id)
        print(" query:", doc.get("query"))
        print(" page_header:", doc.get("page_header"))
        print(" group:", doc.get("group"), " part:", doc.get("part_index"), "/", doc.get("total_parts"))
        content = doc.get("content") or doc.get("markdown") or ""
        print(" total_results:", doc.get("total_results"), " content_len:", len(content))
        print(" content_preview:", (content[:300].replace("\n","\\n")))
        lr = doc.get("line_refs") or []
        print(" line_refs_count:", len(lr))
        for i, r in enumerate(lr[:20]):
            if not isinstance(r, dict):
                print("  -", i, "<non-dict ref>", r)
                continue
            print("  -", i, r.get("chat_id"), r.get("message_id"), (r.get("filename") or "")[:120], r.get("match_type"), r.get("match_score"))
        print("")
    if not found:
        print("No telegraph_pages matching", qname, "found in", tele_db)

if not found_any:
    print("No matching telegraph_pages found for any query (searched: seduction, seduction skills, pdfcoffee, algebra)")

# Check files collection for a few sample message ids mentioned in your snippet
files_db = client[getattr(settings, "DB_NAME", "tg_index")] 
files_coll = files_db.get_collection("files")
sample_ids = [148397, 148303, 148270, 148294, 148985, 151935]
print("Checking files collection for sample message_ids:", sample_ids)
for mid in sample_ids:
    try:
        doc = files_coll.find_one({"message_id": int(mid)})
    except Exception:
        doc = None
    if doc:
        print("found file for message_id", mid, "chat_id:", doc.get("chat_id"), "filename:", (doc.get("filename") or "")[:140])
    else:
        print("no file doc for message_id", mid)

print("done")
