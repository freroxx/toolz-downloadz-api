# toolz-downloadz-api v3

Fast, minimal media-extraction API for **YouTube · TikTok · Instagram Reels**. Built for Vercel serverless.

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Ffreroxx%2Ftoolz-downloadz-api&env=API_SECRET_KEY)

## Stack (modern & minimal)

- **FastAPI** + **yt-dlp** (+ `pytubefix` as an independent YouTube fallback engine)
- Single-file app (`api/index.py`) — zero package-detection issues on Vercel
- In-memory cache + rate limiting built in (no Redis needed)
- Graceful degradation: YouTube/TikTok oEmbed fallbacks return metadata (`blocked:true`) instead of hard errors

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/health` | Status + which cookies are configured |
| `GET /api/platforms` | Supported platforms |
| `GET /api/extract?url=...&audio_only=&format=` | Extract media |
| `POST /api/extract` | Same, JSON body `{"url": "...", "audio_only": false}` |

Auth: send your key via `X-API-KEY: <key>` header (or `Authorization: Bearer <key>`).

## Environment variables (Vercel Dashboard → Settings → Environment Variables)

| Variable | Required | Purpose |
|---|---|---|
| `API_SECRET_KEY` | ✅ | Auth key. Generate: `openssl rand -hex 32` |
| `YOUTUBE_COOKIES` | recommended | Netscape `cookies.txt` content from a logged-in YouTube browser. Fixes "not a bot" blocks on Vercel IPs. |
| `INSTAGRAM_COOKIES` | for Reels | Same format, from a logged-in Instagram browser. Without it most Reels fail. |
| `EXTRACT_TIMEOUT` | optional | Seconds before giving up (default 8 — hobby-plan safe) |
| `CACHE_TTL`, `RATE_LIMIT` | optional | Cache seconds (3600), requests/min per IP (30) |

## Cookie lifecycle (YouTube on Vercel)

Vercel IPs get bot-walled; fresh login cookies are the reliable fix.

1. **Diagnose** — `GET /api/diag` (with API key) → cookie verdict, days-to-rotation, POT health
2. **Refresh without redeploy** — export fresh `cookies.txt` (Get cookies.txt LOCALLY, logged in) then:
   ```bash
   curl -X POST "https://<api>.vercel.app/api/admin/cookies" \
        -H "X-API-KEY: $KEY" -H "Content-Type: text/plain" \
        --data-binary @cookies.txt
   ```
   Requires Vercel KV connected (Storage → Create KV → connect project). Propagates to all lambdas ≤60s. Invalid/logged-out exports are rejected with the reason.
3. **Smart gating** — expired/invalid cookies are never sent (they make blocks worse); `/api/diag` and the blocked card say exactly what's wrong.

### Keep toolz-pot warm (cron-job.org, free)
1. cron-job.org → Create cronjob → URL: `https://toolz-pot.vercel.app/ping`, every 5 min.
   BotGuard stays initialized → token minting stays fast; also keeps yt-dlp's 5s provider-ping gate happy.

## High-quality YouTube ladder (open-source cobalt)

Vercel IPs are bot-walled by YouTube. The fix: run **your own** instance of the
open-source [cobalt](https://github.com/imputnet/cobalt) API on a free host and point this API at it:

```
COBALT_API_URL=https://your-cobalt.example.com
```

Full click-by-click guide (Render/Koyeb, free): [`deploy/COBALT_FREE_HOSTING.md`](deploy/COBALT_FREE_HOSTING.md).
Once set, every YouTube extraction exposes 360p→4K + MP3 via your own instance.

## Local dev

```bash
pip install -r requirements.txt
cp .env.example .env   # set API_SECRET_KEY
uvicorn api.index:app --reload   # http://localhost:8000/docs
pytest  # tests
```

## Deploy (Vercel only)

1. Push this repo to GitHub.
2. Vercel → Add New Project → import.
3. Framework Preset: **Other**. No build settings needed.
4. Add env vars above → Deploy.

See the web companion repo (`toolz-downloadz`) for the frontend tester.
