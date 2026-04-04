from app.core.supabase_client import supabase
from datetime import datetime, timezone


def store_message(conversation_id: str, sender_id: str, content: str):
    """
    Insert a message into app.messages and update last_message_at on the conversation.
    Both tables live in the app schema.
    """
    db = supabase.schema("app")

    result = db.table("messages").insert({
        "conversation_id": conversation_id,
        "sender_id": sender_id,
        "content": content,
    }).execute()

    # Update conversation's last_message_at (best-effort)
    try:
        db.table("conversations").update({
            "last_message_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", conversation_id).execute()
    except Exception:
        pass

    return result


def get_or_create_student_conversation(student1_id: str, student2_id: str) -> str:
    """
    Call the app.get_or_create_student_conversation RPC.
    Returns the conversation UUID as a string.
    """
    result = supabase.rpc(
        "get_or_create_student_conversation",
        {"student1_id": student1_id, "student2_id": student2_id},
    ).execute()
    return result.data
