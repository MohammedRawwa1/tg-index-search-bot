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

from bson import ObjectId
from pymongo import UpdateOne
from pymongo.errors import AutoReconnect, CursorNotFound

try:
    from app.config.settings import settings, strip_quotes
    from app.services.mongo import MongoService
    from app.services.search_utils import make_trigrams, TRIGRAM_MAX
    from app.services.tokenizer import tokenize_filename
    from app.services.textnorm import normalize_text
except Exception:
    # best-effort imports; fall back to env vars below
    settings = None
    strip_quotes = None
    MongoService = None
    make_trigrams = None
    TRIGRAM_MAX = 300
    tokenize_filename = None
    normalize_text = None


def _norm_from_filename(filename: str) -> str:
    """Normalize a filename the way FileIndex._normalize_filename does.

    Drops the extension, then case-folds and folds word separators / Arabic
    variants. A bare ``lower()`` kept separators, punctuation and the
    extension, so the same file could end up with two different dedupe keys.
    """
    s = str(filename or "")
    if "." in s:
        s = s.rsplit(".", 1)[0]
    if normalize_text is None:
        return s.lower()
    try:
        return normalize_text(s)
    except Exception:
        return s.lower()


def build_update_for_doc(
    doc: Dict[str, Any], refresh_tokens: bool = False
) -> Dict[str, Any]:
    title_tokens = doc.get("title_tokens") or []
    quality_tokens = doc.get("quality_tokens") or []
    codec_tokens = doc.get("codec_tokens") or []
    filename = doc.get("filename") or ""

    # --refresh-tokens re-tokenizes EVERY doc from its stored filename so the
    # current tokenizer rules (extension stripping, junk removal) apply to
    # already-indexed documents WITHOUT a Telegram backfill. Legacy behavior
    # (no flag) only fills missing token groups.
    if filename and tokenize_filename and (refresh_tokens or not title_tokens):
        try:
            tk = tokenize_filename(filename)
            if refresh_tokens or not title_tokens:
                title_tokens = tk.get("title_tokens", []) or title_tokens
            if refresh_tokens or not quality_tokens:
                quality_tokens = tk.get("quality_tokens", []) or quality_tokens
            if refresh_tokens or not codec_tokens:
                codec_tokens = tk.get("codec_tokens", []) or codec_tokens
            if (refresh_tokens or not doc.get("year")) and tk.get("year"):
                doc["year"] = tk.get("year")
        except Exception:
            pass

    # normalized filename (dedupe key) + full-title phrase string used by
    # Atlas Search phrase queries
    try:
        if title_tokens:
            norm_filename = " ".join([str(t).lower() for t in title_tokens if t])
        else:
            # normalized words, not a bare lower(): strip the extension and
            # treat separators/punctuation/script variants as separators.
            norm_filename = _norm_from_filename(filename) or doc.get("norm_filename")
    except Exception:
        norm_filename = doc.get("norm_filename") or ""
    title_phrase = norm_filename

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
        "norm_filename": norm_filename,
        "title_phrase": title_phrase,
    }
    return update


