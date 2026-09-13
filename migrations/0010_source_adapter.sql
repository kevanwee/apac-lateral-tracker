-- 0010 — Record which adapter reads each source.
--
-- `sources` knew a source's access_type but not how it is actually read, so
-- the feed_url constraint had to guess. It guessed wrong as soon as the
-- sitemap and mailbox adapters arrived: both legitimately have no feed of
-- their own, and both were rejected.
--
-- The adapter is real information about a source and belongs in the table
-- rather than only in config/sources.yaml. Making the constraint depend on it
-- says what was actually meant: a source read *by a feed adapter* needs a feed
-- URL. Nothing else does.

ALTER TABLE sources
    ADD COLUMN adapter text NOT NULL DEFAULT 'feed'
    CHECK (adapter ~ '^[a-z][a-z0-9_]{1,31}$');

COMMENT ON COLUMN sources.adapter IS
  'Adapter registry key. feed and paginated_feed read a feed; sitemap '
  'enumerates an archive index; imap reads a mailbox folder.';

-- Backfill from what is already known before tightening the rule.
UPDATE sources SET adapter = 'feed' WHERE feed_url IS NOT NULL;

ALTER TABLE sources DROP CONSTRAINT sources_feed_requires_feed_url;

ALTER TABLE sources
    ADD CONSTRAINT sources_feed_adapter_requires_feed_url
    CHECK (adapter NOT IN ('feed', 'paginated_feed') OR feed_url IS NOT NULL);

-- Only one source may be the live feed for a given firm newsroom, but an
-- archive source for the same firm is a different thing and must not collide
-- with it. The tier-1 uniqueness index from 0003 keys on firm_id alone, so
-- narrow it to the live adapters.
DROP INDEX sources_firm_newsroom_key;
CREATE UNIQUE INDEX sources_firm_newsroom_key ON sources (firm_id)
    WHERE firm_id IS NOT NULL
      AND reliability_tier = 1
      AND adapter IN ('feed', 'paginated_feed');
