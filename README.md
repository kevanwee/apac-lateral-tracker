# APAC Lateral Tracker

Partner-level lateral movement intelligence for APAC legal markets, with depth in
Singapore, Hong Kong and Australia.

The pipeline ingests legal trade press and firm newsroom feeds, extracts partner
movements into structured records, deduplicates reports of the same move across
outlets, classifies each move against a fixed versioned practice-group taxonomy,
and surfaces trends through a dashboard. It runs unattended on a schedule with a
human review queue for low-confidence output.

**Precision over coverage.** A false movement record is worse than a missed one,
because the output is used for market analysis that people act on.

## Status

| Phase | Scope | State |
|-------|-------|-------|
| 0 | Compliance constraints | Done — see [docs/constraints.md](docs/constraints.md) |
| 1 | Data model and migrations | Done — see [docs/data-model.md](docs/data-model.md) |
| 2 | Ingestion, extraction, gold set | Done — 3 live sources, [gold set](tests/fixtures/gold_set.yaml) at 29/100 |
| 3 | Taxonomy and classification | Not started |
| 4 | Deduplication | Not started |
| 5 | Trend analytics | Not started |
| 6 | Scheduling and deployment | Not started |
| 7 | Evaluation harness | Not started |

## Quick start

```bash
python -m pip install -e ".[dev]"
cp .env.example .env        # fill in DATABASE_URL
tracker db migrate          # apply migrations
tracker sources sync        # load config/sources.yaml into the database
tracker ingest              # fetch feeds, gate, store item metadata
tracker extract             # pull movement records out of what passed the gate
tracker status              # runs, review queue depth, quiet sources
```

Backfill is the same command with a window, rate limited and resumable:

```bash
tracker ingest --since 2026-01-01
```

## Layout

```
config/         sources.yaml — the whole configuration surface for outlets
docs/           Compliance statement, data model notes
migrations/     Plain SQL, applied in filename order. No ORM-inferred schema.
tracker/        Python package; CLI entry point is `tracker`
  net/          The only place an outbound request is made
  sources/      One adapter per outlet behind fetch() -> list[RawItem]
  extract/      Schema, prompt, span verification, confidence
  pipeline/     Stages and run bookkeeping
tests/          Run against a real Postgres in CI, not mocks
  fixtures/     Gold set and gate cases, versioned
```

## Sources

`tracker sources list` shows the register, including what is not being
collected and why. Adding an outlet or a region is an edit to
[config/sources.yaml](config/sources.yaml), not a code change.

Asian Legal Business — the brief's primary APAC source — is registered but
inactive: the origin returns 403 to our client on every path including
`/robots.txt`, which Phase 0 treats as a disallow. Restoring it is a licensing
conversation, not an engineering one.

## Reading the data

Everything the dashboard shows is derived from `moves`, the canonical
deduplicated record. A move is only canonical while `superseded_by_move_id` is
null; merges create a new canonical row rather than overwriting either input, so
the pre-merge reports stay auditable.

Source article text is never stored. See [docs/constraints.md](docs/constraints.md).
