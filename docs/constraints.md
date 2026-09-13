# Phase 0 — Compliance constraints

How each constraint is handled, stated before any collection code exists. Every
item here is enforced somewhere concrete: a schema constraint, a code path, or a
documented refusal. Where enforcement is by convention rather than by mechanism,
it says so.

---

## 1. Source terms of service

**Position: feeds and APIs are the only default. HTML is opt-in per source, off
until a human has checked that outlet's terms.**

| Access type | When used | Enforcement |
|---|---|---|
| `feed` | RSS/Atom is published | Default for every seeded source |
| `api` | Outlet offers an official API with terms permitting this use | Per-source credentials, never scraped |
| `html` | Only where the outlet's terms permit automated access **and** robots.txt allows the path | `sources.html_access_reviewed_at` must be non-null before the adapter will run |

`sources.html_access_reviewed_at` is null by default. The ingest stage refuses to
run an `html` adapter while it is null, so enabling HTML collection for an outlet
is a deliberate, dated, auditable act rather than a default.

**robots.txt.** Fetched and cached per source before any request, re-checked when
older than 24h (`sources.robots_checked_at`, `sources.robots_allowed`). A
disallowed path is not fetched. A robots.txt that cannot be retrieved is treated
as disallow, not as permission.

**Rate limiting.** One request per source per 10 seconds minimum, as a hard floor
in the HTTP client rather than a per-adapter courtesy. `sources.poll_interval` can
only be set *longer* than the floor; a shorter value is rejected by a check
constraint. `Crawl-delay` in robots.txt overrides the floor upward when longer.

**User-Agent.** A single descriptive UA with a reachable contact address, set
from `TRACKER_USER_AGENT` and required to be non-empty at startup:

```
apac-lateral-tracker/0.1 (+https://github.com/kevanwee/apac-lateral-tracker; contact: kevanwee@gmail.com)
```

No UA rotation, no browser impersonation, no headless-browser rendering of pages
that block simple clients. If a source only yields to evasion, it is out of scope.

---

## 2. Article text is never stored

**Position: the database has nowhere to put article body text.**

`raw_items` holds `url`, `headline`, `published_at`, `fetched_at`, `outlet`,
`content_hash` and processing state. There is no `body`, `content` or `summary`
column — not "unused", absent. Fetched text lives in process memory for the length
of one extraction call and is discarded.

`content_hash` is a SHA-256 of the fetched text used for change detection and
idempotency. It is one-way and not a storage workaround.

**Provenance excerpts.** Extraction records a character span into the source text
for every field (`move_field_evidence.span_start`, `span_end`) plus a short
excerpt. The excerpt is capped at **25 words by a database check constraint**, not
by application discipline:

```sql
CHECK (array_length(regexp_split_to_array(btrim(excerpt), '\s+'), 1) <= 25)
```

Evidence is for reviewers and audit. It is not exposed by the public dashboard
API and is not display content. The dashboard links out to the source URL and
reproduces nothing but the headline, which is needed to make the link usable.

---

## 3. Paywalled sources

**Position: no credentials, no circumvention, ever.**

No login flows, no cookie injection, no archive mirrors, no AMP or
print-view tricks, no referrer spoofing. If an outlet paywalls its content, the
pipeline ingests whatever its public feed exposes — typically headline and
publication date — and nothing else.

Those records are marked `raw_items.source_access = 'headline_only'`. That value
is load-bearing downstream:

- Headline-only items get a **confidence penalty** at extraction, because a
  headline rarely states practice area, title, or origin firm.
- A move corroborated only by headline-only items does **not** qualify for the
  multi-source corroboration boost in Phase 2.
- The Phase 5 coverage banner reports the headline-only share alongside the
  tier-1 share, so a thin-evidence trend is visible as thin.

---

## 4. Personal data

**Position: professional attributes only, and erasure works from day one.**

These records concern named individuals in a professional capacity, sourced from
material those individuals' employers published or briefed to the press. That
supports the professional-context processing this system does; it does not make
the data unregulated.

**Stored:** name and name variants, title, firm, office jurisdiction, practice
group, partner tier, move dates, and the source URLs reporting the move.

**Not stored, and no column exists for it:** email, phone, postal address, social
handles, date of birth, nationality, photograph, education, remuneration,
inferred ethnicity or gender, or any attribute of the person outside their
professional role. Name romanisation handling in Phase 4 works on strings and
must not be used to infer or record ethnicity.

**Erasure.** `erase_person(person_id, reason, actor)` ships in the first
migration, before any data exists to erase. It deletes the person, their moves,
practice-group and sector assignments, evidence rows and review-queue entries in
one transaction, and writes a tombstone to `suppressed_people` holding a
**salted SHA-256 of the normalised name and nothing else**. Ingestion checks that
suppression list, so the next article about that person does not silently
resurrect the record. An erasure that could be undone by tomorrow's cron run is
not an erasure.

Erasures are counted in `erasure_log` (timestamp, actor, reason, row counts) with
no personal data, so the operation is auditable without defeating its purpose.

---

## 5. What this means for coverage

These constraints cost coverage, and the honest version is:

- Outlets that publish no feed and forbid automated access are **excluded
  entirely**, not worked around.
- Paywalled trade press contributes headlines only, which is often enough to know
  a move happened but not enough to classify it. Those records will lean on firm
  newsroom corroboration or sit in review.
- Firm newsroom feeds are therefore the backbone of the dataset, not a
  supplement. That is also the right call on accuracy grounds: they are primary
  sources (reliability tier 1).

The Phase 5 coverage banner exists so that this cost is visible in the output
rather than hidden behind a confident-looking chart.
