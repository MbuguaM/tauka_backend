from app.core.supabase_client import supabase
from datetime import datetime

def log_usage(user_id, event_type, value):
    try:
        supabase.table("usage_logs").insert({
            "user_id": user_id,
            "event_type": event_type,
            "value": value,
            "created_at": datetime.utcnow().isoformat()
        }).execute()
    except Exception:
        pass