"""Vercel / ASGI entrypoint.

The application itself lives in src.api.main; this module only
re-exports the FastAPI ``app`` object so PaaS runtimes that look for
an entrypoint at the repository root (Vercel's Python runtime, plain
uvicorn/gunicorn from the root) find it without configuration.
"""
from src.api.main import app

__all__ = ["app"]
