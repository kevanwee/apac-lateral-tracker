-- 0004 — Practice group and sector taxonomies.
--
-- Two axes, kept apart on purpose. A fintech regulatory partner is
-- practice = Financial Services Regulatory, sector = Financial Services.
-- Collapsing them destroys the ability to answer whether growth is in the
-- practice or in the sector, which is the question the dataset exists to
-- answer, so the schema gives them separate tables and separate join tables.
--
-- Content lives in taxonomy/*.yaml in the repo and is loaded by
-- `tracker taxonomy load`. These tables hold structure and version history;
-- the model never invents a node.

CREATE TABLE taxonomy_versions (
    version      text        PRIMARY KEY CHECK (version ~ '^\d+\.\d+\.\d+$'),
    released_at  timestamptz NOT NULL DEFAULT now(),
    -- SHA-256 of the YAML this version was loaded from. A mismatch means the
    -- file was edited without cutting a new version.
    checksum     bytea       NOT NULL,
    notes        text,
    is_current   boolean     NOT NULL DEFAULT false
);

CREATE UNIQUE INDEX taxonomy_versions_one_current ON taxonomy_versions (is_current)
    WHERE is_current;

COMMENT ON TABLE taxonomy_versions IS
  'Taxonomy releases. Historical assignments keep their version so trend lines '
  'stay comparable across a retaxonomy.';


-- ---------------------------------------------------------------------------
-- Practice groups
-- ---------------------------------------------------------------------------

CREATE TABLE practice_groups (
    id               uuid     PRIMARY KEY DEFAULT gen_random_uuid(),
    taxonomy_version text     NOT NULL REFERENCES taxonomy_versions (version)
                              ON DELETE RESTRICT,
    -- Dotted slug, stable across versions where the node survives:
    -- 'corporate', 'corporate.m_and_a'.
    code             text     NOT NULL CHECK (code ~ '^[a-z0-9_]+(\.[a-z0-9_]+)?$'),
    name             text     NOT NULL,
    parent_id        uuid,
    level            smallint NOT NULL CHECK (level IN (1, 2)),
    sort_order       integer  NOT NULL DEFAULT 0,
    -- The reserved sentinel node. Classification that cannot place a move
    -- assigns this rather than leaving the move without a primary group, so
    -- unclassified records stay in the denominator instead of vanishing.
    is_unclassified  boolean  NOT NULL DEFAULT false,

    UNIQUE (taxonomy_version, code),
    -- Lets the parent FK below pin parent and child to the same version.
    UNIQUE (taxonomy_version, id),

    CONSTRAINT practice_groups_level_matches_parent
        CHECK ((level = 1 AND parent_id IS NULL) OR (level = 2 AND parent_id IS NOT NULL)),
    CONSTRAINT practice_groups_unclassified_is_top_level
        CHECK (NOT is_unclassified OR level = 1),
    -- A level-2 node may not parent to a node from another taxonomy version.
    FOREIGN KEY (taxonomy_version, parent_id)
        REFERENCES practice_groups (taxonomy_version, id) ON DELETE RESTRICT
);

CREATE INDEX practice_groups_version_idx ON practice_groups (taxonomy_version, level, sort_order);
CREATE UNIQUE INDEX practice_groups_one_unclassified_per_version
    ON practice_groups (taxonomy_version) WHERE is_unclassified;


-- ---------------------------------------------------------------------------
-- Sectors — the orthogonal axis
-- ---------------------------------------------------------------------------

CREATE TABLE sectors (
    id               uuid     PRIMARY KEY DEFAULT gen_random_uuid(),
    taxonomy_version text     NOT NULL REFERENCES taxonomy_versions (version)
                              ON DELETE RESTRICT,
    code             text     NOT NULL CHECK (code ~ '^[a-z0-9_]+(\.[a-z0-9_]+)?$'),
    name             text     NOT NULL,
    parent_id        uuid,
    level            smallint NOT NULL CHECK (level IN (1, 2)),
    sort_order       integer  NOT NULL DEFAULT 0,

    UNIQUE (taxonomy_version, code),
    UNIQUE (taxonomy_version, id),

    CONSTRAINT sectors_level_matches_parent
        CHECK ((level = 1 AND parent_id IS NULL) OR (level = 2 AND parent_id IS NOT NULL)),
    FOREIGN KEY (taxonomy_version, parent_id)
        REFERENCES sectors (taxonomy_version, id) ON DELETE RESTRICT
);

CREATE INDEX sectors_version_idx ON sectors (taxonomy_version, level, sort_order);

COMMENT ON TABLE sectors IS
  'Industry sectors. Never merged into practice_groups: a move carries both, '
  'and conflating them is the single most common failure in market trend data.';
