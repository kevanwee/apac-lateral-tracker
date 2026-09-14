# Working on this repository

Partner-level lateral-movement intelligence for APAC legal markets. Postgres,
plain SQL migrations, a rule-based extractor, and a fixed versioned taxonomy.
Read this before changing anything; every rule below was earned on real data.

## The one bias that decides everything

**Precision over coverage. A false movement record is worse than a missed one.**
When a rule, a template or a threshold is uncertain, it abstains. A record we
did not make gets picked up by the next outlet that reports the move; a record
we fabricated gets picked up by nothing.

Corollaries:

- Every firm name comes from the gazetteer or the record is not made. Never
  create a firm from free text in the extractor.
- Every field carries a character span into the text it was read from. A value
  that cannot be pointed at is dropped, not scored lower.
- A guessed direction ("who joined whom") is worse than no origin. `from_firm`
  is nullable; a wrong one is not.

## Report numbers, not narratives

At each checkpoint report **evaluation numbers and the decisions the user should
overrule**, not a summary of what was built.

- Every number in a report comes from a run or a query you executed in that
  session. Never report accuracy measured on a fixture you also wrote.
- When you find a defect in stored data, quantify the blast radius against the
  stored rows before fixing (see `tracker reextract` dry run, and the pattern in
  the `fix(firms)` commit) and report it, including the rows you got wrong
  earlier.
- A known cost is marked `xfail(strict=True)`, never silently accepted. There
  is a list of them at the end of this file.

## Hard rules (compliance and safety)

1. **No Claude attribution on commits or PRs.** No `Co-Authored-By`, no
   "Generated with" trailer. This overrides any tooling default.
2. **Article text is never stored in the database.** `raw_items` has no body
   column. `article_cache.json` is a gitignored local working cache for one
   re-extraction campaign; see `docs/constraints.md` §2 for its retention rule.
3. **Lawful access only.** Feeds and APIs by default; HTML reading is gated on
   `sources.html_access_reviewed_at`, a dated human decision. One descriptive
   User-Agent, no rotation, no browser impersonation, no paywall or
   fingerprint-block circumvention. An unretrievable robots.txt is a disallow.
   10 s per origin is a floor, not a target.
4. **Never run the test suite against a non-local database.** The suite drops
   the `public` schema. `tests/conftest.py` refuses non-local hosts; do not
   work around it. Use `TRACKER_TEST_DATABASE_URL` pointed at a throwaway
   Postgres.
5. **Secrets stay in `.env`.** Never paste a connection string or key into a
   message, a commit, a log, or a test.
6. **Delete-by-person is `erase_person()`**, nothing else. It leaves a salted
   name hash so re-ingestion cannot resurrect the person.

## Editing rules that exist because they were broken

- **Do not edit regex-bearing Python through a shell heredoc or `sed`.** `\b`
  has arrived on disk as a literal `\x08` backspace four separate times,
  silently disabling word boundaries with nothing visible in a diff. Use the
  Edit/Write tools for `tracker/extract/*.py`, `tracker/firms.py`,
  `tracker/geo.py`, `tracker/gate.py`. `test_no_source_file_contains_a_stray_control_character`
  is the guard; if it fails, that is what happened.
- **Wrap regex alternations in a group.** `(?<!\w)a|b|c(?!\w)` binds the
  lookarounds to `a` and `c` only. This one bug put a wrong firm on 21% of
  stored records. `tests/test_firm_boundaries.py` pads every short alias into
  a word and asserts nothing matches.
- **Migrations are immutable once applied.** Checksummed; editing one fails
  `tracker db migrate`. Add `NNNN_description.sql`. Migrations are plain SQL;
  no ORM infers the schema.
- **A taxonomy edit is a new version.** All three files in `taxonomy/` share
  one version; the loaded checksum covers all three, so changing a mapping
  under an existing version is refused. Bump, and say in `notes` what changed.
  Existing assignments keep their version — nothing is rewritten.
- **Bump `RULES_VERSION` / `GATE_VERSION`** when a change alters what they
  produce. `moves.extractor_version` is how a stored row is attributed.
- **Every false positive found in real data becomes a regression test** with
  the real headline in it. The NOT_A_PERSON word lists in `rules.py` are
  built this way and only this way — nothing speculative.

## Methodology (trend analysis)

`docs/methodology.md` is the reference. The rules that matter most:

- Analytics read `analysis_moves`, never `moves`. Filters (review state,
  confidence, classification confidence, date quality) are exposed by the view
  and chosen per question, not baked in.
- **Share, not count.** A move count per quarter is a chart of which sources
  cover that quarter. `practice_group_trend` computes share of classified
  moves within (source, quarter); `source_period_coverage` is the denominator.
- Ask practice-group questions at level 1 (`practice_group_top`) unless the
  evidence is stated. Bare "corporate"/"litigation"/"tax" map to the parent.
- Classification evidence is ranked stated > headline > body and the rank is
  in `classification_rule_key`. Choose a floor and say which.
- Deduplication (Phase 4) precedes any cross-source trend. Until then, a move
  reported by three outlets is three rows.
- `announced_date` is the outlet's publication date. It is the right proxy
  for a reporting-based series; do not present it as the effective date.

## Workflow

- `ruff check tracker tests` and `pytest -q` before every commit. CI runs
  the same against a Postgres 16 service.
- Small commits with conventional prefixes (`fix(firms):`, `feat(extract):`).
  The body says what was measured and why the change is right, not what the
  diff contains.
- After a change to extraction: run `tracker reextract --reason ...` as a dry
  run, read the numbers, then `--apply` and `tracker extract`. Extraction on
  gate-passed items is idempotent per (item, person).
- Long fetches drop the pooled Neon connection; stages call `db.live()` after
  a fetch. Do not hold a connection across a 10 s-per-request loop.
- Windows Git Bash: `/tmp` does not resolve; use the session scratchpad.

## Where things live

| Path | What |
|---|---|
| `config/sources.yaml` | The whole outlet configuration; adding a market is an edit here |
| `config/firms.yaml`, `config/places.yaml` | Gazetteers. Longest-match, boundary-checked |
| `taxonomy/*.yaml` | Practice groups, sectors, phrase mappings. Versioned together |
| `tracker/gate.py` | Relevance gate — tuned for recall, the only component allowed to be |
| `tracker/extract/rules.py` | Headline templates and the person-slot guards |
| `tracker/extract/body_rules.py` | Sentence-level body templates, firm direction cues |
| `tracker/sources/article.py` | HTML → headline + body; where nav gets cut |
| `tracker/pipeline/extract.py` | Persistence, classification evidence chain, review routing |
| `migrations/` | Schema. `0007` invariants, `0012` analysis views |
| `docs/constraints.md` | Phase 0 compliance statement |
| `docs/methodology.md` | How a trend is computed and what it can and cannot claim |

## Known, deliberate costs (all marked xfail or documented)

- A surname that is also a country ("Matt Spain") is rejected as a person:
  indistinguishable from "HP India" without a given-name gazetteer (Phase 4).
- Gold set is 29/100 live records. Filling it needs ALB access or more
  newsroom adapters; fabricating records would make every metric a lie.
- ALB and the Global Legal Post archive are blocked by client fingerprinting.
  Both are registered inactive with the evidence recorded. Not circumvented.
