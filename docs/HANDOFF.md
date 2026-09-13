# Handoff: APAC partner lateral-movement pipeline — paused mid-fix on extraction yield

## Objective

Ingest legal trade press and firm announcements, extract partner-level lateral
movements into structured records, deduplicate across outlets, classify against a
fixed versioned practice-group taxonomy, and surface trends. APAC scope, with
Singapore, Hong Kong and Australia as depth markets. Checkpoint-driven: stop after
each phase group and report **evaluation numbers and decisions to overrule**, not a
summary of what was built.

Currently running the **$0 path** (rule-based extraction, no API key, no database).

## Current State

Repo: **https://github.com/kevanwee/apac-lateral-tracker** (private, `main`).
CI green, **200 tests + 1 deliberate xfail**, against a real Postgres 16 in CI.

| Phase | State |
|---|---|
| 0 Constraints | Done — `docs/constraints.md` |
| 1 Schema | Done — 10 migrations, invariants enforced in-database |
| 2 Ingestion + extraction + gold set | Done; **yield being actively improved** |
| 3 Taxonomy & classification | Not started |
| 4 Deduplication | Not started |
| 5 Trend analytics | Not started |
| 6 Scheduling | `catch-up` workflow done; no dashboard |
| 7 Evaluation | Gold set (29/100) + calibration guards; no extraction P/R (needs API key) |

### The live collection run (the thing being worked on)

`tracker collect --since 2019-01-01 --extractor rules` — no database needed, $0.00:

```
8,083 items fetched  →  760 passed the gate  →  23 records
```

23 records, **every name correct**, spanning 2022–2026. Output in `collected.json`
(gitignored). **The user's stated concern: 23 from 760 is too low.**

### Diagnosis of the low yield (measured, not guessed)

Categorised all 698 gate-passed Australasian Lawyer headlines:

| Share | Cause |
|---|---|
| **63%** | Known firm, but no template fits — and on inspection **most have no person name in the headline at all** |
| **27%** | Firm not in the gazetteer |
| **7%** | Template matched, a precision guard rejected it |
| **3%** | Extracted |

**The key finding: this is an information ceiling, not a parsing one.** Australasian
Lawyer's house style is "Holding Redlich welcomes IP partner" — the name is in the
article body, never the headline. No template can fix that.

Proof the body has what is missing: the article behind *"Bartier Perry brings in
first chief transformation officer appoints four partners"* contains
*"...in Roger Habib... Alison Cui, Kate Ralph, Raffael Maestri and Mario Rashid-Ring
becoming the firm's newest partners. Cui is a lateral hire from HNT Legal"* —
five named partners from a headline naming none.

### Sources as they now stand

| Slug | Adapter | On? | Note |
|---|---|---|---|
| `rajah-tann-asia` | paginated_feed | yes | tier 1, reaches Apr 2020 |
| `global-legal-post` | feed | yes | live window only |
| `australasian-lawyer` | feed | yes | litigation-heavy, low movement yield |
| `law-com-international` | feed | yes | **new**, brief-named, names in headlines |
| `law-com-american-lawyer` | feed | yes | **new**, brief-named |
| `australasian-lawyer-archive` | sitemap | yes | 7,798 items back to 2019 |
| `global-legal-post-archive` | sitemap | **no** | client-fingerprint 403, see below |
| `asian-legal-business` | feed | **no** | 403 on everything incl. robots.txt |
| `alb-newsletter` | imap | **no** | built; needs subscription + credentials |

## Key Decisions Made

Do not re-litigate. Rationale in `docs/data-model.md` and `docs/constraints.md`.

1. **No Claude co-author trailer** on commits or PRs. User instruction.
2. **No client impersonation, ever.** ALB 403s everything including robots.txt.
   Global Legal Post serves `/sitemap.xml` to curl but 403s httpx **with an
   identical User-Agent** while serving `/rss` and `/robots.txt` to both — a
   client-fingerprint block. Getting past either means impersonating a different
   client. We do not. Both are registered inactive with the evidence recorded.
