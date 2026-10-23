# Contributing to toolz-downloadz-api

`toolz-downloadz-api` is a media extraction service for public TikTok, Instagram, and YouTube media, built with FastAPI on Vercel serverless.

## Guidelines & Legal Rules

1. **Lawful Archiving Only**: This project is built solely for personal, non-commercial archiving of content users have the right to access. Contributions that bypass DRM (Widevine, PlayReady, FairPlay) or crack paywalls will be immediately rejected.
2. **License**: All contributions are licensed under the **GNU General Public License v3.0 (GPL-3.0)**. By submitting a PR, you certify you wrote the code or have the right to license it under GPL-3.0.
3. **Keep it single-file**: Vercel routes everything to `api/index.py`. Keep dependencies minimal.

## Local Dev

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn api.index:app --reload
PYTHONPATH=. pytest -q
```

## Security

Report vulnerabilities privately to **frerox.toolz@gmail.com** (see [SECURITY.md](SECURITY.md)).
