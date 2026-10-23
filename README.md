<div align="center">

<img src="https://i.ibb.co/jvt7B2Cy/toolz-glass-icon-dark-bg.png" width="80" alt="Toolz Logo" />

# toolz-downloadz-api

**Media-extraction API for TikTok, Instagram & YouTube — FastAPI on Vercel Serverless**

[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![Vercel](https://img.shields.io/badge/Vercel-Serverless-000000?style=flat-square&logo=vercel&logoColor=white)](https://vercel.com)
[![License: GPL v3](https://img.shields.io/badge/License-GPL--3.0-4CAF50?style=flat-square&logo=gnu&logoColor=white)](LICENSE)

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Ffreroxx%2Ftoolz-downloadz-api&env=API_SECRET_KEY,INSTAGRAM_COOKIES)

<p align="center">
  <a href="https://discord.gg/aAswRUerwh">💬 Discord</a> &nbsp;·&nbsp;
  <a href="https://github.com/freroxx/toolz">📱 Main App</a> &nbsp;·&nbsp;
  <a href="https://github.com/freroxx/toolz-downloadz">🌐 Frontend</a> &nbsp;·&nbsp;
  <a href="CONTRIBUTING.md">🤝 Contributing</a>
</p>

</div>

---

## 🌐 Overview

`toolz-downloadz-api` is the backend media-extraction engine powering the [Toolz](https://github.com/freroxx/toolz) suite. It extracts no-watermark media from **TikTok**, **Instagram**, and **YouTube** using a layered fallback strategy, exposes a clean REST API with auth, rate limiting, and Redis-backed session state, and deploys as a single-file Vercel serverless function.

> **Part of the Toolz ecosystem** — a modern, open-source media downloader suite built with Material 3 Expressive design.

---

## 🏗️ Architecture

```
                        ┌─────────────────────────────────────────────────┐
                        │               toolz-downloadz-api                │
                        │              FastAPI · Vercel · Python            │
                        └───────────────────┬─────────────────────────────┘
                                            │
              ┌─────────────────────────────┼──────────────────────────────┐
              │                             │                              │
     ┌────────▼────────┐          ┌─────────▼────────┐          ┌─────────▼────────┐
     │    🎵 TikTok    │          │  📸 Instagram    │          │   ▶️  YouTube    │
     │                 │          │                  │          │                  │
     │ 1. tikwm API    │          │ 1. yt-dlp        │          │ 1. yt-dlp        │
     │    (no-wm CDN)  │          │    + cookies     │          │    + cookies     │
     │ 2. yt-dlp       │          │                  │          │ 2. cobalt engine │
     │    fallback     │          │                  │          │    fallback      │
     │                 │          │                  │          │ 3. youtubei.js   │
     │                 │          │                  │          │    (toolz-ytapi) │
     └────────┬────────┘          └─────────┬────────┘          └─────────┬────────┘
              │                             │                              │
              └─────────────────────────────┼──────────────────────────────┘
                                            │
                        ┌───────────────────▼─────────────────────────────┐
                        │          Unified Extraction Response             │
                        │    formats · assets · duration · thumbnail       │
                        └───────────────────┬─────────────────────────────┘
                                            │
              ┌─────────────────────────────┼──────────────────────────────┐
              │                             │                              │
     ┌────────▼────────┐          ┌─────────▼────────┐          ┌─────────▼────────┐
     │  Auth & Limits  │          │  In-memory Cache │          │  Upstash Redis   │
     │                 │          │  (per instance)  │          │  (v1 sessions)   │
     │ X-API-KEY       │          │  TTL: CACHE_TTL  │          │  Guest tokens    │
     │ Bearer token    │          │                  │          │  Extraction hdls │
     │ ?key= param     │          │                  │          │                  │
     └─────────────────┘          └──────────────────┘          └──────────────────┘
```

---

## ✨ Key Features

- 🎵 **TikTok** — tikwm API (no-watermark, IP-free CDN) with full yt-dlp quality ladder merged in; yt-dlp fallback
- 📸 **Instagram** — yt-dlp extraction with Netscape cookie support for authenticated content
- ▶️ **YouTube** — yt-dlp primary → cobalt engine fallback → youtubei.js via `toolz-ytapi` fallback
- 🔐 **Auth** — `X-API-KEY` header, `Authorization: Bearer`, or `?key=` query param
- ⚡ **Rate limiting** — per-IP, configurable requests/minute (via `X-Forwarded-For`)
- 💾 **Caching** — in-memory per-instance extraction cache with configurable TTL
- 🔗 **v1 Client Contract** — sessions → extractions → downloads; upstream URLs never exposed
- 🗄️ **Upstash Redis** — distributed session/extraction state for production multi-instance deploys
- 🛡️ **SSRF protection** — blocked private/internal hosts
- 📦 **Single-file deploy** — entire app in `api/index.py`; Vercel-native with `maxDuration: 60`

---

## 🗺️ API Endpoints

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/api/health` | Optional | Status, version, platforms, cookie flags |
| `GET` | `/api/platforms` | Optional | Supported platform list |
| `GET` `POST` | `/api/extract` | Required | Extract media info and format ladder |
| `GET` | `/api/download` | Required | Stream media bytes as attachment (supports `Range`) |
| `POST` | `/api/v1/client-sessions` | — | Create guest session with opaque install ID |
| `POST` | `/api/v1/extractions` | Bearer | Submit URL, get opaque asset handles + download paths |

### `/api/extract` Response Shape

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
    "video": [
      {
        "format_id": "tikwm_hd",
        "ext": "mp4",
        "resolution": "~1080p HD, no watermark",
        "url": "…",
        "filesize": 4561873,
        "has_audio": true,
        "ladder": "fast",
        "ip_free": true
      }
    ],
    "audio": [
      { "format_id": "tikwm_music", "ext": "mp3", "resolution": "audio", "url": "…", "filesize": null }
    ]
  },
  "original_url": "https://…"
}
```

### Status Codes

| Code | Meaning |
|------|---------|
| `400` | Unsupported URL / SSRF-blocked host / slideshow / IG failure |
| `401` | Invalid or missing API key |
| `409` | Attempting to download a blocked extraction |
| `429` | Rate limit exceeded |
| `502` | Upstream CDN refused |
| `504` | Extraction timed out — retry (cache makes retries instant) |

---

## 🔑 Environment Variables

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `API_SECRET_KEY` | **Yes** | — | `openssl rand -hex 32`. Must match the frontend. Unset = no auth (local dev only). |
| `YOUTUBE_COOKIES` | For YouTube | — | Full Netscape `cookies.txt` from a **throwaway** Google account. Export via "Get cookies.txt LOCALLY", paste entire file content. Rotate when bot-wall errors appear. |
| `YT_EXTRACT_URL` | YT fallback | — | Deployed `toolz-ytapi` URL for youtubei.js decipher fallback. |
| `YTAPI_SECRET` | YT fallback | — | Shared secret for `toolz-ytapi` (must match on both sides). |
| `INSTAGRAM_COOKIES` | For Instagram | — | Full Netscape `cookies.txt` from a logged-in instagram.com session. Never commit cookies. |
| `EXTRACT_TIMEOUT` | No | `26` | Seconds before extraction is abandoned. |
| `CACHE_TTL` | No | `3600` | In-memory cache lifetime (seconds) for successful extractions. |
| `RATE_LIMIT` | No | `30` | Max requests per minute per IP. |
| `UPSTASH_REDIS_REST_URL` | v1 production | — | Upstash Redis REST endpoint for distributed session/extraction state. |
| `UPSTASH_REDIS_REST_TOKEN` | v1 production | — | Upstash Redis REST auth token. |

> [!IMPORTANT]
> **Never commit cookies or `API_SECRET_KEY` to source control.** The API reads `INSTAGRAM_COOKIES` and `YOUTUBE_COOKIES` into `/tmp` at runtime for yt-dlp. Rotate cookies immediately if bot-wall errors appear in logs.

---

## 🔄 v1 Client Contract

New Android and web clients **must** use the v1 API. It never exposes upstream media URLs, cookies, or the project API secret to the calling client.

```
1.  POST /api/v1/client-sessions
        Body: { "install_id": "<opaque stable installation ID>" }
        → { "token": "<short-lived guest bearer token>" }

2.  POST /api/v1/extractions
        Headers: Authorization: Bearer <guest token>
        Body: { "url": "<public media URL>" }
        → { "assets": [{ "id": "…", "download_path": "/api/download?…" }], … }

3.  GET <assets[n].download_path>
        Headers: Authorization: Bearer <guest token>
        → streams media bytes
```

> [!NOTE]
> Guest tokens and extraction handles are **short-lived**. In production, set `UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN`. In-memory state is for local dev only. `/api/extract` and `/api/download` remain available as legacy compatibility routes during client rollout.

---

## 🚀 Deploy to Vercel

1. Click the button below (or import this repo manually in Vercel):

   [![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Ffreroxx%2Ftoolz-downloadz-api&env=API_SECRET_KEY,INSTAGRAM_COOKIES)

2. **Framework Preset:** `Other` — no build command needed.
3. Set environment variables (at minimum `API_SECRET_KEY`).
4. Deploy, then verify:

```bash
curl https://<your-api>.vercel.app/api/health
```

> `vercel.json` sets `maxDuration: 60` and routes all traffic to `api/index.py`. The app is intentionally single-file because Vercel's Python builder is unreliable with sub-packages under `api/`.

---

## 💻 Local Development

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
#    → set API_SECRET_KEY (and optional cookie vars)

# 3. Start dev server
uvicorn api.index:app --reload
#    → API docs at http://localhost:8000/docs

# 4. Run tests
PYTHONPATH=. pytest -q
```

---

## 📁 Project Structure

```
toolz-downloadz-api/
├── api/
│   └── index.py        # Entire app: routes, extraction, download, cache, auth
├── tests/
│   └── test_api.py     # pytest suite (offline unit tests + live smoke tests)
├── .env.example        # Documented environment variable template
├── requirements.txt    # fastapi, uvicorn, yt-dlp, python-dotenv, …
└── vercel.json         # maxDuration: 60 + route rewrites
```

---

## 🔗 Toolz Ecosystem

| Repo | Role |
|------|------|
| [freroxx/toolz](https://github.com/freroxx/toolz) | 📱 Main Toolz app |
| [freroxx/toolz-downloadz](https://github.com/freroxx/toolz-downloadz) | 🌐 Frontend web client |
| [freroxx/toolz-downloadz-api](https://github.com/freroxx/toolz-downloadz-api) | ⚙️ **This repo** — media extraction API |
| [freroxx/toolz-ytapi](https://github.com/freroxx/toolz-ytapi) | ▶️ YouTube youtubei.js decipher service |
| [freroxx/toolz-cobalt](https://github.com/freroxx/toolz-cobalt) | 🔧 Cobalt engine fork |
| [freroxx/toolz-pot](https://github.com/freroxx/toolz-pot) | 🪙 YouTube PO Token provider |

---

## 🤝 Contributing

1. Fork the repo and create a feature branch.
2. Make changes — keep `api/index.py` single-file and dependency-light.
3. Run tests: `PYTHONPATH=. pytest -q`
4. Open a PR with test evidence.

---

## ⚖️ Legal & Copyright Disclaimers

1. **Intended Use & Personal Archiving**: This software is provided strictly for educational, technical, and personal archival purposes of content that the user owns or has the legal right to download under applicable fair use or private copying exceptions.
2. **No DRM Circumvention**: This API does not defeat, circumvent, or decrypt Digital Rights Management (DRM) mechanisms (such as Widevine, PlayReady, or FairPlay). It accesses only publicly accessible, unencrypted streaming endpoints.
3. **No Hosting or Storage**: This API does not host, store, index, or distribute copyright-protected video, audio, or image files. Media streams are handled transiently.
4. **Trademarks & Non-Affiliation**: *TikTok*, *ByteDance*, *Instagram*, *Meta*, *YouTube*, *Google*, and *Vercel* are trademarks of their respective owners. This project is independent and not affiliated with, sponsored by, or endorsed by any of those organizations.
5. **User Responsibility**: Users of this software are solely responsible for ensuring that their usage complies with all applicable local, national, and international laws, as well as the terms of service of third-party platforms.

---

## 📄 License & Community

Licensed under the **GNU General Public License v3.0** — see [`LICENSE`](LICENSE) for full details.

- [Code of Conduct](CODE_OF_CONDUCT.md)
- [Contributing Guidelines](CONTRIBUTING.md)
- [Security Policy](SECURITY.md)
- [Accessibility Statement](ACCESSIBILITY.md)

<div align="center">
  <sub>Part of the <a href="https://github.com/freroxx/toolz"><strong>Toolz Suite</strong></a> · Built with ❤️ by <a href="https://github.com/freroxx">freroxx</a></sub>
</div>
