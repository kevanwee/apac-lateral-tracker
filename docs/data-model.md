# Phase 1 — Data model

Postgres, plain SQL migrations in [migrations/](../migrations/), applied in
filename order and checksummed once applied. No ORM infers any part of this.

## Shape

```
jurisdictions ──┬── sources ──── raw_items ──┬── move_sources ──── moves
                │      │                     └── move_field_evidence
                │      └── firms                        │
firms ──┬── firm_aliases                                │
        └── people ──────────────────────────────────── moves ──┬── move_practice_groups ── practice_groups
                                                       │        └── move_sectors ────────── sectors
                                                       ├── team_moves
                                                       ├── review_queue
                                                       └── superseded_by_move_id (self)

taxonomy_versions ──┬── practice_groups
                    └── sectors

pipeline_runs ──┬── pipeline_run_sources
                └── llm_calls

suppressed_people, erasure_log        (erasure residue, no personal data)
```

## The three stated invariants

| Invariant | Enforced by | Test |
|---|---|---|
| Exactly one primary practice group per move | Partial unique index (at most one) + deferred constraint trigger fired when `classification_state` becomes `classified` (at least one) | `test_at_most_one_primary_practice_group`, `test_classified_move_must_have_a_primary_practice_group` |
| `from_firm_id = to_firm_id` only for promotions | `CHECK moves_same_firm_only_for_promotion`, plus its converse `moves_promotion_is_within_one_firm` | `test_move_cannot_leave_and_join_the_same_firm` |
| `announced_date` never null, `effective_date` often is | `NOT NULL` + plausibility range; `effective_date` nullable and allowed to precede announcement | `test_announced_date_cannot_be_null`, `test_effective_date_may_be_null_and_may_precede_announcement` |

"Exactly one" needs the deferred trigger because the classify stage runs in a
different transaction from extract. The trigger only fires once a move claims to
be classified, so partial state during a stage is legitimate and a committed
classified move without a primary group is not.

## Decisions worth contesting

These are judgement calls where a different answer is defensible. Overrule any
of them now rather than after Phase 2 has code depending on them.

**1. `unclassified` is a real taxonomy node, not a null.** The spec says
classification "fails to unclassified and enters review". Modelling that as an
absent row would make "exactly one primary group" unenforceable and would drop
unclassified moves out of every chart denominator. Instead the taxonomy carries
a reserved `unclassified` node, so an unplaceable move still has a primary group
and still counts. The alternative is to let `classification_state` carry it and
weaken the invariant to "at most one".

**2. `to_firm_id` is nullable, but only for retirements.** The spec lists it as
non-null while also listing `retirement` as a move type, and a retirement has no
destination. The alternative — a sentinel "Retired" firm row — pollutes the firm
table and the firm-flow matrix. So the column is nullable with
`CHECK (to_firm_id IS NOT NULL OR move_type = 'retirement')`. Every other move
type still must name a destination.

**3. Merges create a new row; the inputs are kept and marked superseded.** A
move is canonical exactly while `superseded_by_move_id IS NULL`, and the
`canonical_moves` view is what analytics read. Pre-merge reports stay queryable,
which matters for auditing an over-merge. Cost: `moves` holds both reported and
canonical rows, so every analytical query must go through the view.

**4. Reliability tier 1 is reserved for a firm's own newsroom, by constraint.**
`CHECK (reliability_tier <> 1 OR firm_id IS NOT NULL)`. If you want a trade
outlet to be able to reach tier 1 on a particular story, this needs to move to
the `raw_items` row rather than the source.

**5. Promotions must be within one firm.** The spec only says same-firm implies
promotion; I also enforce the converse, so a promotion with a null or differing
origin firm is rejected. This will reject genuine reports that say "promoted to
partner" without naming the firm twice — those should extract as a promotion
with `from_firm = to_firm`, or route to review. If that proves too strict in
Phase 2, this is the constraint to relax.

**6. Move type must agree with the firm kind at each end.** `in_house_exit` must
point at a firm typed `in_house`. This is a precision guard: an extraction that
disagrees has misread the article. It makes firm typing load-bearing, so a
mistyped firm row will block ingestion rather than produce a wrong record —
which is the trade I want, but it does mean firm seeding has to be careful.

**7. Jurisdictions are a table, not free text.** Adding a region is an INSERT
plus a source row. The cost is that an unknown code raises rather than being
stored, so a genuinely new market needs a one-line config change before its
first article can land.

**8. Sectors get their own join table (`move_sectors`), which the spec did not
list.** Without it, the sectors table has no way to attach to a move and the
orthogonality is decorative. Practice and sector are separately versioned within
the same taxonomy release.

**9. `firms.verticals` stays a `text[]` with no referential integrity.** It is
descriptive metadata about a firm, not analytical input; sector analysis reads
`move_sectors`. If you want firm verticals to be queryable alongside sector
trends, this should become a join table.

## Things deliberately not built yet

- No materialised views. They belong in Phase 5, after the shape of the
  questions is settled by real data.
- No taxonomy content. `taxonomy/*.yaml` and the loader are Phase 3; these
  tables hold structure and version history only.
- No `taxonomy_mappings` table. The spec puts the phrase mapping in a YAML file
  that grows from review resolutions, and duplicating it in the database would
  create two sources of truth.

## Running the tests

```bash
docker run -d -e POSTGRES_PASSWORD=pg -p 5432:5432 postgres:16
DATABASE_URL=postgresql://postgres:pg@localhost/postgres pytest
```

The suite drops and recreates the `public` schema, so point it at a throwaway
database. CI does the same against a `postgres:16` service container on every
push.
