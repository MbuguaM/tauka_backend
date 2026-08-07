from app.core.supabase_client import supabase
from datetime import datetime, timezone
from fastapi.concurrency import run_in_threadpool


async def store_message(conversation_id: str, sender_id: str, content: str) -> dict:
    """
    Insert a message into app.messages and update last_message_at on the conversation.
    Both tables live in the app schema.

    PY-001: the Supabase client is synchronous, so its blocking I/O is offloaded
    to a worker thread via run_in_threadpool to keep the async event loop free.
    TDT-004: returns only the inserted row dict, not the raw Supabase response,
    so the route's response schema is decoupled from the SDK's envelope.
    """
    db = supabase.schema("app")

    def _insert():
        return db.table("messages").insert({
            "conversation_id": conversation_id,
            "sender_id": sender_id,
            "content": content,
        }).execute()

    result = await run_in_threadpool(_insert)

    # Update conversation's last_message_at (best-effort)
    try:
        def _touch_conversation():
            return db.table("conversations").update({
                "last_message_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", conversation_id).execute()

        await run_in_threadpool(_touch_conversation)
    except Exception:
        pass

    return result.data[0] if result.data else {}


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
