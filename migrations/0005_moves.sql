-- 0005 — Moves: the canonical record, its sources, evidence and classifications.

-- ---------------------------------------------------------------------------
-- Team moves
-- ---------------------------------------------------------------------------
-- Two or more partners moving between the same firm pair inside 60 days. A
-- distinct entity because a five-partner lift-out is a different market signal
-- from five unrelated laterals, and the dashboard treats it as one.

CREATE TABLE team_moves (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    from_firm_id        uuid        REFERENCES firms (id) ON DELETE SET NULL,
    to_firm_id          uuid        NOT NULL REFERENCES firms (id) ON DELETE CASCADE,
    office_jurisdiction text        REFERENCES jurisdictions (code),
    window_start        date        NOT NULL,
    window_end          date        NOT NULL,
    -- Maintained by the Phase 4 detector; denormalised for the UI.
    partner_count       integer     NOT NULL DEFAULT 0 CHECK (partner_count >= 0),
    detected_by         assigned_by NOT NULL DEFAULT 'rule',
    detected_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT team_moves_window_ordered CHECK (window_end >= window_start),
    CONSTRAINT team_moves_window_within_60_days
        CHECK (window_end - window_start <= 60),
    CONSTRAINT team_moves_not_same_firm
        CHECK (from_firm_id IS NULL OR from_firm_id <> to_firm_id)
);

CREATE INDEX team_moves_pair_idx ON team_moves (to_firm_id, from_firm_id, window_start);


-- ---------------------------------------------------------------------------
-- Moves
-- ---------------------------------------------------------------------------

CREATE TABLE moves (
    id                  uuid                 PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id           uuid                 NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    from_firm_id        uuid                 REFERENCES firms (id) ON DELETE SET NULL,
    -- Null only for a retirement, which has no destination. Every other move
    -- type must name where the person went.
    to_firm_id          uuid                 REFERENCES firms (id) ON DELETE SET NULL,
    title_from          text,
    title_to            text,
    partner_tier        partner_tier         NOT NULL DEFAULT 'undisclosed',
    office_jurisdiction text                 REFERENCES jurisdictions (code),

    announced_date      date                 NOT NULL,
    -- Frequently null: firms announce a hire long before the person starts, and
    -- often never state the start date. Every consumer must handle the null.
    effective_date      date,

    move_type           move_type            NOT NULL,
    team_move_id        uuid                 REFERENCES team_moves (id) ON DELETE SET NULL,

    confidence          numeric(4, 3)        NOT NULL
                                             CHECK (confidence >= 0 AND confidence <= 1),
    -- The inputs that produced `confidence`: source tier, field completeness,
    -- span quality, corroboration count. Kept so a score can be explained and
    -- so threshold changes can be replayed against history.
    confidence_components jsonb              NOT NULL DEFAULT '{}'::jsonb,

    review_state        review_state         NOT NULL DEFAULT 'pending_review',
    classification_state classification_state NOT NULL DEFAULT 'pending',
    -- Which taxonomy release classified this move. Frozen at classification
    -- time so a later retaxonomy does not silently rewrite past trend lines.
    classified_taxonomy_version text         REFERENCES taxonomy_versions (version)
                                             ON DELETE RESTRICT,

    -- Phase 4 merges never overwrite. Losing values from field-level conflicts
    -- are kept here, keyed by field, with the source tier that supplied them,
    -- so disagreement between outlets is itself visible data.
    field_conflicts     jsonb                NOT NULL DEFAULT '{}'::jsonb,
    -- Set when a merge replaced this row with a canonical one. A row is
    -- canonical exactly while this is null.
    superseded_by_move_id uuid               REFERENCES moves (id) ON DELETE RESTRICT,
    merged_at           timestamptz,

    -- Idempotency for the extract stage: a deterministic fingerprint of
    -- (raw_item, person slot) so re-extracting an item cannot create a second
    -- row. Null on rows created by a merge, which have no single origin item.
    extraction_fingerprint text,

    created_at          timestamptz          NOT NULL DEFAULT now(),
    updated_at          timestamptz          NOT NULL DEFAULT now(),

    -- Invariant: a move cannot be from and to the same firm unless it is a
    -- promotion.
    CONSTRAINT moves_same_firm_only_for_promotion
        CHECK (
            move_type = 'promotion'
            OR from_firm_id IS NULL
            OR to_firm_id IS NULL
            OR from_firm_id <> to_firm_id
        ),
    -- ...and a promotion is by definition within one firm.
    CONSTRAINT moves_promotion_is_within_one_firm
        CHECK (
            move_type <> 'promotion'
            OR (from_firm_id IS NOT NULL AND from_firm_id = to_firm_id)
        ),
    -- Invariant: a destination is required for everything except retirement.
    CONSTRAINT moves_destination_required
        CHECK (to_firm_id IS NOT NULL OR move_type = 'retirement'),
    -- Invariant: announced_date is never null (NOT NULL above) and must be a
    -- plausible date. A fixed range, because CHECK cannot call now().
    CONSTRAINT moves_announced_date_plausible
        CHECK (announced_date BETWEEN DATE '1990-01-01' AND DATE '2100-01-01'),
    -- effective_date may precede announced_date: moves are often reported after
    -- the fact. Only the absurd is rejected.
    CONSTRAINT moves_effective_date_plausible
        CHECK (effective_date IS NULL
               OR effective_date BETWEEN DATE '1990-01-01' AND DATE '2100-01-01'),

    CONSTRAINT moves_not_superseded_by_self
        CHECK (superseded_by_move_id IS DISTINCT FROM id),
    -- Supersession and review_state cannot disagree about whether this row is
    -- still canonical.
    CONSTRAINT moves_superseded_state_agrees
        CHECK ((superseded_by_move_id IS NULL) = (review_state <> 'superseded')),
    CONSTRAINT moves_merged_at_with_supersession
        CHECK ((superseded_by_move_id IS NULL) = (merged_at IS NULL)),
    CONSTRAINT moves_classified_has_version
        CHECK (classification_state <> 'classified' OR classified_taxonomy_version IS NOT NULL)
);

