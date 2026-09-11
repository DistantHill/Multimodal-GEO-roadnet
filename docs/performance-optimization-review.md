# Pipeline Performance and Runtime Architecture

Last updated: 2026-09-02.

This document describes the current Pipeline execution path, the 1,000,000-row
validation run, and the trade-offs introduced by the performance work. The
source import record in `services/pipeline/SOURCE.md` is provenance only; it is
not the current runtime specification.

The authoritative recorded full-Pipeline result is the
[`2026-09-01 1M benchmark Spec`](pipeline-1m-performance-benchmark-spec.md),
which records the command, code/config hashes, stage timings, cgroup peak,
per-optimization effects, and output counts. The older measurements retained
below are historical investigation evidence and are not the current baseline.

The separate
[`20M memory-budget Spec`](pipeline-20m-memory-budget-spec.md) scales downstream
counts from that benchmark and audits every eager materialization. The current
shared-workspace Detect and eager Clip architecture does **not** meet its
3,000,000,000-byte Pipeline cgroup contract; the 1M result must not be presented
as 20M capacity evidence.

## Terminology

- **Current runtime** means the production path under `track.v2.pipeline` and
  `track.v2.intersect2`.
- **Legacy run registry** means only an early database that still contains the
  single `pipeline_runs` table. Current lineage is
  `clean_runs → detect_runs → fit_runs`.
- **Historical implementation** is qualified by the concrete component being
  discussed. `_cross_track_legacy.py` is a frozen comparison oracle and is not
  selected by the production Pipeline. By contrast, `_run_dbscan_scan` remains
  the current small/non-indexable-input path, and the in-memory point-window
  implementation remains the current capacity fallback.
- **Compatibility variables** means the source-compatible `TRACK_PG_*`
  environment aliases. Their support does not imply compatibility with the
  complete historical execution architecture.

## Current architecture

### Detect shared workspace

DB Detect loads the completed Clean trajectory stream once. The resulting
vehicle-track workspace is shared by turn/orthogonal extraction, cross-track
candidate extraction, and topology verification. The production path no longer
issues one spatial SQL query for each of the former 8,541 cross-track windows or
one point query per candidate cluster.

Cross-track extraction builds one capacity-guarded segment grid:

- maximum estimated grid memberships: `8,000,000`;
- maximum estimated overflow pair-space: `20,000,000`;
- per-segment cell cap is derived from the configured padded window and is 729
  for the validated configuration;
- the 1M dataset used 2,904,079 memberships, one overflow segment, and an
  overflow pair-space of 437,416, so it stayed on the global path.

