# Pipeline Source Provenance

The `track.v2` implementation was migrated from the host repository
`D:\Ai_Projects\track` at commit `75fa562` on branch
`codex/pipe-06a-integration`. That branch is seven commits ahead of source
`main` commit `1e54374` and contains patch-equivalent versions of the focused
`pipe-03a-core`, `pipe-04a-events`, and `pipe-05a-store` branches.

Only committed files were used. Uncommitted changes present in the host source
working tree on August 30, 2026 were intentionally excluded. The package name
`track.v2` is preserved to avoid algorithm-level import rewrites.

At the initial import, migration-only adaptations were limited to:

- standard Compose `POSTGRES_*` environment variable support while preserving
  source-compatible `TRACK_PG_*` aliases;
- removal of the insecure fallback database password;
- the documented empty result for an explicitly missing `.env` file;
- a default `allow_running=False` argument for Clip replay, fixing the source
  branch's undefined local while preserving completed-run replay semantics;
- environment-relative defaults for migrated manual benchmark tools that had
  host-specific Windows paths;
- the `python -m pipeline` container wrapper for `run`, `init-db`, `serve`, and
  `check-config` commands.

## Post-import evolution

The list above records provenance and import-time adaptations only. It is not
an exhaustive description of the current implementation and is not a promise
of output, ordering, persistence, or replay equivalence with the source branch.

After the import, the repository adopted explicit
`refine_runs → detect_runs → fit_runs` lineage, a shared Detect track workspace,
a capacity-guarded global cross-track index, indexed DBSCAN and topology
verification, COPY/batched persistence, bounded Clip indexing, phase-level
Fitting telemetry, Detect failure-state handling, destructive schema-v3
versioning, resolved p/i/c sequences, and schema-qualified PostgreSQL sources.
See the current
[performance and runtime architecture review](../../docs/performance-optimization-review.md)
for the active design, measured results, and accepted trade-offs.

Schema v3 removed the old `pipeline_runs` migration and runtime compatibility
branches. The later configuration-centralization cutover also removed the live
`TRACK_PG_*` aliases; this document records them only as import provenance.
`_cross_track_legacy.py` is a frozen benchmark oracle, not the production Detect
implementation or a runtime fallback.
