# Handoff — finishing the pipeline and retrieving the full record

Written 2026-09-14 for the next session. Read `CLAUDE.md` first; it is short
and every rule in it was earned on this data. Then this document. Then
`docs/methodology.md` before touching anything analytical.

The user wants two things, in this order: **the complete record** (every
partner move the reachable sources report, extracted at ≥0.98 precision on
`person` and `to_firm`), and **the remaining phases** (dedupe, analytics,
evaluation). They have said: *"I do not wish to keep rerunning the extraction
on the base corpus"* — get each fix right, then run once. Report numbers and
decisions to overrule at each checkpoint, not summaries.

## 1. State on 2026-09-14

Repo `github.com/kevanwee/apac-lateral-tracker`, private, `main`, CI green at
317 tests + 2 strict xfails. Database is Neon Postgres (free tier,
ap-southeast-1); `DATABASE_URL` (pooled) and `DATABASE_URL_DIRECT` in `.env`,
never in chat. Migrations applied through **0012**. Taxonomy **1.1.0** is
current (1.0.0 kept, nothing assigned to it any more). 174 firms / 171
aliases seeded. Everything runs at **$0** — rule extractor, no API key.

### A re-extraction is running — check it before anything else

`rules/2.0.0` produced 285 records of which 21% carried a wrong firm (see §3
of this doc). All 285 were discarded with `tracker reextract --apply` and
every gate-passed item was requeued. `tracker extract --extractor rules` was
then started in the background, logging to `reextract_2_1_0.log` in the repo
root. It processes ~1,640 items at the 10 s/origin floor with 858 article
bodies already in `article_cache.json`; expect 2–4 hours total.

```bash
tail -3 reextract_2_1_0.log                       # "exit=0" on the last line when done
tracker status                                    # last run row: stage=extract
python - <<'EOF'
from tracker.db import connect
with connect() as c:
    print(c.execute("select processing_state, count(*) from raw_items where gate_passed group by 1").fetchall())
    print(c.execute("select count(*) total, count(*) filter (where not practice_unclassified) classified, "
                    "count(*) filter (where office_jurisdiction is not null) with_j, "
                    "count(*) filter (where from_firm_id is not null) with_origin from analysis_moves").fetchone())
EOF
```

At handoff time: 79 items extracted → 91 moves, 87 classified, 80 with
jurisdiction, 28 with origin, 170 items rejected (no named move), 1,390
pending. If the process died (no `exit=` line, no python process), just run
`tracker extract --extractor rules` again — it is idempotent per (item,
person) and resumes from the cache.

**When it finishes, the very next task is a precision audit** (§4, step 1).
Do not build on the records before that.

### Two hygiene items left by this session

- **221 orphaned `people` rows.** `reextract` deleted moves but not the
  people they pointed at. The command now deletes orphans on `--apply`
  (commit after this handoff), but the 221 from the run already done are still
  there. Delete them once the extraction has finished (a person a new move
  re-creates is fine; the row is recreated by `_resolve_person`):
  `DELETE FROM people p WHERE NOT EXISTS (SELECT 1 FROM moves m WHERE m.person_id = p.id)`.
- **`article_cache.json`** (858 bodies) should be purged with
  `tracker cache purge` when this campaign's extraction and audit are done.
  Retention rule is in `docs/constraints.md` §2.

## 2. Sources: what is reachable, what is in, what is not

| Source | Adapter | Items | Gate-passed | Depth | Status |
|---|---|---|---|---|---|
| `asia-business-law-journal-archive` | sitemap + article pages | 16,677 | 888 | 2019→ | **Backfill cursor never recorded** (`backfilled_to` null). 32 post-sitemaps were walked with translations excluded; verify completeness (§4 step 2) |
| `australasian-lawyer-archive` | year sitemaps + article pages | 7,780 | 698 | 2019→ | Backfill complete 2026-09-13 |
| `rajah-tann-asia` | paginated WordPress feed | 155 | 41 | 2020→ | Complete. Tier 1 |
| `global-legal-post` | feed | 68 | 5 | live window | 15 of 68 items have a *section* URL (`/region/...`), not an article — see §5 |
| `law-com-*` (7 ALM titles) | feed + article pages | 32 | 7 | **20 items per title, no pagination, sitemaps 404** | Only 2 of 7 titles have ever returned items; run `tracker ingest` to poll the rest |
| `asia-business-law-journal` (live feed) | feed | 0 | — | live | Never polled; run `tracker ingest` |
| `legal-business` | feed | 0 | — | live | Never polled |
| `australasian-lawyer` (live feed) | feed | 18 | 0 | live | Litigation-heavy, expected |
| `asian-legal-business` | — | — | — | — | **Blocked**: 403 on every path incl. robots.txt. Not circumvented. Do not try |
| `alb-newsletter` | IMAP | 0 | — | as far as the subscription | Built, inactive. Needs the user to subscribe and set `IMAP_USERNAME`/`IMAP_PASSWORD`; folder `legal-tracker/alb` |
| `global-legal-post-archive` | — | — | — | — | **Blocked**: client-fingerprint 403 on `/sitemap.xml`. Not circumvented |

