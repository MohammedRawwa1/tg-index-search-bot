#!/usr/bin/env python3
"""Interactive search tuning helper.

Run this script locally, enter queries, mark results as relevant/irrelevant,
then get suggested adjustments for `RANK_*` weights.

Usage:
  python scripts/tune_search.py

Keyboard commands while prompted:
 - Enter a query (empty to finish and compute suggestions)
 - After results displayed, enter space-separated indexes flagged as relevant (e.g. "1 3 4").
 - Leave blank to mark no relevant results for that query.
"""

import sys
from pathlib import Path
repo_root = Path(__file__).resolve().parents[1]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

import os
import argparse
import json
import httpx
from typing import List, Dict, Any

try:
    from app.config.settings import settings
    from app.services.mongo import MongoService
    from app.services.tokenizer import tokenize_query
    from app.services.search_utils import make_trigrams, trigram_similarity, TRIGRAM_MAX
    from app.models.file_index_impl import FileIndex, TITLE_WEIGHT, QUALITY_WEIGHT, CODEC_WEIGHT, TRIGRAM_WEIGHT, FILENAME_MATCH
except Exception:
    print("Failed to import project modules. Ensure you run this script from the project root and have the venv active.")
    raise


def doc_features(doc: Dict[str, Any], tokens: List[str], q_tris: List[str]) -> Dict[str, float]:
    doc_titles = [t.lower() for t in (doc.get("title_tokens") or [])]
    doc_quality = [t.lower() for t in (doc.get("quality_tokens") or [])]
    doc_codec = [t.lower() for t in (doc.get("codec_tokens") or [])]

    title_exact = float(len(set(tokens) & set(doc_titles)))
    quality_exact = float(len(set(tokens) & set(doc_quality)))
    codec_exact = float(len(set(tokens) & set(doc_codec)))
    tri_sim = float(trigram_similarity(q_tris, doc.get("trigrams", []) or []))
    filename = (doc.get("filename") or "").lower()
    fname_match = float(any(t in filename for t in tokens))

    return {
        "title_matches": title_exact,
        "quality_matches": quality_exact,
        "codec_matches": codec_exact,
        "trigram_sim": tri_sim,
        "filename_match": fname_match,
    }


def aggregate_feature_means(items: List[Dict[str, float]]) -> Dict[str, float]:
    if not items:
        return {}
    sums = {}
    for it in items:
        for k, v in it.items():
            sums[k] = sums.get(k, 0.0) + float(v)
    n = float(len(items))
    return {k: (v / n) for k, v in sums.items()}


def suggest_weights(base: Dict[str, float], pos_means: Dict[str, float], neg_means: Dict[str, float]) -> Dict[str, float]:
    # For each feature, compute a safe multiplier = (pos_mean + eps) / (neg_mean + eps)
    eps = 1e-4
    suggestions = {}
    for feat, base_w in base.items():
        p = pos_means.get(feat, 0.0)
        n = neg_means.get(feat, 0.0)
        ratio = (p + eps) / (n + eps)
        # clamp multiplier to avoid extreme jumps
        multiplier = max(0.2, min(ratio, 5.0))
        suggestions[feat] = float(base_w) * multiplier
    return suggestions


