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
