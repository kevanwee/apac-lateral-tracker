-- 0011 — Separate "how good is the headline" from "how much of the item did we get".
--
-- Migration 0009 tied them together:
--
--     CHECK (NOT headline_is_derived OR source_access = 'headline_only')
--
-- That was right when a sitemap slug was all we had. It is wrong now that the
-- extract stage fetches the article body for sources whose terms have been
-- reviewed. Those items have a reconstructed headline *and* the full public
-- article, and the constraint forced them to keep claiming headline-only
-- access.
--
-- The cost was not cosmetic. `headline_only` carries a confidence penalty and
-- never auto-accepts, so 215 of 228 records sat in the review queue —
-- 94% against a 15% ceiling — describing evidence we did in fact have.
--
-- They are two independent facts:
--   headline_is_derived  the headline was rebuilt from a URL slug and is lossy
--   source_access        how much of the item the outlet actually gave us
--
-- A record can be both derived-headline and full-text, and that combination is
-- now the common case rather than an impossible one.

ALTER TABLE raw_items
    DROP CONSTRAINT raw_items_derived_headline_is_headline_only;

COMMENT ON COLUMN raw_items.source_access IS
  'How much of the item we actually obtained. Upgraded from headline_only to '
  'full_public when the extract stage successfully reads the article body. '
  'Independent of headline_is_derived, which describes the headline only.';

-- Records how the body was obtained, so a run that read articles can be told
-- apart from one that worked off slugs alone.
ALTER TABLE raw_items
    ADD COLUMN body_read_at timestamptz;

COMMENT ON COLUMN raw_items.body_read_at IS
  'When the article body was last read. The text itself is never stored — see '
  'docs/constraints.md section 2 — only the fact that we had it.';

CREATE INDEX raw_items_body_read_idx ON raw_items (source_id)
    WHERE body_read_at IS NOT NULL;