def main():
    parser = argparse.ArgumentParser(description="Interactive search tuning tool")
    parser.add_argument("--save", type=str, default=None, help="Save suggestions to a JSON file")
    parser.add_argument("--per-page", type=int, default=8, help="Results per query display")
    parser.add_argument("--use-stream", action="store_true", help="Use remote NDJSON streaming endpoint instead of local DB")
    parser.add_argument("--stream-url", type=str, default=os.getenv("SEARCH_STREAM_URL", "http://127.0.0.1:10000/search/stream"), help="Streaming endpoint URL (NDJSON)")
    args = parser.parse_args()

    MONGO_URI = os.getenv("MONGO_URI")
    DB_NAME = os.getenv("DB_NAME")
    if not MONGO_URI or not DB_NAME:
        MONGO_URI = MONGO_URI or (settings.MONGO_URI if settings else None)
        DB_NAME = DB_NAME or (settings.DB_NAME if settings else None)

    # If using remote streaming endpoint, we do not require local Mongo.
    stream_url = args.stream_url or os.getenv("SEARCH_STREAM_URL", "http://127.0.0.1:10000/search/stream")
    if args.use_stream:
        ms = None
        fi = None
        print(f"Using remote stream: {stream_url}")
    else:
        if not MONGO_URI:
            print("ERROR: MONGO_URI not configured")
            return 2
        ms = MongoService(MONGO_URI, DB_NAME, connect_timeout_ms=(settings.MONGO_CONNECT_TIMEOUT_MS if settings else 10000))
        ms.connect()
        fi = FileIndex(ms.db)

    print("Interactive tuning: enter queries and mark relevant results. Empty query to finish.")

    positives = []
    negatives = []

    while True:
        try:
            q = input("Query (empty to finish): ").strip()
        except EOFError:
            break
        if not q:
            break
        tokens = tokenize_query(q)
        q_tris = make_trigrams(q or "", TRIGRAM_MAX)
        rows = []
        if args.use_stream:
            # consume NDJSON stream and collect up to per_page results
            try:
                params = {"q": q, "per_batch": str(args.per_page)}
                with httpx.stream("GET", stream_url, params=params, timeout=None) as resp:
                    if resp.status_code != 200:
                        print(f"Stream request failed: {resp.status_code}")
                    else:
                        for line in resp.iter_lines():
                            if not line:
                                continue
                            try:
                                doc = json.loads(line)
                            except Exception:
                                continue
                            rows.append(doc)
                            if len(rows) >= args.per_page:
                                break
            except Exception as exc:
                print("Stream error:", exc)
                rows = []
        else:
            res = fi.search_with_ranking(tokens=tokens, query=q, page=1, per_page=args.per_page)
            rows = res.get("results", [])
        if not rows:
            print("No results for this query.")
            continue
        print("Results:")
        for idx, doc in enumerate(rows, start=1):
            fname = doc.get("filename") or "<no-filename>"
            score = doc.get("_score", 0.0)
            short = fname[:80]
            print(f" {idx}. {short} | score={score:.3f} chat={doc.get('chat_id')} msg={doc.get('message_id')}")
        # prompt for positive indices
        s = input("Enter space-separated indexes considered relevant (e.g. '1 3') or blank: ").strip()
        pos_idxs = []
        if s:
            try:
                pos_idxs = [int(x) - 1 for x in s.split() if x.strip().isdigit()]
            except Exception:
                pos_idxs = []
        # mark labeled docs
        for i, doc in enumerate(rows):
            feats = doc_features(doc, tokens, q_tris)
            if i in pos_idxs:
                positives.append(feats)
            else:
                negatives.append(feats)

        print(f"Recorded {len(pos_idxs)} positives, {len(rows)-len(pos_idxs)} negatives for this query")

    # compute aggregate means
    pos_means = aggregate_feature_means(positives)
    neg_means = aggregate_feature_means(negatives)

    base = {
        "title_matches": fi.TITLE_WEIGHT,
        "quality_matches": fi.QUALITY_WEIGHT,
        "codec_matches": fi.CODEC_WEIGHT,
        "trigram_sim": fi.TRIGRAM_WEIGHT,
        "filename_match": fi.FILENAME_MATCH,
    }

    if not pos_means:
        print("No positive labels collected; cannot suggest weights.")
        return 0

    suggestions = suggest_weights(base, pos_means, neg_means)

    print("\nBase weights:")
    print(json.dumps(base, indent=2))
    print("\nObserved means (positives):")
    print(json.dumps(pos_means, indent=2))
    print("\nObserved means (negatives):")
    print(json.dumps(neg_means, indent=2))

    print("\nSuggested new weights (apply as RANK_* env vars):")
    print(json.dumps(suggestions, indent=2))

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump({"base": base, "pos_means": pos_means, "neg_means": neg_means, "suggestions": suggestions}, f, indent=2)
        print(f"Suggestions saved to {args.save}")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())