Rules that are not negotiable, from the user and Phase 0: no UA rotation, no
browser impersonation, unretrievable robots.txt = disallow, 10 s/origin floor,
HTML reading only where `html_access_reviewed_at` is set by the user.

## 3. What went wrong before, so it does not go wrong again

Each of these is fixed and has a regression test; the list is here so the
pattern is recognised if it recurs in a new form.

1. **`(?<!\w)a|b|c(?!\w)`** — the firm gazetteer's alternation had no group,
   so only the outer surfaces were boundary-checked. `EY` matched inside
   *Cooley*. 60 of 285 records affected. `tests/test_firm_boundaries.py`.
2. **Shell-mediated edits corrupt regex escapes** — five times. `\b` → `\x08`,
   `\n` → newline. Use the Edit/Write tools for anything with a backslash.
   `test_no_source_file_contains_a_stray_control_character` catches the first.
3. **The test suite drops `public`** — it once ran against production (nothing
   was lost; nothing had been collected). `conftest.DestructiveTestGuard`
   refuses non-local hosts. Use `TRACKER_TEST_DATABASE_URL`.
4. **CI was red for ten commits and nobody looked.** `gh run watch` after
   every push. Green is the only acceptable state to report from.
5. **Neon's pooler drops idle connections** during 10 s-per-request loops.
   Stages call `db.live()` after a fetch; `_ingest_one` must return the live
   connection to its caller.
6. **Sampling error** — I judged ABLJ "mostly Chinese" from its oldest
   sitemap. English is at the root; `/zh-hans/`, `/ja/`, `/ko/` are
   translations. Sample across the range before generalising.
7. **A view's column list freezes at creation.** `SELECT *` in
   `canonical_moves` did not see `extractor_version` until re-created (0012).

## 4. The path to the complete record, in order

### Step 1 — Precision audit of `rules/2.1.0` (blocking; do first)

Sample 50 records at random from `analysis_moves`, print person, from, to,
title, jurisdiction, practice, evidence kind, headline and URL, and verify
each against the page by hand. Record precision on `person` and `to_firm`
separately, and on `from_firm`, `office_jurisdiction` and `practice_group_top`.
The brief's bar is **≥0.98 on person and to_firm**. Under it, find the
failure class, add it as a regression test with the real headline, fix, and
`tracker reextract --source <slug> --apply` for the affected source only —
not the whole corpus. Put the measured numbers in the README "Data quality"
section and in the commit message.

Known residual risks to look for: names glued to breadcrumbs the
`NAVIGATION_WORDS` list does not know; a body's related-article rail that
`_TAIL_MARKERS` does not cut (the symptom is one person on several unrelated
headlines); "X replaces Y" recorded as Y moving.

### Step 2 — Verify the ABLJ archive is complete, then poll every live feed

`sources.backfilled_to` is null for the ABLJ archive because the sitemap
walk did not record its cursor. Count URLs across the 32 `post-sitemapN.xml`
files minus the translation prefixes and compare with 16,677 `raw_items`. If
short, `tracker backfill --only asia-business-law-journal-archive --since
2019-01-01` — it is idempotent on URL. Then `tracker ingest` to poll the
seven Law.com titles, the ABLJ live feed and Legal Business, which have never
been fetched. Then `tracker extract`.

### Step 3 — Recover the ABLJ headlines that name nobody

Most of ABLJ's 888 gate-passed items were "rejected: no named move" (170 so
far). Many are real moves whose body sentence shape is not in
`body_rules.py`. Take 100 rejected ABLJ items, read the cached bodies, and
tally the sentence shapes that name a person. Add templates only for shapes
that are unambiguous, each with a test on the real sentence. Then
`tracker reextract --source asia-business-law-journal-archive --apply`.
This is the largest remaining yield lever inside the current sources.

### Step 4 — ALB through the newsletter

Ask the user to subscribe and filter the newsletter into the IMAP folder.
Activate `alb-newsletter` in `config/sources.yaml`, `tracker sources sync`,
`tracker ingest --only alb-newsletter`. Headlines are real (link text), no
body: expect headline-template yield only, and `headline_only` access, so
these never auto-accept. That is correct.

