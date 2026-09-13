-- 0007 — Invariants that a CHECK constraint cannot express, plus erasure.
--
-- Everything here is enforced by the database rather than by pipeline code,
-- because the pipeline is not the only thing that will ever write to it: a
-- reviewer resolving a queue item, a backfill script and a hotfix all get the
-- same guarantees.

-- ---------------------------------------------------------------------------
-- Exactly one primary practice group per classified move
-- ---------------------------------------------------------------------------
-- The "at most one" half is a partial unique index in 0005. "At least one"
-- needs to look across rows, so it is a deferred constraint trigger: the
-- classify stage may insert the move and its groups in any order within a
-- transaction, but may not commit a classified move without a primary group.
--
-- A move the classifier could not place is still assigned a primary group —
-- the reserved `unclassified` node — so unclassified records stay in the
-- denominator of every chart instead of quietly disappearing from it.

CREATE FUNCTION check_move_classification(p_move_id uuid) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    m                record;
    n_primary        integer;
    n_secondary      integer;
    n_wrong_version  integer;
BEGIN
    SELECT classification_state, classified_taxonomy_version
      INTO m
      FROM moves
     WHERE id = p_move_id;

    -- Deleted later in the same transaction: nothing to enforce.
    IF NOT FOUND THEN
        RETURN;
    END IF;

    -- Before classification runs, partial state is legitimate.
    IF m.classification_state <> 'classified' THEN
        RETURN;
    END IF;

    SELECT count(*) FILTER (WHERE is_primary),
           count(*) FILTER (WHERE NOT is_primary),
           count(*) FILTER (WHERE taxonomy_version IS DISTINCT FROM m.classified_taxonomy_version)
      INTO n_primary, n_secondary, n_wrong_version
      FROM move_practice_groups
     WHERE move_id = p_move_id;

    IF n_primary <> 1 THEN
        RAISE EXCEPTION
            'move % is classified but has % primary practice groups (must be exactly 1)',
            p_move_id, n_primary
            USING ERRCODE = 'check_violation';
    END IF;

    IF n_secondary > 2 THEN
        RAISE EXCEPTION
            'move % has % secondary practice groups (at most 2 allowed)',
            p_move_id, n_secondary
            USING ERRCODE = 'check_violation';
    END IF;

    IF n_wrong_version > 0 THEN
        RAISE EXCEPTION
            'move % has practice group assignments from a taxonomy version other than %',
            p_move_id, m.classified_taxonomy_version
            USING ERRCODE = 'check_violation';
    END IF;
END;
$$;


CREATE FUNCTION trg_moves_classification_invariant() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM check_move_classification(NEW.id);
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER moves_classification_invariant
    AFTER INSERT OR UPDATE ON moves
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION trg_moves_classification_invariant();


CREATE FUNCTION trg_mpg_classification_invariant() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM check_move_classification(OLD.move_id);
    ELSE
        PERFORM check_move_classification(NEW.move_id);
        IF TG_OP = 'UPDATE' AND OLD.move_id <> NEW.move_id THEN
            PERFORM check_move_classification(OLD.move_id);
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER move_practice_groups_classification_invariant
    AFTER INSERT OR UPDATE OR DELETE ON move_practice_groups
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION trg_mpg_classification_invariant();


-- ---------------------------------------------------------------------------
-- Move type must agree with the kind of firm at each end
-- ---------------------------------------------------------------------------
-- Precision guard. An extraction that calls something an in-house exit while
-- pointing at a law firm has misread the article, and that is worth failing on
-- rather than storing.

CREATE FUNCTION trg_moves_firm_kind_agrees() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    to_kind   firm_type;
    from_kind firm_type;
