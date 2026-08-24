"""Local dev entry — imports app from api/index.py for `uvicorn api.main:app`."""
from .index import app  # noqa: F401

if __name__ == "__main__":
    import uvicorn, os
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("api.main:app", host="0.0.0.0", port=port, reload=True)
