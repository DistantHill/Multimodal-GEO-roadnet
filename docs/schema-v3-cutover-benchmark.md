# Schema v3 cutover benchmark

## Scope

This is the A65 record for the destructive `track.v2` schema-v3 switch on
2026-09-04. It reports observations as measured and does not treat empty-table
DDL overhead as evidence of a runtime improvement.

Target verification before reset was:

```text
database=roadnet user=roadnet schema=public
schema version metadata: absent
```

The old database contained 50,003 `trajectory_points` rows and 100,006
`kept_trajectory_points` rows. Its Detect artifact tables were empty, so it did
not contain a comparable crossing/Clip/Centerline behavior sample.

## Frozen pre-reset storage

| Table | Rows | Heap/TOAST bytes | Total bytes | Heap bytes/row |
|---|---:|---:|---:|---:|
| `trajectory_points` | 50,003 | 7,110,656 | 13,139,968 | 142.20 |
| `kept_trajectory_points` | 100,006 | 12,959,744 | 34,045,952 | 129.59 |
| `clean_runs` | 5 | 16,384 | 49,152 | 3,276.80 |
| `detect_runs` | 5 | 16,384 | 49,152 | 3,276.80 |
| `fit_runs` | 4 | 16,384 | 65,536 | 4,096.00 |

All other old Detect/Clip/Centerline core tables had zero rows. PostgreSQL's
minimum relation pages therefore make their per-row values undefined.

## Post-reset validation

The reset created schema metadata `track.v2 = 3`, 17 authoritative tables, 19
indexes, and six semantic constraint triggers. A read-only storage audit found
zero business rows, as expected immediately after reset. A populated
before/after per-row storage comparison is therefore **not available** from
this database and no storage win is claimed.

A single PostgreSQL transaction (rolled back after verification) exercised two
complete circles on the same source segment plus a second crossing-free
subtrack. It produced:

```text
boundary_end
between_intersection   # exactly two crossing endpoints, no interior point
boundary_start
full_track
```

The sparse two-point `between_intersection` Clip produced two Centerline
variants with `min_clips_per_pair=1`; the persisted manifest replayed its exit
and entry crossing IDs successfully. The same transaction also exercised a
cleaned-point Turn Event and a cross-vehicle Track Cross. Constraint evaluation
was forced before the transaction was rolled back. Observed Clip database load
plus assembly time was 0.020454 s; this is a smoke observation, not a stable
performance ratio.

The deterministic in-memory active-set fixture used 20,000 resolved points and
2,500 complete intersections. Five assembly samples were 0.125418, 0.157268,
0.129683, 0.177974, and 0.165571 s; median was 0.157268 s (7.863 µs/input point).
It emitted 2,501 Clips, including 2,499 `between_intersection` Clips.

## Repeatable populated audit

Pipeline now records the five required phase values:

- `detect_compute_s`
- `detect_persist_s`
- `resolved_sequence_s`
- `clip_assembly_s`
- `_elapsed_s` (whole Pipeline wall time)

Run a representative immutable source, then combine those timings with exact
table size, per-row size, Clip query/assembly, crossing/Clip/Centerline counts,
and deterministic crossing, entry/exit pairing, Clip, and Centerline behavior
digests:

```sh
python -m pipeline run \
  --source-table public.day01 \
  --processing-srid 32650 \
  --stats-json /tmp/roadnet-v3-run.json

The `manual_bench_schema_v3` entry point was removed from the repository on
2026-09-25 with the temporary manual-debug scripts. The benchmark figures and
acceptance rules below are historical evidence; this checkout no longer provides
a command to reproduce them. Historical reports could be compared with a prior
report using the then-supported `--baseline` option; the tool had no arbitrary
percentage threshold and failed on comparable timing or bytes-per-row changes
greater than 10×, or a changed behavior document. Host load, cache state, source
fingerprint, and dataset still had to be held constant for a valid comparison.
