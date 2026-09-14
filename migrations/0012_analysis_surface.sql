-- 0012 — Record which extractor made each move, and give analysis one surface.
--
-- Two findings from the first audit of stored records drove this migration.
--
-- 1. Nothing recorded which extractor version produced a move. When 21% of
--    the stored rows turned out to carry a wrong firm, there was no way to
--    select "everything rules/2.0.0 wrote" except by date. Evaluation (Phase 7)
--    needs to attribute precision to a version, so the version goes on the row.
--
-- 2. Every analytical query had to re-derive the same joins and the same
--    methodology decisions — canonical rows only, primary practice group under
--    its own taxonomy version, the primary source's tier and date quality —
--    and each re-derivation was a chance to get one of them wrong. The views
--    below make the methodology executable in one place.
--
-- The views are deliberately not materialised. The dataset is refreshed a few
-- times a year and is small; a materialised view is a cache to get stale.

ALTER TABLE moves ADD COLUMN extractor_version text;

COMMENT ON COLUMN moves.extractor_version IS
  'Which extractor wrote this row: "rules/2.1.0", or a model id. Null on rows '
  'created by a Phase 4 merge, which have no single extractor.';

CREATE INDEX moves_extractor_version_idx ON moves (extractor_version)
    WHERE extractor_version IS NOT NULL;

-- A view's column list is frozen when it is created, so `SELECT *` in
-- canonical_moves (0005) does not see the new column until the view is
-- re-created. OR REPLACE may append columns, which is all this does.
CREATE OR REPLACE VIEW canonical_moves AS
SELECT * FROM moves WHERE superseded_by_move_id IS NULL AND review_state <> 'rejected';


-- ---------------------------------------------------------------------------
-- analysis_moves — one row per canonical move, with everything a trend query
-- needs and the flags that decide whether the row should be in that query.
-- ---------------------------------------------------------------------------
-- The flags are exposed rather than applied. `review_state`, `confidence`,
-- `classification_confidence` and `date_is_estimated` are all left to the
-- consumer, because the right floor depends on the question: a headline
-- count can tolerate an estimated date, a monthly series cannot.
--
-- `practice_group_top` is the level-1 code. Most trend questions should be
-- asked at this level: a bare "corporate partner" classifies to `corporate`
-- and would be missed by a query filtered on `corporate.m_and_a`.

CREATE VIEW analysis_moves AS
SELECT m.id,
       m.person_id,
       m.from_firm_id,
       m.to_firm_id,
       m.move_type,
       m.title_to,
       m.office_jurisdiction,
       m.announced_date,
       date_trunc('month',   m.announced_date)::date AS period_month,
       date_trunc('quarter', m.announced_date)::date AS period_quarter,
       m.review_state,
       m.confidence,
       m.extractor_version,
       m.classification_state,
       m.classified_taxonomy_version,
       pg.code                                      AS practice_group_code,
       split_part(pg.code, '.', 1)                  AS practice_group_top,
       coalesce(pg.is_unclassified, true)           AS practice_unclassified,
       mpg.confidence                               AS classification_confidence,
       mpg.rule_key                                 AS classification_rule_key,
       -- "stated", "headline" or "body": where the practice evidence came from.
       split_part(mpg.rule_key, ':', 1)             AS classification_evidence,
       s.slug                                       AS source_slug,
       s.reliability_tier,
       ri.url                                       AS source_url,
       ri.published_at_is_estimated                 AS date_is_estimated,
       ri.headline_is_derived
FROM canonical_moves m
LEFT JOIN move_practice_groups mpg
       ON mpg.move_id = m.id AND mpg.is_primary
LEFT JOIN practice_groups pg
       ON pg.id = mpg.practice_group_id
LEFT JOIN move_sources ms
       ON ms.move_id = m.id AND ms.is_primary
LEFT JOIN raw_items ri
       ON ri.id = ms.raw_item_id
LEFT JOIN sources s
       ON s.id = ri.source_id;

COMMENT ON VIEW analysis_moves IS
  'The analytics surface. Canonical, non-rejected moves with their primary '
  'practice group and primary source. Filters are exposed, not applied.';


-- ---------------------------------------------------------------------------
-- source_period_coverage — the denominator.
-- ---------------------------------------------------------------------------
-- A count of moves per quarter measures how much the corpus covers that
-- quarter at least as much as it measures the market. Law.com feeds reach
-- back twenty items; the ABLJ archive reaches 2019. Any series that does not
-- divide by this, or restrict itself to sources with uniform depth over the
-- window, is a chart of source composition.

CREATE VIEW source_period_coverage AS
SELECT s.slug                                        AS source_slug,
       s.reliability_tier,
       date_trunc('month',   ri.published_at)::date  AS period_month,
       date_trunc('quarter', ri.published_at)::date  AS period_quarter,
       count(*)                                      AS items,
       count(*) FILTER (WHERE ri.gate_passed)        AS gate_passed,
       count(*) FILTER (WHERE ri.processing_state = 'extracted') AS items_with_moves,
       count(*) FILTER (WHERE ri.published_at_is_estimated)      AS items_date_estimated
FROM raw_items ri
JOIN sources s ON s.id = ri.source_id
GROUP BY s.slug, s.reliability_tier,
         date_trunc('month', ri.published_at), date_trunc('quarter', ri.published_at);

COMMENT ON VIEW source_period_coverage IS
  'Items per source per period. The denominator for every trend series; '
  'a move count that is not normalised by this is a chart of source depth.';


-- ---------------------------------------------------------------------------
-- practice_group_trend — share of classified moves, per source and quarter.
-- ---------------------------------------------------------------------------
-- Share within (source, quarter) is the unit that survives the two things the
-- corpus cannot control: how many items a source published that quarter, and
-- which sources exist for that quarter at all. Unclassified rows are reported
-- alongside so their weight is visible rather than hidden in a denominator.

CREATE VIEW practice_group_trend AS
WITH base AS (
    SELECT source_slug, period_quarter, practice_group_top, practice_unclassified
    FROM analysis_moves
    WHERE announced_date IS NOT NULL
)
SELECT source_slug,
       period_quarter,
       practice_group_top,
       count(*)                                                      AS moves,
       sum(count(*)) OVER (PARTITION BY source_slug, period_quarter) AS moves_in_period,
       sum(count(*)) FILTER (WHERE NOT practice_unclassified)
           OVER (PARTITION BY source_slug, period_quarter)            AS classified_in_period,
       CASE WHEN practice_unclassified THEN NULL
            ELSE round(
                count(*)::numeric
                / nullif(sum(count(*)) FILTER (WHERE NOT practice_unclassified)
                             OVER (PARTITION BY source_slug, period_quarter), 0),
                4)
       END                                                           AS share_of_classified
FROM base
GROUP BY source_slug, period_quarter, practice_group_top, practice_unclassified;

COMMENT ON VIEW practice_group_trend IS
  'Practice-group share of classified moves per source per quarter. Share, '
  'not count: counts track source depth. Unclassified rows are kept so their '
  'weight is visible.';
