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

## 1. State on 2026-09-14 (end of the second session)

Repo `github.com/kevanwee/apac-lateral-tracker`, private, `main`, CI green at
**530 tests + 4 strict xfails**. Database is Neon Postgres (free tier,
ap-southeast-1); `DATABASE_URL` in `.env`, never in chat. Migrations applied
through **0012**. Taxonomy **1.1.0** is current. **206 firms** seeded.
Everything runs at **$0** — rule extractor, no API key.

### Precision is at the bar; recall is the open problem

The corpus is **439 canonical records** under `rules/2.3.0` from 1,639
gate-passed items, after four corrective re-extractions and one dedupe pass.

| Measure | 2.1.0 | **2.3.0** | How it was measured |
|---|---|---|---|
| Person precision | 0.850 | **0.980** | 50 stored records, hand-audited against the article text |
| Destination firm | 0.975 | **0.980** | same sample |
| Record is partner-level | 0.775 | **0.940** | same sample |
| **Recall** | — | **0.304** | 40 gate-passed *items* sampled uniformly, hand-adjudicated |

Reproduce both with `tracker evaluate` (no database, no network): it reports
precision 1.000 and recall 0.304 against `tests/fixtures/gold_set.yaml`.

| | |
|---|---|
| moves | 439 canonical (447 rows; 8 superseded by 4 merges) |
| classified / jurisdiction / origin | 86% / 66% / 28% |
| team moves | 2 |
| review queue | 61 `unclassified_practice`, 1 `dedupe_ambiguous` |
| invariant violations | 0 |

**Recall is where the work is.** Across the 1,278 rejected items: 45% fail
because the headline names a firm the gazetteer does not hold, so the body is
never read; 44% because no body template matches a plain hire sentence. See
§4 Step 3 — it is now the largest remaining lever by a wide margin.

### Five defect classes found and fixed, each with a regression test

1. **The article cache froze parsing.** It stores the *parsed* body, so a
   cache hit skipped `body_of` and replayed weeks-old behaviour: 129 of 1,586
   bodies still carried a related-article rail, and 11 records named a partner
   who appears nowhere else. `trim_tail` is now re-applied on cache read.
2. **`counsel` and `director` passed as partner-level** — 17 records.
3. **A body naming someone the outlet's own URL slug contradicts** — 22.
4. **Mentions referring back to an earlier hire** — 13.
5. **Organisation names in the person slot** — 3.

### Hygiene

- **`people` orphans: 0.** `reextract --apply` deletes them itself.
- **`article_cache.json`** (1,586 bodies) is kept indefinitely by decision;
  `trim_tail` on read means stale entries are no longer a hazard. Purge with
  `tracker cache purge`.
- **Surname keys need no backfill.** The re-extraction recreated every person
  row under the new name rules; 0 stale keys.

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

### Step 1 — Precision audit — **DONE**

Person 0.980, destination firm 0.980 on a 50-record hand audit of
`rules/2.3.0`. Five defect classes found and fixed; see §1. The original
instructions are kept below because the method is the one to repeat.

#### Method (repeat this after any extraction change)

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

### Step 3 — Body template coverage — **THE LARGEST REMAINING LEVER**

Now measured rather than estimated: 557 of 1,278 rejected items (44%) have a
resolved destination firm and a cached body, and fail only because no body
template matches the sentence. Real examples, each a plain hire:

- "Han Kun Law Offices has hired Zhang Dong at its Shenzhen office"
- "Thomson Geer has appointed Clayton Utz lawyer Cameron Forbes as a partner"
- "Ian Bennett, Catherine Morton, Jehan Mata and Andrew Ferguson recently
  joined the partnership" (four in one sentence)

A further 576 (45%) fail earlier still, because the headline names a firm the
gazetteer does not hold and `_from_body` returns before reading the body.
32 firms were added this session (+38 records); the tail is boutiques.

Add templates only for shapes that are unambiguous, each with a test on the
real sentence, then re-extract once. `tracker evaluate` measures the effect
on recall directly — that is what it is for.

#### Original note

1,232 of the 1,639 gate-passed items were rejected as "no rule template
matched the headline" — the extractor found no named person. Many are real
moves whose body sentence shape is not in `body_rules.py`. Take 100 rejected ABLJ items, read the cached bodies, and
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

### Step 6 — Phase 4, deduplication — **DONE**

Built and applied: `tracker/names.py` (surname-first ordering),
`tracker/dedupe.py` (pair scoring, given name as a hard gate),
`tracker/pipeline/dedupe.py` (blocking, merge, review routing, team moves),
`tracker dedupe`. On the corpus: 4 merges, 2 team moves, 1 ambiguous pair
queued. Two partners sharing a surname at one firm in one window stay two
records — that guard is exercised by live data, not only by tests.

#### Design notes (kept)

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

### Step 8 — Phase 7, evaluation — **DONE**

`tracker evaluate` reports precision, recall and per-field agreement with no
database and no network, and fails under the 0.98 bar. The gold set gained 40
**item-sampled** records (69 total, 18 reporting no move).

The sampling change matters and should not be undone: a gold set built from
records the extractor produced can only confirm what it already got right, so
recall is unmeasurable and precision is inflated by selection. The set still
falls short of 100 live moves and the strict xfail still marks that.

#### Original note

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
