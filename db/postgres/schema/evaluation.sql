-- Apply only to the independent roadnet_eval database.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE IF NOT EXISTS eval_experiments (
    id uuid PRIMARY KEY, record jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), cleaned boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS eval_predictions (
    experiment_id uuid REFERENCES eval_experiments(id), feature_id text NOT NULL,
    feature jsonb NOT NULL, geom geometry(Geometry,4326) NOT NULL,
    PRIMARY KEY(experiment_id,feature_id)
);
CREATE INDEX IF NOT EXISTS eval_predictions_geom ON eval_predictions USING gist(geom);
CREATE TABLE IF NOT EXISTS eval_evaluations (
    id uuid PRIMARY KEY, experiment_id uuid REFERENCES eval_experiments(id),
    rules jsonb NOT NULL, metrics jsonb NOT NULL, matches jsonb NOT NULL,
    truth_version text NOT NULL, projection text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS eval_evaluations_experiment ON eval_evaluations(experiment_id,created_at);
CREATE TABLE IF NOT EXISTS eval_truths (
    evaluation_id uuid REFERENCES eval_evaluations(id), feature_id text NOT NULL,
    feature jsonb NOT NULL, geom geometry(Geometry,4326) NOT NULL,
    PRIMARY KEY(evaluation_id,feature_id)
);
CREATE INDEX IF NOT EXISTS eval_truths_geom ON eval_truths USING gist(geom);
