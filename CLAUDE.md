# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Run dev server:**
```bash
uv run fastapi dev
```
Starts at `http://localhost:8000`. API docs at `/docs`.

**Add dependencies:**
```bash
uv add <package>
```

## Environment

Required variables in `.env`:

| Variable | Required | Purpose |
|---|---|---|
| `DEEPSEEK_API_KEY` | Yes | DeepSeek (primary AI provider) |
| `SUPABASE_URL` / `SUPABASE_KEY` | Yes | Supabase (service role key) |
| `SUPABASE_JWT_SECRET` | Yes | Verifying Supabase-issued JWTs |
| `REDIS_URL` | No | Redis (default: `redis://localhost:6379`) |
| `DAILY_API_KEY` | For calls | Daily.co room/token creation |
| `LIVEKIT_API_KEY` / `LIVEKIT_SECRET` | For calls | LiveKit JWT signing |
| `OPENAI_API_KEY` | Optional | OpenAI chat + vision (only if using openai provider) |
| `GEMINI_API_KEY` | Optional | Gemini chat + vision (only if using gemini provider) |
| `MODEL_NAME` | No | Default DeepSeek model (default: `deepseek-chat`) |
| `TOKEN_LIMIT` | No | Monthly token cap (default: `100000`) |

## Architecture

**Stack:** FastAPI + Supabase (app schema) + Redis + OpenAI + DeepSeek + Gemini + Daily.co + LiveKit

**Layered structure under `app/`:**

| Layer | Path | Responsibility |
|---|---|---|
| Routes | `app/routes/` | HTTP handlers — thin, delegate to services |
| Services | `app/services/` | Business logic per domain |
| Core | `app/core/` | Infrastructure singletons (Redis, Supabase) |
| Models | `app/models/schemas.py` | Pydantic schemas |
| Config | `app/config.py` | `Settings` class loaded from `.env` |

All Supabase tables live in the **`app` schema** — use `supabase.schema("app").table(...)`.

## AI Service (`POST /ai/generate`)

Three providers, three modes:

| provider | chat | translation | image_translation |
|---|---|---|---|
| `openai` | ✓ | ✓ | ✓ (gpt-4o-mini vision) |
| `deepseek` | ✓ | ✓ | — |
| `gemini` | ✓ | ✓ | ✓ (gemini-1.5-flash vision) |

**Request:**
```json
{
  "prompt": "Hello",
  "user_id": "<uuid>",
  "provider": "openai",
  "mode": "chat",
  "target_language": "Spanish",   // required for translation modes
  "image_url": "https://..."      // required for image_translation (URL or base64 data URI)
}
```

**Request flow:**
1. `routes/ai.py` — validates image_url present for image_translation
2. `rate_limit_service.py` — checks Redis per `(provider, user_id)` — each provider has independent limits; image requests add a 1,000-token surcharge
3. `ai_services.call_ai()` — dispatches to the correct provider + mode function
4. `token_service.count_tokens()` — uses tiktoken cl100k_base for unknown models (DeepSeek, Gemini)
5. `usage_service.log_usage()` — fire-and-forget background task; event type = `ai_tokens:{provider}:{mode}`

## Calling Service (`POST /calls/token`)

Vendor selection order: request override → `app.video_vendor_config` active row → `"daily"`.

- **Daily.co**: creates room via REST API, returns meeting token + `room_url`
- **LiveKit**: returns a signed JWT with `video` grant (roomJoin, canPublish, canSubscribe)

**Response** always includes `token`, `room`, `room_url` (null for LiveKit), `vendor`.

The active vendor is controlled by the `app.video_vendor_config` table (single active row enforced by partial unique index). Change vendor server-side without a client release.

## Messaging Service (`POST /messages/send`)

Aligned with the DB schema — uses `conversation_id` (not sender/receiver pair).

```json
{ "conversation_id": "<uuid>", "sender_id": "<uuid>", "content": "..." }
```

Inserts into `app.messages`, updates `app.conversations.last_message_at`.
Use `messaging_service.get_or_create_student_conversation(s1, s2)` to call the `app.get_or_create_student_conversation` RPC before sending.

## Rate Limiting

Redis key namespace: `rate:{provider}:{user_id}:{minute}` and `tokens:{provider}:{user_id}:day|month:{date}`.

Default limits per provider:

| provider | req/min | daily tokens | monthly tokens |
|---|---|---|---|
| openai | 10 | 5,000 | 100,000 |
| deepseek | 15 | 8,000 | 150,000 |
| gemini | 15 | 10,000 | 200,000 |
| messaging | 20 | 10,000 | 200,000 |

## Adding a New Feature

1. Add schema to `app/models/schemas.py`
2. Create service in `app/services/`
3. Add route in `app/routes/`
4. Register router in `app/main.py`
5. All I/O must use `async def` / `await`
6. Usage logging must never raise — wrap in try/except
