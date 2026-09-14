# Methodology — how a practice-group trend is computed, and what it can claim

This is the reference for anyone turning the `moves` table into a chart. It
describes the unit of analysis, what is and is not in the denominator, and the
biases that the corpus carries whatever the query does. The database encodes
the mechanical parts as views (`analysis_moves`, `source_period_coverage`,
`practice_group_trend`, migration 0012); this document is the reasoning behind
them and the rules the views cannot enforce.

## 1. The unit of analysis

One row of `analysis_moves` is one **reported** partner-level move: one person,
one destination firm, one primary source article. It is not yet one *event* —
until Phase 4 deduplication runs, a move covered by three outlets is three rows.

Consequences:

- Any cross-source trend computed before Phase 4 over-weights firms and
  markets with dense press coverage. Treat pre-Phase-4 output as per-source
  only.
- Within one source, a row is close to one event: outlets seldom report the
  same hire twice. `practice_group_trend` is therefore partitioned by source.

## 2. What is in the denominator

`analysis_moves` exposes, and does not apply, four filters. Choose them per
question and state them with the result.

| Column | Meaning | Default for a published number |
|---|---|---|
| `review_state` | `auto_accepted`, `pending_review`, `accepted`, `rejected` (rejected already excluded) | `auto_accepted` or human-`accepted` only |
| `confidence` | Evidence-based record confidence, 0–1 | ≥ 0.85 (the auto-accept line) |
| `classification_confidence` | 0–1, discounted by evidence kind (§4) | ≥ 0.6 for a level-1 series; ≥ 0.75 for level-2 |
| `date_is_estimated` | `published_at` came from a bulk-regenerated sitemap `lastmod` | exclude from any time series |

Rows with `practice_unclassified = true` stay in the table and in
`practice_group_trend`, reported as their own line. They are not dropped from
the denominator silently. If the unclassified share in a period exceeds a
third, the classified shares in that period are not reportable — say so
rather than charting them.

## 3. Share, not count

A count of moves per quarter measures the corpus at least as much as the
market. Law.com feeds reach back twenty items; the Australasian Lawyer archive
reaches 2019; the Asia Business Law Journal archive is the only reachable
source for Hong Kong and China before 2024. A raw series is a chart of which
sources exist for which years — the 2026 spike in the first extraction (68
moves against 18 for 2021) was exactly this.

The reportable statistic is **share of classified moves within
(source, period)**, which cancels source volume, and — once Phase 4 has run
— share within period across sources of comparable depth. Always show
`source_period_coverage` alongside: gate-passed items per source per period is
the denominator, and a period whose denominator is under ~30 gate-passed items
is not a period, it is an anecdote.

Never compare shares across sources of different reliability tier without
saying so. Tier 1 (a firm's own newsroom) reports every hire the firm makes,
including ones no outlet would cover; tier 2 reports the ones it finds
newsworthy. Their practice mixes differ by construction.

## 4. Classification: where the practice came from, and how much to trust it

Every move carries exactly one primary practice group under one taxonomy
version (`classified_taxonomy_version`). The group is assigned from the
strongest evidence available, in this order, and the rank is recorded in
`classification_rule_key` and surfaced as `classification_evidence`:

| Evidence | Example | Weight |
|---|---|---|
| `stated` | the clause attached to the person: "as a partner in its white-collar defence practice" | 1.00 |
| `headline` | "Cooley grows capital markets with new partner in Beijing" | 0.75 |
| `body` | a sentence in the body that mentions this person | 0.60 |

`classification_confidence` is the mapping's own confidence (0.95 whole-string
match, 0.8 phrase-in-longer-text) multiplied by the weight. The headline is
stored, so a headline-derived assignment can be re-verified; body-derived
ones can be re-verified only by re-reading the article, which is why they are
weighted lowest.

**Ask questions at level 1.** A bare "corporate partner" classifies to
`corporate`, not to `corporate.m_and_a`, because the text did not say. Under
taxonomy 1.0.0 it was filed as Corporate Governance; that would have produced
a governance trend the articles never stated. A level-2 series must be
restricted to rows whose `practice_group_code` has a dot, and should say what
share of the level-1 total that is.

**Sectors are a separate axis** (`move_sectors`), never folded into practice.
A fintech regulatory partner is practice = Financial Services Regulatory,
sector = Financial Services. Sector assignment is not yet rule-driven; treat
`move_sectors` as empty until it is.

**Taxonomy versions are never mixed in one series.** Assignments keep the
version that made them. A series spans a version change only if the codes
being compared exist unchanged in both versions, and it says so.

## 5. Dates

`announced_date` is the **outlet's publication date** of the primary source.
It is the right basis for a reporting-based series and the wrong basis for an
"effective date" claim; `effective_date` is usually null and should be
described as such. A series bucketed by quarter is robust to a few weeks of
reporting lag; a monthly series near a source's archive boundary is not.

## 6. Jurisdiction

`office_jurisdiction` is the office the person joined, at the finest level the
text gave: `AU-NSW`, `HK`, `SG`. It comes, in order of preference, from the
role clause ("as a partner in Sydney"), the headline, or — for outlets that
put the city in the URL — the URL slug, in which case the slug is recorded as
evidence. It is absent when nothing stated it. A "by market" series must
report its null share; before the URL-slug source was added it was 91%.

`jurisdiction_focus` on a source describes the *outlet*, not the move, and is
deliberately not used to fill the field.

## 7. Known biases the query cannot remove

- **Language.** English-language outlets only; the ABLJ archive excludes its
  Chinese, Japanese and Korean editions to avoid quadruple counting the same
  piece, not because they carry different moves.
- **Headline style.** Some outlets name the person in the headline and some
  never do; the latter yield records only where the article body could be
  read (`html_access_reviewed_at`). Source yield is therefore not comparable
  across outlets and says nothing about market activity.
- **Firm gazetteer coverage.** A move to a firm the gazetteer does not know is
  abstained on, not guessed. Coverage is skewed towards the ~175 firms in
  `config/firms.yaml`; boutiques and regional firms are under-represented by
  construction. Adding a firm is a config change and a partial re-extraction.
- **Blocked sources.** ALB — the brief's primary APAC source — and the Global
  Legal Post archive are inaccessible to a polite client and are not
  circumvented. Coverage of Singapore and Hong Kong is thinner than the
  market, and thinner than it would be with those sources.
- **Precision floor.** The pipeline abstains rather than guesses, so recall is
  low and uneven by outlet style. Trends are trends in *reported, extractable*
  moves.

## 8. Minimum reporting standard for any published number

State, with the number: the sources included, the period, the review-state
and confidence floors applied, the taxonomy version, whether Phase 4
deduplication had run, the unclassified share, and the gate-passed denominator
for the smallest period shown. If any of those is unknown, the number is not
ready.

## 9. Worked query

Share of classified moves by level-1 practice group, per quarter, one source,
auto-accepted rows, stated or headline evidence only:

```sql
SELECT period_quarter, practice_group_top,
       count(*) AS moves,
       round(count(*)::numeric
             / sum(count(*)) OVER (PARTITION BY period_quarter), 3) AS share
FROM analysis_moves
WHERE source_slug = 'asia-business-law-journal-archive'
  AND review_state = 'auto_accepted'
  AND NOT practice_unclassified
  AND classification_evidence IN ('stated', 'headline')
  AND NOT date_is_estimated
GROUP BY 1, 2
ORDER BY 1, 3 DESC;
```

and its denominator:

```sql
SELECT period_quarter, gate_passed, items_with_moves
FROM source_period_coverage
WHERE source_slug = 'asia-business-law-journal-archive'
ORDER BY 1;
```
