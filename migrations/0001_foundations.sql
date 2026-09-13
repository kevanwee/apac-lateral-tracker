-- 0001 — Foundations: extensions, enum vocabularies, shared helpers.
--
-- Fixed vocabularies are native enum types rather than text + CHECK. They are
-- self-documenting in \d output and cheap to store. The cost is that removing a
-- value later needs a type rebuild; adding one is a one-line ALTER TYPE. Every
-- vocabulary here is small and stable enough for that trade to be worth it.
-- Anything expected to grow (practice groups, sectors, firm aliases) is a table.

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid, digest, gen_random_bytes
CREATE EXTENSION IF NOT EXISTS unaccent;   -- name normalisation for suppression hashing
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- trigram indexes for Phase 4 blocking


-- ---------------------------------------------------------------------------
-- Vocabularies
-- ---------------------------------------------------------------------------

-- How a source is read. Phase 0: 'html' is opt-in per source and blocked until
-- a human has reviewed that outlet's terms.
CREATE TYPE source_access_type AS ENUM ('feed', 'html', 'api');

-- How much of an item the outlet actually exposed to us. Paywalled outlets give
-- 'headline_only', which carries a confidence penalty and does not qualify for
-- the corroboration boost.
CREATE TYPE item_access_level AS ENUM ('headline_only', 'summary', 'full_public');

CREATE TYPE raw_item_state AS ENUM ('new', 'extracted', 'rejected', 'error');

CREATE TYPE firm_type AS ENUM (
    'global',      -- multi-jurisdiction, single or verein brand
    'regional',    -- pan-APAC or pan-ASEAN
    'local',       -- single-jurisdiction full service
    'boutique',    -- single-jurisdiction specialist
    'in_house',    -- corporate legal department
    'chambers',    -- barristers' chambers / advocacy set
    'government',  -- AGC, regulators, judiciary
    'other'
);

CREATE TYPE firm_alias_type AS ENUM (
    'legacy_name',       -- pre-merger or pre-rebrand name
    'verein_member',     -- member firm of a Swiss verein / multi-entity structure
    'abbreviation',      -- trade press shorthand
    'local_office_name', -- joint law venture or local practising entity
    'misspelling'
);

CREATE TYPE partner_tier AS ENUM ('equity', 'salaried', 'undisclosed');

CREATE TYPE move_type AS ENUM (
    'lateral',          -- firm to firm
    'promotion',        -- within the same firm
    'in_house_exit',    -- private practice to in-house
    'in_house_entry',   -- in-house to private practice
    'retirement',
    'firm_launch',      -- founding a new firm
    'merger_absorbed'   -- arrived via a firm combination, not an individual move
);

-- Lifecycle of a moves row.
--   pending_review / in_review / accepted / rejected are review-queue states.
--   auto_accepted cleared the confidence threshold without a human looking.
--   superseded means a Phase 4 merge replaced this row with a canonical one.
CREATE TYPE review_state AS ENUM (
    'auto_accepted',
    'pending_review',
    'in_review',
    'accepted',
    'rejected',
    'superseded'
);

-- Whether a move has been through classification yet. Separates "nobody has
-- looked" from "looked and could not place it", which are different problems.
CREATE TYPE classification_state AS ENUM ('pending', 'classified', 'unclassified');

CREATE TYPE assigned_by AS ENUM ('model', 'human', 'rule');

CREATE TYPE review_reason AS ENUM (
    'low_confidence',
    'missing_required_field',
    'dedupe_ambiguous',       -- pair scored in the 0.60-0.85 band
    'unclassified_practice',
    'conflicting_sources',
    'team_move_candidate',
    'suppressed_name_match'   -- matched an erasure tombstone; must not be re-ingested
);

CREATE TYPE pipeline_stage AS ENUM (
    'ingest', 'extract', 'dedupe', 'classify', 'refresh_views', 'reconcile'
);

CREATE TYPE run_status AS ENUM ('running', 'succeeded', 'failed', 'aborted');


-- ---------------------------------------------------------------------------
-- Shared helpers
-- ---------------------------------------------------------------------------

CREATE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

-- Instance-local salt for suppression-list hashing, generated here so it never
-- exists in the repo and differs per deployment. Losing it means erasure
-- tombstones stop matching, so it is backed up with the database, not rotated.
CREATE TABLE instance_secrets (
    id          boolean PRIMARY KEY DEFAULT true CHECK (id),
    name_salt   bytea   NOT NULL DEFAULT gen_random_bytes(32)
);
INSERT INTO instance_secrets DEFAULT VALUES;
REVOKE ALL ON instance_secrets FROM PUBLIC;

COMMENT ON TABLE instance_secrets IS
  'Single-row table holding the salt used to hash erased names. Never export.';

-- Conservative normalisation used only for suppression hashing and coarse
-- blocking. The real matcher, with romanisation and given-name handling, is the
-- tested Python utility in Phase 4 — this is deliberately not a second
-- implementation of it.
-- STABLE, not IMMUTABLE: unaccent() depends on a dictionary and is itself only
-- STABLE. Never use this in an index expression or generated column.
CREATE FUNCTION normalise_name_for_hash(raw text) RETURNS text
LANGUAGE sql STABLE STRICT AS $$
    SELECT btrim(regexp_replace(
        regexp_replace(
            regexp_replace(
                lower(public.unaccent(raw)),
                -- honorifics and post-nominals carry no identity
                '\m(mr|mrs|ms|miss|dr|prof|sir|dame|hon)\M'
                || '|\m(sc|kc|qc|llb|llm|jd|ba|ma|phd|faciarb|ficarb)\M',
                ' ', 'g'),
            '[^a-z0-9 ]', ' ', 'g'),
        '\s+', ' ', 'g'),
    ' ');
$$;

COMMENT ON FUNCTION normalise_name_for_hash(text) IS
  'Coarse normalisation for erasure tombstones. Not the Phase 4 name matcher.';

CREATE FUNCTION person_name_hash(raw text) RETURNS bytea
LANGUAGE sql STABLE STRICT AS $$
    SELECT digest(s.name_salt || convert_to(normalise_name_for_hash(raw), 'UTF8'), 'sha256')
    FROM instance_secrets s;
$$;

COMMENT ON FUNCTION person_name_hash(text) IS
  'Salted one-way hash of a normalised name. The only residue an erasure leaves.';
