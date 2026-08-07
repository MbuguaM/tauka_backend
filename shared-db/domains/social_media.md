# Domain: Social & Media

> **Owner project:** tauka-flutter
> **Last updated by:** tauka-flutter on 2026-05-26
> **Spec sections:** §5.5 Explore Branch, §2.5 Branch Contents (Explore)

---

## 1. Business Context

### What real-world problem does this domain solve?
Immersive content — YouTube music videos, song lyrics with word-level annotations — is one of the most engaging ways to learn a language. This domain stores the Explore feed of curated YouTube playlists and videos, and the associated lyric annotation files. It also contains legacy tables (langexchange, social, tk_tokens, tk_video) that are not currently active.

### How does this domain fit into the larger system?
`yt_playlist` and `yt_video` power the Explore branch. The Flutter app loads playlists and their videos, uses the YouTube Data API v3 to fetch metadata, and displays videos with lyrics synced to `app.lyrics.json_file_path`. Tutors can submit playlists for admin review (§B5 status workflow). The `langexchange` table is a legacy table predating the full [[broadcast]] language exchange system; new code should use `app.language_exchange_match`.

---

## 2. Design Decisions

### Key invariants
- `yt_playlist.user_id` and `yt_video.playlist_id` have a DEFAULT gen_random_uuid() bug (D6 / CONFLICT-007). Always supply these values explicitly on INSERT until the bug is fixed.
- `app.langexchange.user_a` and `user_b` store UUID strings as TEXT (not uuid type) — a schema quirk; cast with `auth.uid()::text` in queries.
- `yt_playlist.status` controls the tutor submission workflow: 'pending' → admin review → 'published' or 'rejected'.

---

## 3. Tables

### Table: yt_playlist

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| created_at | timestamptz | — |
| user_id | uuid | FK → auth.users(id); **WARNING: DEFAULT gen_random_uuid() bug — always supply explicitly** |
| admin_id | uuid | FK → admin_users(id) — which admin curated this |
| title | text | Playlist display name |
| thumbnail_url | text | Cover image |
| status | text | published/pending/rejected (§B5) |
| submitted_by | uuid | FK → user_profiles(id) — tutor who submitted |
| reviewed_by | uuid | FK → user_profiles(id) — admin reviewer |
| reviewed_at | timestamptz | Review timestamp |

### Table: yt_video

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| created_at | timestamptz | — |
| video_id | text | YouTube video ID (e.g., 'dQw4w9WgXcQ') |
| json_file_url | text | URL to video metadata JSON |
| playlist_id | uuid | FK → yt_playlist(id); **WARNING: DEFAULT gen_random_uuid() bug** |
| video_title | text | Display title |
| thumbnail_url | text | Thumbnail URL |
| views_count | text | Display view count (stored as text for display formatting) |
| channel_name | text | YouTube channel name |
| lyric_file | text | Partial path to lyric annotation file |

### Table: lyrics

| Column | Type | Purpose |
|---|---|---|
| id | uuid | PK |
| video_id | uuid | FK → yt_video(id) ON DELETE CASCADE |
| json_file_path | text | Storage path to lyric annotation JSON |
| title | text | Song title |
| artist | text | Artist name |
| created_at | timestamptz | — |
| created_by | uuid | FK → auth.users(id) |
| deleted_at | timestamptz | Soft delete |

### Table: langexchange (LEGACY)
Not actively used. Superseded by `app.language_exchange_match` (see [[broadcast]] domain). `user_a` and `user_b` are TEXT columns storing UUID strings — a typing inconsistency.

### Tables: social, tk_tokens, tk_video (UNUSED)
`social`: no known business purpose. `tk_tokens`/`tk_video`: TikTok integration, explicitly commented out. Remove from active development; candidates for DROP in a future cleanup migration.

---

## 4. Extension Rules

#### If you need to add metadata to a video
Add a column to `yt_video`. Do NOT create a `yt_video_metadata` satellite table.

#### Specifically do NOT
- Do NOT use `app.langexchange` for new language exchange matching — use `app.language_exchange_match`.
- Do NOT fix the `DEFAULT gen_random_uuid()` bug inline in application code — fix it at the DB level via Amendment A6.
- Do NOT build features on `social`, `tk_tokens`, or `tk_video` until TikTok integration is explicitly re-scoped.
