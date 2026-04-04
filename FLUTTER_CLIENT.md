# Flutter Client — Backend API Reference

This document is the authoritative guide for building a Flutter client against this FastAPI backend.
All communication is HTTP/JSON. There is no GraphQL layer, no WebSocket endpoint, and no REST pagination on any current route.

---

## Base URL

```
http://localhost:8000          # local dev
```

The server is started with `uv run fastapi dev`. In production, replace with your deployed URL.
API docs (auto-generated Swagger UI) are available at `<base_url>/docs`.

---

## Authentication

Every request to this API **must** include a valid Supabase JWT in the `Authorization` header.
The backend verifies the token signature using the Supabase project JWT secret and extracts the
caller's identity from the `sub` claim. There are no `user_id` / `sender_id` fields in any
request body — the server derives the user identity from the verified token only.

### How to obtain and send the token

```dart
// Sign in (once, at app start / login screen)
await supabase.auth.signInWithPassword(email: email, password: password);

// Retrieve the current access token
final token = supabase.auth.currentSession?.accessToken;

// Attach to every API request
final response = await http.post(
  Uri.parse('$baseUrl/ai/generate'),
  headers: {
    'Content-Type': 'application/json',
    'Authorization': 'Bearer $token',
  },
  body: jsonEncode({...}),
);
```

### Token refresh

Supabase access tokens expire after **1 hour**. Listen to `onAuthStateChange` and update the
token you pass to the API whenever a new session is issued:

```dart
supabase.auth.onAuthStateChange.listen((data) {
  final session = data.session;
  if (session != null) {
    ApiClient.instance.setToken(session.accessToken);
  }
});
```

### Authentication errors

| Status | `detail` | Meaning |
|---|---|---|
| `401` | `"Token has expired"` | Refresh the session and retry once |
| `401` | `"Invalid token"` | Token is malformed or signed with wrong secret — re-authenticate |
| `401` | `"Not authenticated"` | `Authorization` header missing entirely — re-authenticate |

A `401` should trigger a token refresh attempt (via `supabase.auth.refreshSession()`). If that
also fails, redirect to the login screen.

---

## Routes

### 1. AI Generation

**`POST /ai/generate`**

Generate a chat reply, translate text, or translate text from an image using one of three AI providers.

#### Request body

```json
{
  "prompt": "string",
  "provider": "openai" | "deepseek" | "gemini",
  "mode": "chat" | "translation" | "image_translation",
  "target_language": "string",
  "image_url": "string"
}
```

| Field | Required | Notes |
|---|---|---|
| `prompt` | Always | The user's input text |
| `provider` | No | Defaults to `"deepseek"` |
| `mode` | No | Defaults to `"chat"` |
| `target_language` | When `mode` is `"translation"` or `"image_translation"` | e.g. `"Spanish"`, `"French"` |
| `image_url` | When `mode` is `"image_translation"` | HTTPS URL **or** base64 data URI (`data:image/jpeg;base64,...`) |

**Provider × mode capability matrix:**

| provider | chat | translation | image_translation |
|---|---|---|---|
| `openai` | ✓ | ✓ | ✓ (gpt-4o-mini vision) |
| `deepseek` | ✓ | ✓ | ✗ |
| `gemini` | ✓ | ✓ | ✓ (gemini-1.5-flash) |

#### Success response — `200 OK`

```json
{
  "response": "string",
  "tokens": 123,
  "provider": "openai",
  "mode": "chat"
}
```

| Field | Type | Notes |
|---|---|---|
| `response` | `string` | The AI-generated text |
| `tokens` | `int` | Actual tokens consumed (prompt + completion) |
| `provider` | `string` | Echoes the resolved provider |
| `mode` | `string` | Echoes the resolved mode |

#### Error responses

