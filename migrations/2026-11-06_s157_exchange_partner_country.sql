-- §157 A partner's country (2026-11-06). Owner: tauka-python. Domain: exchange.
--
-- The join modal asked only for a city or region (free text). The country is its own
-- ISO 3166-1 alpha-2 column so ops can see where a partner is without parsing text:
-- data top-ups (rewards) are bought per country. Optional: rows from before §157 have none.
-- tauka-python writes it (POST /exchange/partner/join); nothing gates on it.

ALTER TABLE app.exchange_partner
  ADD COLUMN IF NOT EXISTS country_code text
  CHECK (country_code IS NULL OR country_code ~ '^[A-Z]{2}$');

COMMENT ON COLUMN app.exchange_partner.country_code IS
  'ISO 3166-1 alpha-2, from the join modal (§157). Shown to the partner and ops. Never an input to matching or access.';