BEGIN
    SELECT firm_type INTO to_kind FROM firms WHERE id = NEW.to_firm_id;
    SELECT firm_type INTO from_kind FROM firms WHERE id = NEW.from_firm_id;

    IF NEW.move_type = 'in_house_exit' AND to_kind IS DISTINCT FROM 'in_house' THEN
        RAISE EXCEPTION
            'move % is an in_house_exit but its destination firm is %, not in_house',
            NEW.id, coalesce(to_kind::text, 'unknown')
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.move_type = 'in_house_entry'
       AND NEW.from_firm_id IS NOT NULL
       AND from_kind IS DISTINCT FROM 'in_house' THEN
        RAISE EXCEPTION
            'move % is an in_house_entry but its origin firm is %, not in_house',
            NEW.id, coalesce(from_kind::text, 'unknown')
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER moves_firm_kind_agrees
    AFTER INSERT OR UPDATE OF move_type, from_firm_id, to_firm_id ON moves
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION trg_moves_firm_kind_agrees();


-- ---------------------------------------------------------------------------
-- Team move membership
-- ---------------------------------------------------------------------------
-- A move may only join a team move whose firm pair and date window it actually
-- fits, and the denormalised partner_count is maintained here rather than by
-- whichever caller happened to write last.

CREATE FUNCTION trg_moves_team_membership() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    tm record;
BEGIN
    IF NEW.team_move_id IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT * INTO tm FROM team_moves WHERE id = NEW.team_move_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    IF NEW.to_firm_id IS DISTINCT FROM tm.to_firm_id THEN
        RAISE EXCEPTION 'move % joins team move % but has a different destination firm',
            NEW.id, tm.id USING ERRCODE = 'check_violation';
    END IF;

    IF tm.from_firm_id IS NOT NULL AND NEW.from_firm_id IS DISTINCT FROM tm.from_firm_id THEN
        RAISE EXCEPTION 'move % joins team move % but has a different origin firm',
            NEW.id, tm.id USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.announced_date NOT BETWEEN tm.window_start AND tm.window_end THEN
        RAISE EXCEPTION 'move % announced % falls outside team move % window % to %',
            NEW.id, NEW.announced_date, tm.id, tm.window_start, tm.window_end
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER moves_team_membership
    AFTER INSERT OR UPDATE OF team_move_id, to_firm_id, from_firm_id, announced_date ON moves
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION trg_moves_team_membership();


CREATE FUNCTION trg_sync_team_move_count() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    affected uuid[];
    t        uuid;
BEGIN
    affected := ARRAY(
        SELECT DISTINCT x FROM unnest(ARRAY[
            CASE WHEN TG_OP <> 'DELETE' THEN NEW.team_move_id END,
            CASE WHEN TG_OP <> 'INSERT' THEN OLD.team_move_id END
        ]) AS x WHERE x IS NOT NULL
    );

    FOREACH t IN ARRAY affected LOOP
        UPDATE team_moves tm
           SET partner_count = (
               SELECT count(*) FROM moves m
                WHERE m.team_move_id = tm.id
                  AND m.superseded_by_move_id IS NULL
           )
         WHERE tm.id = t;
    END LOOP;

    RETURN NULL;
END;
$$;

CREATE TRIGGER moves_sync_team_move_count
    AFTER INSERT OR DELETE OR UPDATE OF team_move_id, superseded_by_move_id ON moves
    FOR EACH ROW EXECUTE FUNCTION trg_sync_team_move_count();


-- ---------------------------------------------------------------------------
-- Erasure durability (Phase 0, section 4)
-- ---------------------------------------------------------------------------
-- An erasure that tomorrow's cron run can undo is not an erasure. Ingestion
-- cannot create a person whose normalised name matches a tombstone.

CREATE FUNCTION trg_people_reject_suppressed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    n     text;
    names text[] := ARRAY[NEW.canonical_name] || NEW.name_variants;
BEGIN
    FOREACH n IN ARRAY names LOOP
        IF EXISTS (
            SELECT 1 FROM suppressed_people WHERE name_hash = person_name_hash(n)
        ) THEN
            RAISE EXCEPTION
                'refusing to create or rename a person matching an erasure tombstone'
                USING ERRCODE = 'restrict_violation',
                      HINT = 'This person was erased on request. See suppressed_people.';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;

