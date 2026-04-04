-- =====================================================================
-- CONSOLIDATED DATABASE SCHEMA WITH PRODUCTION FIXES
-- All tables in app schema with RLS policies, soft deletes, and indexes
-- =====================================================================

-- Enable UUID extension if not already enabled
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- =====================================================================
-- CORE TABLES
-- =====================================================================

-- Issue #9: Added deleted_at for soft deletes
-- User Profiles (extending auth.users)
CREATE TABLE app.user_profiles (
  id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  first_name text NOT NULL,
  last_name text NULL,
  avatar_url text NULL,
  location text NULL,
  phone_number text NULL,
  deleted_at timestamp with time zone NULL,
  created_at timestamp with time zone DEFAULT now()
);

-- Issue #16: RLS Policy for user_profiles
ALTER TABLE app.user_profiles ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view their own profile"
  ON app.user_profiles FOR SELECT
  USING (auth.uid() = id OR deleted_at IS NULL);

CREATE POLICY "Users can update their own profile"
  ON app.user_profiles FOR UPDATE
  USING (auth.uid() = id AND deleted_at IS NULL);

CREATE POLICY "Users can insert their own profile"
  ON app.user_profiles FOR INSERT
  WITH CHECK (auth.uid() = id);

-- Issue #7: Index for frequently queried columns
CREATE INDEX idx_user_profiles_deleted_at ON app.user_profiles(deleted_at) WHERE deleted_at IS NULL;

-- =====================================================================

-- Issue #9: Added deleted_at for soft deletes
-- Admin Users
CREATE TABLE app.admin_users (
  id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  role character varying(50) DEFAULT 'admin',
  permissions jsonb DEFAULT '{"users": true, "content": true, "settings": true}'::jsonb,
  created_at timestamp with time zone DEFAULT now(),
  created_by uuid REFERENCES auth.users(id),
  updated_at timestamp with time zone DEFAULT now(),
  deleted_at timestamp with time zone NULL
);

-- Issue #16: RLS Policy for admin_users
ALTER TABLE app.admin_users ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Only admins can view admin users"
  ON app.admin_users FOR SELECT
  USING (
    auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL)
  );

CREATE POLICY "Only admins can manage admin users"
  ON app.admin_users FOR ALL
  USING (
    auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL)
  );

-- Issue #7: Index for admin queries
CREATE INDEX idx_admin_users_deleted_at ON app.admin_users(deleted_at) WHERE deleted_at IS NULL;

-- =====================================================================

-- Issue #9: Added deleted_at for soft deletes
-- Classes
CREATE TABLE app.classes (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  name text NOT NULL,
  description text NULL,
  created_at timestamp with time zone DEFAULT now(),
  created_by uuid REFERENCES auth.users(id),
  deleted_at timestamp with time zone NULL
);

-- Issue #16: RLS Policy for classes
ALTER TABLE app.classes ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view classes they are members of"
  ON app.classes FOR SELECT
  USING (
    deleted_at IS NULL AND (
      id IN (
        SELECT class_id FROM app.class_members 
        WHERE user_id = auth.uid() AND deleted_at IS NULL
      )
      OR created_by = auth.uid()
      OR auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL)
    )
  );

CREATE POLICY "Teachers and admins can create classes"
  ON app.classes FOR INSERT
  WITH CHECK (
    auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL)
    OR auth.uid() IN (
      SELECT user_id FROM app.class_members WHERE role = 'teacher' AND deleted_at IS NULL
    )
  );

CREATE POLICY "Creators and admins can update classes"
  ON app.classes FOR UPDATE
  USING (
    deleted_at IS NULL AND (
      created_by = auth.uid()
      OR auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL)
    )
  );

-- Issue #7: Indexes for frequently queried foreign keys
CREATE INDEX idx_classes_created_by ON app.classes(created_by);
CREATE INDEX idx_classes_deleted_at ON app.classes(deleted_at) WHERE deleted_at IS NULL;

-- =====================================================================

