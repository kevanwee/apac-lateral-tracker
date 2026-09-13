-- 0002 — Entities: jurisdictions, firms, firm aliases, people, erasure records.

-- ---------------------------------------------------------------------------
-- Jurisdictions
-- ---------------------------------------------------------------------------
-- A lookup table rather than free text, because "add a region by configuration,
-- not by code change" has to mean something enforceable. Office jurisdictions
-- are ISO 3166-1 alpha-2, optionally with a subdivision (AU-NSW, CN-SH).

CREATE TABLE jurisdictions (
    code            text    PRIMARY KEY CHECK (code ~ '^[A-Z]{2}(-[A-Z0-9]{2,3})?$'),
    name            text    NOT NULL,
    region          text    NOT NULL,         -- APAC, EMEA, Americas
    is_depth_market boolean NOT NULL DEFAULT false,
    active          boolean NOT NULL DEFAULT true
);

COMMENT ON COLUMN jurisdictions.is_depth_market IS
  'Depth markets get full source coverage and are the scope for coverage metrics.';

INSERT INTO jurisdictions (code, name, region, is_depth_market) VALUES
    ('SG',     'Singapore',            'APAC', true),
    ('HK',     'Hong Kong SAR',        'APAC', true),
    ('AU',     'Australia',            'APAC', true),
    ('AU-NSW', 'New South Wales',      'APAC', true),
    ('AU-VIC', 'Victoria',             'APAC', true),
    ('AU-QLD', 'Queensland',           'APAC', true),
    ('AU-WA',  'Western Australia',    'APAC', true),
    ('CN',     'Mainland China',       'APAC', false),
    ('JP',     'Japan',                'APAC', false),
    ('KR',     'South Korea',          'APAC', false),
    ('IN',     'India',                'APAC', false),
    ('ID',     'Indonesia',            'APAC', false),
    ('MY',     'Malaysia',             'APAC', false),
    ('TH',     'Thailand',             'APAC', false),
    ('VN',     'Vietnam',              'APAC', false),
    ('PH',     'Philippines',          'APAC', false),
    ('TW',     'Taiwan',               'APAC', false),
    ('NZ',     'New Zealand',          'APAC', false),
    ('AE',     'United Arab Emirates', 'EMEA', false),
    ('GB',     'United Kingdom',       'EMEA', false),
    ('US',     'United States',        'Americas', false);


-- ---------------------------------------------------------------------------
-- Firms
-- ---------------------------------------------------------------------------

CREATE TABLE firms (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name    text        NOT NULL CHECK (btrim(canonical_name) <> ''),
    firm_type         firm_type   NOT NULL,
    hq_jurisdiction   text        REFERENCES jurisdictions (code),
    -- Industry verticals the firm is known for. Free-form slugs validated
    -- against sectors.slug at application level; not an FK because a firm's
    -- self-described verticals are descriptive, not analytical input.
    verticals         text[]      NOT NULL DEFAULT '{}',

    -- Swiss verein and other multi-entity structures. Member firms point at the
    -- brand-level row so Phase 4 can resolve "Firm X Australia" to the brand
    -- while keeping the member entity distinct where the distinction matters.
    is_verein_brand   boolean     NOT NULL DEFAULT false,
    verein_parent_id  uuid        REFERENCES firms (id) ON DELETE SET NULL,

    website           text,
    newsroom_url      text,
    -- Rank in the top-100-by-APAC-presence registry that seeds newsroom polling.
    apac_presence_rank integer    CHECK (apac_presence_rank > 0),

    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT firms_verein_parent_not_self CHECK (verein_parent_id IS DISTINCT FROM id),
    CONSTRAINT firms_brand_has_no_parent
        CHECK (NOT is_verein_brand OR verein_parent_id IS NULL)
);

CREATE UNIQUE INDEX firms_canonical_name_key ON firms (lower(btrim(canonical_name)));
CREATE UNIQUE INDEX firms_apac_presence_rank_key
    ON firms (apac_presence_rank) WHERE apac_presence_rank IS NOT NULL;
CREATE INDEX firms_canonical_name_trgm ON firms USING gin (canonical_name gin_trgm_ops);
CREATE INDEX firms_verein_parent_idx ON firms (verein_parent_id)
    WHERE verein_parent_id IS NOT NULL;