If either global budget is exceeded, Detect falls back to an in-memory window
path that performs no per-window SQL. The fallback retains historical
point-window semantics and is discussed under [Accepted trade-offs](#accepted-trade-offs-and-limits).

### Indexed DBSCAN and topology

For inputs larger than 256 points, DBSCAN uses an exact uniform-grid lookup,
short-circuits core-point counting at `min_samples`, and removes points from an
active frontier when they are queued. Small inputs and non-indexable/non-finite
private boundary cases retain the historical full scan. Differential tests
cover noise absorption, shared border points, queue order, dense inputs, and
label parity.

Topology verification builds one `_TopologyPointIndex` over the shared tracks.
It sorts the wider coordinate axis once, uses binary search plus an exact bbox
predicate for each cluster, and materializes only the current query window.
Duplicate-timestamp tracks retain stream order instead of acquiring a new
adjacency from the spatial sort.

Implementation: [`intersect2.py`](../services/pipeline/src/track/v2/intersect2.py),
[`cross_track.py`](../services/pipeline/src/track/v2/cross_track.py), and
[`queries.py`](../services/pipeline/src/track/v2/db/queries.py).

### Persistence and Run lifecycle

Detect append-only artifacts that do not require immediate generated-ID
mapping use PostgreSQL COPY:

- `tracks_cross`;
- `turn_events`;
- `entry_exit_points`.

`turn_events.id` is the sole persisted event identifier.  Its calculation
index exists only in memory, so COPY no longer needs a run-scoped ID-map read.

`candidate_intersections.id` is likewise the sole persisted candidate identity.
Detect keeps `cluster_calculate_index` only long enough to associate in-memory
products. PostgreSQL sequence IDs are reserved in calculation order and written
explicitly with the candidate and verified-intersection batches, so dependent
rows receive real foreign keys without persisting the calculation index.

Candidate membership is stored directly on `turn_events` and `tracks_cross`
through `candidate_intersection_id`; there is no separate membership table.

Tables whose generated IDs are needed by dependent rows continue to use
batched inserts with validated database-ID mappings. Fitting writes the complete
centerline bundle with one batched RETURNING operation and writes all
`centerline_source_clips` manifests with one COPY operation. On the validated run
this reduced roughly 1,680 centerline/manifest write operations to two.

Detect calculation runs outside the final write transaction. After calculation,
one transaction writes the complete p/i/c `resolved_subtrack_points` sequence,
all Detect children, and the completed status. An ordinary exception rolls that
transaction back, then records the Detect Run as `failed` separately. Telemetry
now separates `detect_compute`, `detect_persist`, and the resolved-sequence part
of persistence. Fitting continues to separate `fit_compute` and `fit_persist`.

Implementation: [`db_writer.py`](../services/pipeline/src/track/v2/db_writer.py)
and [`pipeline.py`](../services/pipeline/src/track/v2/pipeline.py).

### Resolved sequence and Clip

Schema v3 moves every segment/circle root to Detect. Detect persists only roots
that change circle membership and orders simultaneous events by exit, entry,
intersection ID, then crossing ID. `resolved_subtrack_points` stores stable p/i
identity, immutable Clean ordering, and the final p/i/c order without copying
coordinates or vehicle IDs.

Clip no longer owns a spatial grid or solves roots. It streams that final order,
uses complete-intersection geometry only to initialize the active set at the
first point, and emits a road fragment only while the active set is empty. Its
interior contract is the minimal `(resolved_point_id, lon, lat)` tuple. Natural
boundaries are explicit `boundary_start`/`boundary_end`; an entirely outside
track with no crossing is `full_track`, while an entirely inside track emits
nothing. Fitting accepts only `between_intersection`.

Implementation: [`clip.py`](../services/pipeline/src/track/v2/clip.py) and
[`queries.py`](../services/pipeline/src/track/v2/db/queries.py).

### Source tables and schema version

PostgreSQL progress estimates resolve the source with
`to_regclass(source_table)`, so schema-qualified names such as `public.day01` are
handled correctly. The value comes from `pg_class.reltuples` and remains an
estimate used only for progress.

`python -m pipeline init-db` is the Schema authority. Version 3 has no in-place
upgrade path: v1/v2 or unversioned tables require an explicitly confirmed
`--reset`. Pipeline and Backend fail at startup when the metadata version does
not match. Pipeline itself accepts only `source_table` (or a completed
`clean_id`); CSV remains a separate parser/importer facility.

Implementation: [`sources.py`](../services/pipeline/src/track/v2/sources.py) and
[`init_db.py`](../services/pipeline/src/track/v2/cli/init_db.py).

The schema-v3 cutover audit and repeatable A65 commands are recorded in
[`schema-v3-cutover-benchmark.md`](schema-v3-cutover-benchmark.md).

## 2026-08-31 1,000,000-row validation (historical, pre-v3)

Dataset and machine:

- database/table: `test.public.day01`;
- 1,000,000 rows, 1,598 vehicles;
- source IDs 6,241,001–21,747,000;
- AMD Ryzen 5 5600H, 12 logical CPUs;
- Docker memory approximately 6.7 GiB.

This section records the 2026-08-31 investigation. The Detect comparison uses an isolated calculation over the same completed
Clean Run. The later full CLI run was executed while the host load average was
about 58 and swap was full; that run is correctness evidence, not the clean
performance baseline. Do not use the values in this section as the current
benchmark; use the linked 2026-09-01 Spec.

| Stage or hotspot | Before | Current | Approximate speed-up |
|---|---:|---:|---:|
| Detect calculation | 633.628s | 46.401s isolated | 13.7× |
| Cross-track spatial work | 348.775s | about 20.3s isolated | 17.2× |
| Cross-track DBSCAN | 143.9s | about 2.0s | about 72× |
| Topology verification | 120.388s | 5.342s | 22.5× |
| Clip wall time (full CLI observations) | 84.811s under contention | 19.470s under contention | not a controlled ratio |
| Fitting persistence | about 17.8s | 0.938s | about 19× |

The earlier 1,757.409s Fitting wall time was not reproducible as algorithm
time. Concurrent Docker/BuildKit work, full swap, and high CPU/memory pressure
contaminated that run. Controlled Fitting compute was 3.6–4.4s; the reproducible
hotspot was persistence, which is the part changed here.

The two Clip values also came from full runs under different levels of host
contention. They show that the optimized path completed with identical Clip
counts, but they do not establish a controlled speed-up ratio.

The final full run completed Detect, Clip, Fitting, and Network in about 523.4s
despite the host contention. It persisted:

- 58,669 cross-track events and 56,170 turn events;
- 394 candidate clusters and 175 complete intersections;
- 31,826 clips containing 494,646 points;
- 310 combined plus 530 directed centerlines;
- 28,588 centerline manifests;
- a network with 175 nodes, 310 edges, and 10 isolated nodes.

The targeted regression set for the changed paths completed with 148 passing
tests. The full `make test` command is not green: Backend completed with 26
passing tests, while Pipeline collection stops at the pre-existing indentation
error in
[`test_delete_run.py`](../services/pipeline/tests/v2/test_delete_run.py#L194).
Replay also has two unrelated existing failures when run separately. None of
those failures is counted as a pass or hidden by the performance result.

## Output semantics

The global broad phase sees segments that the historical DB window strategy
could miss at window boundaries. On the validated dataset, cross-track input to
DBSCAN changed from 53,549 to 58,669 points and final candidate clusters changed
from 390 to 394; the number of complete intersections remained 175. This is an
intentional architecture change, not a claim of byte-for-byte historical
equivalence.

COPY paths assume a freshly-created Detect/Fit Run ID. Replaying persistence
against the same Run is not idempotent and may fail a uniqueness constraint;
recomputation must create a new Run.

## Accepted trade-offs and limits

- The global track workspace and spatial indexes exchange memory for database
  round trips. The resource-uncontended 2026-09-01 warm-cache run reached a
  Pipeline cgroup peak of 1.204 GiB on the validated 6.7 GiB Docker allocation;
  the explicit global-grid budgets prevent unbounded grid growth.
- If a global budget is exceeded, the in-memory point-window fallback can omit
  an extreme long segment when its endpoints never share a window. The 1M run
  did not enter this fallback. A future exact fallback should route segment
  AABBs rather than only points.
- Ordinary Detect exceptions are recorded as `failed`. `SIGKILL`, host loss, or
  another termination that prevents cleanup can still leave a Run in
  `running`; operational stale-run cleanup remains necessary.
- COPY intentionally gives up same-Run idempotent replay for substantially
  lower persistence overhead.

## First low-risk pass (historical record)

The following table records the earlier 15-candidate review. It is retained as
decision history, not as the complete current architecture. Later changes are
called out where they supersede the original implementation detail.

| # | Candidate | Practical | Changed | Decision and current status |
|---|---|---|---|---|
| 1 | Named PostgreSQL cursors use batched fetches | Yes | Yes | Core sources and the remaining 1M seed cursor use batched fetches. |
| 2 | Group DBSCAN labels in one pass at three call sites | Yes | Yes | O(KN) result grouping became O(N); large-input neighbour discovery is now also the exact grid/active-frontier path described above. |
| 3 | Reuse the Clip intersection grid globally | Yes | Superseded | This was valid for the pre-v3 implementation. Schema v3 consumes resolved crossings and has no Clip grid/root solver. |
| 4 | Batch candidate/intersection INSERT RETURNING | Yes | Yes | ID-dependent tables retain batched RETURNING; append-only Detect artifacts and Fitting manifests have since moved to COPY. |
| 5 | Change high-frequency performance timing to block level | Yes | Yes | G* and DBSCAN retain telemetry names while avoiding timer calls for every candidate. |
| 6 | Generate only cross-vehicle segment pairs | Yes | Yes | The rank-ordered iterator emits canonical cross-vehicle pairs only; it is now used by the global broad phase. |
| 7 | Construct DB models directly from tuples | Yes | No | This was already present in `PostgresTrajectorySourceHandle`; the dict parser remains for general rows. |
| 8 | Replay manifest first and query named subtracks | Yes | Superseded | Schema v3 manifests retain only crossing endpoints; replay slices the final resolved sequence between those stable IDs. |
| 9 | Add a Cleaner pending-point flush cap | Yes | Yes | A configurable 100k-point cap flushes only between complete vehicle groups. |
| 10 | Cache Cross-track endpoint projections | Yes | Yes | Accepted point indexes are projected once per vehicle, allowing adjacent segments to reuse endpoints. |
| 11 | Remove redundant indexes and add a composite index | Yes | Yes | The covering and membership indexes replace the superseded `idx_ktp_tp_id` and `idx_ktp_vehicle_sub`; structural migration drops those redundant indexes. |
| 12 | Accumulate cluster provenance in lists | Yes | Yes | Merge provenance stays mutable during absorption and becomes immutable once at model construction. |
| 13 | Process Topology segment metadata once | Yes | Yes | Metadata sharing remains; DB topology now additionally uses one global `_TopologyPointIndex` instead of per-cluster SQL. |
| 14 | Adapt Clip scanning for long AABBs | Yes | Superseded | Detect owns roots in schema v3, so Clip has no AABB/circle scan. |
| 15 | Remove redundant writer/source counting | Yes | Yes | Streaming paths reuse their write/parse counts; schema-qualified PostgreSQL estimates now resolve through `to_regclass`. |