-- Issue #8: Changed date columns to timestamp with time zone for consistency
-- Issue #9: Added deleted_at for soft deletes
-- Subclasses
CREATE TABLE app.subclasses (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  class_id uuid REFERENCES app.classes(id) ON DELETE CASCADE,
  name text NOT NULL,
  description text NULL,
  start_date timestamp with time zone NOT NULL,
  end_date timestamp with time zone NOT NULL,
  deleted_at timestamp with time zone NULL,
  CHECK (end_date > start_date)
);

-- Issue #16: RLS Policy for subclasses
ALTER TABLE app.subclasses ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view subclasses of their classes"
  ON app.subclasses FOR SELECT
  USING (
    deleted_at IS NULL AND
    class_id IN (
      SELECT class_id FROM app.class_members 
      WHERE user_id = auth.uid() AND deleted_at IS NULL
    )
  );

CREATE POLICY "Teachers can manage subclasses"
  ON app.subclasses FOR ALL
  USING (
    deleted_at IS NULL AND (
      class_id IN (
        SELECT class_id FROM app.class_members 
        WHERE user_id = auth.uid() AND role = 'teacher' AND deleted_at IS NULL
      )
      OR auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL)
    )
  );

-- Issue #7: Indexes for frequently queried columns
CREATE INDEX idx_subclasses_class_id ON app.subclasses(class_id);
-- Issue #22: Composite index for active subclass queries
CREATE INDEX idx_subclasses_dates ON app.subclasses(class_id, start_date, end_date) 
  WHERE deleted_at IS NULL;
CREATE INDEX idx_subclasses_deleted_at ON app.subclasses(deleted_at) WHERE deleted_at IS NULL;

-- =====================================================================

-- Issue #9: Added deleted_at for soft deletes
-- Class Members (students and teachers)
CREATE TABLE app.class_members (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  class_id uuid REFERENCES app.classes(id) ON DELETE CASCADE,
  subclass_id uuid REFERENCES app.subclasses(id) ON DELETE SET NULL,
  user_id uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  role text NOT NULL CHECK (role IN ('teacher', 'student')),
  joined_at timestamp with time zone DEFAULT now(),
  deleted_at timestamp with time zone NULL,
  UNIQUE(class_id, user_id)
);

-- Issue #16: RLS Policy for class_members
ALTER TABLE app.class_members ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view class members of their classes"
  ON app.class_members FOR SELECT
  USING (
    deleted_at IS NULL AND (
      class_id IN (
        SELECT class_id FROM app.class_members 
        WHERE user_id = auth.uid() AND deleted_at IS NULL
      )
      OR auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL)
    )
  );

CREATE POLICY "Teachers and admins can manage class members"
  ON app.class_members FOR ALL
  USING (
    deleted_at IS NULL AND (
      class_id IN (
        SELECT class_id FROM app.class_members 
        WHERE user_id = auth.uid() AND role = 'teacher' AND deleted_at IS NULL
      )
      OR auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL)
    )
  );

-- Issue #7: Indexes for frequently queried foreign keys
CREATE INDEX idx_class_members_class_id ON app.class_members(class_id);
CREATE INDEX idx_class_members_user_id ON app.class_members(user_id);
CREATE INDEX idx_class_members_subclass_id ON app.class_members(subclass_id);
CREATE INDEX idx_class_members_role ON app.class_members(role);
CREATE INDEX idx_class_members_deleted_at ON app.class_members(deleted_at) WHERE deleted_at IS NULL;

-- =====================================================================

-- Issue #9: Added deleted_at for soft deletes
-- Conversations (for all message types)
CREATE TABLE app.conversations (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  type text NOT NULL CHECK (type IN ('student_to_student', 'teacher_to_student', 'class_group', 'admin_broadcast')),
  class_id uuid REFERENCES app.classes(id) ON DELETE SET NULL,
  created_at timestamp with time zone DEFAULT now(),
  last_message_at timestamp with time zone,
  deleted_at timestamp with time zone NULL
);

-- Issue #16: RLS Policy for conversations
ALTER TABLE app.conversations ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view their conversations"
  ON app.conversations FOR SELECT
  USING (
    deleted_at IS NULL AND (
      id IN (
        SELECT conversation_id FROM app.conversation_participants 
        WHERE user_id = auth.uid() AND deleted_at IS NULL
      )
      OR auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL)
    )
  );

