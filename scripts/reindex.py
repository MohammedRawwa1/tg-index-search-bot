#!/usr/bin/env python3
"""Reindex script: recompute `search_text` and `trigrams` for documents in the `files` collection.

Usage:
  python scripts/reindex.py --batch-size 500 --dry-run

This script connects to Mongo using `MONGO_URI` / `DB_NAME` from env or app.config.settings,
iterates the `files` collection, and updates `search_text`, `trigrams`, and token fields
using existing values or by tokenizing `filename` when missing.
"""

import sys
from pathlib import Path
repo_root = Path(__file__).resolve().parents[1]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

import argparse
import os
import time
from typing import Dict, Any

from pymongo import UpdateOne

try:
    from app.config.settings import settings, strip_quotes
    from app.services.mongo import MongoService
    from app.services.search_utils import make_trigrams, TRIGRAM_MAX
    from app.services.tokenizer import tokenize_filename
except Exception:
    # best-effort imports; fall back to env vars below
    settings = None
    strip_quotes = None
    MongoService = None
    make_trigrams = None
    TRIGRAM_MAX = 300
    tokenize_filename = None


def build_update_for_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    title_tokens = doc.get("title_tokens") or []
    quality_tokens = doc.get("quality_tokens") or []
    codec_tokens = doc.get("codec_tokens") or []
    filename = doc.get("filename") or ""

    # attempt to fill missing token groups from filename
    if (not title_tokens) and filename and tokenize_filename:
        try:
            tk = tokenize_filename(filename)
            title_tokens = tk.get("title_tokens", []) or title_tokens
            quality_tokens = tk.get("quality_tokens", []) or quality_tokens
            codec_tokens = tk.get("codec_tokens", []) or codec_tokens
            if (not doc.get("year")) and tk.get("year"):
                doc["year"] = tk.get("year")
        except Exception:
            pass

    search_parts = []
    search_parts.extend([t for t in title_tokens if t])
    search_parts.extend([t for t in quality_tokens if t])
    search_parts.extend([t for t in codec_tokens if t])
    if filename:
        try:
            search_parts.append(str(filename).lower())
        except Exception:
            pass

    search_text = " ".join([str(x).lower() for x in search_parts if x])
    try:
        tris = make_trigrams(search_text or "", TRIGRAM_MAX)
    except Exception:
        tris = []

    update: Dict[str, Any] = {
        "search_text": search_text,
        "trigrams": tris,
        # keep token fields canonicalized
        "title_tokens": title_tokens,
        "quality_tokens": quality_tokens,
        "codec_tokens": codec_tokens,
        "year": doc.get("year"),
    }
    return update


def main():
    parser = argparse.ArgumentParser(description="Reindex files collection: recompute search_text & trigrams")
    parser.add_argument("--batch-size", type=int, default=500, help="Bulk batch size")
    parser.add_argument("--dry-run", action="store_true", help="Don't write; just preview updates")
    parser.add_argument("--limit", type=int, default=0, help="Limit documents processed (0 = all)")
    parser.add_argument("--skip-trigrams", action="store_true", help="Skip docs that already have trigrams")

    args = parser.parse_args()

    # determine Mongo connection
    MONGO_URI = os.getenv("MONGO_URI")
    DB_NAME = os.getenv("DB_NAME")
    if MONGO_URI and strip_quotes:
        MONGO_URI = strip_quotes(MONGO_URI)
    if not MONGO_URI or not DB_NAME:
        if settings is not None:
            MONGO_URI = MONGO_URI or settings.MONGO_URI
            DB_NAME = DB_NAME or settings.DB_NAME

    if not MONGO_URI:
        print("ERROR: MONGO_URI not set in env and could not import settings.")
        return 2

    ms = MongoService(MONGO_URI, DB_NAME, connect_timeout_ms=(settings.MONGO_CONNECT_TIMEOUT_MS if settings else 10000))
    print(f"Connecting to Mongo: {MONGO_URI} (db={DB_NAME})")
    ms.connect()
    print("Connected; ensuring indexes...")
    try:
        ms.ensure_indexes()
    except Exception:
        pass

    col = ms.db.get_collection("files")

    query = {}
    cursor = col.find(query)
    total_docs = col.count_documents(query)
    if args.limit and args.limit > 0:
        total_docs = min(total_docs, args.limit)
        cursor = col.find(query).limit(args.limit)

    print(f"Reindexing up to {total_docs} documents (batch={args.batch_size})")

    bulk_ops = []
    processed = 0
    updated = 0
    start = time.time()

    for doc in cursor:
        processed += 1
        if args.skip_trigrams and doc.get("trigrams"):
            # skip docs that already have trigrams
            continue
        upd = build_update_for_doc(doc)
        if args.dry_run:
            print(f"[DRY] _id={doc.get('_id')} -> search_text_len={len(upd.get('search_text',''))} trigrams={len(upd.get('trigrams',[]))}")
        else:
            bulk_ops.append(UpdateOne({"_id": doc.get("_id")}, {"$set": upd}, upsert=False))
        if not args.dry_run and len(bulk_ops) >= args.batch_size:
            try:
                res = col.bulk_write(bulk_ops, ordered=False)
                updated += (res.modified_count + res.upserted_count)
            except Exception as exc:
                print("Bulk write failed:", exc)
            bulk_ops = []
        if processed % 500 == 0:
            elapsed = time.time() - start
            print(f"Processed {processed}/{total_docs} docs, updated {updated}, elapsed {elapsed:.1f}s")

    # flush remaining
    if not args.dry_run and bulk_ops:
        try:
            res = col.bulk_write(bulk_ops, ordered=False)
            updated += (res.modified_count + res.upserted_count)
        except Exception as exc:
            print("Final bulk write failed:", exc)

    elapsed = time.time() - start
    print(f"Done. Processed={processed} Updated={updated} Elapsed={elapsed:.1f}s")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