CREATE TRIGGER firms_set_updated_at BEFORE UPDATE ON firms
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


CREATE TABLE firm_aliases (
    id          uuid            PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id     uuid            NOT NULL REFERENCES firms (id) ON DELETE CASCADE,
    alias       text            NOT NULL CHECK (btrim(alias) <> ''),
    alias_type  firm_alias_type NOT NULL,
    confidence  numeric(4, 3)   NOT NULL DEFAULT 1.000
                                CHECK (confidence >= 0 AND confidence <= 1),
    -- Where this alias came from: 'seed', a review_queue id, or a merger note.
    provenance  text,
    created_at  timestamptz     NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX firm_aliases_firm_alias_key
    ON firm_aliases (firm_id, lower(btrim(alias)));
CREATE INDEX firm_aliases_alias_idx ON firm_aliases (lower(btrim(alias)));
CREATE INDEX firm_aliases_alias_trgm ON firm_aliases USING gin (alias gin_trgm_ops);

COMMENT ON TABLE firm_aliases IS
  'Alias -> firm. Deliberately not globally unique on alias: genuinely ambiguous '
  'aliases exist and surfacing them is the point. See ambiguous_firm_aliases.';

-- An alias resolving to more than one firm cannot be applied automatically.
-- Phase 4 routes these to review instead of guessing.
CREATE VIEW ambiguous_firm_aliases AS
SELECT lower(btrim(alias)) AS alias_key,
       count(DISTINCT firm_id) AS firm_count,
       array_agg(DISTINCT firm_id) AS firm_ids
FROM firm_aliases
GROUP BY 1
HAVING count(DISTINCT firm_id) > 1;


-- ---------------------------------------------------------------------------
-- People
-- ---------------------------------------------------------------------------

CREATE TABLE people (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name      text        NOT NULL CHECK (btrim(canonical_name) <> ''),
    -- Every spelling seen in the wild, including romanisation variants and
    -- surname-first orderings. Written by the Phase 4 name utility.
    name_variants       text[]      NOT NULL DEFAULT '{}',
    -- Blocking key for Phase 4. Maintained by the Python normaliser so there is
    -- one implementation of the rules, not two.
    surname_normalised  text        NOT NULL CHECK (btrim(surname_normalised) <> ''),
    given_normalised    text,
    current_firm_id     uuid        REFERENCES firms (id) ON DELETE SET NULL,
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- The blocking index for Phase 4: surname plus destination firm inside a date
-- window. Cheap, and it is the only lookup dedupe is allowed to do.
CREATE INDEX people_surname_idx ON people (surname_normalised);
CREATE INDEX people_canonical_name_trgm ON people USING gin (canonical_name gin_trgm_ops);
CREATE INDEX people_variants_idx ON people USING gin (name_variants);
CREATE INDEX people_current_firm_idx ON people (current_firm_id)
    WHERE current_firm_id IS NOT NULL;

CREATE TRIGGER people_set_updated_at BEFORE UPDATE ON people
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE people IS
  'Professional attributes only. No contact details, no personal characteristics, '
  'no inferred attributes. See docs/constraints.md section 4.';


-- ---------------------------------------------------------------------------
-- Erasure (Phase 0, section 4)
-- ---------------------------------------------------------------------------

-- What an erasure leaves behind: a salted hash and nothing else. Ingestion
-- checks this before creating a person, so tomorrow's cron run cannot quietly
-- resurrect someone who asked to be removed.
CREATE TABLE suppressed_people (
    name_hash     bytea       PRIMARY KEY,
    reason        text        NOT NULL,
    suppressed_at timestamptz NOT NULL DEFAULT now(),
    actor         text        NOT NULL
);

REVOKE ALL ON suppressed_people FROM PUBLIC;

COMMENT ON TABLE suppressed_people IS
  'Erasure tombstones. Holds no name, only person_name_hash() output.';

-- Audit trail for erasures, carrying counts but no personal data, so the
-- operation is reviewable without defeating its own purpose.
CREATE TABLE erasure_log (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    occurred_at       timestamptz NOT NULL DEFAULT now(),
    actor             text        NOT NULL,
    reason            text        NOT NULL,
    moves_deleted     integer     NOT NULL,
    evidence_deleted  integer     NOT NULL,
    sources_unlinked  integer     NOT NULL
);