CREATE TRIGGER people_reject_suppressed
    BEFORE INSERT OR UPDATE OF canonical_name, name_variants ON people
    FOR EACH ROW EXECUTE FUNCTION trg_people_reject_suppressed();


CREATE FUNCTION erase_person(p_person_id uuid, p_reason text, p_actor text)
RETURNS TABLE (moves_deleted integer, evidence_deleted integer, sources_unlinked integer)
LANGUAGE plpgsql AS $$
DECLARE
    v_name     text;
    v_variants text[];
    v_move_ids uuid[];
BEGIN
    SELECT canonical_name, name_variants
      INTO v_name, v_variants
      FROM people
     WHERE id = p_person_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    SELECT coalesce(array_agg(id), '{}'::uuid[])
      INTO v_move_ids
      FROM moves
     WHERE person_id = p_person_id;

    SELECT count(*)::integer INTO evidence_deleted
      FROM move_field_evidence WHERE move_id = ANY (v_move_ids);
    SELECT count(*)::integer INTO sources_unlinked
      FROM move_sources WHERE move_id = ANY (v_move_ids);

    -- Break supersession links pointing into the set about to be deleted; the
    -- FK is RESTRICT precisely so this has to be deliberate.
    UPDATE moves
       SET superseded_by_move_id = NULL,
           merged_at = NULL,
           review_state = 'rejected'
     WHERE superseded_by_move_id = ANY (v_move_ids)
       AND NOT (id = ANY (v_move_ids));

    UPDATE moves
       SET superseded_by_move_id = NULL,
           merged_at = NULL,
           review_state = 'rejected'
     WHERE id = ANY (v_move_ids)
       AND superseded_by_move_id IS NOT NULL;

    DELETE FROM moves WHERE person_id = p_person_id;
    GET DIAGNOSTICS moves_deleted = ROW_COUNT;

    DELETE FROM people WHERE id = p_person_id;

    -- The only residue: a salted hash of each name spelling, so re-ingestion
    -- cannot resurrect this person under any variant we had seen.
    INSERT INTO suppressed_people (name_hash, reason, actor)
    SELECT person_name_hash(n), p_reason, p_actor
      FROM unnest(ARRAY[v_name] || v_variants) AS n
     WHERE btrim(n) <> ''
    ON CONFLICT (name_hash) DO NOTHING;

    INSERT INTO erasure_log (actor, reason, moves_deleted, evidence_deleted, sources_unlinked)
    VALUES (p_actor, p_reason, moves_deleted, evidence_deleted, sources_unlinked);

    RETURN NEXT;
END;
$$;

COMMENT ON FUNCTION erase_person(uuid, text, text) IS
  'Delete-by-person. Removes the person and every derived record, leaves a '
  'salted name hash so ingestion cannot recreate them, and logs counts only.';


-- ---------------------------------------------------------------------------
-- Monitoring
-- ---------------------------------------------------------------------------
-- Should always be empty. Checked before the analytics refresh so a broken
-- invariant surfaces as a failed run rather than as a wrong chart.

CREATE VIEW invariant_violations AS
SELECT 'classified_move_without_primary_practice' AS check_name,
       m.id AS subject_id
FROM moves m
WHERE m.classification_state = 'classified'
  AND NOT EXISTS (
      SELECT 1 FROM move_practice_groups g
      WHERE g.move_id = m.id AND g.is_primary
  )
UNION ALL
SELECT 'move_with_multiple_primary_sources', ms.move_id
FROM move_sources ms
GROUP BY ms.move_id
HAVING count(*) FILTER (WHERE ms.is_primary) > 1
UNION ALL
SELECT 'canonical_move_without_any_source', m.id
FROM moves m
WHERE m.superseded_by_move_id IS NULL
  AND m.review_state <> 'rejected'
  AND NOT EXISTS (SELECT 1 FROM move_sources s WHERE s.move_id = m.id)
UNION ALL
SELECT 'team_move_with_fewer_than_two_partners', tm.id
FROM team_moves tm
WHERE tm.partner_count < 2;
