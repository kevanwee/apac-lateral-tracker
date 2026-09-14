# APAC Lateral Tracker

Partner-level lateral movement intelligence for APAC legal markets, with depth in
Singapore, Hong Kong and Australia.

The pipeline reads legal trade press and firm newsrooms, extracts partner
movements into structured records with span-level provenance, classifies each
against a fixed versioned practice-group taxonomy, and exposes an analysis
surface built for share-of-market trend questions. It costs nothing to run: the
extractor is rule-based, and no model is called anywhere in the default path.

**Precision over coverage.** A false movement record is worse than a missed one,
because the output is used for market analysis that people act on. Every
component abstains rather than guesses; the measured consequence is low recall,
which is the intended trade.

## Status

| Phase | Scope | State |
|-------|-------|-------|
| 0 | Compliance constraints | Done — [docs/constraints.md](docs/constraints.md) |
| 1 | Data model and migrations | Done — 12 plain-SQL migrations, invariants enforced in-database |
| 2 | Ingestion, extraction, gold set | Done at $0 — 14 active sources, 24,730 archive items back to 2019, 206 firms; gold set 69 records / 63 live moves, target 100 (strict xfail) |
| 3 | Taxonomy and classification | Done — taxonomy 1.1.0, 46 practice nodes, 34 sectors, 154 mappings; evidence-ranked classification |
| 4 | Deduplication | Done — blocking, pair scoring, merge with field conflicts kept, team-move detection; `tracker dedupe` |
| 5 | Trend analytics | Analysis views done (`analysis_moves`, `source_period_coverage`, `practice_group_trend`); no dashboard |
| 6 | Scheduling | Manual `catch-up` workflow; quarterly reminder issue, no unattended spend |
| 7 | Evaluation | Done — `tracker evaluate` reports precision, recall and per-field agreement against item-sampled ground truth |

### Data quality, honestly

The corpus under `rules/2.3.0` is **439 canonical records** from 1,639
gate-passed items: 86% classified, 66% with a jurisdiction, 28% with an
origin firm, 2 team moves, zero invariant violations.

**Precision is at the brief's bar. Recall is not, and is the open problem.**

| Measure | `rules/2.1.0` | `rules/2.3.0` |
|---|---|---|
| Person | 0.850 [0.709, 0.929] | **0.980** [0.895, 0.996] |
| Destination firm | 0.975 | **0.980** |
| Record is a partner-level move | 0.775 | **0.940** |
| Recall | not measured | **0.304** |

Precision is a hand audit of 50 records drawn at random from the stored
corpus, each checked against the article text. Recall is separate and is
measured from 40 gate-passed *items* sampled uniformly — including the ones
extraction found nothing in — because a sample drawn from the extractor's own
output cannot see what it missed. `tracker evaluate` reproduces both against
`tests/fixtures/gold_set.yaml` with no database and no network.

Recall of 0.304 means roughly one partner move in three is found. Measured
across the 1,278 rejected items, 45% fail because the headline names a firm
the gazetteer does not hold, so the body is never read, and 44% because no
body template matches a plain sentence such as "Han Kun Law Offices has hired
Zhang Dong at its Shenzhen office". The second is the largest lever left.

Five defect classes were found by audit and each is fixed with a regression
test on the real text: article-cache entries replaying uncut related-article
rails, `counsel` and `director` passing as partner-level, a body naming
someone the outlet's own URL slug contradicts, mentions that refer back to an
earlier hire, and organisation names reaching the person slot.
`docs/methodology.md` §8 lists what a published number must state.

## Quick start

```bash
python -m pip install -e ".[dev]"
cp .env.example .env        # DATABASE_URL (pooled) and DATABASE_URL_DIRECT
tracker doctor              # checks config, connectivity, migrations
tracker db migrate
tracker sources sync        # config/sources.yaml -> sources
tracker firms --sync        # config/firms.yaml   -> firms, firm_aliases
tracker taxonomy --load     # taxonomy/*.yaml     -> practice_groups, sectors
tracker backfill --since 2019-01-01   # walk the archives; resumable
tracker extract             # read gate-passed items; $0
tracker status
```

Tests need a throwaway Postgres: the suite drops the `public` schema, and
`tests/conftest.py` refuses to run it against anything but a local host.

```bash
docker run -d -e POSTGRES_PASSWORD=pg -p 5432:5432 postgres:16
TRACKER_TEST_DATABASE_URL=postgresql://postgres:pg@localhost/postgres pytest -q
```

