# Repository Guidelines

## Project Structure

- `apps/frontend/` contains the standalone Node UI and production Nginx image.
- `apps/backend/` contains the FastAPI service, with `src/backend/` and `tests/`.
- `services/pipeline/` is the independent Pipeline runtime. Keep Cleaner, Detect, Clip, Fitting, Network, persistence, and replay stages here; do not run long jobs in Backend request handlers.
- `services/vision/` contains the optional YOLOv8 GPU batch runtime and tracking export tools. Keep model weights under the ignored `models/` path or an external `MODEL_ROOT`.
- `db/postgres/` and `db/qdrant/` contain only initialization, schema, migration, and collection definitions. Runtime database data belongs in Docker named volumes.
- `infra/docker/` contains Compose files. Runtime trajectory, Parquet, exports, and model files stay outside the repository and mount through `DATA_ROOT`.

## Build, Test, and Development Commands

```sh
cp .env.example .env
make dev-up          # Build and start the development stack
make dev-logs        # Follow service logs
make test            # Run Backend and Pipeline pytest suites in containers
make pipeline-check  # Validate Pipeline runtime configuration and data mount
make dev-config      # Validate the development Compose file
make dev-down        # Stop the development stack
```

Check frontend production assets with `docker run --rm roadnet-dev-frontend npm run build`. Use Compose service names (`postgres`, `qdrant`, `backend`), not container IPs.

## Coding Style and Naming

Use Python 3.11+, four-space indentation, type hints where practical, and `snake_case` for modules, functions, and variables. Keep packages under each service’s `src/`. Use ESM JavaScript, two-space indentation, `camelCase` variables/functions, and `PascalCase` UI components. Move code to `libs/` only when stable and shared by at least two runtime units.

## Testing Guidelines

Backend and Pipeline tests use `pytest`. Name files `test_*.py` and test behavior, not implementation details. Run `make test` before submitting. Add regression coverage for path, configuration, API, or migration changes; no threshold is configured.

## Commits and Pull Requests

This checkout has no valid Git history, so no convention can be inferred. Use concise imperative messages, preferably Conventional Commit style (for example, `feat: add pipeline run endpoint`). PRs should explain architectural impact, list validation commands, identify configuration or data-directory changes, and include UI screenshots when relevant. Never commit `.env`, credentials, runtime data, model weights, database files, or Qdrant storage.

## Security and Configuration

Copy `.env.example` to `.env` and replace placeholder secrets locally. Keep `POSTGRES_PASSWORD` out of Git. Treat `/data` as runtime-only and verify bind-mount paths before starting production Compose.
