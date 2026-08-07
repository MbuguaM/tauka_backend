from supabase import create_client, Client
from app.config import settings

# Service role client — bypasses RLS. NEVER expose this to clients.
supabase_admin: Client = create_client(
    settings.supabase_url,
    settings.supabase_key,
)

# Anon client — respects RLS (used for testing/validation only in FastAPI)
supabase_anon: Client = create_client(
    settings.supabase_url,
    settings.supabase_anon_key or settings.supabase_key,
)
