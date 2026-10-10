SELECT c.id AS short_candidate_id,e.detect_id,count(*) AS turn_count,count(*) FILTER (WHERE e.g_star_z >= 0.5) AS hotspot_count,count(*) FILTER (WHERE e.candidate_intersection_id IS NOT NULL) AS clustered_count,array_agg(DISTINCT e.candidate_intersection_id) AS assigned_candidates,min(e.g_star_z),max(e.g_star_z)
FROM candidate_intersections c
JOIN turn_events_geom e ON e.geom && c.geom AND ST_Covers(c.geom,e.geom)
WHERE c.id IN (55,56) AND e.detect_id IN ('cf84aabc-614a-46d1-a66b-7575df02f221','1a37960b-5f69-44a5-a8cf-58bb3da31eb5')
GROUP BY c.id,e.detect_id ORDER BY c.id,e.detect_id;