-- Only one row per (raw item, person slot) may exist: re-running extract is a
-- no-op. Merge-created rows carry no fingerprint and are exempt.
CREATE UNIQUE INDEX moves_extraction_fingerprint_key
    ON moves (extraction_fingerprint) WHERE extraction_fingerprint IS NOT NULL;

-- Phase 4 blocking: normalised surname plus destination firm inside a 60-day
-- window. This index serves the firm-and-date half; people.surname_normalised
-- serves the other.
CREATE INDEX moves_blocking_idx ON moves (to_firm_id, announced_date)
    WHERE superseded_by_move_id IS NULL;
CREATE INDEX moves_person_idx ON moves (person_id, announced_date DESC);
CREATE INDEX moves_announced_idx ON moves (announced_date DESC)
    WHERE superseded_by_move_id IS NULL;
CREATE INDEX moves_from_firm_idx ON moves (from_firm_id, announced_date)
    WHERE from_firm_id IS NOT NULL AND superseded_by_move_id IS NULL;
CREATE INDEX moves_review_idx ON moves (review_state, confidence)
    WHERE review_state IN ('pending_review', 'in_review');
CREATE INDEX moves_classification_pending_idx ON moves (classification_state)
    WHERE classification_state <> 'classified';
CREATE INDEX moves_team_idx ON moves (team_move_id) WHERE team_move_id IS NOT NULL;

CREATE TRIGGER moves_set_updated_at BEFORE UPDATE ON moves
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON COLUMN moves.field_conflicts IS
  'Values that lost a merge, kept rather than discarded: '
  '{"title_to": [{"value": "Partner", "source_id": "...", "tier": 2}]}';

-- Everything the dashboard reads goes through here.
CREATE VIEW canonical_moves AS
SELECT * FROM moves WHERE superseded_by_move_id IS NULL AND review_state <> 'rejected';


-- ---------------------------------------------------------------------------
-- Move sources
-- ---------------------------------------------------------------------------
-- A move with two or more independent sources earns a corroboration boost.
-- "Independent" means distinct sources, not distinct URLs from one outlet, so
-- the boost is computed over distinct source_id.

CREATE TABLE move_sources (
    move_id     uuid        NOT NULL REFERENCES moves (id) ON DELETE CASCADE,
    raw_item_id uuid        NOT NULL REFERENCES raw_items (id) ON DELETE CASCADE,
    is_primary  boolean     NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (move_id, raw_item_id)
);

CREATE UNIQUE INDEX move_sources_one_primary ON move_sources (move_id) WHERE is_primary;
CREATE INDEX move_sources_raw_item_idx ON move_sources (raw_item_id);


