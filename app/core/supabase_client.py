# app/core/supabase_client.py
from supabase import create_client
from app.config import settings
from app.services.supabase_client import _cache_schemas

# Same schema-client cache as app.services.supabase_client (see _cache_schemas).
supabase = _cache_schemas(create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY))
