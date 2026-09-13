-- 0006 — Operations: review queue, run records, per-source yield, LLM spend.

-- ---------------------------------------------------------------------------
-- Review queue
-- ---------------------------------------------------------------------------
-- Everything below threshold lands here rather than being discarded. The queue
-- is also where the mapping file and the alias table grow from: a resolution is
-- expected to leave the system more deterministic than it found it.

CREATE TABLE review_queue (
    id              uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    move_id         uuid          NOT NULL REFERENCES moves (id) ON DELETE CASCADE,
    -- The counterpart, for dedupe decisions. A dedupe item is about a pair.
    related_move_id uuid          REFERENCES moves (id) ON DELETE CASCADE,
    reason          review_reason NOT NULL,
    -- Whatever the reviewer needs to decide: pair scores, competing field
    -- values, the candidate practice nodes the classifier could not separate.
    detail          jsonb         NOT NULL DEFAULT '{}'::jsonb,
    surfaced_at     timestamptz   NOT NULL DEFAULT now(),

    resolved_at     timestamptz,
    resolver        text,
    resolver_note   text,
    resolution      text          CHECK (resolution IN (
                        'accepted', 'rejected', 'merged', 'kept_separate',
                        'reclassified', 'deferred')),

    CONSTRAINT review_queue_resolution_is_complete
        CHECK (num_nulls(resolved_at, resolution, resolver) IN (0, 3)),
    CONSTRAINT review_queue_dedupe_has_counterpart
        CHECK (reason <> 'dedupe_ambiguous' OR related_move_id IS NOT NULL),
    CONSTRAINT review_queue_not_self_paired
        CHECK (related_move_id IS DISTINCT FROM move_id)
);

-- Idempotency: a stage that re-examines the same move must not stack duplicate
-- open items for the same reason.
CREATE UNIQUE INDEX review_queue_one_open_per_reason
    ON review_queue (move_id, reason, coalesce(related_move_id, move_id))
    WHERE resolved_at IS NULL;
CREATE INDEX review_queue_open_idx ON review_queue (surfaced_at)
    WHERE resolved_at IS NULL;


-- ---------------------------------------------------------------------------
-- Pipeline runs
-- ---------------------------------------------------------------------------
-- One row per stage execution. `tracker run-all` creates a parent row and one
-- child per stage, so per-stage yield is visible without parsing logs.

CREATE TABLE pipeline_runs (
    run_id          uuid           PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_run_id   uuid           REFERENCES pipeline_runs (run_id) ON DELETE CASCADE,
    stage           pipeline_stage NOT NULL,
    status          run_status     NOT NULL DEFAULT 'running',
    started_at      timestamptz    NOT NULL DEFAULT now(),
    finished_at     timestamptz,

    items_fetched   integer        NOT NULL DEFAULT 0 CHECK (items_fetched >= 0),
    items_gate_rejected integer    NOT NULL DEFAULT 0 CHECK (items_gate_rejected >= 0),
    moves_created   integer        NOT NULL DEFAULT 0 CHECK (moves_created >= 0),
    moves_merged    integer        NOT NULL DEFAULT 0 CHECK (moves_merged >= 0),
    moves_queued_for_review integer NOT NULL DEFAULT 0
                                   CHECK (moves_queued_for_review >= 0),
    errors          integer        NOT NULL DEFAULT 0 CHECK (errors >= 0),
    error_detail    jsonb          NOT NULL DEFAULT '[]'::jsonb,

    -- CLI arguments for this run, so a backfill or a reclassification job is
    -- reproducible: {"since": "2024-01-01"}, {"taxonomy_version": "1.1.0"}.
    params          jsonb          NOT NULL DEFAULT '{}'::jsonb,
    llm_cost_usd    numeric(10, 4) NOT NULL DEFAULT 0 CHECK (llm_cost_usd >= 0),
    git_sha         text,

    CONSTRAINT pipeline_runs_finished_after_start
        CHECK (finished_at IS NULL OR finished_at >= started_at),
    CONSTRAINT pipeline_runs_running_is_unfinished
        CHECK ((status = 'running') = (finished_at IS NULL))
);

CREATE INDEX pipeline_runs_stage_started_idx ON pipeline_runs (stage, started_at DESC);
CREATE INDEX pipeline_runs_parent_idx ON pipeline_runs (parent_run_id)
    WHERE parent_run_id IS NOT NULL;
CREATE INDEX pipeline_runs_failed_idx ON pipeline_runs (started_at DESC)
    WHERE status IN ('failed', 'aborted');


CREATE TABLE pipeline_run_sources (
    run_id          uuid        NOT NULL REFERENCES pipeline_runs (run_id) ON DELETE CASCADE,
    source_id       uuid        NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    items_fetched   integer     NOT NULL DEFAULT 0 CHECK (items_fetched >= 0),
    items_new       integer     NOT NULL DEFAULT 0 CHECK (items_new >= 0),
    items_gate_passed integer   NOT NULL DEFAULT 0 CHECK (items_gate_passed >= 0),
    http_status     integer,
    -- Set when robots.txt disallowed the fetch, so a zero yield caused by
    -- politeness is not mistaken for a broken feed.
    skipped_reason  text,
    duration_ms     integer     CHECK (duration_ms >= 0),
    error           text,

    PRIMARY KEY (run_id, source_id),
    CONSTRAINT pipeline_run_sources_new_within_fetched
        CHECK (items_new <= items_fetched)
);

CREATE INDEX pipeline_run_sources_source_idx ON pipeline_run_sources (source_id);


-- Zero-yield alert feed. Three consecutive empty runs almost always means the
-- feed URL moved or the layout changed, not that the market went quiet.
CREATE VIEW source_yield_alerts AS
SELECT s.id AS source_id,
       s.slug,
       s.name,
       s.consecutive_zero_yield_runs,
       s.last_polled_at
FROM sources s
WHERE s.active
  AND s.consecutive_zero_yield_runs >= 3;


-- ---------------------------------------------------------------------------
-- LLM spend
-- ---------------------------------------------------------------------------
-- Per-call ledger so the per-run cost ceiling is auditable rather than a number
-- in a log line. A run that would breach the ceiling fails loudly; it never
-- silently processes a truncated set of items.

CREATE TABLE llm_calls (
    id            uuid           PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        uuid           NOT NULL REFERENCES pipeline_runs (run_id) ON DELETE CASCADE,
    raw_item_id   uuid           REFERENCES raw_items (id) ON DELETE SET NULL,
    move_id       uuid           REFERENCES moves (id) ON DELETE SET NULL,
    stage         pipeline_stage NOT NULL,
    purpose       text           NOT NULL,   -- 'extract', 'classify_ambiguous'
    model         text           NOT NULL,
    input_tokens  integer        NOT NULL CHECK (input_tokens >= 0),
    output_tokens integer        NOT NULL CHECK (output_tokens >= 0),
    cost_usd      numeric(10, 6) NOT NULL CHECK (cost_usd >= 0),
    latency_ms    integer        CHECK (latency_ms >= 0),
    succeeded     boolean        NOT NULL,
    error         text,
    created_at    timestamptz    NOT NULL DEFAULT now()
);

CREATE INDEX llm_calls_run_idx ON llm_calls (run_id);
CREATE INDEX llm_calls_created_idx ON llm_calls (created_at DESC);
