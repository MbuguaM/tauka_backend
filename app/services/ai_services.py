"""
Multi-provider AI service.

Supported providers:  openai | deepseek | gemini
Supported modes:      chat | translation | image_translation
                      lesson_explain | analyse_amharic  (framed — see ai_prompts)

Image URL accepted as either:
  • An HTTPS URL  (https://...)
  • A base64 data URI  (data:image/jpeg;base64,<data>)
"""

import base64
from contextvars import ContextVar
from dataclasses import dataclass

import httpx
from app.config import settings

# ---------------------------------------------------------------------------
# The provider's own token count
# ---------------------------------------------------------------------------
#
# Every provider returns usage alongside the text, and until now every one of
# those numbers was parsed and thrown away — `call_ai` returns a `str`. Phase 9
# of activity-telemetry-plan.md wants a ledger built from what the model
# actually charged, so the number has to survive the trip out.
#
# It travels in a ContextVar rather than in the return type on purpose. Widening
# `call_ai` to a tuple would touch every wrapper and every test that patches it
# with a plain string, for a value only one caller wants. A ContextVar is
# per-task, so two concurrent requests cannot see each other's usage.
#
# It is CONSUMED on read (`take_last_usage`), because a context is copied when a
# task is spawned: a value left behind could otherwise be read by a later
# request and billed to the wrong person.


@dataclass(frozen=True)
class ProviderUsage:
    """What the model said it charged. Never a local estimate."""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model: str | None = None


_last_usage: ContextVar[ProviderUsage | None] = ContextVar(
    "ai_last_usage", default=None
)


def take_last_usage() -> ProviderUsage | None:
    """
    The usage reported by the most recent provider call in this task, if any.

    Returns None when the provider did not report it — the caller must then say
    so rather than passing an estimate off as a measurement.
    """
    usage = _last_usage.get()
    _last_usage.set(None)
    return usage


def _record_openai_usage(body: dict, model: str | None) -> None:
    """OpenAI and DeepSeek share this shape."""
    usage = body.get("usage") or {}
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    if total is None and prompt is None and completion is None:
        return  # nothing reported; leave it unset so the caller can tell
    prompt = int(prompt or 0)
    completion = int(completion or 0)
    _last_usage.set(ProviderUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=int(total if total is not None else prompt + completion),
        model=body.get("model") or model,
    ))


def _record_gemini_usage(body: dict, model: str | None) -> None:
    """Gemini names all three fields differently, and omits them on some paths."""
    usage = body.get("usageMetadata") or {}
    prompt = usage.get("promptTokenCount")
    completion = usage.get("candidatesTokenCount")
    total = usage.get("totalTokenCount")
    if total is None and prompt is None and completion is None:
        return
    prompt = int(prompt or 0)
    completion = int(completion or 0)
    _last_usage.set(ProviderUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=int(total if total is not None else prompt + completion),
        model=body.get("modelVersion") or model,
    ))


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

    body = res.json()
    _record_openai_usage(body, model)
    return body["choices"][0]["message"]["content"]


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

async def _call_deepseek(
    messages: list[dict],
    *,
    max_tokens: int | None = None,
    timeout: float = 60,
    temperature: float | None = None,
    json_object: bool = False,
) -> tuple[str, str | None]:
    """Call deepseek-chat. Returns ``(content, finish_reason)``.

    The keyword arguments exist for lyric annotation, which is a different shape
    of request from a chat turn and was silently broken without them:

    * **max_tokens** — omitted, the provider default of 4096 applies. A whole
      annotated song is 6,000+ output tokens, so the response came back cut in
      half with no error.
    * **timeout** — 60s is right for a chat reply and not enough for a song.
    * **finish_reason** — the caller cannot otherwise tell a complete answer
      from a truncated one. `length` is what drives the split-on-overflow path;
      without it, truncation looks like a model that dropped half the lines.
    """
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload: dict = {"model": "deepseek-chat", "messages": messages}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if temperature is not None:
        payload["temperature"] = temperature
    if json_object:
        payload["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=timeout) as client:
        res = await client.post(url, json=payload, headers=headers)
        res.raise_for_status()

    body = res.json()
    _record_openai_usage(body, "deepseek-chat")
    choice = body["choices"][0]
    return choice["message"]["content"], choice.get("finish_reason")


async def call_deepseek_annotate(
    system: str,
    user: str,
    *,
    max_tokens: int = 8000,
    timeout: float = 300,
) -> tuple[str, str | None]:
    """One lyric-annotation request. Deterministic, bounded, and honest about
    running out of room."""
    return await _call_deepseek(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        max_tokens=max_tokens,
        timeout=timeout,
        temperature=0,
        json_object=True,
    )


async def call_deepseek_chat(prompt: str) -> str:
    content, _ = await _call_deepseek([{"role": "user", "content": prompt}])
    return content


async def call_deepseek_translate(text: str, target_language: str) -> str:
    prompt = _build_translation_prompt(text, target_language)
    content, _ = await _call_deepseek([{"role": "user", "content": prompt}])
    return content


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

    body = res.json()
    _record_gemini_usage(body, model)
    return body["candidates"][0]["content"]["parts"][0]["text"]


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
    system: str | None = None,
) -> str:
    """
    Route a request to the correct provider and mode.

    `system` carries server-owned instructions for the framed modes. It is built
    by app.services.ai_prompts and never comes from the client — that is the
    whole point of those modes. Passing it on any other mode is allowed and
    simply prepends a system turn.

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

    if system:
        if provider == "openai":
            return await _call_openai(
                [{"role": "system", "content": system},
                 {"role": "user", "content": prompt}]
            )
        if provider == "deepseek":
            content, _ = await _call_deepseek(
                [{"role": "system", "content": system},
                 {"role": "user", "content": prompt}]
            )
            return content
        if provider == "gemini":
            # This Gemini payload shape (v1beta generateContent with bare
            # `contents`) carries no system role, so the instructions are
            # prepended to the single part instead. The fence still does its
            # work — it is the nonce, not the role, that content cannot forge —
            # but the separation is weaker here than on the other two providers,
            # which is a reason to keep deepseek as the default for these modes.
            return await _call_gemini(
                [{"text": f"{system}\n\n{prompt}"}]
            )
        raise ValueError(f"Unknown provider '{provider}'")

    # Default: chat
    if provider == "openai":
        return await call_openai_chat(prompt)
    if provider == "deepseek":
        return await call_deepseek_chat(prompt)
    if provider == "gemini":
        return await call_gemini_chat(prompt)

    raise ValueError(f"Unknown provider '{provider}'")
