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
| 2 | Ingestion, extraction, gold set | Not started |
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
tracker db status           # show applied / pending migrations
```

## Layout

```
docs/           Compliance statement, data model notes, source register
migrations/     Plain SQL, applied in filename order. No ORM-inferred schema.
tracker/        Python package; CLI entry point is `tracker`
tests/          Invariant tests run against a real Postgres in CI
```

## Reading the data

Everything the dashboard shows is derived from `moves`, the canonical
deduplicated record. A move is only canonical while `superseded_by_move_id` is
null; merges create a new canonical row rather than overwriting either input, so
the pre-merge reports stay auditable.

Source article text is never stored. See [docs/constraints.md](docs/constraints.md).
