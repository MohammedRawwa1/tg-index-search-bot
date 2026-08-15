# tg-file-index-bot

Telegram supergroup filename indexer + search (skeleton)

Start (local):

1. Create `.env` from `.env.example` and fill credentials.
2. Install: `pip install -r requirements.txt`
3. Run: `python -m app.main`

Render: use `render.yaml` as a Background Worker (start: `python -m app.main`).

Web API (optional):

The project provides an async FastAPI web service exposing `/search` and `/health` useful for monitoring or powering a tiny web UI.

Run locally:

```bash
pip install -r requirements.txt
uvicorn app.api:app --host 0.0.0.0 --port 8000
```

Docker:

```bash
docker build -t tg-file-index-bot .
docker run -e PORT=8000 -p 8000:8000 tg-file-index-bot
```
 
Server-side cache (optional)
---------------------------

This project includes a lightweight server-side search cache to speed up repeated queries.

- Enabled automatically when the FastAPI app connects to MongoDB. The app creates an
	`AsyncSearchCache` instance (in-memory LRU with optional Mongo persistence) at startup.
- Configuration (via environment or `settings`):
	- `SEARCH_CACHE_MAX_ENTRIES` — max in-memory entries (default 1024)
	- `SEARCH_CACHE_TTL` — per-entry TTL in seconds (default 3600)

Admin cache endpoints (protected by `DIAG_SECRET`)
 - POST `/_admin/cache/invalidate` — JSON body: `{"all":true}` to clear entire cache, or `{"query":"..."}` to invalidate by query, or `{"key":"<cache_key>"}` to delete a specific key.
 - GET `/_admin/cache/stats` — returns memory/mongo entry counts.

Backfill integration
 - When `backfill_history` runs in-process (via admin `/reindex` or admin panel), the app clears the in-process cache after a successful (non-dry-run) backfill.
 - When running `scripts/backfill.py` externally, set `API_URL` and `DIAG_SECRET` in the environment so the script can notify the API to invalidate the cache after it finishes.

Notes
 - Cache invalidation is conservative (clears entire cache on backfill by default) to ensure search results reflect newly indexed data.
 - The cache persists entries into Mongo if available; these documents are stored in the `search_cache` collection and are removed via a TTL index.

# tg-file-index-bot

Telegram supergroup filename indexer + search (skeleton)

Start (local):

1. Create `.env` from `.env.example` and fill credentials.
2. Install: `pip install -r requirements.txt`
3. Run: `python -m app.main`

Render: use `render.yaml` as a Background Worker (start: `python -m app.main`).

Web API (optional):

The project provides an async FastAPI web service exposing `/search` and `/health` useful for monitoring or powering a tiny web UI.

Run locally:

```bash
pip install -r requirements.txt
uvicorn app.api:app --host 0.0.0.0 --port 8000
```

Docker:

```bash
docker build -t tg-file-index-bot .
docker run -e PORT=8000 -p 8000:8000 tg-file-index-bot
```
Search relevance (tiered pipeline)
----------------------------------

Search is precision-first: results are ranked by a tier ladder and weak
matches are never surfaced just because they share a trigram or a regex.

    Tier   Condition                                    Priority
    -----  -------------------------------------------  --------
    exact  normalized filename/title equals the query    100
    phrase all query tokens as a contiguous phrase        90
    all    every query token present                     80
    most   at least half the query tokens present        60
    prefix token prefix / autocomplete                   40
    typo   1-edit trigram tolerance                      20
    broad  regex fallback (opt-in only)                   5

- The normal path starts at `most` and only auto-broadens to `prefix` then
  `typo` when a tier returns fewer than `SEARCH_MIN_RESULTS` results.
- The permissive regex/`$text` fallback is NOT part of the normal path; it
  is only reachable via `allow_broad=True` (bot: `FileIndex.search_with_ranking(..., allow_broad=True)`; API: `?allow_broad=true`).
- The web API (`/search`) runs the same tier filter/scoring as the bot, and
  the Atlas Search compound keeps fuzzy matching off the precision clauses
  (single low-boost fuzzy clause on `search_text`; `minimumShouldMatch=1` so
  multi-word queries like "breaking bad season 3" still match).

Tuning (environment variables, no code changes):

    SEARCH_MIN_TIER       starting accepted tier (default "most")
    SEARCH_MIN_RESULTS    broaden below this many results (default 5)
    MOST_TOKENS_RATIO     fraction of tokens needed for "most" (default 0.5)
    TRIGRAM_MIN_SIM       minimum similarity for the "typo" tier (default 0.22)
    SEARCH_QUALITY_LOG    per-query quality logging (default true)
    ATLAS_SEARCH_FUZZY    enable the low-boost fuzzy clause (default true)
    RANK_*                fine-grained tie-breaker weights

Search quality: every query logs one line
(`search_quality query=... results=... top_score=... min_tier=... fuzzy_used=... broad_used=...`)
so relevance can be measured and tuned from real usage.

Note: the tokenizer now strips common media/document extensions
(mkv, mp4, epub, pdf, ...) from `title_tokens`. Existing indexed documents
were stored with extension tokens, so a full re-index (backfill) is
recommended to get exact-title matches and cleaner dedupe keys.

Atlas Search index
------------------

The web API uses Atlas Search only when the Mongo URI is an Atlas URI
(`mongodb+srv://` or `ENABLE_ATLAS_SEARCH=true`). The `$search` stages target
the **default** search index on the `files` collection (no `index` name is
passed in code), so the index must be created as the default one.

Apply `docs/atlas_search_index.json` as the index definition in Atlas
(Atlas UI → your cluster → Search → Create Index → paste the JSON body).
It maps exactly the fields the code queries with `lucene.standard` so the
`text`, `phrase` and `fuzzy` operators all behave as intended:

- `title_tokens`   — token array (text operator)
- `title_phrase`   — full-title string used by the **phrase** operator
  (phrase cannot match across array elements, hence the dedicated field)
- `search_text`    — aggregated text; hosts the low-boost fuzzy clause
- `filename`       — text operator
- `quality_tokens`, `codec_tokens` — token arrays
- `year`, `timestamp`, `message_thread_id` — numeric/date filters

If `title_phrase` is missing from existing documents, the phrase clause will
error and the API automatically falls back to the legacy candidate path —
so a re-index (backfill) is required to activate phrase boosting.
