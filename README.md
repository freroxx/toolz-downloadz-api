# toolz-downloadz-api

Media-extraction API for public TikTok and Instagram media. FastAPI, built for Vercel serverless.

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Ffreroxx%2Ftoolz-downloadz-api&env=API_SECRET_KEY,INSTAGRAM_COOKIES)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Companion frontend: [freroxx/toolz-downloadz](https://github.com/freroxx/toolz-downloadz)

## How it works

```
GET /api/extract?url=…
  TikTok    → tikwm API (no-watermark, IP-free CDN)
              → fallback: yt-dlp → fallback: oEmbed (blocked: true)
  Instagram → yt-dlp (+ INSTAGRAM_COOKIES)

GET /api/download?u=…&f=best
  Re-resolves on the same instance and streams the bytes.
  Same-instance matters: TikTok signs media URLs to the extracting
  server's IP, so a separate proxy gets 403.
```

The v1 API supports individual gallery images as ordered assets, as well as native source video and audio formats. It does not transcode or mux on Vercel.

## v1 client contract

New Android and web clients must use v1. It never exposes upstream media URLs,
cookies, or a project API secret:

1. `POST /api/v1/client-sessions` with an opaque, stable installation ID.
2. `POST /api/v1/extractions` with `Authorization: Bearer <guest token>` and a public URL.
3. Select an opaque `assets[].id` and download it at its returned `download_path` with the same bearer token.

Guest tokens and extraction handles are short-lived. In production set
`UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`; in-memory state is intended only for local development. `/api/extract` and `/api/download` remain legacy compatibility routes during client rollout.

Tikwm reports byte counts and duration but no dimensions or codecs, so TikTok quality labels are approximate and carry a `~` prefix. Anything unobserved is `null`, never guessed.

## Deploy

1. Import this repo in Vercel (Framework Preset: Other, no build command).
2. Set the env vars below, deploy.
3. Check `GET https://<your-api>.vercel.app/api/health`.

`vercel.json` sets `maxDuration: 60` and routes everything to `api/index.py`, which is the entire app in one file (Vercel's Python builder is unreliable with packages under `api/`).

### Env vars

| Variable | Required | Default | Notes |
|---|---|---|---|
| `API_SECRET_KEY` | yes | — | `openssl rand -hex 32`. Must match the frontend. Unset = no auth (local dev only). |
| `YOUTUBE_COOKIES` | for YouTube | — | Full Netscape `cookies.txt` from a browser logged into a **throwaway** Google account (never your main). Same pattern as Instagram: export with "Get cookies.txt LOCALLY", paste whole file content, redeploy. Rotate when bot-wall errors return. |
| `YT_EXTRACT_URL` / `YTAPI_SECRET` | YouTube decipher fallback | — | Deployed `toolz-ytapi` URL + shared secret (must match on both sides). When yt-dlp finds zero usable formats (ciphered streams need JS deciphering), the API retries via youtubei.js. Unset = yt-dlp only. |
| `INSTAGRAM_COOKIES` | for IG | — | Full Netscape `cookies.txt` from a logged-in instagram.com browser. TikTok needs nothing. |
| `EXTRACT_TIMEOUT` | no | `26` | Seconds before extraction gives up. |
| `CACHE_TTL` | no | `3600` | Cache seconds for successful extractions (in-memory, per instance). |
| `RATE_LIMIT` | no | `30` | Requests per minute per IP (uses `X-Forwarded-For` behind Vercel). |
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | v1 production | — | Upstash Redis REST state for sessions and short-lived extraction handles. |

Instagram cookies: log in to instagram.com, export with "Get cookies.txt LOCALLY", paste the whole file content as the var, redeploy. Never commit cookies; the API only reads them into `/tmp` for yt-dlp.

## Local dev

```bash
pip install -r requirements.txt
cp .env.example .env   # set API_SECRET_KEY
uvicorn api.index:app --reload   # docs at http://localhost:8000/docs
PYTHONPATH=. pytest -q
```

## API reference

Auth via `X-API-KEY`, `Authorization: Bearer`, or `?key=` (the last one exists so browser-navigated downloads can authenticate).

`GET /api/health` returns status, version, platforms, and whether IG cookies are set.

`GET /api/platforms` returns `[{id: tiktok}, {id: instagram}]`.

`GET /api/extract?url=…&audio_only=false&format=` (or `POST /api/extract` with `{"url": …}`) returns:

```json
{
  "platform": "tiktok",
  "title": "…",
  "thumbnail": "https://…",
  "duration": 32,
  "uploader": "…",
  "stats": { "view_count": 1, "like_count": 2, "comment_count": 3 },
  "download_url": "https://…mp4",
  "ext": "mp4",
  "blocked": false,
  "formats": {
    "video": [{ "format_id": "tikwm_hd", "ext": "mp4", "resolution": "~1080p HD, no watermark", "url": "…", "filesize": 4561873, "has_audio": true }],
    "audio": [{ "format_id": "tikwm_music", "ext": "mp3", "resolution": "audio", "url": "…", "filesize": null }]
  },
  "original_url": "https://…"
}
```

With `audio_only=true`, the TikTok response serves the music track as `download_url` (ext `mp3`).

TikTok extracts always include the full quality ladder: exact yt-dlp rungs (dimensions, sizes, codecs, e.g. 540p / 720p H.264 / 720p HEVC / 1080p) merged into the fast tikwm result. Exact rows supersede the approximate ones, duplicates and the watermarked variant are dropped, and the default download stays the IP-free HD file. Rows carry `ladder: fast|full` and `ip_free` flags; video rows carry explicit `has_audio`. Only exact file sizes are ever reported. Default quality is 1080p wherever a 1080 rendition exists, highest available otherwise.

Status codes: `400` unsupported URL / SSRF-blocked host / slideshow / Instagram failure (includes the cookie setup hint); `401` bad key; `409` downloading a blocked extraction; `429` rate limited; `502` CDN refused; `504` timed out (retry, cache makes retries faster).

`GET /api/download?u=…&f=best&n=name.mp4` streams the file as an attachment. `f` is a `format_id` from extract (default `best`). Supports `Range`.

## Project structure

```
api/index.py       # the whole app: routes, extraction, download, cache, auth
tests/test_api.py  # pytest suite (offline unit tests + live smoke tests)
.env.example       # documented env template
requirements.txt   # fastapi, uvicorn, yt-dlp, python-dotenv
vercel.json        # maxDuration + rewrites
```

Contributions: fork, branch, `PYTHONPATH=. pytest -q`, PR with test evidence. Keep `api/index.py` single-file and dependency-light.

Legal: only download content you own or have the right to save. Respect TikTok/Instagram ToS and creators' rights. Not affiliated with TikTok, Meta, Vercel, or tikwm.

License: [GPL-3.0](LICENSE).
