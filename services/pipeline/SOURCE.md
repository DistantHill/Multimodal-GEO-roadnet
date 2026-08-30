# Pipeline Source Provenance

The `track.v2` implementation was migrated from the host repository
`D:\Ai_Projects\track` at commit `75fa562` on branch
`codex/pipe-06a-integration`. That branch is seven commits ahead of source
`main` commit `1e54374` and contains patch-equivalent versions of the focused
`pipe-03a-core`, `pipe-04a-events`, and `pipe-05a-store` branches.

Only committed files were used. Uncommitted changes present in the host source
working tree on August 30, 2026 were intentionally excluded. The package name
`track.v2` is preserved to avoid algorithm-level import rewrites.

Migration-only adaptations are limited to:

- standard Compose `POSTGRES_*` environment variable support while preserving
  legacy `TRACK_PG_*` variables;
- removal of the insecure fallback database password;
- the documented empty result for an explicitly missing `.env` file;
- a default `allow_running=False` argument for Clip replay, fixing the source
  branch's undefined local while preserving completed-run replay semantics;
- environment-relative defaults for migrated manual benchmark tools that had
  host-specific Windows paths;
- the `python -m pipeline` container wrapper for `run`, `init-db`, `serve`, and
  `check-config` commands.
