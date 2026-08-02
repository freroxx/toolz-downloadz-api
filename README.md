# toolz-downloadz-api

Lightweight, production-ready FastAPI backend optimized for Vercel Serverless. Powering cross-platform media extraction for the Toolz ecosystem using `yt-dlp`.

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fyour-username%2Ftoolz-downloadz-api&env=API_SECRET_KEY)

## Features

- **Serverless Ready**: Designed to run on Vercel Functions with zero disk writes.
- **FastAPI**: High-performance Python backend with automatic OpenAPI documentation.
- **Secure**: Protected by `X-API-KEY` header authentication.
- **Bot-Resistant**: Configured to bypass datacenter IP blocks using TV/Mobile client signatures.
- **CORS Enabled**: Ready for connection from Android apps, Web UIs, and CLI tools.

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
   cp .env .env
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
