-- 0009 — Backfill support.
--
-- The pipeline's operating mode changed: instead of a daily cron over a live
-- feed window, it loads years of archive once and then catches up on demand
-- every few months. Three things follow from that.

-- 1. Some headlines are reconstructed, not published.
--
-- The sitemap adapter rebuilds a headline from a URL slug, which loses
-- punctuation and capitalisation. That is a materially weaker piece of
-- evidence than a headline an outlet wrote, and the difference has to be
-- visible to anything that reads the row — including the coverage banner.
ALTER TABLE raw_items
    ADD COLUMN headline_is_derived boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN raw_items.headline_is_derived IS
  'Headline rebuilt from a URL slug rather than published by the outlet. '
  'Lossy; always paired with source_access = headline_only.';

-- A derived headline is never full evidence, whatever the source tier.
ALTER TABLE raw_items
    ADD CONSTRAINT raw_items_derived_headline_is_headline_only
    CHECK (NOT headline_is_derived OR source_access = 'headline_only');

CREATE INDEX raw_items_derived_idx ON raw_items (source_id)
    WHERE headline_is_derived;


-- 2. A backfill has to be resumable across runs.
--
-- Historical loads are long and get interrupted. The cursor records how far
-- back a source has been walked, so the next run continues rather than
-- restarting. It is advisory: correctness still rests on the url unique
-- constraint, which makes any re-run a no-op regardless.
ALTER TABLE sources
    ADD COLUMN backfilled_to date,
    ADD COLUMN backfill_started_at timestamptz,
    ADD COLUMN backfill_completed_at timestamptz;

COMMENT ON COLUMN sources.backfilled_to IS
  'Oldest publication date successfully ingested from this source. Advisory '
  'resume cursor; idempotency comes from the url unique constraint.';


-- 3. Runs need to say which mode they were.
--
-- A backfill and a catch-up have very different expected yields, and mixing
-- them in one table makes the zero-yield alert meaningless.
ALTER TYPE pipeline_stage ADD VALUE IF NOT EXISTS 'backfill';
