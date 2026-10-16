
    WITH hits AS (
      SELECT k.vehicle_id, k.subtrack_index,
             k.sequence_in_subtrack AS anchor, 0 AS kind, k.id AS point_id,
             k.source_row_key AS source_row_key, ST_X(ST_Transform(s.geom, 32649)) AS x,
             ST_Y(ST_Transform(s.geom, 32649)) AS y, s.heading, s.timestamp, s.speed,
             s.lon, s.lat, NULL::text AS generated_reason,
             k.sequence_in_subtrack + (
               SELECT COUNT(*) FROM injected_points rank_i
               WHERE rank_i.refine_id = k.refine_id
                 AND rank_i.subtrack_index = k.subtrack_index
                 AND rank_i.after_sequence_in_subtrack < k.sequence_in_subtrack
             ) AS refined_sequence,
             'public.trajectory_points'::text AS source_id
      FROM "public"."trajectory_points" s
      JOIN kept_trajectory_points k
        ON s.id = k.source_row_key AND k.refine_id = %s
      WHERE ST_Intersects(ST_Transform(s.geom, 32649), ST_MakeEnvelope(%s, %s, %s, %s, 32649))
      UNION ALL
      SELECT i.vehicle_id, i.subtrack_index, i.after_sequence_in_subtrack,
             1, i.id, NULL::bigint, i.x, i.y, i.heading,
             i.timestamp, i.speed, i.lon, i.lat, i.generated_reason,
             i.after_sequence_in_subtrack + (
               SELECT COUNT(*) FROM injected_points rank_i
               WHERE rank_i.refine_id = i.refine_id
                 AND rank_i.subtrack_index = i.subtrack_index
                 AND rank_i.after_sequence_in_subtrack <= i.after_sequence_in_subtrack
             ) AS refined_sequence,
             'public.injected_points'::text AS source_id
      FROM injected_points i
      WHERE i.refine_id = %s
        AND i.x >= %s AND i.y >= %s AND i.x <= %s AND i.y <= %s
    )
    
    SELECT h.vehicle_id, h.subtrack_index, h.anchor, h.kind, h.point_id,
           h.source_row_key, h.x, h.y, h.heading, h.timestamp, h.speed,
           h.lon, h.lat, h.generated_reason, h.refined_sequence, h.source_id,
           prev.anchor, prev.kind, prev.point_id, prev.source_row_key,
           prev.x, prev.y, prev.heading, prev.timestamp, prev.speed,
           prev.lon, prev.lat, prev.generated_reason, prev.refined_sequence,
           prev.source_id,
           nxt.anchor, nxt.kind, nxt.point_id, nxt.source_row_key,
           nxt.x, nxt.y, nxt.heading, nxt.timestamp, nxt.speed,
           nxt.lon, nxt.lat, nxt.generated_reason, nxt.refined_sequence,
           nxt.source_id
    FROM hits h
    
        LEFT JOIN LATERAL (
          SELECT candidate.anchor, candidate.kind, candidate.point_id,
                 candidate.source_row_key, candidate.x, candidate.y,
                 candidate.heading, candidate.timestamp, candidate.speed,
                 candidate.lon, candidate.lat, candidate.generated_reason,
                 candidate.refined_sequence, candidate.source_id
          FROM (
            (SELECT k.sequence_in_subtrack AS anchor, 0 AS kind,
                   k.id AS point_id, k.source_row_key AS source_row_key,
                   ST_X(ST_Transform(src.geom, 32649)) AS x,
                   ST_Y(ST_Transform(src.geom, 32649)) AS y,
                   src.heading, src.timestamp, src.speed, src.lon, src.lat,
                   NULL::text AS generated_reason,
                   k.sequence_in_subtrack + (
                     SELECT COUNT(*) FROM injected_points rank_i
                     WHERE rank_i.refine_id = k.refine_id
                       AND rank_i.subtrack_index = k.subtrack_index
                       AND rank_i.after_sequence_in_subtrack < k.sequence_in_subtrack
                   ) AS refined_sequence,
                   'public.trajectory_points'::text AS source_id
            FROM kept_trajectory_points k
            JOIN "public"."trajectory_points" src
              ON src.id = k.source_row_key
            WHERE k.refine_id = %s AND k.subtrack_index = h.subtrack_index
              AND ROW(k.sequence_in_subtrack, 0, k.id)
                  < ROW(h.anchor, h.kind, h.point_id)
            ORDER BY k.sequence_in_subtrack DESC, k.id DESC
            LIMIT 1)
            UNION ALL
            (SELECT i.after_sequence_in_subtrack, 1, i.id, NULL::bigint,
                 i.x, i.y, i.heading, i.timestamp, i.speed, i.lon, i.lat,
                 i.generated_reason,
                 i.after_sequence_in_subtrack + (
                   SELECT COUNT(*) FROM injected_points rank_i
                   WHERE rank_i.refine_id = i.refine_id
                     AND rank_i.subtrack_index = i.subtrack_index
                     AND rank_i.after_sequence_in_subtrack <= i.after_sequence_in_subtrack
                 ) AS refined_sequence,
                 'public.injected_points'::text AS source_id
          FROM injected_points i
          WHERE i.refine_id = %s AND i.subtrack_index = h.subtrack_index
            AND ROW(i.after_sequence_in_subtrack, 1, i.id)
                < ROW(h.anchor, h.kind, h.point_id)
          ORDER BY i.after_sequence_in_subtrack DESC, i.id DESC
            LIMIT 1)
        ) candidate
        ORDER BY candidate.anchor DESC, candidate.kind DESC,
                 candidate.point_id DESC
        LIMIT 1
        ) prev ON TRUE
        
    
        LEFT JOIN LATERAL (
          SELECT candidate.anchor, candidate.kind, candidate.point_id,
                 candidate.source_row_key, candidate.x, candidate.y,
                 candidate.heading, candidate.timestamp, candidate.speed,
                 candidate.lon, candidate.lat, candidate.generated_reason,
                 candidate.refined_sequence, candidate.source_id
          FROM (
            (SELECT k.sequence_in_subtrack AS anchor, 0 AS kind,
                   k.id AS point_id, k.source_row_key AS source_row_key,
                   ST_X(ST_Transform(src.geom, 32649)) AS x,
                   ST_Y(ST_Transform(src.geom, 32649)) AS y,
                   src.heading, src.timestamp, src.speed, src.lon, src.lat,
                   NULL::text AS generated_reason,
                   k.sequence_in_subtrack + (
                     SELECT COUNT(*) FROM injected_points rank_i
                     WHERE rank_i.refine_id = k.refine_id
                       AND rank_i.subtrack_index = k.subtrack_index
                       AND rank_i.after_sequence_in_subtrack < k.sequence_in_subtrack
                   ) AS refined_sequence,
                   'public.trajectory_points'::text AS source_id
            FROM kept_trajectory_points k
            JOIN "public"."trajectory_points" src
              ON src.id = k.source_row_key
            WHERE k.refine_id = %s AND k.subtrack_index = h.subtrack_index
              AND ROW(k.sequence_in_subtrack, 0, k.id)
                  > ROW(h.anchor, h.kind, h.point_id)
            ORDER BY k.sequence_in_subtrack ASC, k.id ASC
            LIMIT 1)
            UNION ALL
            (SELECT i.after_sequence_in_subtrack, 1, i.id, NULL::bigint,
                 i.x, i.y, i.heading, i.timestamp, i.speed, i.lon, i.lat,
                 i.generated_reason,
                 i.after_sequence_in_subtrack + (
                   SELECT COUNT(*) FROM injected_points rank_i
                   WHERE rank_i.refine_id = i.refine_id
                     AND rank_i.subtrack_index = i.subtrack_index
                     AND rank_i.after_sequence_in_subtrack <= i.after_sequence_in_subtrack
                 ) AS refined_sequence,
                 'public.injected_points'::text AS source_id
          FROM injected_points i
          WHERE i.refine_id = %s AND i.subtrack_index = h.subtrack_index
            AND ROW(i.after_sequence_in_subtrack, 1, i.id)
                > ROW(h.anchor, h.kind, h.point_id)
          ORDER BY i.after_sequence_in_subtrack ASC, i.id ASC
            LIMIT 1)
        ) candidate
        ORDER BY candidate.anchor ASC, candidate.kind ASC,
                 candidate.point_id ASC
        LIMIT 1
        ) nxt ON TRUE
        
    ORDER BY h.vehicle_id, h.subtrack_index, h.anchor, h.kind, h.point_id
    