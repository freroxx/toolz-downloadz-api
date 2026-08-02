# toolz-downloadz-api

Lightweight, production-ready FastAPI backend optimized for Vercel Serverless. Powering cross-platform media extraction for the Toolz ecosystem using `yt-dlp`.

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fyour-username%2Ftoolz-downloadz-api&env=API_SECRET_KEY)

## Features

- **Serverless Ready**: Designed to run on Vercel Functions with zero disk writes.
- **FastAPI**: High-performance Python backend with automatic OpenAPI documentation.
- **Secure**: Protected by `X-API-KEY` header authentication.
- **Bot-Resistant**: Multi-client rotation for YouTube plus a never-blocked oEmbed
  metadata fallback; optional cookie file support for datacenter IP blocks.
- **CORS Enabled**: Ready for connection from Android apps, Web UIs, and CLI tools.

## Platform Support & Resilience

`yt-dlp` runs against a datacenter IP on hosting platforms, and YouTube (and
sometimes Instagram/Reddit) will flag it with *"Sign in to confirm you're not a
bot."* This API handles that with three layers:

1. **Client rotation** – YouTube is tried with several player clients
   (`android`, `mweb`, `web_safari`, `ios`, `web`, `tv`, `android_vr`) until one
   succeeds.
2. **oEmbed fallback** – if YouTube blocks the extractor, metadata (title,
   thumbnail, uploader) is still returned from YouTube's public oEmbed endpoint,
   which is never blocked. The client sees a `blocked: true` card with an
   *Open on source* action instead of a hard failure.
3. **Optional cookie auth** – set `YOUTUBE_COOKIES` (Netscape format exported
   from a browser) to authenticate and bypass the block entirely. Never keep
   using that browser session after exporting.
4. **PO Token provider (strongest)** – run the
   [bgutil POT provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
   (Docker image `brainicism/bgutil-ytdlp-pot-provider`, or Node/Deno) alongside
   this app and set `YT_DLP_POT_PROVIDER_URL` to its URL. The plugin
   (`bgutil-ytdlp-pot-provider`, pinned in `requirements.txt`) is auto-detected
   on `http://127.0.0.1:4416`, but a custom `base_url` is passed through via
   extractor args. Requires a Node/Deno-capable host (e.g. self-host), not
   Vercel serverless.

### Self-hosting for maximum success

Serverless datacenter IPs are the #1 trigger for bot blocks. For the most
reliable behavior, run the same code on a persistent VPS/residential IP, and if
YouTube still blocks, add the POT provider:

```bash
docker build -t toolz-downloadz-api .
docker run --rm -p 8000:8000 -e API_SECRET_KEY=your_key toolz-downloadz-api
```

To run the PO-Origin token server alongside the API on one host:

```bash
docker run --name bgutil-provider -d --init \
  brainicism/bgutil-ytdlp-pot-provider
# provider listens on http://127.0.0.1:4416 by default — the plugin finds it
# automatically, no env var needed. For a custom URL set:
docker run --rm -p 8000:8000 \
  -e API_SECRET_KEY=your_key \
  -e YT_DLP_POT_PROVIDER_URL=http://127.0.0.1:4416 toolz-downloadz-api
```

## Setup & Local Development

### Prerequisites

- Python 3.11+
- Pip (Python Package Manager)

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/your-username/toolz-downloadz-api.git
   cd toolz-downloadz-api
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Environment Variables:**
   Copy `.env.example` to `.env` and set your secret key:
   ```bash
   cp .env.example .env
   ```

4. **Run the development server:**
   ```bash
   python api/index.py
   ```
   The API will be available at `http://localhost:8000`.

## API Documentation

Once running, you can access the interactive Swagger UI at `http://localhost:8000/docs`.

### Endpoint: `GET /api/extract`

Extracts metadata and direct stream URLs for a given media URL.

**Headers:**
- `X-API-KEY`: Your secret key (must match `API_SECRET_KEY`).

**Query Parameters:**
- `url` (string, required): The URL of the video/audio to extract.

**Response Schema:**
```json
{
  "title": "Video Title",
  "thumbnail": "https://cdn.example.com/thumb.jpg",
  "duration": 120,
  "uploader": "Channel Name",
  "download_url": "https://direct-stream-url.com/...",
  "ext": "mp4",
  "formats": [...]
}
```

## Deployment on Vercel

1. Push this repository to GitHub/GitLab/Bitbucket.
2. Connect your repository to [Vercel](https://vercel.com).
3. Set the `API_SECRET_KEY` Environment Variable in the Vercel Dashboard.
4. Deploy! Vercel will automatically detect the Python configuration and route `/api/*` to `api/index.py`.

## License

This project is licensed under the **GNU GPLv3** - see the [LICENSE](LICENSE) file for details.