CREATE POLICY "Users can create conversations"
  ON app.conversations FOR INSERT
  WITH CHECK (auth.uid() IS NOT NULL);

-- Issue #7: Indexes for conversations
CREATE INDEX idx_conversations_class_id ON app.conversations(class_id);
CREATE INDEX idx_conversations_type ON app.conversations(type);
CREATE INDEX idx_conversations_deleted_at ON app.conversations(deleted_at) WHERE deleted_at IS NULL;

-- =====================================================================

-- Issue #9: Added deleted_at for soft deletes
-- Conversation Participants
CREATE TABLE app.conversation_participants (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  conversation_id uuid REFERENCES app.conversations(id) ON DELETE CASCADE,
  user_id uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  joined_at timestamp with time zone DEFAULT now(),
  deleted_at timestamp with time zone NULL,
  UNIQUE(conversation_id, user_id)
);

-- Issue #16: RLS Policy for conversation_participants
ALTER TABLE app.conversation_participants ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view participants of their conversations"
  ON app.conversation_participants FOR SELECT
  USING (
    deleted_at IS NULL AND (
      conversation_id IN (
        SELECT conversation_id FROM app.conversation_participants 
        WHERE user_id = auth.uid() AND deleted_at IS NULL
      )
      OR auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL)
    )
  );

-- Issue #7: Indexes for conversation participants
CREATE INDEX idx_conversation_participants_conversation_id ON app.conversation_participants(conversation_id);
CREATE INDEX idx_conversation_participants_user_id ON app.conversation_participants(user_id);
CREATE INDEX idx_conversation_participants_deleted_at ON app.conversation_participants(deleted_at) 
  WHERE deleted_at IS NULL;

-- =====================================================================

-- Issue #9: Added deleted_at for soft deletes
-- Messages
CREATE TABLE app.messages (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  conversation_id uuid REFERENCES app.conversations(id) ON DELETE CASCADE,
  sender_id uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  content text NOT NULL,
  created_at timestamp with time zone DEFAULT now(),
  deleted_at timestamp with time zone NULL
);

-- Issue #16: RLS Policy for messages
ALTER TABLE app.messages ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view messages in their conversations"
  ON app.messages FOR SELECT
  USING (
    deleted_at IS NULL AND
    conversation_id IN (
      SELECT conversation_id FROM app.conversation_participants 
      WHERE user_id = auth.uid() AND deleted_at IS NULL
    )
  );

CREATE POLICY "Users can send messages to their conversations"
  ON app.messages FOR INSERT
  WITH CHECK (
    conversation_id IN (
      SELECT conversation_id FROM app.conversation_participants 
      WHERE user_id = auth.uid() AND deleted_at IS NULL
    )
  );

-- Issue #7: Indexes for messages (critical for performance)
CREATE INDEX idx_messages_conversation_id ON app.messages(conversation_id);
CREATE INDEX idx_messages_sender_id ON app.messages(sender_id);
CREATE INDEX idx_messages_created_at ON app.messages(created_at DESC);
CREATE INDEX idx_messages_deleted_at ON app.messages(deleted_at) WHERE deleted_at IS NULL;

-- =====================================================================

-- Message Read Receipts
CREATE TABLE app.message_read_receipts (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  message_id uuid REFERENCES app.messages(id) ON DELETE CASCADE,
  user_id uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  read_at timestamp with time zone,
  UNIQUE(message_id, user_id)
);

-- Issue #16: RLS Policy for message_read_receipts
ALTER TABLE app.message_read_receipts ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can manage their own read receipts"
  ON app.message_read_receipts FOR ALL
  USING (user_id = auth.uid());

-- Issue #7: Indexes for read receipts
CREATE INDEX idx_message_read_receipts_message_id ON app.message_read_receipts(message_id);
CREATE INDEX idx_message_read_receipts_user_id ON app.message_read_receipts(user_id);

-- =====================================================================

-- Issue #9: Added deleted_at for soft deletes
-- Notifications
CREATE TABLE app.notifications (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  subclass_id uuid REFERENCES app.subclasses(id) ON DELETE CASCADE,
  sender_id uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  title text NOT NULL,
  content text NOT NULL,
  created_at timestamp with time zone DEFAULT now(),
  is_urgent boolean DEFAULT false,
  deleted_at timestamp with time zone NULL
);

