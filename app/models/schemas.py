from pydantic import BaseModel
from typing import Literal, Optional


class AIRequest(BaseModel):
    prompt: str
    provider: Literal["openai", "deepseek", "gemini"] = "deepseek"
    mode: Literal["chat", "translation", "image_translation"] = "chat"
    # Required when mode == "translation" or "image_translation"
    target_language: Optional[str] = None
    # Required when mode == "image_translation": HTTPS URL or base64 data URI
    image_url: Optional[str] = None


class MessageRequest(BaseModel):
    conversation_id: str
    content: str


class CallRequest(BaseModel):
    room: str
    # Override DB vendor config when specified; otherwise active config is used
    vendor: Optional[Literal["daily", "livekit"]] = None
