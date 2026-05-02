#!/usr/bin/env python3
"""Delete duplicates grouped by (chat_id, normalized filename) keeping newest per group.

Now LIMITED to media files only (video/audio like mp4, mkv, mp3, etc.).

Usage:
  python scripts/delete_dups_by_norm.py --preview 10
  python scripts/delete_dups_by_norm.py --delete --yes
  python scripts/delete_dups_by_norm.py --delete --yes --limit 10000
"""
import argparse
import sys
import pathlib
from pprint import pprint

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config.settings import settings
from app.services.mongo import MongoService
from app.models.file_index_impl import FileIndex
from pymongo import UpdateOne
from datetime import datetime

# ✅ MEDIA ONLY (edit if you want stricter filtering like only .mp4)
MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".mp3", ".wav", ".aac", ".ogg", ".m4a"
}


def is_media_file(filename: str) -> bool:
    if not filename:
        return False
    ext = pathlib.Path(filename).suffix.lower()
    return ext in MEDIA_EXTENSIONS


def parse_args():
    p = argparse.ArgumentParser(description="Delete duplicate MEDIA file docs by normalized filename")
    p.add_argument("--preview", type=int, default=0, help="Show up to N sample duplicate groups (no deletes)")
    p.add_argument("--delete", action="store_true", help="Perform deletion")
    p.add_argument("--yes", action="store_true", help="Skip interactive confirmation when --delete is used")
    p.add_argument("--limit", type=int, default=0, help="Limit number of MEDIA documents to process (0=all)")
    return p.parse_args()


def to_timestamp_val(ts):
    if ts is None:
        return 0.0
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, datetime):
        return ts.timestamp()
    try:
        return datetime.fromisoformat(str(ts)).timestamp()
    except Exception:
        return 0.0


def main():
    args = parse_args()

    mongo = MongoService(settings.MONGO_URI, settings.DB_NAME)
    mongo.connect()
    coll = mongo.db.get_collection("files")
    fi = FileIndex(mongo.db)

    print("Scanning MEDIA documents to compute normalized filename and find duplicates...")

    projection = {
        "_id": 1,
        "chat_id": 1,
        "filename": 1,
        "norm_filename": 1,
        "title_tokens": 1,
        "message_id": 1,
        "timestamp": 1
    }

    # ✅ Mongo-side filtering for performance
    media_regex = r"\.(mp4|mkv|avi|mov|wmv|flv|webm|mp3|wav|aac|ogg|m4a)$"
    cursor = coll.find(
        {"filename": {"$regex": media_regex, "$options": "i"}},
        projection=projection
    )

    groups = {}
    processed = 0

    for d in cursor:
        if args.limit and processed >= args.limit:
            break

        filename = d.get("filename") or ""

        # ✅ Double-check filter (safety)
        if not is_media_file(filename):
            continue

        processed += 1

        chat_id = d.get("chat_id")

        # compute normalized filename
        try:
            if d.get("norm_filename"):
                norm = (d.get("norm_filename") or "").strip()
            elif d.get("title_tokens"):
                norm = " ".join([t.lower() for t in d.get("title_tokens") if t])
            else:
                norm = fi._normalize_filename(filename)
        except Exception:
            norm = fi._normalize_filename(filename)

        if not norm:
            continue

        key = (int(chat_id), norm)
        groups.setdefault(key, []).append(d)

    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    total_groups = len(dup_groups)
    total_docs = sum(len(v) for v in dup_groups.values())
    total_to_delete = sum(len(v) - 1 for v in dup_groups.values())

    print(f"Processed MEDIA documents: {processed}")
    print(f"Duplicate groups found: {total_groups}")
    print(f"Total docs in duplicate groups: {total_docs}")
    print(f"Estimated docs to delete: {total_to_delete}")

    if args.preview:
        print(f"Showing up to {args.preview} sample duplicate groups:")
        i = 0
        for (chat_id, norm), docs in dup_groups.items():
            if i >= args.preview:
                break
            print("---")
            print(f"chat_id={chat_id} norm='{norm}' group_size={len(docs)}")
            for dd in sorted(
                docs,
                key=lambda x: (to_timestamp_val(x.get("timestamp")), int(x.get("message_id") or 0)),
                reverse=True
            ):
                print({
                    "_id": str(dd.get("_id")),
                    "message_id": dd.get("message_id"),
                    "filename": dd.get("filename"),
                    "timestamp": dd.get("timestamp")
                })
            i += 1

    if not args.delete:
        print("No deletion requested. Rerun with --delete --yes to perform deletions.")
        return

    if not args.yes:
        confirm = input("Type DELETE to permanently remove duplicate documents: ")
        if confirm.strip() != "DELETE":
            print("Confirmation failed; aborting.")
            return

    # ✅ Step 1: update norm_filename
    print("Updating `norm_filename`...")
    updates = []
    for (chat_id, norm), docs in groups.items():
        for d in docs:
            if (d.get("norm_filename") or "") != norm:
                updates.append(UpdateOne({"_id": d.get("_id")}, {"$set": {"norm_filename": norm}}))

    try:
        BATCH = 1000
        for i in range(0, len(updates), BATCH):
            coll.bulk_write(updates[i:i + BATCH], ordered=False)
        print(f"Patched {len(updates)} documents")
    except Exception as e:
        print("Bulk update failed:", e)

    # ✅ Step 2: delete duplicates
    print("Deleting duplicates...")
    to_delete_ids = []

    for docs in dup_groups.values():
        sorted_docs = sorted(
            docs,
            key=lambda x: (to_timestamp_val(x.get("timestamp")), int(x.get("message_id") or 0)),
            reverse=True
        )
        to_delete_ids.extend([d["_id"] for d in sorted_docs[1:]])

    deleted_count = 0
    if to_delete_ids:
        BATCH = 1000
        for i in range(0, len(to_delete_ids), BATCH):
            res = coll.delete_many({"_id": {"$in": to_delete_ids[i:i + BATCH]}})
            deleted_count += int(res.deleted_count or 0)

    print(f"Deleted {deleted_count} documents")

    # ✅ Step 3: normalize remaining
    print("Running background normalization...")
    try:
        import subprocess
        subprocess.Popen(
            [sys.executable, "scripts/compute_norm_filenames.py", "--apply"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        print("Normalization started in background")
    except Exception as e:
        print("Failed to start normalization:", e)

    print("Done.")
    print(f"Processed: {processed}")
    print(f"Groups: {total_groups}")
    print(f"Deleted: {deleted_count}")


if __name__ == "__main__":
    main()