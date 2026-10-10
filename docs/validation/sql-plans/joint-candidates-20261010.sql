SELECT c.id,c.detect_id,c.centroid_x,c.centroid_y,c.centroid_lon,c.centroid_lat,c.radius_m,c.contributors,c.critical_contributors,i.id final_id,i.observation_count,i.clip_radius_m,i.branch_centers_deg,i.transition_counts
FROM candidate_intersections c
LEFT JOIN intersections i ON i.candidate_intersection_id=c.id AND i.detect_id=c.detect_id
WHERE c.detect_id IN ('9f79758c-5a04-4d73-ad6b-2db11cff8a61','b2fa03bc-2931-4a0a-9f63-811a508cae2f')
ORDER BY c.detect_id,c.id