3. **Google News RSS is out**: `news.google.com/robots.txt` is `Disallow: /` with
   an allow-list that excludes `/rss/`. Checked and rejected on our own rule.
4. **Law.com is explicitly permitted**: `Allow: /`, `Crawl-delay: 1`, sitemaps
   published. Our 10s floor exceeds their delay.
5. **No daily cron.** Manual `tracker catch-up`; quarterly workflow opens a
   reminder issue rather than spending unattended.
6. **Article text is never stored.** `raw_items` has no body column; absence is the
   policy. Provenance excerpts capped at 25 words by CHECK constraint.
7. **Rules abstain rather than guess.** Six false-positive classes are regression
   tests: role abbreviations (`Qic gc`), prepositional phrases (`in london`),
   quantities (`even dozen`), practice epithets (`corrs ip star`), descriptive
   epithets (`seasoned investment funds star`), non-partner titles (advisor).
8. Confidence recalibrated: completeness is a 0.05 bonus, not 25% of the score.
9. `unclassified` is a real taxonomy node; `to_firm` nullable only for retirement;
   merges create a new row and mark the old superseded; tier 1 = firm newsrooms only.

## Constraints & Preferences

- **Precision over coverage.** A false record is worse than a missed one. But 3%
  yield is too low — the user has said so explicitly.
- **Measure, don't assert.** Every number in a report needs a run behind it.
  Never report accuracy from a fixture you also authored.
- Politeness lives in `tracker/net/client.py`: 10s/origin floor, robots cached 24h,
  unretrievable robots = disallow, no UA rotation, no paywall circumvention.
- HTML article reading is gated on `sources.html_access_reviewed_at` — a dated
  human decision, not something an adapter enables itself.
- Plain SQL migrations, immutable once applied. Tests hit real Postgres.
- Commit in small logical increments, push, watch CI before reporting.
- **Bash heredocs corrupt regex escapes** — `\b` became literal `\x08` backspace
  bytes in `rules.py` and silently broke word boundaries. Use the Write/Edit tools
  for anything containing regex or long multi-file content.
- Windows Git Bash: `/tmp` does not resolve. Use `$TEMP`.

## Open Threads / Questions

1. **`tracker/sources/article.py` is written but wired into nothing.** It converts
   fetched HTML to article body text. This is the main unblock for yield.
2. **No database.** User said they will create Postgres later. Until then
   `tracker collect` (in-memory, writes JSON) is the only run path;
   `tracker backfill` has nowhere to write.
3. **No API key.** All LLM-dependent work and all Phase 7 precision/recall unmeasured.
4. **Gold set 29/100**, strict xfail marks the gap.
5. **ALB still unreached.** Newsletter/IMAP is built and is the lawful route; needs
   the user to subscribe and set `IMAP_USERNAME`/`IMAP_PASSWORD`. A Wayback CDX
   index route was probed (public API, returns 200, metadata only) and **not yet
   discussed with the user** — it reads Internet Archive's index, not ALB's servers.
6. `tracker/names.py` is an honest stub — assumes given-name-first, wrong for
   surname-first names. Under-merges. Phase 4 replaces it.

## Immediate Next Step

Finish the yield fix, in this order:

1. **Wire `article.py` into the extract path.** Add a `--fetch-articles` flag to
   `tracker collect`/`extract`; when a source has `html_access_reviewed_at` set and
   an item has no body text, fetch the article through `PoliteClient` and pass the
   body to the extractor. Australasian Lawyer's robots.txt allows the article paths
   (`/au/news/`, `/au/practice-areas/`); only `/au/business-news/` and
   `/nz/business-news/` are disallowed. **Ask the user to confirm the terms review
   before enabling it** — that is what the gate is for.
2. **Add body-aware templates** to `rules.py`. Current templates are anchored to a
   headline with `^`/`$`; bodies need sentence-level patterns ("X is a lateral hire
   from Y", "A, B and C becoming the firm's newest partners").
3. **Re-run `tracker collect --since 2019-01-01`** and report the new yield against
   the 23-record baseline. Budget ~2h for 698 AU articles at the 10s floor; make it
   resumable.

Then return to checkpoint 3 (classification + dedupe).
