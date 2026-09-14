# toolz-downloadz-api

Fast, minimal media-extraction API for **TikTok** and **Instagram** — built for Vercel serverless (Python + FastAPI + yt-dlp).

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Ffreroxx%2Ftoolz-downloadz-api&env=API_SECRET_KEY,INSTAGRAM_COOKIES)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)](https://fastapi.tiangolo.com/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

> Companion frontend: [`freroxx/toolz-downloadz`](https://github.com/freroxx/toolz-downloadz)

---

## ✨ Features

- 🎵 **TikTok** — HD no-watermark downloads via [tikwm](https://www.tikwm.com/) (primary, ~1s, IP-free CDN) with yt-dlp fallback + oEmbed graceful degradation
- 📸 **Instagram** Reels & posts — yt-dlp extraction with optional login-cookie support for server IPs
- 🔑 **API-key auth** (`X-API-KEY` / `Authorization: Bearer` / `?key=`)
- ⚡ **Zero-infra caching** (in-memory TTL) + **per-IP rate limiting** — no Redis needed
- 🛡️ **SSRF protection** — private/internal hosts rejected
- 📦 **Same-instance downloads** — `/api/download` streams media from the lambda that extracted it, so signed CDN URLs stay valid (no 403s from a separate proxy)
- 🧩 **Single-file app** (`api/index.py`) — no Vercel Python package-detection surprises
- 🧪 **Tested** — `pytest` smoke suite included

## 🚫 Non-goals

- No YouTube support (removed in v4 — TikTok + Instagram only, on purpose: smaller surface, fewer blocks, easier to self-host)
- No transcoding / merging (Vercel has no ffmpeg) — direct CDN URLs only
- No playlist / bulk downloads — one URL at a time

---

## 🏗️ Architecture

```
Browser / Frontend ──▶ /api/extract?url=… ──▶ extract_sync()
                                                    ├── TikTok ──▶ tikwm API (HD, no watermark)
                                                    │              └─▶ fallback: yt-dlp ──▶ fallback: oEmbed (blocked:true)
                                                    └── Instagram ─▶ yt-dlp (+ INSTAGRAM_COOKIES)
                                                          │
                                                          ▼
                                              ┌─────────────────────┐
                                              │  shape() → JSON     │
                                              │  title, thumbnail,  │
                                              │  stats, formats[],  │
                                              │  download_url       │
                                              └─────────────────────┘
                                                          │
Browser ◀── 307 / stream ── /api/download?u=…&f=best ◀──┘ (same lambda re-resolves + streams)
```

---

## 🚀 Deploy (Vercel — recommended)

1. Push/fork this repo to GitHub.
2. **Vercel → Add New Project → Import** this repo.
3. Framework Preset: **Other**. No build command needed.
4. Add environment variables (see table below) → **Deploy**.
5. Verify: `GET https://<your-api>.vercel.app/api/health` → `{"status":"online",…}`.

> `vercel.json` already sets `maxDuration: 60` and rewrites everything to `api/index.py`.

### Environment variables

Set in **Vercel Dashboard → Settings → Environment Variables**:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `API_SECRET_KEY` | ✅ | — | Auth key. Generate: `openssl rand -hex 32`. Must match the frontend's `API_SECRET_KEY`. |
| `INSTAGRAM_COOKIES` | for IG | — | Full Netscape `cookies.txt` from a logged-in instagram.com browser. Without it most Reels 400 on server IPs. TikTok needs nothing. |
| `EXTRACT_TIMEOUT` | — | `26` | Seconds before extraction gives up (fits `maxDuration: 60`). |
| `CACHE_TTL` | — | `3600` | Seconds to cache successful extractions. |
| `RATE_LIMIT` | — | `30` | Requests/min per IP. |

`.env.example` documents the same list for local dev.

### Instagram cookies — how to export

1. Log in to **instagram.com** in Chrome/Edge/Firefox.
2. Install **“Get cookies.txt LOCALLY”** (Chrome Web Store / Firefox Add-ons).
3. With the instagram.com tab active → export → copy the **entire file content**.
4. Paste it as the `INSTAGRAM_COOKIES` env var (Vercel) or into `.env` locally.
5. Redeploy (or restart `uvicorn`). No cookies → Instagram returns a clean `400` telling you exactly this.

> Cookies are your login session — **never commit them**, never log them, never expose them. The API only ever reads them from env into `/tmp` for yt-dlp.

---

## 💻 Local development

```bash
pip install -r requirements.txt
cp .env.example .env   # set API_SECRET_KEY (+ INSTAGRAM_COOKIES for Reels)
uvicorn api.index:app --reload   # http://localhost:8000/docs
```

Interactive docs (Swagger): `http://localhost:8000/docs`

Run tests:

```bash
PYTHONPATH=. pytest -q
```

---

## 🔌 API reference

Base URL: `https://<your-api>.vercel.app`

**Auth** — send your key one of three ways:

```
X-API-KEY: <API_SECRET_KEY>
Authorization: Bearer <API_SECRET_KEY>
GET /api/extract?url=…&key=<API_SECRET_KEY>   # handy for browser navigation / downloads
```

If `API_SECRET_KEY` is unset, auth is skipped (local dev mode).

### `GET /api/health`

```json
{
  "status": "online",
  "service": "toolz-downloadz-api",
  "version": "4.0.0",
  "platforms": ["tiktok", "instagram"],
  "auth": true,
  "cookies": { "instagram": true }
}
```

### `GET /api/platforms`

```json
{
  "platforms": [
    { "id": "tiktok", "name": "TikTok", "color": "#000000" },
    { "id": "instagram", "name": "Instagram", "color": "gradient" }
  ]
}
```

### `GET /api/extract?url=…&audio_only=false&format=`

| Param | Type | Description |
|---|---|---|
| `url` | string (required) | TikTok or Instagram URL |
| `audio_only` | bool | Prefer audio-only formats |
| `format` | string | Custom yt-dlp format selector (advanced) |

`POST /api/extract` accepts the same as JSON: `{"url": "…", "audio_only": false, "format": null}`.

**Success response (200):**

```json
{
  "platform": "tiktok",
  "title": "…",
  "thumbnail": "https://…",
  "duration": 12,
  "uploader": "…",
  "uploader_url": "https://www.tiktok.com/@…",
  "stats": { "view_count": 123, "like_count": 45, "comment_count": 6 },
  "upload_date": null,
  "description": null,
  "download_url": "https://…mp4",
  "download_headers": { "User-Agent": "…" },
  "download_cookies": null,
  "ext": "mp4",
  "blocked": false,
  "formats": {
    "video": [{ "format_id": "tikwm_hd", "ext": "mp4", "resolution": "1080p no-watermark", "url": "https://…", "filesize": null, "vcodec": "avc1", "acodec": "mp4a", "height": 1920, "tbr": null, "abr": null }],
    "audio": [{ "format_id": "tikwm_music", "ext": "mp3", "resolution": "audio", "url": "https://…", "filesize": null }]
  },
  "original_url": "https://www.tiktok.com/…",
  "source": "tikwm"
}
```

**Graceful degradation** — TikTok IP-blocked but oEmbed reachable (still `200`, `blocked: true`):

```json
{
  "platform": "tiktok",
  "blocked": true,
  "blocked_message": "TikTok extraction failed on this server IP. …",
  "title": "…", "thumbnail": "https://…",
  "formats": { "video": [], "audio": [] }
}
```

**Errors:**

| Status | Meaning |
|---|---|
| `400` | Unsupported URL (`Only TikTok and Instagram are supported`), SSRF-blocked host, or Instagram failure (includes `INSTAGRAM_COOKIES` setup hint) |
| `401` | Missing/invalid API key |
| `409` | `/api/download` on a blocked extraction |
| `429` | Rate limit exceeded |
| `502` | Media CDN refused the fetch |
| `504` | Extraction timed out — retry (cache makes retries faster) |

### `GET /api/download?u=…&f=best&n=name.mp4`

One-step download. Re-resolves (cache-first) **on the same instance** and streams the bytes with `Content-Disposition: attachment`.

| Param | Description |
|---|---|
| `u` | Original page URL (required) |
| `f` | `format_id` from `/api/extract`, or `best` (default) |
| `n` | Download filename |
| `key` | API key (query form, so plain browser navigation works) |

Supports `Range` requests (seekable video).

---

## 📁 Project structure

```
toolz-downloadz-api/
├── api/
│   └── index.py        # the entire app (routes, extraction, download, cache, auth)
├── tests/
│   └── test_api.py     # pytest smoke suite (health, auth, SSRF, TT/IG, cache)
├── .env.example        # documented env template (no secrets)
├── requirements.txt    # fastapi + uvicorn + yt-dlp + python-dotenv
├── vercel.json         # maxDuration + rewrites → api/index.py
└── LICENSE             # GPL-3.0
```

---

## 🧪 Testing

```bash
PYTHONPATH=. pytest -q
```

Covers: health/platforms shape, platform allowlist (incl. YouTube/Vimeo rejected), auth gate, SSRF guard, TikTok live extraction (or graceful `blocked`), Instagram graceful error without cookies, cache-hit flag, POST body, `audio_only` flag.

---

## 🤝 Contributing

1. Fork → branch (`feat/…` / `fix/…`).
2. `pip install -r requirements.txt` + `PYTHONPATH=. pytest -q`.
3. Keep `api/index.py` single-file and dependency-light (Vercel constraint).
4. Update `.env.example` + this README if you add env vars or endpoints.
5. Open a PR describing behavior + test evidence.

---

## ⚖️ Legal & fair use

- Only download content **you own or have the right to save** (your own posts, Creative Commons, explicit permission).
- Respect TikTok's and Instagram's **Terms of Service** and creators' rights. This tool is for personal archiving/fair-use workflows — not for re-uploading others' work.
- This project is not affiliated with TikTok, Instagram/Meta, Vercel, or tikwm.
- Cookies are sensitive credentials: rotate them if ever exposed, scope test accounts where possible.

---

## 🙏 Acknowledgements

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — extraction backbone
- [tikwm](https://www.tikwm.com/) — fast no-watermark TikTok API
- [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) — server
- Frontend: [`freroxx/toolz-downloadz`](https://github.com/freroxx/toolz-downloadz) (Next.js)

---

## 📄 License

[GNU General Public License v3.0](LICENSE) — free to use, modify, and share under the same terms.
