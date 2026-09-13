-- 0003 — Source register and ingested items.

-- Validates that every element of a text[] column is a known jurisdiction code.
-- A CHECK constraint cannot do this (subqueries are not allowed), and a join
-- table would be heavier than the read pattern warrants.
CREATE FUNCTION validate_jurisdiction_array() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    col     text := TG_ARGV[0];
    codes   text[];
    unknown text[];
BEGIN
    -- Read the column generically via jsonb rather than dynamic SQL over a
    -- record type, which is fragile across Postgres versions.
    SELECT array_agg(v) INTO codes
    FROM jsonb_array_elements_text(coalesce(to_jsonb(NEW) -> col, '[]'::jsonb)) AS v;

    IF codes IS NULL OR cardinality(codes) = 0 THEN
        RETURN NEW;
    END IF;
    SELECT array_agg(c) INTO unknown
    FROM unnest(codes) AS c
    WHERE NOT EXISTS (SELECT 1 FROM jurisdictions j WHERE j.code = c);
    IF unknown IS NOT NULL THEN
        RAISE EXCEPTION 'unknown jurisdiction code(s) in %.%: %',
            TG_TABLE_NAME, col, array_to_string(unknown, ', ');
    END IF;
    RETURN NEW;
END;
$$;


-- ---------------------------------------------------------------------------
-- Sources
-- ---------------------------------------------------------------------------
-- Adding an outlet, or a whole region, is an INSERT here plus an adapter slug
-- that already exists. Region expansion is configuration, not code.

CREATE TABLE sources (
    id                  uuid               PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Stable key the adapter registry dispatches on.
    slug                text               NOT NULL UNIQUE
                                           CHECK (slug ~ '^[a-z0-9][a-z0-9_-]{1,63}$'),
    name                text               NOT NULL,
    base_url            text               NOT NULL,
    feed_url            text,
    access_type         source_access_type NOT NULL,
    -- 1 = firm primary announcement, 2 = established trade press,
    -- 3 = aggregator or secondary. Drives confidence and conflict resolution.
    reliability_tier    smallint           NOT NULL CHECK (reliability_tier BETWEEN 1 AND 3),
    -- Set when this source is a firm's own newsroom.
    firm_id             uuid               REFERENCES firms (id) ON DELETE CASCADE,
    jurisdiction_focus  text[]             NOT NULL DEFAULT '{}',
    -- What this outlet exposes publicly. Paywalled outlets are 'headline_only'.
    default_access_level item_access_level NOT NULL DEFAULT 'summary',

    active              boolean            NOT NULL DEFAULT true,
    poll_interval       interval           NOT NULL DEFAULT interval '6 hours',
    last_polled_at      timestamptz,

    -- Phase 0 politeness state.
    robots_checked_at   timestamptz,
    robots_allowed      boolean,
    robots_crawl_delay_seconds integer     CHECK (robots_crawl_delay_seconds >= 0),
    -- HTML collection stays off until a human has dated a terms review.
    html_access_reviewed_at timestamptz,
    html_access_reviewed_by text,
    terms_url           text,
    terms_note          text,

    -- Zero-yield alerting: incremented per run that returns nothing, reset on
    -- any yield. Three consecutive usually means the feed URL moved.
    consecutive_zero_yield_runs integer    NOT NULL DEFAULT 0
                                           CHECK (consecutive_zero_yield_runs >= 0),

    created_at          timestamptz        NOT NULL DEFAULT now(),
    updated_at          timestamptz        NOT NULL DEFAULT now(),

    -- A feed source without a feed URL is a misconfiguration, not a fallback
    -- to scraping.
    CONSTRAINT sources_feed_requires_feed_url
        CHECK (access_type <> 'feed' OR feed_url IS NOT NULL),

    -- Phase 0, section 1: an active HTML source must carry a dated terms review.
    CONSTRAINT sources_html_requires_terms_review
        CHECK (access_type <> 'html' OR NOT active OR html_access_reviewed_at IS NOT NULL),

    -- Phase 0, section 1: the 10s floor is a floor, not a target. A source may
    -- be polled more slowly, never faster.
    CONSTRAINT sources_poll_interval_floor
        CHECK (poll_interval >= interval '10 seconds'),

    -- Tier 1 means "the firm said so", which only holds for a firm's own feed.
    CONSTRAINT sources_tier1_is_a_firm_newsroom
        CHECK (reliability_tier <> 1 OR firm_id IS NOT NULL)
);

