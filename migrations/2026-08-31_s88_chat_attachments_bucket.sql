-- s88 — the chat-attachments bucket, which the client has been uploading to
-- since before it existed.
--
-- Owner: tauka-flutter (messaging domain).
-- Raised by: desktop local-storage security review, 2026-08-31.
--
-- APPLIED 2026-08-31. Verified after applying: the bucket is private with a
-- 25 MB limit; `app.chat_attachment_conversation()` returns NULL for a path with
-- no slash, a non-uuid first segment, an empty string, a traversal attempt, or a
-- truncated uuid, and resolves only a well-formed one; a conversation
-- participant passes the read predicate, a non-participant does not, and a
-- participant probing a conversation they are not in does not.
--
-- WHY
-- `SupabaseService.uploadChatAttachment` calls
-- `storage.from('chat-attachments').uploadBinary(...)` against a bucket that
-- was never created, so every image or file a user attached to a chat has been
-- failing with "Bucket not found" for as long as the feature has existed. It
-- then called `getPublicUrl`, which — had the bucket existed as a public one —
-- would have made every attachment readable by anyone who learned or guessed
-- the path. Creating it public would have shipped that second bug for real.
--
-- Private, and scoped to the conversation the object belongs to, using the same
-- `app.is_conversation_participant()` predicate that guards `app.messages`.

insert into storage.buckets (id, name, public, file_size_limit)
values ('chat-attachments', 'chat-attachments', false, 26214400)
on conflict (id) do update
  set public = false,
      file_size_limit = excluded.file_size_limit;

-- Objects are `<conversation_id>/<timestamp>_<filename>`. A path whose first
-- segment is not a uuid yields NULL, and every policy below then fails closed:
-- an object nobody can attribute to a conversation is an object nobody reads.
create or replace function app.chat_attachment_conversation(p_name text)
returns uuid
language plpgsql
immutable
as $function$
begin
  return split_part(p_name, '/', 1)::uuid;
exception when others then
  return null;
end;
$function$;

comment on function app.chat_attachment_conversation(text) is
  'Conversation id from a chat-attachments object path, or NULL if the path is not shaped that way. Fails closed by design.';

grant execute on function app.chat_attachment_conversation(text) to authenticated;

drop policy if exists "chat_attachments_read"   on storage.objects;
drop policy if exists "chat_attachments_insert" on storage.objects;
drop policy if exists "chat_attachments_delete" on storage.objects;

create policy "chat_attachments_read"
  on storage.objects for select to authenticated
  using (
    bucket_id = 'chat-attachments'
    and app.chat_attachment_conversation(name) is not null
    and app.is_conversation_participant(app.chat_attachment_conversation(name))
  );

create policy "chat_attachments_insert"
  on storage.objects for insert to authenticated
  with check (
    bucket_id = 'chat-attachments'
    and owner = auth.uid()
    and app.chat_attachment_conversation(name) is not null
    and app.is_conversation_participant(app.chat_attachment_conversation(name))
  );

-- Deleting is the uploader's own business. There is deliberately no UPDATE
-- policy: replacing an attachment in place would silently change what an
-- already-delivered message shows, which is not something a chat should allow.
create policy "chat_attachments_delete"
  on storage.objects for delete to authenticated
  using (
    bucket_id = 'chat-attachments'
    and owner = auth.uid()
  );

-- ROLLBACK
--   drop policy if exists "chat_attachments_read"   on storage.objects;
--   drop policy if exists "chat_attachments_insert" on storage.objects;
--   drop policy if exists "chat_attachments_delete" on storage.objects;
--   drop function if exists app.chat_attachment_conversation(text);
--   delete from storage.buckets where id = 'chat-attachments';  -- only if empty