-- Issue #16: RLS Policy for notifications
ALTER TABLE app.notifications ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view notifications for their subclasses"
  ON app.notifications FOR SELECT
  USING (
    deleted_at IS NULL AND
    subclass_id IN (
      SELECT subclass_id FROM app.class_members 
      WHERE user_id = auth.uid() AND deleted_at IS NULL
    )
  );

CREATE POLICY "Teachers can create notifications"
  ON app.notifications FOR INSERT
  WITH CHECK (
    subclass_id IN (
      SELECT subclass_id FROM app.class_members 
      WHERE user_id = auth.uid() AND role = 'teacher' AND deleted_at IS NULL
    )
  );

-- Issue #7: Indexes for notifications
CREATE INDEX idx_notifications_subclass_id ON app.notifications(subclass_id);
CREATE INDEX idx_notifications_sender_id ON app.notifications(sender_id);
CREATE INDEX idx_notifications_created_at ON app.notifications(created_at DESC);
CREATE INDEX idx_notifications_deleted_at ON app.notifications(deleted_at) WHERE deleted_at IS NULL;

-- =====================================================================

-- Notification Receipts
CREATE TABLE app.notification_receipts (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  notification_id uuid REFERENCES app.notifications(id) ON DELETE CASCADE,
  user_id uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  read_at timestamp with time zone,
  UNIQUE(notification_id, user_id)
);

-- Issue #16: RLS Policy for notification_receipts
ALTER TABLE app.notification_receipts ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can manage their own notification receipts"
  ON app.notification_receipts FOR ALL
  USING (user_id = auth.uid());

-- Issue #7: Indexes for notification receipts
CREATE INDEX idx_notification_receipts_notification_id ON app.notification_receipts(notification_id);
CREATE INDEX idx_notification_receipts_user_id ON app.notification_receipts(user_id);
CREATE INDEX idx_notification_receipts_unread ON app.notification_receipts(user_id, read_at) 
  WHERE read_at IS NULL;

-- =====================================================================

-- Languages
CREATE TABLE app.languages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code character varying(10) NOT NULL UNIQUE,
  name character varying(100) NOT NULL,
  created_at timestamp with time zone DEFAULT now()
);

-- Issue #16: RLS Policy for languages (public read)
ALTER TABLE app.languages ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Anyone can view languages"
  ON app.languages FOR SELECT
  USING (true);

CREATE POLICY "Only admins can manage languages"
  ON app.languages FOR ALL
  USING (
    auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL)
  );

-- =====================================================================

-- Issue #9: Added deleted_at for soft deletes
-- User Languages
CREATE TABLE app.user_languages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  language_id uuid REFERENCES app.languages(id) ON DELETE CASCADE,
  type character varying(20) NOT NULL CHECK (type IN ('known', 'target')),
  proficiency character varying(50),
  target_proficiency character varying(50),
  current_proficiency character varying(50),
  is_primary boolean DEFAULT false,
  learning_since date,
  created_at timestamp with time zone DEFAULT now(),
  deleted_at timestamp with time zone NULL,
  UNIQUE(user_id, language_id, type)
);

-- Issue #16: RLS Policy for user_languages
ALTER TABLE app.user_languages ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can manage their own languages"
  ON app.user_languages FOR ALL
  USING (user_id = auth.uid() AND deleted_at IS NULL);

CREATE POLICY "Others can view user languages"
  ON app.user_languages FOR SELECT
  USING (deleted_at IS NULL);

-- Issue #7: Indexes for user languages
CREATE INDEX idx_user_languages_user_id ON app.user_languages(user_id);
CREATE INDEX idx_user_languages_language_id ON app.user_languages(language_id);
CREATE INDEX idx_user_languages_type ON app.user_languages(type);

-- =====================================================================

-- Course
CREATE TABLE app.course (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  title text,
  speaker_for text,
  unit_count bigint,
  dict_url text[],
  cover_url text,
  deleted_at timestamp with time zone NULL
);

