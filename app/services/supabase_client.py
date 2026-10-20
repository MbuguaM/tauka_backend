from supabase import create_client, Client
from app.config import settings


def _cache_schemas(client: Client) -> Client:
    """`client.schema("app")` builds a new PostgREST client — and with it a new
    httpx client that loads the CA bundle — on every call. Every query in this
    service starts with `db.schema("app")`, so each one paid that cost (about
    half a second on Windows, measured 2026-10-20; less on Linux, never zero).
    One PostgREST client per schema, reused, removes it. httpx clients are safe
    to share across threads."""
    made: dict[str, object] = {}
    original = client.schema

    def schema(name: str):
        c = made.get(name)
        if c is None:
            c = made[name] = original(name)
        return c

    client.schema = schema  # type: ignore[method-assign]
    return client


# Service role client — bypasses RLS. NEVER expose this to clients.
supabase_admin: Client = _cache_schemas(create_client(
    settings.supabase_url,
    settings.supabase_key,
))

# Anon client — respects RLS (used for testing/validation only in FastAPI)
supabase_anon: Client = _cache_schemas(create_client(
    settings.supabase_url,
    settings.supabase_anon_key or settings.supabase_key,
))