| Status | When |
|---|---|
| `400` | Unsupported provider/mode combination (e.g. `deepseek` + `image_translation`) |
| `401` | Missing or invalid JWT |
| `422` | `image_url` missing when `mode == "image_translation"` |
| `429` | Rate limit exceeded — see [Rate Limits](#rate-limits) |

#### Rate limits (AI)

Each provider has **independent** counters keyed to the authenticated user.

| Provider | Req/min | Daily tokens | Monthly tokens |
|---|---|---|---|
| `openai` | 10 | 5,000 | 100,000 |
| `deepseek` | 15 | 8,000 | 150,000 |
| `gemini` | 15 | 10,000 | 200,000 |

`image_translation` requests add a flat **1,000-token surcharge** on top of actual tokens when
checking limits (images have higher API cost regardless of size).

When a `429` is returned, the `detail` field contains one of:
- `"Too many requests"` — req/min cap hit
- `"Daily token limit exceeded"`
- `"Monthly token limit exceeded"`

---

### 2. Messaging

**`POST /messages/send`**

Send a message into an existing conversation. The sender identity is taken from the JWT — do not
include a `sender_id` in the body.

Before calling this endpoint for the first time between two students, obtain a `conversation_id`
by calling the Supabase RPC `get_or_create_student_conversation` (see [Supabase RPC](#supabase-rpc)).

#### Request body

```json
{
  "conversation_id": "uuid-string",
  "content": "string"
}
```

| Field | Required | Notes |
|---|---|---|
| `conversation_id` | Always | UUID of the target `app.conversations` row |
| `content` | Always | Message text |

#### Success response — `200 OK`

```json
{ "status": "sent" }
```

After a successful send the backend also updates `app.conversations.last_message_at` (best-effort, non-fatal if it fails).

#### Error responses

| Status | When |
|---|---|
| `401` | Missing or invalid JWT |
| `429` | Messaging rate limit exceeded |

**Messaging rate limits** (keyed to the authenticated user):

| Req/min | Daily chars | Monthly chars |
|---|---|---|
| 20 | 10,000 | 200,000 |

The character count of `content` is used as the token proxy for messaging limits.

---

### 3. Calls / Video

**`POST /calls/token`**

Create a video call token. The backend selects the active vendor (Daily.co or LiveKit) automatically
from its database config, so the client does **not** need to know which SDK is active at any given
time. The caller identity is taken from the JWT.

#### Request body

```json
{
  "room": "string",
  "vendor": "daily" | "livekit"
}
```

| Field | Required | Notes |
|---|---|---|
| `room` | Always | Room name string (alphanumeric + hyphens recommended) |
| `vendor` | No | Omit to let the server decide from its DB config. Override only when explicitly needed. |

**Vendor resolution order:**
1. `vendor` field in request (if provided)
2. Active row in `app.video_vendor_config` database table
3. Defaults to `"daily"` if neither is set

#### Success response — `200 OK`

```json
{
  "token": "string",
  "room": "string",
  "room_url": "https://..." | null,
  "vendor": "daily" | "livekit"
}
```

| Field | Type | Notes |
|---|---|---|
| `token` | `string` | Meeting token (Daily) or signed JWT (LiveKit) |
| `room` | `string` | Echoes the requested room name |
| `room_url` | `string \| null` | Full room URL for Daily.co; `null` for LiveKit |
| `vendor` | `string` | The vendor that was actually used |

**Flutter integration notes by vendor:**

- **Daily.co**: Use the `daily_flutter` SDK or a WebView pointed at `room_url`. Pass `token` as the meeting token.
- **LiveKit**: Use the `livekit_client` Flutter SDK. Connect to your LiveKit server URL with the returned `token`. `room_url` will be `null`; your LiveKit server WebSocket URL must be configured client-side or fetched from `app.video_vendor_config.metadata->>'ws_url'` via Supabase.

#### Error responses

| Status | When |
|---|---|
| `401` | Missing or invalid JWT |
| `502` | Upstream vendor API (Daily.co) failed |

---

## Supabase RPC

Some operations are performed directly against Supabase from the Flutter client, not through this API.
These calls use the Supabase anon key and are governed by Supabase RLS policies.

### `get_or_create_student_conversation`

Returns the `conversation_id` UUID for a student-to-student conversation, creating it if it does
not exist. Both students must share at least one class or this call raises an exception.

```dart
final result = await supabase.rpc(
  'get_or_create_student_conversation',
  params: {
    'student1_id': supabase.auth.currentUser!.id,
    'student2_id': otherUserId,
  },
);
final conversationId = result as String;
```

Call this once before the first `POST /messages/send` between two users, then cache the returned
`conversation_id` for the lifetime of the conversation.

---

## Error Handling

All errors return a JSON body:

```json
{ "detail": "Human-readable reason string" }
```

Handle these status codes universally across all routes:

| Code | Meaning | Recommended Flutter behavior |
|---|---|---|
| `400` | Bad request (invalid combination) | Show error to user, do not retry |
| `401` | JWT missing, expired, or invalid | Refresh token and retry once; redirect to login if refresh fails |
| `422` | Validation error (missing required field) | Fix request, do not retry |
| `429` | Rate limited | Show "try again" message; parse `detail` to distinguish req/min vs daily/monthly |
| `502` | Upstream vendor failure | Retry with exponential backoff; show degraded-service message |
| `5xx` | Server error | Retry with backoff; log for diagnostics |

---

## Data Models (Dart reference)

```dart
// POST /ai/generate
class AIRequest {
  final String prompt;
  final String provider;        // "openai" | "deepseek" | "gemini"
  final String mode;            // "chat" | "translation" | "image_translation"
  final String? targetLanguage;
  final String? imageUrl;
  // No userId — extracted from JWT server-side
}

class AIResponse {
  final String response;
  final int tokens;
  final String provider;
  final String mode;
}

// POST /messages/send
class MessageRequest {
  final String conversationId;
  final String content;
  // No senderId — extracted from JWT server-side
}

// POST /calls/token
class CallRequest {
  final String room;
  final String? vendor;         // omit to use server config
  // No userId — extracted from JWT server-side
}

class CallResponse {
  final String token;
  final String room;
  final String? roomUrl;        // null for LiveKit
  final String vendor;
}
```

---

## Environment Notes for Flutter

- **Local dev**: The backend runs at `http://localhost:8000`. On Android emulator use `http://10.0.2.2:8000`. On a physical device, use your machine's local IP.
- **Supabase**: Initialise `supabase_flutter` with the project URL and **anon key** (not the service key). The anon key is safe for client use; the service key must stay server-side only.
- **`SUPABASE_JWT_SECRET`**: Must be set in the backend `.env`. Find it in your Supabase dashboard under **Settings → API → JWT Secret**. Do not expose it to the client.
- **LiveKit** (if active vendor): The client needs the LiveKit server WebSocket URL separately. Read it from `app.video_vendor_config.metadata->>'ws_url'` via a Supabase query, or configure it per environment in your Flutter app constants.