-- Issue #16: RLS Policy for courses
ALTER TABLE app.course ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Anyone can view courses"
  ON app.course FOR SELECT
  USING (deleted_at IS NULL);

CREATE POLICY "Only admins can manage courses"
  ON app.course FOR ALL
  USING (
    auth.uid() IN (SELECT id FROM app.admin_users WHERE deleted_at IS NULL)
  );

CREATE INDEX idx_course_deleted_at ON app.course(deleted_at) WHERE deleted_at IS NULL;

-- =====================================================================

-- Additional tables from full_database_definitions.sql

CREATE TABLE app.langexchange (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  lang_a text,
  lang_b text,
  user_a text,
  user_b text,
  deleted_at timestamp with time zone NULL
);

ALTER TABLE app.langexchange ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view their own language exchanges"
  ON app.langexchange FOR SELECT
  USING (deleted_at IS NULL AND (user_a = auth.uid()::text OR user_b = auth.uid()::text));

-- =====================================================================

CREATE TABLE app.student (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  is_con_creator text,
  account_status text DEFAULT 'active',
  tutor_id uuid DEFAULT gen_random_uuid(),
  deleted_at timestamp with time zone NULL
);

ALTER TABLE app.student ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Students can view their own record"
  ON app.student FOR SELECT
  USING (deleted_at IS NULL AND id = auth.uid());

-- =====================================================================

CREATE TABLE app.student_course (
  student_id uuid REFERENCES app.student(id) ON DELETE CASCADE,
  course_id uuid REFERENCES app.course(id) ON DELETE CASCADE,
  level bigint,
  PRIMARY KEY (student_id, course_id)
);

ALTER TABLE app.student_course ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Students can view their courses"
  ON app.student_course FOR SELECT
  USING (student_id = auth.uid());

-- =====================================================================

CREATE TABLE app.tutor (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  rating bigint,
  status text DEFAULT 'active',
  user_id uuid REFERENCES auth.users(id),
  deleted_at timestamp with time zone NULL
);

ALTER TABLE app.tutor ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Anyone can view active tutors"
  ON app.tutor FOR SELECT
  USING (deleted_at IS NULL AND status = 'active');

-- =====================================================================

CREATE TABLE app.tutor_course (
  student_id uuid REFERENCES app.tutor(id) ON DELETE CASCADE,
  course_id uuid REFERENCES app.course(id) ON DELETE CASCADE,
  student_rating bigint,
  PRIMARY KEY (student_id, course_id)
);

ALTER TABLE app.tutor_course ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Anyone can view tutor courses"
  ON app.tutor_course FOR SELECT
  USING (true);

-- =====================================================================

CREATE TABLE app.unit (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  title text,
  json_file text,
  audio_folder text,
  course_id uuid REFERENCES app.course(id),
  deleted_at timestamp with time zone NULL
);

ALTER TABLE app.unit ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Anyone can view units"
  ON app.unit FOR SELECT
  USING (deleted_at IS NULL);

CREATE INDEX idx_unit_course_id ON app.unit(course_id);

-- =====================================================================

CREATE TABLE app.progress (
  id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  updated_at timestamp with time zone NOT NULL DEFAULT now(),
  status boolean NOT NULL DEFAULT true,
  prog_file text,
  inprogress boolean DEFAULT false,
  unit_id bigint,
  user_id uuid REFERENCES auth.users(id) ON UPDATE CASCADE ON DELETE CASCADE DEFAULT gen_random_uuid()
);

ALTER TABLE app.progress ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can manage their own progress"
  ON app.progress FOR ALL
  USING (user_id = auth.uid());

-- =====================================================================

-- Other supporting tables with basic RLS

CREATE TABLE app.social (
  id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  account text NOT NULL,
  user_name text,
  permissioned boolean
);

CREATE TABLE app.tk_tokens (
  id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  auth_token text,
  auth_toke_exp timestamp without time zone,
  refresh_token text,
  refresh_token_exp timestamp without time zone,
  user_id uuid REFERENCES app.user_profiles(id) ON DELETE CASCADE
);

CREATE TABLE app.tk_video (
  id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  video_id text,
  creator_id text,
  txt_file text,
  feature_ref bigint
);

