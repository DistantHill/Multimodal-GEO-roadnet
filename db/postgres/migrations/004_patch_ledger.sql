-- Explicit opt-in installation; {schema} is a psycopg.sql.Identifier template.
-- This does not alter the production schema-v3 initializer or publish its runs.
CREATE TABLE IF NOT EXISTS {schema}.pipeline_patch_runs (
    run_id text NOT NULL,
    stage text NOT NULL,
    signature jsonb NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    sealed boolean NOT NULL DEFAULT false,
    published boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, stage),
    CHECK (NOT published OR sealed)
);
CREATE TABLE IF NOT EXISTS {schema}.pipeline_patch_executions (
    run_id text NOT NULL,
    stage text NOT NULL,
    generation bigint NOT NULL,
    execution_config jsonb NOT NULL,
    environment jsonb NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, stage, generation),
    FOREIGN KEY (run_id, stage) REFERENCES {schema}.pipeline_patch_runs
);
CREATE TABLE IF NOT EXISTS {schema}.pipeline_patch_tasks (
    run_id text NOT NULL,
    stage text NOT NULL,
    task_id text NOT NULL,
    manifest jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'working', 'complete', 'failed')),
    attempt_no integer NOT NULL DEFAULT 0 CHECK (attempt_no BETWEEN 0 AND 3),
    attempt_token text,
    generation bigint,
    next_retry_at timestamptz,
    completed_at timestamptz,
    PRIMARY KEY (run_id, stage, task_id),
    FOREIGN KEY (run_id, stage) REFERENCES {schema}.pipeline_patch_runs,
    CHECK ((status = 'working') = (attempt_token IS NOT NULL)),
    CHECK ((status = 'complete') = (completed_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS pipeline_patch_tasks_status_idx
    ON {schema}.pipeline_patch_tasks (run_id, stage, status, task_id);
CREATE TABLE IF NOT EXISTS {schema}.pipeline_patch_attempts (
    run_id text NOT NULL,
    stage text NOT NULL,
    task_id text NOT NULL,
    attempt_no integer NOT NULL CHECK (attempt_no BETWEEN 1 AND 3),
    attempt_token text NOT NULL,
    generation bigint NOT NULL,
    status text NOT NULL CHECK (status IN ('working', 'complete', 'failed', 'abandoned')),
    failure_kind text CHECK (failure_kind IN ('transient', 'resource', 'permanent', 'interrupted')),
    error_message text,
    next_retry_at timestamptz,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    PRIMARY KEY (run_id, stage, task_id, attempt_no),
    FOREIGN KEY (run_id, stage, task_id) REFERENCES {schema}.pipeline_patch_tasks,
    CHECK ((status = 'working') = (finished_at IS NULL))
);
