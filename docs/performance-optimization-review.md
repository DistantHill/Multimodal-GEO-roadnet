# Pipeline Performance Optimization Review

Scope: the 15 low-risk candidates supplied on 2026-08-31. The separately
listed high-loss items are excluded until stable-key, ordering, telemetry, and
memory-boundary baselines exist.

| # | Candidate | Practical | Changed | Decision |
|---|---|---|---|---|
| 1 | Named PostgreSQL cursors use batched fetches | Yes | Yes | Core source already used `itersize=10_000`; set the same batch size on the remaining 1M seed cursor. |
| 2 | Group DBSCAN labels in one pass at three call sites | Yes | Yes | Replaced three O(KN) rescans with one O(N) grouping pass while retaining ascending cluster IDs, point order, and noise exclusion. |
