# Pipeline Performance Optimization Review

Scope: the 15 low-risk candidates supplied on 2026-08-31. The separately
listed high-loss items are excluded until stable-key, ordering, telemetry, and
memory-boundary baselines exist.

| # | Candidate | Practical | Changed | Decision |
|---|---|---|---|---|
| 1 | Named PostgreSQL cursors use batched fetches | Yes | Yes | Core source already used `itersize=10_000`; set the same batch size on the remaining 1M seed cursor. |
| 2 | Group DBSCAN labels in one pass at three call sites | Yes | Yes | Replaced three O(KN) rescans with one O(N) grouping pass while retaining ascending cluster IDs, point order, and noise exclusion. |
| 3 | Reuse the Clip intersection grid globally | Yes | Yes | Build one immutable grid per Clip input and carry each projected endpoint into the next segment; clip ordering and geometry are unchanged. |
| 4 | Batch candidate/intersection INSERT RETURNING | Yes | Yes | Added parameter-limited multi-row inserts that retain PostGIS geometry construction and returned-ID mappings; candidate and verified rows now take batch-count round trips. |
| 5 | Change high-frequency performance timing to block level | Yes | Yes | G* and both DBSCAN paths retain telemetry names but record one neighbour block per event/seed expansion, removing two timer calls per candidate. |
| 6 | Generate only cross-vehicle segment pairs | Yes | Yes | A rank-ordered batch iterator now emits only canonical cross-vehicle pairs; same-vehicle and reverse pairs are never materialized, while owner filtering and output order remain stable. |
| 7 | Construct DB models directly from tuples | Yes | No | Already implemented in `PostgresTrajectorySourceHandle` via precomputed column indexes and `_parse_row_from_tuple`; the dict parser remains only for non-PG/general rows. |
| 8 | Replay manifest first and query named subtracks | Yes | Yes | Centerline replay now validates its manifest before loading points, queries only referenced `(vehicle_id, subtrack_index)` pairs, and caches Detect-to-Clean resolution. |
| 9 | Add a Cleaner pending-point flush cap | Yes | Yes | Added a configurable 100k-point cap across kept/derived/excluded output; flushing remains between complete vehicle groups, so a single vehicle is never split. |
| 10 | Cache Cross-track endpoint projections | Yes | Yes | Each accepted point index is projected at most once per vehicle, so adjacent straight segments reuse their shared endpoint without changing segment filtering. |
| 11 | Remove redundant indexes and add a composite index | Yes | Yes | The user-added covering index is retained; its left prefix replaces `idx_ktp_tp_id`, while the membership UNIQUE index replaces the identical `idx_ktp_vehicle_sub`. Both legacy indexes are dropped during structural migration. |
| 12 | Accumulate cluster provenance in lists | Yes | Yes | Greedy merge now extends mutable per-source lists through all absorptions and converts to immutable tuples once at final model construction. |