def _redact_uri(uri: str) -> str:
    """Drop the password before a connection string reaches a terminal/log."""
    try:
        if "://" not in uri:
            return uri
        scheme, rest = uri.split("://", 1)
        if "@" not in rest:
            return uri
        creds, host = rest.rsplit("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    except Exception:
        return "<mongo uri>"


def _coerce_id(raw):
    """Accept a 24-hex ObjectId (or any scalar _id) from --start-after."""
    if raw is None:
        return None
    s = str(raw).strip().strip('"').strip("'")
    if not s:
        return None
    if ObjectId.is_valid(s):
        return ObjectId(s)
    return s


def main():
    parser = argparse.ArgumentParser(
        description="Reindex files collection: recompute search_text & trigrams"
    )
    parser.add_argument("--batch-size", type=int, default=500, help="Bulk batch size")
    parser.add_argument(
        "--dry-run", action="store_true", help="Don't write; just preview updates"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Limit documents processed (0 = all)"
    )
    parser.add_argument(
        "--skip-trigrams",
        action="store_true",
        help="Skip docs that already have trigrams (ignored with --refresh-tokens)",
    )
    parser.add_argument(
        "--refresh-tokens",
        action="store_true",
        help="Re-tokenize every doc from its stored filename (no Telegram backfill needed)",
    )
    parser.add_argument(
        "--start-after",
        default=None,
        help="Resume: skip docs up to and including this _id (printed by a previous run)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Database name override (default: DB_NAME env var, then settings)",
    )

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

    if args.db:
        DB_NAME = args.db

    if not MONGO_URI:
        print("ERROR: MONGO_URI not set in env and could not import settings.")
        return 2

    ms = MongoService(
        MONGO_URI,
        DB_NAME,
        connect_timeout_ms=(settings.MONGO_CONNECT_TIMEOUT_MS if settings else 10000),
    )
    print(f"Connecting to Mongo: {_redact_uri(MONGO_URI)} (db={DB_NAME})")
    ms.connect()
    print("Connected; ensuring indexes...")
    try:
        ms.ensure_indexes()
    except Exception:
        pass

    col = ms.db.get_collection("files")

    start_after = _coerce_id(args.start_after)
    query = {"_id": {"$gt": start_after}} if start_after is not None else {}
    total_docs = col.count_documents(query)
    if args.limit and args.limit > 0:
        total_docs = min(total_docs, args.limit)
    fetch_size = max(args.batch_size, 200)

    if total_docs == 0 and start_after is None:
        # A zero-doc run almost always means the wrong database: DB_NAME from the
        # shell can point somewhere other than the DB the deployed bot writes to.
        print(f"No documents in {ms.db.name}.files -- check DB_NAME or pass --db.")
        try:
            for name in sorted(ms.client.list_database_names()):
                if name in ("admin", "local", "config"):
                    continue
                try:
                    n = (
                        ms.client[name]
                        .get_collection("files")
                        .estimated_document_count()
                    )
                except Exception:
                    continue
                if n:
                    print(f"  {name}.files -> {n} docs")
            print("Re-run with --db <name-of-the-database-that-has-your-files>")
        except Exception as exc:
            print(f"  (could not list databases: {exc})")

    # MongoDB closes cursors that stay idle for ~10 minutes, and a getMore that
    # fails mid-flight (Atlas network blip / retryable-read failover) kills the
    # cursor outright. That is the CursorNotFound that stopped a 26.6k-doc run at
    # 15k docs. Page by _id keyset instead: every page is a fresh find(), so no
    # server-side cursor can time out, and --start-after resumes a partial run.
    tail = {"_id": start_after}

    def _fetch_page(after_id):
        q = dict(query)
        if after_id is not None:
            q["_id"] = {"$gt": after_id}
        for attempt in range(1, 4):
            try:
                return list(col.find(q).sort("_id", 1).limit(fetch_size))
            except (AutoReconnect, CursorNotFound) as exc:
                print(
                    f"Fetch failed ({type(exc).__name__}) after _id={after_id}; "
                    f"retry {attempt}/3"
                )
                time.sleep(2.0 * attempt)
        raise SystemExit(
            f"Mongo kept dropping the cursor; rerun with --start-after {tail['_id']}"
        )

    def _iter_docs():
        after_id = start_after
        emitted = 0
        while True:
            page = _fetch_page(after_id)
            if not page:
                return
            for doc in page:
                after_id = doc.get("_id")
                tail["_id"] = after_id
                emitted += 1
                yield doc
                if args.limit and emitted >= args.limit:
                    return

    print(f"Reindexing up to {total_docs} documents (batch={args.batch_size})")

    bulk_ops = []
    processed = 0
    updated = 0
    start = time.time()

    for doc in _iter_docs():
        processed += 1
        if args.skip_trigrams and not args.refresh_tokens and doc.get("trigrams"):
            # skip docs that already have trigrams
            continue
        upd = build_update_for_doc(doc, refresh_tokens=args.refresh_tokens)
        if args.dry_run:
            print(
                f"[DRY] _id={doc.get('_id')} -> search_text_len={len(upd.get('search_text', ''))} trigrams={len(upd.get('trigrams', []))}"
            )
        else:
            bulk_ops.append(
                UpdateOne({"_id": doc.get("_id")}, {"$set": upd}, upsert=False)
            )
        if not args.dry_run and len(bulk_ops) >= args.batch_size:
            try:
                res = col.bulk_write(bulk_ops, ordered=False)
                updated += res.modified_count + res.upserted_count
            except Exception as exc:
                print("Bulk write failed:", exc)
            bulk_ops = []
        if processed % 500 == 0:
            elapsed = time.time() - start
            print(
                f"Processed {processed}/{total_docs} docs, updated {updated}, "
                f"elapsed {elapsed:.1f}s (last _id={tail['_id']})"
            )

    # flush remaining
    if not args.dry_run and bulk_ops:
        try:
            res = col.bulk_write(bulk_ops, ordered=False)
            updated += res.modified_count + res.upserted_count
        except Exception as exc:
            print("Final bulk write failed:", exc)

    elapsed = time.time() - start
    print(f"Done. Processed={processed} Updated={updated} Elapsed={elapsed:.1f}s")
    print(f"Last _id processed: {tail['_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