## Operating model

A historical record refreshed on demand, not a daily feed. Load the archives
once, then `tracker catch-up` every few months. Every stage is idempotent:
`raw_items.url` is unique, extraction is keyed per (item, person), and
re-running costs time and nothing else.

When an extractor defect is found, the fix is followed by a **re-extraction**,
never an in-place repair: every field on a wrong record came from the same bad
parse.

```bash
tracker reextract --reason "boundary bug in firm gazetteer"          # dry run
tracker reextract --reason "boundary bug in firm gazetteer" --apply  # discard and requeue
tracker extract
```

## How extraction works

1. **Gate** (`tracker/gate.py`) — tuned for recall, the opposite of everything
   downstream. Roughly one item in twenty passes. Outlets that slug entities
   instead of headlines (Asia Business Law Journal) use a shape gate: known
   firm + known place + room for a name.
2. **Text** — the feed summary if the outlet gave one; the article page only
   for a source whose terms a human has reviewed (`html_access_reviewed_at`).
   The page's `<h1>` replaces a slug-derived headline. Bodies are cut at the
   byline and at the outlet's trailing rails so other articles' names cannot
   bleed in. Article text is never stored.
3. **Rules** (`tracker/extract/rules.py`, `body_rules.py`) — fourteen headline
   templates and a set of sentence-level body templates. A match *is* a
   character span, so provenance is native. Firms resolve through the
   gazetteer or the record is not made; direction comes from cue words, not
   from which firm was mentioned first.
4. **Person slot guards** — every word list in `rules.py` was added because a
   real headline produced a wrong record (`Qic gc`, `even dozen`,
   `Most Popular Malaysia`). Nothing speculative.
5. **Classification** — stated practice clause first, then the headline, then
   the sentences about the person, each at a lower weight; the evidence kind
   is stored with the assignment. Bare, ambiguous words map to the parent
   group.
6. **Confidence and review** — a function of evidence quality (source tier,
   access level, span quality), not of record richness. Unknown firms and
   unplaceable practices go to the review queue, whose resolutions are meant
   to grow the gazetteer and the mapping file.

## Sources

`tracker sources list` shows the register. Adding an outlet or a market is an
edit to [config/sources.yaml](config/sources.yaml).

| Source | Route | Depth | Notes |
|---|---|---|---|
| Asia Business Law Journal + archive | feed, sitemaps, article pages | 2019 | The strongest reachable APAC source: HK, SG, CN, JP, KR, ID |
| Australasian Lawyer + archive | feed, year sitemaps, article pages | 2019 | AU/NZ; headlines rarely name the person, the body does |
| Law.com (7 ALM titles) | feeds, article pages | live window only (20 items per title) | Named in the brief; names both firms in the headline |
| Rajah & Tann Asia | paginated WordPress feed | 2020 | Tier 1 — firm newsroom |
| Global Legal Post, Legal Business | feeds | live window | |
| Asian Legal Business | — | — | 403 on every path including robots.txt; not circumvented. Newsletter (IMAP) adapter built, needs a subscription |
| Global Legal Post archive | — | — | Client-fingerprint block on `/sitemap.xml`; not circumvented |

## Reading the data

Read `analysis_moves`, not `moves`. It joins each canonical move to its primary
practice group, primary source, tier and date quality, and exposes — rather
than applies — the filters a question needs. `practice_group_trend` gives share
of classified moves per source per quarter; `source_period_coverage` is the
denominator every series must be shown against. The reasoning, and the biases
no query can remove, are in [docs/methodology.md](docs/methodology.md).

## Layout

```
config/         sources.yaml, firms.yaml, places.yaml
taxonomy/       practice_groups.yaml, sectors.yaml, taxonomy_mappings.yaml (one version)
docs/           constraints (Phase 0), data-model (Phase 1), methodology (Phase 5), setup
migrations/     Plain SQL, applied in order, checksummed, immutable once applied
tracker/        Python package; CLI entry point is `tracker`
  net/          The only place an outbound request is made
  sources/      One adapter per outlet behind fetch() -> list[RawItem]
  extract/      Rules, body rules, role parsing, spans, confidence
  pipeline/     Stages and run bookkeeping
tests/          Real Postgres in CI; guarded against non-local databases
CLAUDE.md       Working rules for anyone (or anything) changing this repo
```
