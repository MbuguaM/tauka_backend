"""
Vercel serverless entry point.

Vercel's Python runtime looks for an ASGI/WSGI callable named `app` in a module
under `api/`. Everything is routed here by the rewrite in `vercel.json`, so this
file exists only to expose the real application — put no logic in it.

Note that `app.main` opens Supabase clients at import, so a cold start fails
fast and loudly if SUPABASE_URL / SUPABASE_KEY are missing from the project's
environment variables. That is intentional: a backend that boots without a
database would answer every request with a confusing 500 instead.
"""
from app.main import app

__all__ = ["app"]
