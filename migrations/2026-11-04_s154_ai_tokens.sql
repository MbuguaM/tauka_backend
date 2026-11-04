-- §154 AI token packs through Paystack (2026-11-04, plan ALL-18). Owner: tauka-python.
--
-- A learner who reaches their daily or monthly AI allowance can spend tokens they bought.
-- What was paid for must survive a Redis outage, so it lives here, not in the Redis counters.
--
-- ai_token_purchase  one row per order, in the shape refund_service expects.
-- ai_token_ledger    append-only. A balance is a fold over delta, never a column.
--
-- Neither table has a client policy or client grant. Learners see this only through
-- /account/ai-tokens and /ai/balance. Every balance-sensitive write goes through a function
-- below, under a per-learner lock, so two requests can't spend the same tokens.

CREATE TABLE IF NOT EXISTS app.ai_token_purchase (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id          uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  tokens              bigint NOT NULL CHECK (tokens > 0),   -- what this order buys, fixed when it opens
  amount_minor        bigint NOT NULL CHECK (amount_minor > 0),
  currency            text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  email               text NOT NULL,
  status              text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','paid','refunded','failed')),
  payment_provider    text,
  payment_reference   text UNIQUE,                          -- tk-<id>
  paid_at             timestamptz,
  refund_state        text CHECK (refund_state IS NULL OR refund_state IN ('requested','processed','failed','needs_attention')),
  refund_reason       text,
  refunded_by         uuid,
  refund_requested_at timestamptz,
  refund_processed_at timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ai_token_purchase_student ON app.ai_token_purchase (student_id, created_at DESC);

CREATE TABLE IF NOT EXISTS app.ai_token_ledger (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id  uuid NOT NULL REFERENCES app.student(id) ON DELETE CASCADE,
  delta       bigint NOT NULL CHECK (delta <> 0),
  reason      text NOT NULL CHECK (reason IN ('purchase','spend','return','refund')),
  purchase_id uuid REFERENCES app.ai_token_purchase(id) ON DELETE CASCADE,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ai_token_ledger_student ON app.ai_token_ledger (student_id);
-- The grant-once guard: a repeated webhook can't credit a purchase twice.
CREATE UNIQUE INDEX IF NOT EXISTS ai_token_ledger_one_purchase ON app.ai_token_ledger (purchase_id)
  WHERE reason = 'purchase';

ALTER TABLE app.ai_token_purchase ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.ai_token_ledger ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON app.ai_token_purchase, app.ai_token_ledger FROM PUBLIC, anon, authenticated;
GRANT ALL ON app.ai_token_purchase TO service_role;
GRANT SELECT, INSERT ON app.ai_token_ledger TO service_role;
COMMENT ON TABLE app.ai_token_purchase IS 'AI token orders (§154). tauka-python writes; learners read through /account/ai-tokens.';
COMMENT ON TABLE app.ai_token_ledger IS 'Append-only AI token ledger (§154). A balance is the sum of delta, never a column.';

-- What a learner has left. A small overrun on a long reply can make the fold negative; that
-- reads as empty, and the next purchase pays it off.
CREATE OR REPLACE FUNCTION app.ai_token_balance(p_student_id uuid)
RETURNS bigint LANGUAGE sql STABLE SET search_path = '' AS $$
  SELECT GREATEST(COALESCE(SUM(delta), 0), 0)::bigint FROM app.ai_token_ledger WHERE student_id = p_student_id;
$$;

-- Take tokens if they are all there. False, and no row, if they aren't.
CREATE OR REPLACE FUNCTION app.spend_ai_tokens(p_student_id uuid, p_tokens bigint)
RETURNS boolean LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
  IF p_tokens <= 0 THEN
    RAISE EXCEPTION 'spend must be positive';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('ai_tokens:' || p_student_id::text, 0));
  IF app.ai_token_balance(p_student_id) < p_tokens THEN
    RETURN false;
  END IF;
  INSERT INTO app.ai_token_ledger (student_id, delta, reason) VALUES (p_student_id, -p_tokens, 'spend');
  RETURN true;
END;
$$;

-- A paid order gets its tokens, once. True only for the call that granted them. Status and
-- ledger change in one transaction, so a failure leaves the order pending for the retry.
CREATE OR REPLACE FUNCTION app.settle_ai_token_purchase(p_purchase_id uuid)
RETURNS boolean LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE
  o app.ai_token_purchase%ROWTYPE;
BEGIN
  UPDATE app.ai_token_purchase SET status = 'paid', paid_at = now()
   WHERE id = p_purchase_id AND status = 'pending' RETURNING * INTO o;
  IF NOT FOUND THEN
    RETURN false;
  END IF;
  INSERT INTO app.ai_token_ledger (student_id, delta, reason, purchase_id)
  VALUES (o.student_id, o.tokens, 'purchase', o.id);
  RETURN true;
END;
$$;

-- Remove what's left of a refunded pack, up to its size. Returns how many were removed.
CREATE OR REPLACE FUNCTION app.take_back_ai_tokens(p_purchase_id uuid)
RETURNS bigint LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE
  o app.ai_token_purchase%ROWTYPE;
  removed bigint;
BEGIN
  SELECT * INTO o FROM app.ai_token_purchase WHERE id = p_purchase_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no such purchase';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('ai_tokens:' || o.student_id::text, 0));
  removed := LEAST(app.ai_token_balance(o.student_id), o.tokens);
  IF removed > 0 THEN
    INSERT INTO app.ai_token_ledger (student_id, delta, reason, purchase_id)
    VALUES (o.student_id, -removed, 'refund', o.id);
  END IF;
  RETURN removed;
END;
$$;

REVOKE ALL ON FUNCTION app.ai_token_balance(uuid), app.spend_ai_tokens(uuid, bigint),
  app.settle_ai_token_purchase(uuid), app.take_back_ai_tokens(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION app.ai_token_balance(uuid), app.spend_ai_tokens(uuid, bigint),
  app.settle_ai_token_purchase(uuid), app.take_back_ai_tokens(uuid) TO service_role;

INSERT INTO app.feature_flag (key, status, description) VALUES
  ('ai_tokens', 'coming', 'ALL-18: AI token packs through Paystack, sold on the website and linked from desktop apps only.')
ON CONFLICT (key) DO NOTHING;