-- ---------------------------------------------------------------------------
-- Field evidence
-- ---------------------------------------------------------------------------
-- Phase 2: every extracted field carries a character span into the source text.
-- Fields without a span are dropped before they reach moves, so a row here is
-- the proof that a field was stated rather than inferred.

CREATE TABLE move_field_evidence (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    move_id         uuid        NOT NULL REFERENCES moves (id) ON DELETE CASCADE,
    raw_item_id     uuid        NOT NULL REFERENCES raw_items (id) ON DELETE CASCADE,
    field_name      text        NOT NULL CHECK (field_name IN (
                        'person_name', 'from_firm', 'to_firm', 'title_from',
                        'title_to', 'partner_tier', 'office_jurisdiction',
                        'announced_date', 'effective_date', 'move_type',
                        'practice_group', 'sector', 'team_size')),
    span_start      integer     NOT NULL CHECK (span_start >= 0),
    span_end        integer     NOT NULL,
    -- The value the extractor read out of that span.
    extracted_value text        NOT NULL,
    -- Provenance excerpt, for reviewers and audit only. Never display content,
    -- never served by the public API.
    excerpt         text        NOT NULL CHECK (btrim(excerpt) <> ''),
    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT move_field_evidence_span_ordered CHECK (span_end > span_start),

    -- Phase 0, section 2: 25 words, enforced by the database rather than by
    -- application discipline.
    CONSTRAINT move_field_evidence_excerpt_word_cap
        CHECK (array_length(regexp_split_to_array(btrim(excerpt), '\s+'), 1) <= 25),

    UNIQUE (move_id, raw_item_id, field_name)
);

CREATE INDEX move_field_evidence_move_idx ON move_field_evidence (move_id);

COMMENT ON TABLE move_field_evidence IS
  'Provenance spans. Excerpts are capped at 25 words by constraint and are not '
  'display content; the dashboard links to the source instead.';


-- ---------------------------------------------------------------------------
-- Classification joins
-- ---------------------------------------------------------------------------

CREATE TABLE move_practice_groups (
    move_id           uuid          NOT NULL REFERENCES moves (id) ON DELETE CASCADE,
    practice_group_id uuid          NOT NULL,
    -- Carried explicitly so the composite FK below pins the assignment to the
    -- taxonomy release that made it.
    taxonomy_version  text          NOT NULL,
    is_primary        boolean       NOT NULL DEFAULT false,
    confidence        numeric(4, 3) NOT NULL
                                    CHECK (confidence >= 0 AND confidence <= 1),
    assigned_by       assigned_by   NOT NULL,
    -- Which taxonomy_mappings.yaml key fired, when assigned_by = 'rule'.
    rule_key          text,
    assigned_at       timestamptz   NOT NULL DEFAULT now(),

    PRIMARY KEY (move_id, practice_group_id),
    FOREIGN KEY (taxonomy_version, practice_group_id)
        REFERENCES practice_groups (taxonomy_version, id) ON DELETE RESTRICT,
    CONSTRAINT move_practice_groups_rule_has_key
        CHECK (assigned_by <> 'rule' OR rule_key IS NOT NULL)
);

-- Invariant, first half: at most one primary practice group per move.
-- The "at least one" half needs a trigger and lives in 0007.
CREATE UNIQUE INDEX move_practice_groups_one_primary
    ON move_practice_groups (move_id) WHERE is_primary;
CREATE INDEX move_practice_groups_group_idx
    ON move_practice_groups (practice_group_id, taxonomy_version);


CREATE TABLE move_sectors (
    move_id          uuid          NOT NULL REFERENCES moves (id) ON DELETE CASCADE,
    sector_id        uuid          NOT NULL,
    taxonomy_version text          NOT NULL,
    is_primary       boolean       NOT NULL DEFAULT false,
    confidence       numeric(4, 3) NOT NULL
                                   CHECK (confidence >= 0 AND confidence <= 1),
    assigned_by      assigned_by   NOT NULL,
    assigned_at      timestamptz   NOT NULL DEFAULT now(),

    PRIMARY KEY (move_id, sector_id),
    FOREIGN KEY (taxonomy_version, sector_id)
        REFERENCES sectors (taxonomy_version, id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX move_sectors_one_primary ON move_sectors (move_id) WHERE is_primary;
CREATE INDEX move_sectors_sector_idx ON move_sectors (sector_id, taxonomy_version);

COMMENT ON TABLE move_sectors IS
  'Sector tags, independent of practice group. A move may have neither, one or '
  'both; absence of a sector is not a reason to widen the practice group.';
