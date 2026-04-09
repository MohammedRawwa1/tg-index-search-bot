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