### Step 5 — Firm newsrooms (tier 1) for the depth markets

Only Rajah & Tann is a tier-1 source. The brief's Singapore/Hong Kong/
Australia depth needs the newsrooms of the top ~20 firms in each; a firm's
own announcement is the most precise source there is and never paywalled.
Each is a `sources.yaml` entry (`adapter: feed` where WordPress exposes
`/feed`, otherwise a small HTML adapter behind `html_access_reviewed_at`).
Check robots.txt and terms per firm before enabling; record the date. This is
also how the gold set reaches 100 (Step 8).

### Step 6 — Phase 4, deduplication

Schema is ready (`superseded_by_move_id`, `field_conflicts`, `merged_at`,
`canonical_moves`, `moves_blocking_idx`, `review_reason = 'dedupe_ambiguous'`).
`tracker/names.py` is an honest stub: given-name-first only. Build:

- blocking on `(surname_normalised, to_firm_id, announced_date ± 60 days)`;
- scoring on given-name agreement (with bracketed Western names and
  surname-first ordering), origin agreement, title agreement;
- merge creates a **new** canonical row, marks inputs superseded, keeps losing
  values in `field_conflicts`, recomputes `confidence` with the corroboration
  bonus (`confidence.CORROBORATION_STEP`), and routes ambiguous pairs to the
  review queue with `related_move_id`;
- team-move detection: ≥2 canonical moves with the same firm pair inside 60
  days → `team_moves` row (triggers maintain `partner_count`).

Until this runs, no cross-source trend is reportable (methodology §1).

### Step 7 — Phase 5, dashboard

The analysis surface exists (`analysis_moves`, `source_period_coverage`,
`practice_group_trend`). Build the dashboard on those views only, with the
filters from methodology §2 as visible controls, the unclassified share and
the gate-passed denominator shown on every chart, and the firm-to-firm flow
matrix from `canonical_moves`. Depth-market filter is
`office_jurisdiction LIKE 'SG' | 'HK' | 'AU%'`. `.gitignore` already
anticipates Next.js; a static SQL-to-JSON export plus a small page is enough
and costs nothing to host.

### Step 8 — Phase 7, evaluation

Gold set is 29/100 live records (`tests/fixtures/gold_set.yaml`, strict
xfail on size). Fill it from Step 5's newsroom items and from verified
Step 1 audit records — real URLs, text capped at 25 words, `expect` derived
only from the text. Then implement precision/recall per field against it,
classification accuracy, and dedupe pair accuracy, as a `tracker evaluate`
command that prints a table and fails under the 0.98 bar.

### Step 9 — Phase 6, run it on the schedule that exists

`.github/workflows/catch-up.yml` is manual with a quarterly reminder issue.
`tracker catch-up` with a two-week overlap. Nothing else is needed; do not add
a daily cron — the user does not want one.

## 5. Open oddities, not yet chased

- Global Legal Post feed gives a section URL for 15 of 68 items
  (`/region/latin-america/mexico`); the record built from one is correct but
  the "source link for verification" the user asked for points at a section.
  Fix in `tracker/sources/feed.py`: prefer `<link>` over `<guid>` or vice
  versa — check which carries the article.
- `people=307` while moves=91 (the orphans in §1).
- `announced_date` is the outlet's publication date; `published_at` on the
  ABLJ sitemap is `lastmod`, marked estimated where regenerated in bulk. No
  estimated dates in the current moves, but re-check after Step 2.
- `GATE_VERSION` was not bumped when `evaluate_entity_slug` was added.

## 6. Decisions the user has made — do not re-litigate

No Claude co-author trailer. Lawful access only, nothing circumvented. $0 by
default. No daily cron. Two names in one article are two records. Every
record carries its source URL. Precision over coverage — abstain rather than
guess — but 23 records from 1,000 items was called "failing quite severely",
so recall matters once precision is held. The user reviews terms per source
and sets `html_access_reviewed_at` themselves.

## 7. Decisions of mine the user may still overrule (ask at the first checkpoint)

- `article_cache.json` exists at all (retention rule documented, purge command
  provided).
- Surnames that are countries are rejected as people (`Matt Spain`, xfail).
- Rule-path records auto-accept with no extractor-reliability prior; Step 1
  is what would justify one.
- Headline-derived classification at weight 0.75; level-2 series should
  require `stated` evidence.
- Taxonomy 1.1.0 maps bare "corporate"/"litigation"/"tax"/"compliance" to the
  parent group.
