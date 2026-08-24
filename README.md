# toolz-downloadz-api — Vercel Serverless Only

Lightweight, production-ready FastAPI backend for Vercel Functions. Powering cross-platform media extraction for the Toolz ecosystem using `yt-dlp`.

> **Vercel-only mode**: This API is designed to run *exclusively* on Vercel (no Docker, no VPS). All infra — cache, rate-limit, POT bypass — is adapted for serverless.

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fyour-username%2Ftoolz-downloadz-api&env=API_SECRET_KEY)

## Features (v2.0)

- **Vercel Serverless Native**: `vercel.json` `functions.maxDuration=30`, `/tmp` cookie write, `memory + Upstash Redis REST` cache, `asyncio.wait_for` timeout guard.
- **FastAPI + OpenAPI**: Auto docs at `/docs`, strict `pydantic` validation, `X-API-KEY` *or* `Authorization: Bearer`.
- **Secure**: SSRF block (private IPs), CORS via `CORS_ORIGINS`, `slowapi` rate-limit (`30/min` extract).
- **Resilient on Vercel**: YouTube bot-block handling (see below), never-blocked oEmbed fallback, optional `YOUTUBE_COOKIES` bypass.
- **Production Observability**: Request logging with `X-Request-ID`, `/api/health`, `X-Cache` headers, optional Sentry.

## Platform Support & Vercel Resilience

Vercel = datacenter IP → YouTube often returns *Sign in to confirm you're not a bot*. This API handles it in **Vercel-optimized** 3 layers:

1. **Client rotation (Vercel-tuned)** – Tries `android,mweb` then `ios` only (2 strategies, not 4) to stay inside Vercel function timeout. `api/services/extractor.py:213`
2. **oEmbed fallback** – If blocked, returns YouTube's public oEmbed metadata (`title/thumbnail/uploader`) which is *never* blocked. Client sees `blocked: true` card with `Open on source`. `api/services/extractor.py:147`
3. **Cookie auth (recommended on Vercel)** – Set `YOUTUBE_COOKIES` (Netscape `cookies.txt` content exported from your browser) in **Vercel Env**. This is the *only* reliable Vercel fix; datacenter blocks disappear. `api/services/extractor.py:27` writes to `/tmp/yt_cookies.txt` (writable on Vercel).
4. **External POT provider (optional)** – Vercel cannot run `bgutil` alongside Python (needs Node). If you host `brainicism/bgutil-ytdlp-pot-provider` elsewhere, set `YT_DLP_POT_PROVIDER_URL=https://pot.yourdomain.com` to enable real PO token bypass externally.

**Vercel Timeout Guard**: `EXTRACT_TIMEOUT` capped at `9s` on Vercel hobby to avoid `504`. `api/routers/extract.py:13` uses `asyncio.wait_for`.

**Cache on Vercel**: In-memory per-lambda + optional **Upstash Redis REST / Vercel KV** for cross-lambda persistence. `api/core/cache.py:1`
- Create Vercel Storage → KV (Upstash) → Connect to project → auto-sets `KV_REST_API_URL` + `KV_REST_API_TOKEN` (or `UPSTASH_REDIS_REST_URL`).
- If not set, cache gracefully degrades to memory.

## Setup — Vercel Deploy

1. Push to GitHub.
2. Import project in [Vercel Dashboard](https://vercel.com/new) — auto-detects `vercel.json`.
3. **Environment Variables** (Settings → Environment Variables):
   - `API_SECRET_KEY` (required, `openssl rand -hex 32`)
   - `YOUTUBE_COOKIES` (recommended — paste full `cookies.txt`)
   - `UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN` *or* `KV_*` (auto-set if you added Vercel KV)
   - `CORS_ORIGINS` (e.g. `https://toolz-downloadz.vercel.app`)
4. Deploy. Vercel routes `/api/(.*)` → `api/index.py`. No Docker needed.

### Local Dev (still runs via Vercel's Python runtime, but you can also run locally)

```bash
pip install -r requirements.txt --break-system-packages
cp .env.example .env  # set API_SECRET_KEY
python api/index.py  # or uvicorn api.main:app --reload
# http://localhost:8000/docs
```

## API Documentation

Swagger UI: `http://localhost:8000/docs` (or `https://your-api.vercel.app/docs`)

### `GET /api/extract`

**Headers:** `X-API-KEY: <secret>` or `Authorization: Bearer <secret>`

**Query:** `url` (required), `format` (yt-dlp selector), `audio_only` (bool), `playlist` (bool), `subtitles` (bool)

**Example:**
```bash
curl "https://your-api.vercel.app/api/extract?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ" -H "X-API-KEY: $API_SECRET_KEY"
```

**Response:**
```json
{
  "platform": "youtube",
  "title": "Video Title",
  "thumbnail": "https://...",
  "duration": 120,
  "uploader": "Channel",
  "download_url": "https://...",
  "ext": "mp4",
  "blocked": false,
  "formats": {"video": [...], "audio": [...]},
  "subtitles": {...},
  "playlist_entries": [...]
}
```
If YouTube blocked on Vercel IP: `blocked:true` + `blocked_message` + oEmbed `title/thumbnail` — set `YOUTUBE_COOKIES` to fix.

Other endpoints: `GET /api/platforms`, `GET /api/detect?url=`, `POST /api/extract` (JSON body), `GET /api/health`.

## Web Tester

The companion `toolz-downloadz` Next.js app is a full tester harness: Tester panel exposes `audio_only/playlist/subtitles/format`, `curl` preview, `/api/health` badge, raw JSON. Set `API_URL` to your Vercel API URL in `toolz-downloadz/.env`.

## License

GNU GPLv3 — see LICENSE.
