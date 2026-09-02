BEGIN;

ALTER TABLE turn_events
    ADD COLUMN IF NOT EXISTS candidate_intersection_id BIGINT;

ALTER TABLE tracks_cross
    ADD COLUMN IF NOT EXISTS candidate_intersection_id BIGINT;

DO $$
DECLARE
    conflict BOOLEAN;
BEGIN
    IF to_regclass('candidate_intersection_events') IS NOT NULL THEN
        EXECUTE $sql$
            SELECT EXISTS (
                SELECT 1 FROM candidate_intersection_events
                WHERE event_kind = 'turn'
                GROUP BY detect_id, turn_event_id
                HAVING COUNT(DISTINCT candidate_intersection_id) > 1
            )
        $sql$ INTO conflict;
        IF conflict THEN
            RAISE EXCEPTION
                'turn event belongs to multiple candidate intersections';
        END IF;

        EXECUTE $sql$
            SELECT EXISTS (
                SELECT 1 FROM candidate_intersection_events
                WHERE event_kind = 'orthogonal_cross_track'
                GROUP BY detect_id, from_source_id, from_source_row_key,
                         to_source_id, to_source_row_key
                HAVING COUNT(DISTINCT candidate_intersection_id) > 1
            )
        $sql$ INTO conflict;
        IF conflict THEN
            RAISE EXCEPTION
                'cross event belongs to multiple candidate intersections';
        END IF;

        EXECUTE $sql$
            UPDATE turn_events AS te
            SET candidate_intersection_id = membership.candidate_id
            FROM (
                SELECT detect_id, turn_event_id,
                       MIN(candidate_intersection_id) AS candidate_id
                FROM candidate_intersection_events
                WHERE event_kind = 'turn'
                GROUP BY detect_id, turn_event_id
            ) AS membership
            WHERE te.detect_id = membership.detect_id
              AND te.id = membership.turn_event_id
        $sql$;

        EXECUTE $sql$
            UPDATE tracks_cross AS tc
            SET candidate_intersection_id = membership.candidate_id
            FROM (
                SELECT detect_id, from_source_id, from_source_row_key,
                       to_source_id, to_source_row_key,
                       MIN(candidate_intersection_id) AS candidate_id
                FROM candidate_intersection_events
                WHERE event_kind = 'orthogonal_cross_track'
                GROUP BY detect_id, from_source_id, from_source_row_key,
                         to_source_id, to_source_row_key
            ) AS membership
            WHERE tc.detect_id = membership.detect_id
              AND tc.from_source_id = membership.from_source_id
              AND tc.from_source_row_key = membership.from_source_row_key
              AND tc.to_source_id = membership.to_source_id
              AND tc.to_source_row_key = membership.to_source_row_key
        $sql$;

        EXECUTE $sql$
            SELECT EXISTS (
                SELECT 1 FROM candidate_intersection_events AS cie
                WHERE cie.event_kind = 'turn'
                  AND NOT EXISTS (
                      SELECT 1 FROM turn_events AS te
                      WHERE te.detect_id = cie.detect_id
                        AND te.id = cie.turn_event_id
                        AND te.candidate_intersection_id =
                            cie.candidate_intersection_id
                  )
            )
        $sql$ INTO conflict;
        IF conflict THEN
            RAISE EXCEPTION 'unmapped turn candidate membership';
        END IF;

        EXECUTE $sql$
            SELECT EXISTS (
                SELECT 1 FROM candidate_intersection_events AS cie
                WHERE cie.event_kind = 'orthogonal_cross_track'
                  AND (
                      SELECT COUNT(*) FROM tracks_cross AS tc
                      WHERE tc.detect_id = cie.detect_id
                        AND tc.from_source_id = cie.from_source_id
                        AND tc.from_source_row_key = cie.from_source_row_key
                        AND tc.to_source_id = cie.to_source_id
                        AND tc.to_source_row_key = cie.to_source_row_key
                  ) <> 1
            )
        $sql$ INTO conflict;
        IF conflict THEN
            RAISE EXCEPTION 'cross candidate membership is not uniquely mapped';
        END IF;

        EXECUTE $sql$
            SELECT EXISTS (
                SELECT 1 FROM candidate_intersection_events AS cie
                WHERE cie.event_kind = 'orthogonal_cross_track'
                  AND NOT EXISTS (
                      SELECT 1 FROM tracks_cross AS tc
                      WHERE tc.detect_id = cie.detect_id
                        AND tc.from_source_id = cie.from_source_id
                        AND tc.from_source_row_key = cie.from_source_row_key
                        AND tc.to_source_id = cie.to_source_id
                        AND tc.to_source_row_key = cie.to_source_row_key
                        AND tc.candidate_intersection_id =
                            cie.candidate_intersection_id
                  )
            )
        $sql$ INTO conflict;
        IF conflict THEN
            RAISE EXCEPTION 'unmapped cross candidate membership';
        END IF;
    END IF;
END $$;

DROP TABLE IF EXISTS candidate_intersection_events;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'turn_events_candidate_intersection_fkey'
          AND conrelid = 'turn_events'::regclass
    ) THEN
        ALTER TABLE turn_events
            ADD CONSTRAINT turn_events_candidate_intersection_fkey
            FOREIGN KEY (candidate_intersection_id, detect_id)
            REFERENCES candidate_intersections(id, detect_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'tracks_cross_candidate_intersection_fkey'
          AND conrelid = 'tracks_cross'::regclass
    ) THEN
        ALTER TABLE tracks_cross
            ADD CONSTRAINT tracks_cross_candidate_intersection_fkey
            FOREIGN KEY (candidate_intersection_id, detect_id)
            REFERENCES candidate_intersections(id, detect_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_te_candidate
    ON turn_events (detect_id, candidate_intersection_id)
    WHERE candidate_intersection_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tc_candidate
    ON tracks_cross (detect_id, candidate_intersection_id)
    WHERE candidate_intersection_id IS NOT NULL;

COMMIT;
