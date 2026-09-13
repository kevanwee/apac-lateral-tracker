# Handoff: APAC partner lateral-movement pipeline, Phases 0–2 complete, awaiting direction on Phase 3

## Objective

Build a production pipeline that ingests legal trade press and firm announcements,
extracts partner-level lateral movements into structured records, deduplicates
reports of the same move across outlets, classifies them against a fixed versioned
practice-group taxonomy, and surfaces trends through a dashboard. Primary scope is
APAC with Singapore, Hong Kong and Australia as depth markets. The brief is
checkpoint-driven: stop after each phase group and report **evaluation numbers and
decisions the user should overrule** — not a summary of what was built.

## Current State

Repo: **https://github.com/kevanwee/apac-lateral-tracker** (private, `main`).
CI green: **195 tests + 1 deliberate xfail**, run against a real Postgres 16
service container on every push. Nothing is mocked.

| Phase | State |
|---|---|
| 0 Constraints | Done — `docs/constraints.md` |
| 1 Schema | Done — 10 SQL migrations, invariants enforced by constraints + deferred triggers |
| 2 Ingestion + extraction + gold set | Done — see below |
| 3 Taxonomy & classification | **Not started** |
| 4 Deduplication | **Not started** |
| 5 Trend analytics | **Not started** |
| 6 Scheduling | Catch-up workflow done; dashboard not started |
| 7 Evaluation | Gold set + calibration guards exist; no extraction precision/recall yet |

**Sources** (`config/sources.yaml` is the whole config surface):
- `rajah-tann-asia` — tier 1 firm newsroom, `paginated_feed`, reaches **April 2020**
- `global-legal-post`, `australasian-lawyer` — tier 2 live feeds
- `australasian-lawyer-archive`, `global-legal-post-archive` — `sitemap`, reach **2019**
- `asian-legal-business` — registered **inactive**, 403s our client on every path
- `alb-newsletter` — `imap`, built and **off by default** pending credentials

**Measured numbers so far:**
- Relevance gate: 83% of live items rejected; 96% precision, 100% recall on the fixture
- Sitemap enumeration: 2,868 AU URLs since 2024 from **3 HTTP requests**
- Rule extractor on 192 live gate-passed headlines: **10 records, 10 correct, 0 false positives, 95% abstention, $0.00**
- Gold set: **29 records / 33 moves** (23 live, 6 synthetic) against a target of 100

## Key Decisions Made

Do not re-litigate. Full rationale in `docs/data-model.md` ("Decisions worth contesting").

1. **No Claude co-author trailer** on any commit or PR. User instruction, overrides default.
2. **ALB is not scraped.** It 403s including `/robots.txt`; Phase 0 treats unretrievable robots as disallow. **Never vary the User-Agent to get past it.** The user's TDM/copyright argument is sound but Singapore's s.244 exception needs *lawful access* first. The IMAP newsletter is the lawful route.
3. **No daily cron.** This is a historical record refreshed on demand: `tracker catch-up`, plus a quarterly workflow that opens a reminder issue rather than spending money unattended.
4. **Article text is never stored.** `raw_items` has no body/content/summary column — absence *is* the policy. Provenance excerpts capped at 25 words by CHECK constraint.
5. `unclassified` is a **real taxonomy node**, not a null, so unclassified moves stay in chart denominators and "exactly one primary practice group" stays enforceable.
6. `to_firm` is nullable **only** for `retirement`; promotions must be within one firm; `move_type` must agree with firm kind at each end (trigger).
7. **Merges never overwrite.** A canonical row is created; inputs keep their values and point at it via `superseded_by_move_id`. `canonical_moves` view is what analytics read.
8. Tier 1 is reserved for a firm's own newsroom, by CHECK constraint.
9. Jurisdictions are a **table**, not free text. Adding a market costs a migration — this already bit once (LA/KH/MM) and that was the design working.
10. **Confidence was recalibrated** after measurement: completeness used to be 25% of the score and put 79% of *perfect* extractions into review against a 15% ceiling. It is now a 0.05 bonus. Dropped-span count and model self-doubt force review outright.
11. **Span verification is the fabrication guard, not the model.** It is provider-agnostic, which makes a cheaper/weaker model safer here than usual.
12. Default LLM is `claude-opus-5`. Rules-first extraction (`--extractor rules|llm|auto`) is the free path.

## Constraints & Preferences

- **Precision over coverage.** A false movement record is worse than a missed one.
- **Measure, don't assert.** Every claim in a checkpoint report needs a number behind it. Do not report accuracy from a fixture you also authored — say so instead.
- Plain SQL migrations, **immutable once applied** (checksummed). Add a new one; never edit.
- Tests run against real Postgres in CI. An invariant only asserted in Python is not an invariant.
- Politeness is enforced in `tracker/net/client.py`, not per adapter: 10s/origin floor, robots cached 24h, unretrievable robots = disallow, no UA rotation, no paywall circumvention, no headless rendering.
- `html` sources stay blocked until `html_access_reviewed_at` is set by a human.
- Commit in **small logical increments** ("slowly phase in the code pushes"), push, and watch CI before reporting.
- ruff rule set is pinned in `pyproject.toml`; keep it green.
- Heredocs in the Bash tool break on long multi-file writes — use the Write tool for files over ~100 lines.

## Open Threads / Questions

1. **No API credentials of any kind.** No `ANTHROPIC_API_KEY`, no `ant` CLI, no profile. Everything LLM-dependent is therefore unmeasured.
2. **No extraction precision/recall.** Phase 7 requires >0.98 on `to_firm` and `person`. Blocked on (1).
3. **Gold set is 29/100.** `test_gold_set_has_reached_its_target_size` is a strict xfail so the gap stays visible. Filling it needs a DB-backed backfill or ALB access.
4. **Two things offered, neither accepted yet:**
   - `--extractor local` Ollama provider. User has an **RTX 5070 Ti (16GB)** + 31GB RAM — runs Qwen3 14B comfortably, genuinely $0, nothing leaves the machine. Not built because it can't be tested on hardware I don't have.
   - HTML article-page adapter behind the `html_access_reviewed_at` gate, to turn the ~27,000 headline-only sitemap leads into full records.
5. **Review-queue rate on a cold database is unmeasured.** Gazetteer seeding (`tracker firms --sync`, 120 firms) should fix the every-move-goes-to-review problem, but nobody has run it end to end.
6. `tracker/names.py` is an **honest stub** — assumes given-name-first, wrong for surname-first names. Under-merges rather than over-merges. Phase 4 replaces it; `gold-s001` and `gold-s004` are its tests.

## Immediate Next Step

Read `README.md`, `docs/constraints.md` and `docs/data-model.md` to load the design,
then **ask the user to pick one** — do not start building without an answer, because
both are substantial and they conflict:

- **(a)** Build the `--extractor local` Ollama provider, completing the $0 path they
  have been pushing toward for the last three turns.
- **(b)** Proceed to **checkpoint 3** — Phase 3 classification (taxonomy YAML,
  `taxonomy_mappings.yaml`, deterministic rule-first classification) and Phase 4
  dedupe (blocking, scoring, name normalisation), with Phase 7 metrics against the
  gold set. This needs an API key for the metrics to mean anything.

Their last message asked whether *any* of this can be free, so (a) is the likelier
want — but the checkpoint protocol says Phase 3 is next, so confirm rather than assume.
