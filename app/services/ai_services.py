"""
Multi-provider AI service.

Supported providers:  openai | deepseek | gemini
Supported modes:      chat | translation | image_translation

Image URL accepted as either:
  • An HTTPS URL  (https://...)
  • A base64 data URI  (data:image/jpeg;base64,<data>)
"""

import base64
import httpx
from app.config import settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_translation_prompt(text: str, target_language: str) -> str:
    return (
        f"Translate the following text into {target_language}. "
        "Return only the translated text, nothing else.\n\n"
        f"{text}"
    )


def _build_image_translation_prompt(target_language: str | None) -> str:
    if target_language:
        return (
            f"Extract all text visible in this image and translate it into "
            f"{target_language}. Return only the translated text."
        )
    return "Extract and return all text visible in this image."


def _parse_image_url(image_url: str) -> tuple[str, str | None]:
    """
    Returns (url_or_b64_data, mime_type_or_None).
    If the input is a data URI, returns (base64_data, mime_type).
    If it's a plain URL, returns (url, None).
    """
    if image_url.startswith("data:"):
        # data:image/jpeg;base64,<data>
        header, data = image_url.split(",", 1)
        mime_type = header.split(";")[0].split(":")[1]
        return data, mime_type
    return image_url, None


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

async def _call_openai(
    messages: list[dict],
    model: str | None = None,
) -> str:
    model = model or settings.MODEL_NAME
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "messages": messages}

    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(url, json=payload, headers=headers)
        res.raise_for_status()

    return res.json()["choices"][0]["message"]["content"]


async def call_openai_chat(prompt: str) -> str:
    return await _call_openai([{"role": "user", "content": prompt}])


async def call_openai_translate(text: str, target_language: str) -> str:
    prompt = _build_translation_prompt(text, target_language)
    return await _call_openai([{"role": "user", "content": prompt}])


async def call_openai_image_translation(
    image_url: str,
    target_language: str | None,
) -> str:
    prompt_text = _build_image_translation_prompt(target_language)
    b64_or_url, mime = _parse_image_url(image_url)

    if mime:
        image_part = {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64_or_url}"},
        }
    else:
        image_part = {"type": "image_url", "image_url": {"url": b64_or_url}}

    messages = [
        {
            "role": "user",
            "content": [
                image_part,
                {"type": "text", "text": prompt_text},
            ],
        }
    ]
    # Vision requires gpt-4o or gpt-4o-mini
    model = settings.MODEL_NAME if "4o" in (settings.MODEL_NAME or "") else "gpt-4o-mini"
    return await _call_openai(messages, model=model)


# ---------------------------------------------------------------------------
# DeepSeek  (OpenAI-compatible API)
# ---------------------------------------------------------------------------

async def _call_deepseek(messages: list[dict]) -> str:
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"model": "deepseek-chat", "messages": messages}

    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(url, json=payload, headers=headers)
        res.raise_for_status()

    return res.json()["choices"][0]["message"]["content"]


async def call_deepseek_chat(prompt: str) -> str:
    return await _call_deepseek([{"role": "user", "content": prompt}])


async def call_deepseek_translate(text: str, target_language: str) -> str:
    prompt = _build_translation_prompt(text, target_language)
    return await _call_deepseek([{"role": "user", "content": prompt}])


# ---------------------------------------------------------------------------
# Gemini  (Google Generative Language REST API)
# ---------------------------------------------------------------------------

def _gemini_url(model: str = "gemini-1.5-flash") -> str:
    return (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={settings.GEMINI_API_KEY}"
    )


async def _call_gemini(parts: list[dict], model: str = "gemini-1.5-flash") -> str:
    payload = {"contents": [{"parts": parts}]}

    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(_gemini_url(model), json=payload)
        res.raise_for_status()

    return res.json()["candidates"][0]["content"]["parts"][0]["text"]


async def call_gemini_chat(prompt: str) -> str:
    return await _call_gemini([{"text": prompt}])


async def call_gemini_translate(text: str, target_language: str) -> str:
    prompt = _build_translation_prompt(text, target_language)
    return await _call_gemini([{"text": prompt}])


async def call_gemini_image_translation(
    image_url: str,
    target_language: str | None,
) -> str:
    prompt_text = _build_image_translation_prompt(target_language)
    b64_or_url, mime = _parse_image_url(image_url)

    if mime:
        # Inline base64 image
        image_part = {
            "inline_data": {
                "mime_type": mime,
                "data": b64_or_url,
            }
        }
    else:
        # Fetch the image and convert to base64 for Gemini
        async with httpx.AsyncClient(timeout=30) as client:
            img_res = await client.get(b64_or_url)
            img_res.raise_for_status()
        mime = img_res.headers.get("content-type", "image/jpeg").split(";")[0]
        b64_data = base64.b64encode(img_res.content).decode()
        image_part = {
            "inline_data": {
                "mime_type": mime,
                "data": b64_data,
            }
        }

    # gemini-1.5-flash supports vision
    return await _call_gemini([image_part, {"text": prompt_text}], model="gemini-1.5-flash")


# ---------------------------------------------------------------------------
# Unified dispatcher
# ---------------------------------------------------------------------------

async def call_ai(
    prompt: str,
    provider: str = "deepseek",
    mode: str = "chat",
    target_language: str | None = None,
    image_url: str | None = None,
) -> str:
    """
    Route a request to the correct provider and mode.

    Raises ValueError for unsupported combinations.
    """
    if mode == "image_translation":
        if not image_url:
            raise ValueError("image_url is required for image_translation mode")
        if provider == "openai":
            return await call_openai_image_translation(image_url, target_language)
        if provider == "gemini":
            return await call_gemini_image_translation(image_url, target_language)
        raise ValueError(f"image_translation is not supported for provider '{provider}'")

    if mode == "translation":
        lang = target_language or "English"
        if provider == "openai":
            return await call_openai_translate(prompt, lang)
        if provider == "deepseek":
            return await call_deepseek_translate(prompt, lang)
        if provider == "gemini":
            return await call_gemini_translate(prompt, lang)

    # Default: chat
    if provider == "openai":
        return await call_openai_chat(prompt)
    if provider == "deepseek":
        return await call_deepseek_chat(prompt)
    if provider == "gemini":
        return await call_gemini_chat(prompt)

    raise ValueError(f"Unknown provider '{provider}'")
