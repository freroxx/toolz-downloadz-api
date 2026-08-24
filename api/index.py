"""
Vercel entrypoint — re-exports app from main.py
Keeps `python api/index.py` runnable locally too.
This file must stay at api/index.py for vercel.json rewrite: source "/api/(.*)" -> "/api/index.py"
"""
import os
import sys

# Ensure parent dir on path for relative imports when run directly via `python api/index.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from api.main import app  # type: ignore
except ImportError:
    # Fallback when running as `python api/index.py` directly
    from main import app  # type: ignore

# For `python api/index.py` local dev
if __name__ == "__main__":
    import uvicorn

    # Load env from project root
    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("api.main:app", host="0.0.0.0", port=port, reload=True)