CREATE TABLE app.yt_playlist (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  playlist_id text,
  user_id uuid REFERENCES auth.users(id) DEFAULT gen_random_uuid(),
  admin_id uuid REFERENCES app.admin_users(id) ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE app.yt_video (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  video_id text,
  json_file_url text,
  playlist_id uuid REFERENCES app.yt_playlist(id) ON UPDATE CASCADE ON DELETE CASCADE DEFAULT gen_random_uuid()
);

-- =====================================================================
-- DATABASE FUNCTIONS (Issue #12: RPC functions for transactional operations)
-- =====================================================================

-- Issue #18: Helper function to validate UUID
CREATE OR REPLACE FUNCTION app.is_valid_uuid(uuid_string text)
RETURNS boolean
LANGUAGE plpgsql
AS $$
BEGIN
  PERFORM uuid_string::uuid;
  RETURN true;
EXCEPTION WHEN invalid_text_representation THEN
  RETURN false;
END;
$$;

-- Check if two students share at least one class (respects soft deletes)
CREATE OR REPLACE FUNCTION app.students_share_class(student1_id uuid, student2_id uuid)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
AS $$
  SELECT EXISTS (
    SELECT 1
    FROM app.class_members cm1
    JOIN app.class_members cm2 ON cm1.class_id = cm2.class_id
    WHERE cm1.user_id = student1_id
      AND cm2.user_id = student2_id
      AND cm1.role = 'student'
      AND cm2.role = 'student'
      AND cm1.deleted_at IS NULL
      AND cm2.deleted_at IS NULL
  );
$$;

-- Issue #13: Fixed RPC to return scalar UUID instead of table
-- Issue #12: Transactional conversation creation
CREATE OR REPLACE FUNCTION app.get_or_create_student_conversation(
  student1_id uuid, 
  student2_id uuid
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_conversation_id uuid;
BEGIN
  -- Issue #18: Validate UUIDs
  IF NOT app.is_valid_uuid(student1_id::text) THEN
    RAISE EXCEPTION 'Invalid UUID for student1_id';
  END IF;
  IF NOT app.is_valid_uuid(student2_id::text) THEN
    RAISE EXCEPTION 'Invalid UUID for student2_id';
  END IF;

  -- Verify they share at least one class
  IF NOT app.students_share_class(student1_id, student2_id) THEN
    RAISE EXCEPTION 'Students do not share any classes';
  END IF;
  
  -- Check for existing conversation
  SELECT c.id INTO v_conversation_id
  FROM app.conversations c
  JOIN app.conversation_participants cp1 ON c.id = cp1.conversation_id
  JOIN app.conversation_participants cp2 ON c.id = cp2.conversation_id
  WHERE c.type = 'student_to_student'
    AND c.deleted_at IS NULL
    AND cp1.user_id = student1_id
    AND cp2.user_id = student2_id
    AND cp1.deleted_at IS NULL
    AND cp2.deleted_at IS NULL
  LIMIT 1;
  
  -- If not found, create new conversation (transactional)
  IF v_conversation_id IS NULL THEN
    INSERT INTO app.conversations (type) 
    VALUES ('student_to_student')
    RETURNING id INTO v_conversation_id;
    
    -- Add both students
    INSERT INTO app.conversation_participants (conversation_id, user_id)
    VALUES
      (v_conversation_id, student1_id),
      (v_conversation_id, student2_id);
  END IF;
  
  RETURN v_conversation_id;
END;
$$;

-- Issue #12: Transactional admin broadcast creation
CREATE OR REPLACE FUNCTION app.add_all_students_to_conversation(p_conversation_id uuid)
RETURNS void
LANGUAGE sql
SECURITY DEFINER
AS $$
  INSERT INTO app.conversation_participants (conversation_id, user_id)
  SELECT p_conversation_id, cm.user_id
  FROM app.class_members cm
  WHERE cm.role = 'student'
    AND cm.deleted_at IS NULL
  ON CONFLICT (conversation_id, user_id) DO NOTHING;
$$;

-- Check if teacher and student share a subclass (respects soft deletes)
CREATE OR REPLACE FUNCTION app.teacher_student_share_subclass(
  p_teacher_id uuid, 
  p_student_id uuid
)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
AS $$
  SELECT EXISTS (
    SELECT 1
    FROM app.class_members cm1
    JOIN app.class_members cm2 
      ON cm1.subclass_id = cm2.subclass_id
      AND cm1.class_id = cm2.class_id
    JOIN app.subclasses s ON s.id = cm1.subclass_id
    WHERE cm1.user_id = p_teacher_id
      AND cm2.user_id = p_student_id
      AND cm1.role = 'teacher'
      AND cm2.role = 'student'
      AND cm1.deleted_at IS NULL
      AND cm2.deleted_at IS NULL
      AND s.deleted_at IS NULL
      AND CURRENT_DATE BETWEEN s.start_date AND s.end_date
  );
$$;

-- Issue #12: Transactional notification creation with receipts
-- Issue #17: Added admin check for sensitive operations
CREATE OR REPLACE FUNCTION app.create_subclass_notification(
  p_subclass_id uuid,
  p_sender_id uuid,
  p_title text,
  p_content text,
  p_is_urgent boolean
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_notification_id uuid;
  v_is_admin boolean;
BEGIN
  -- Issue #18: Validate UUIDs
  IF NOT app.is_valid_uuid(p_subclass_id::text) THEN
    RAISE EXCEPTION 'Invalid UUID for subclass_id';
  END IF;
  IF NOT app.is_valid_uuid(p_sender_id::text) THEN
    RAISE EXCEPTION 'Invalid UUID for sender_id';
  END IF;

  -- Issue #17: Check if sender is admin
  SELECT EXISTS(
    SELECT 1 FROM app.admin_users 
    WHERE id = p_sender_id AND deleted_at IS NULL
  ) INTO v_is_admin;

  -- Verify sender is a teacher in this subclass OR is an admin
  IF NOT v_is_admin AND NOT EXISTS (
    SELECT 1 
    FROM app.class_members 
    WHERE user_id = p_sender_id 
      AND subclass_id = p_subclass_id
      AND role = 'teacher'
      AND deleted_at IS NULL
  ) THEN
    RAISE EXCEPTION 'Only teachers in this subclass or admins can send notifications';
  END IF;

  -- Create notification
  INSERT INTO app.notifications (
    subclass_id, sender_id, title, content, is_urgent
  )
  VALUES (
    p_subclass_id,
    p_sender_id,
    p_title,
    p_content,
    p_is_urgent
  )
  RETURNING id INTO v_notification_id;

  -- Create receipts for all students in subclass
  INSERT INTO app.notification_receipts (notification_id, user_id)
  SELECT v_notification_id, user_id
  FROM app.class_members
  WHERE subclass_id = p_subclass_id
    AND role = 'student'
    AND deleted_at IS NULL;

  RETURN v_notification_id;
END;
$;

-- Count unread messages (respects soft deletes)
CREATE OR REPLACE FUNCTION app.count_unread_messages(p_user_id uuid, p_conversation_id uuid)
RETURNS bigint
LANGUAGE sql
SECURITY DEFINER
AS $
  SELECT COUNT(*)
  FROM app.messages m
  WHERE m.conversation_id = p_conversation_id
    AND m.sender_id != p_user_id
    AND m.deleted_at IS NULL
    AND NOT EXISTS (
      SELECT 1 
      FROM app.message_read_receipts r
      WHERE r.message_id = m.id
        AND r.user_id = p_user_id
    );
$;

-- Issue #9: Soft delete function for any table
CREATE OR REPLACE FUNCTION app.soft_delete(
  p_table_name text,
  p_record_id uuid
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $
BEGIN
  EXECUTE format('UPDATE app.%I SET deleted_at = NOW() WHERE id = $1', p_table_name)
  USING p_record_id;
END;
$;

-- Issue #9: Restore soft-deleted record
CREATE OR REPLACE FUNCTION app.restore_deleted(
  p_table_name text,
  p_record_id uuid
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $
BEGIN
  EXECUTE format('UPDATE app.%I SET deleted_at = NULL WHERE id = $1', p_table_name)
  USING p_record_id;
END;
$;

-- Issue #12: Transactional class creation with creator as teacher
CREATE OR REPLACE FUNCTION app.create_class_with_teacher(
  p_name text,
  p_description text,
  p_creator_id uuid
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
AS $
DECLARE
  v_class_id uuid;
BEGIN
  -- Validate UUID
  IF NOT app.is_valid_uuid(p_creator_id::text) THEN
    RAISE EXCEPTION 'Invalid UUID for creator_id';
  END IF;

  -- Create class
  INSERT INTO app.classes (name, description, created_by)
  VALUES (p_name, p_description, p_creator_id)
  RETURNING id INTO v_class_id;
  
  -- Add creator as teacher
  INSERT INTO app.class_members (class_id, user_id, role)
  VALUES (v_class_id, p_creator_id, 'teacher');
  
  RETURN v_class_id;
END;
$;

-- Issue #10: Upsert conversation participant
CREATE OR REPLACE FUNCTION app.upsert_conversation_participant(
  p_conversation_id uuid,
  p_user_id uuid
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
AS $
DECLARE
  v_participant_id uuid;
BEGIN
  INSERT INTO app.conversation_participants (conversation_id, user_id)
  VALUES (p_conversation_id, p_user_id)
  ON CONFLICT (conversation_id, user_id) 
  DO UPDATE SET deleted_at = NULL
  RETURNING id INTO v_participant_id;
  
  RETURN v_participant_id;
END;
$;

-- Issue #14: Rate limiting table and function
CREATE TABLE app.rate_limits (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  action_type text NOT NULL,
  action_count integer DEFAULT 1,
  window_start timestamp with time zone DEFAULT now(),
  UNIQUE(user_id, action_type)
);

CREATE INDEX idx_rate_limits_user_action ON app.rate_limits(user_id, action_type);

-- Rate limit check function
CREATE OR REPLACE FUNCTION app.check_rate_limit(
  p_user_id uuid,
  p_action_type text,
  p_max_actions integer,
  p_window_minutes integer
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
AS $
DECLARE
  v_current_count integer;
  v_window_start timestamp with time zone;
BEGIN
  -- Get current rate limit record
  SELECT action_count, window_start 
  INTO v_current_count, v_window_start
  FROM app.rate_limits
  WHERE user_id = p_user_id 
    AND action_type = p_action_type;
  
  -- If no record or window expired, reset
  IF v_current_count IS NULL OR 
     v_window_start < (now() - (p_window_minutes || ' minutes')::interval) THEN
    INSERT INTO app.rate_limits (user_id, action_type, action_count, window_start)
    VALUES (p_user_id, p_action_type, 1, now())
    ON CONFLICT (user_id, action_type) 
    DO UPDATE SET action_count = 1, window_start = now();
    RETURN true;
  END IF;
  
  -- Check if limit exceeded
  IF v_current_count >= p_max_actions THEN
    RETURN false;
  END IF;
  
  -- Increment counter
  UPDATE app.rate_limits
  SET action_count = action_count + 1
  WHERE user_id = p_user_id AND action_type = p_action_type;
  
  RETURN true;
END;
$;

-- =====================================================================
-- TRIGGERS FOR AUTOMATIC UPDATES
-- =====================================================================

-- Update updated_at timestamp for admin_users
CREATE OR REPLACE FUNCTION app.update_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$;

CREATE TRIGGER trigger_admin_users_updated_at
  BEFORE UPDATE ON app.admin_users
  FOR EACH ROW
  EXECUTE FUNCTION app.update_updated_at();

-- =====================================================================
-- INITIAL DATA SEEDS
-- =====================================================================

-- Insert common languages
INSERT INTO app.languages (code, name) VALUES
  ('en', 'English'),
  ('es', 'Spanish'),
  ('fr', 'French'),
  ('de', 'German'),
  ('zh', 'Chinese'),
  ('ja', 'Japanese'),
  ('ko', 'Korean'),
  ('ar', 'Arabic'),
  ('pt', 'Portuguese'),
  ('ru', 'Russian'),
  ('hi', 'Hindi'),
  ('it', 'Italian')
ON CONFLICT (code) DO NOTHING;