CREATE INDEX sources_active_due_idx ON sources (last_polled_at NULLS FIRST) WHERE active;
CREATE INDEX sources_firm_idx ON sources (firm_id) WHERE firm_id IS NOT NULL;
CREATE UNIQUE INDEX sources_firm_newsroom_key ON sources (firm_id)
    WHERE firm_id IS NOT NULL AND reliability_tier = 1;

CREATE TRIGGER sources_set_updated_at BEFORE UPDATE ON sources
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER sources_validate_jurisdictions
    BEFORE INSERT OR UPDATE OF jurisdiction_focus ON sources
    FOR EACH ROW EXECUTE FUNCTION validate_jurisdiction_array('jurisdiction_focus');

COMMENT ON COLUMN sources.reliability_tier IS
  '1 firm primary announcement, 2 established trade press, 3 aggregator/secondary.';


-- ---------------------------------------------------------------------------
-- Raw items
-- ---------------------------------------------------------------------------
-- There is no body, content or summary column, and adding one would breach
-- Phase 0 section 2. Article text lives in process memory for the duration of
-- one extraction call and is then discarded. The only text persisted from an
-- outlet is the headline, which is needed to make the outbound link usable,
-- and capped provenance excerpts in move_field_evidence.

CREATE TABLE raw_items (
    id                  uuid              PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id           uuid              NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    url                 text              NOT NULL CHECK (url ~ '^https?://'),
    headline            text              NOT NULL CHECK (btrim(headline) <> ''),
    -- Falls back to fetched_at when the feed omits a date; the flag says which.
    published_at        timestamptz       NOT NULL,
    published_at_is_estimated boolean     NOT NULL DEFAULT false,
    fetched_at          timestamptz       NOT NULL DEFAULT now(),
    -- SHA-256 of the fetched text. Change detection and idempotency only; it is
    -- one-way and is not a way to keep the article.
    content_hash        bytea             NOT NULL,
    source_access       item_access_level NOT NULL,

    processing_state    raw_item_state    NOT NULL DEFAULT 'new',
    reject_reason       text,
    error_message       text,
    processed_at        timestamptz,

    -- Relevance gate audit. Everything the cheap gate discards is recorded so
    -- its false-negative rate can be measured against the gold set rather than
    -- assumed.
    gate_version        text,
    gate_passed         boolean,
    gate_score          numeric(4, 3)     CHECK (gate_score >= 0 AND gate_score <= 1),
    gate_matched_terms  text[]            NOT NULL DEFAULT '{}',

    created_at          timestamptz       NOT NULL DEFAULT now(),

    -- Idempotency: re-ingesting the same article is a no-op, not a duplicate.
    CONSTRAINT raw_items_url_key UNIQUE (url),

    CONSTRAINT raw_items_rejected_needs_reason
        CHECK (processing_state <> 'rejected' OR reject_reason IS NOT NULL),
    CONSTRAINT raw_items_error_needs_message
        CHECK (processing_state <> 'error' OR error_message IS NOT NULL)
);

CREATE INDEX raw_items_pending_idx ON raw_items (source_id, fetched_at)
    WHERE processing_state = 'new';
CREATE INDEX raw_items_published_idx ON raw_items (published_at DESC);
CREATE INDEX raw_items_source_published_idx ON raw_items (source_id, published_at DESC);
-- Backfill resumption: find the oldest item already held for a source.
CREATE INDEX raw_items_source_published_asc_idx ON raw_items (source_id, published_at);
CREATE INDEX raw_items_gate_rejected_idx ON raw_items (fetched_at)
    WHERE gate_passed = false;

COMMENT ON TABLE raw_items IS
  'Ingested item metadata. Deliberately has no column for article text.';
COMMENT ON COLUMN raw_items.source_access IS
  'headline_only marks paywalled items: confidence penalty, no corroboration boost.';
