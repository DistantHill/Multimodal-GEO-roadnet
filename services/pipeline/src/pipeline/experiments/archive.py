"""Independent immutable experiment archive; no development database foreign keys."""
from __future__ import annotations

from contextlib import contextmanager
import json
import uuid

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .evaluation import evaluate, validated_features
from shapely.geometry import shape

SCHEMA_SQL = """
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
"""


@contextmanager
def connect_archive(settings: dict):
    values = _connection_settings(settings)
    values["dbname"] = values.pop("database", values.get("dbname", "roadnet_eval"))
    with psycopg.connect(**values) as connection:
        yield connection


def ensure_archive_database(settings: dict):
    """Create the explicitly configured independent DB during CLI initialization."""
    values = _connection_settings(settings)
    database = values.pop("database", values.pop("dbname", "roadnet_eval"))
    if not database or database in {"postgres", "template0", "template1", "roadnet", "roadnet_test"}:
        raise ValueError("archive database must be independent of development and system databases")
    values["dbname"] = "postgres"
    with psycopg.connect(**values, autocommit=True) as connection:
        if connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
            return
        try:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        except psycopg.errors.DuplicateDatabase:
            pass  # Concurrent initialization completed first.


def _connection_settings(settings: dict):
    # libpq expects integer text for its boolean-valued connection switches.
    # dataclasses.asdict(PostgresConfig) preserves Python bools, whose string
    # representation is not accepted by libpq for keepalives.
    return {key: int(value) if isinstance(value, bool) else value
            for key, value in settings.items()}


class ArchiveStore:
    def __init__(self, connection):
        self.connection = connection

    def initialize(self):
        self.connection.execute(SCHEMA_SQL)

    def save_experiment(self, record: dict, predictions: dict | list):
        _reject_secrets(record)
        features = validated_features(predictions)
        region = record.get("case_snapshot", {}).get("geometry")
        if region:
            boundary = shape(region)
            features = [feature for feature in features if boundary.covers(shape(feature["geometry"]).centroid)]
        identifier = record.get("id", record.get("experiment_id"))
        with self.connection.transaction():
            self.connection.execute(
                "INSERT INTO eval_experiments(id,record) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (identifier, Jsonb(record)),
            )
            existing = self.connection.execute(
                "SELECT record FROM eval_experiments WHERE id=%s FOR UPDATE", (identifier,)
            ).fetchone()[0]
            if existing != record:
                raise ValueError("experiment UUID already contains a different immutable snapshot")
            for feature in features:
                self.connection.execute(
                    "INSERT INTO eval_predictions(experiment_id,feature_id,feature,geom) "
                    "VALUES (%s,%s,%s,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)) ON CONFLICT DO NOTHING",
                    (identifier, feature["id"], Jsonb(feature), json.dumps(feature["geometry"])),
                )
            saved = self.connection.execute(
                "SELECT feature FROM eval_predictions WHERE experiment_id=%s ORDER BY feature_id",
                (identifier,),
            ).fetchall()
            if [row[0] for row in saved] != features:
                raise ValueError("experiment UUID already contains different prediction geometries")
        return str(identifier)

    def append_evaluation(self, experiment_id: str, truth_features: dict | list, rules: dict):
        record = self.get_experiment(experiment_id)
        predictions = [row[0] for row in self.connection.execute(
            "SELECT feature FROM eval_predictions WHERE experiment_id=%s ORDER BY feature_id", (experiment_id,)
        ).fetchall()]
        truths = validated_features(truth_features, truth=True)
        region = record.get("case_snapshot", {}).get("geometry")
        if region:
            boundary = shape(region)
            truths = [feature for feature in truths if boundary.covers(shape(feature["geometry"]).centroid)]
        result = evaluate(predictions, truths, rules)
        stored_rules = {**rules, "region_policy": "centroid_covers" if region else "all_features"}
        identifier = str(uuid.uuid4())
        with self.connection.transaction():
            self.connection.execute(
                "INSERT INTO eval_evaluations(id,experiment_id,rules,metrics,matches,truth_version,projection,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,clock_timestamp())",
                (identifier, experiment_id, Jsonb(stored_rules), Jsonb(result["metrics"]),
                 Jsonb(result["matches"]), result["truth_version"], result["projection"]),
            )
            for feature in truths:
                self.connection.execute(
                    "INSERT INTO eval_truths VALUES (%s,%s,%s,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))",
                    (identifier, feature["id"], Jsonb(feature), json.dumps(feature["geometry"])),
                )
        return {"evaluation_id": identifier, "rules": stored_rules, **result}

    def get_experiment(self, identifier: str):
        row = self.connection.execute(
            "SELECT record,cleaned FROM eval_experiments WHERE id=%s", (identifier,)
        ).fetchone()
        if row is None:
            raise KeyError(identifier)
        record = {"algorithm": "detect", **row[0], "experiment_id": str(identifier), "cleaned": row[1], "archive_status": "archived"}
        evaluations = self.evaluations(identifier)
        record["metrics"] = evaluations[0]["metrics"] if evaluations else None
        record["latest_evaluation_id"] = evaluations[0]["evaluation_id"] if evaluations else None
        record["evaluations"] = evaluations
        return record

    def evaluations(self, identifier: str):
        with self.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT id AS evaluation_id,rules,metrics,truth_version,projection,created_at "
                           "FROM eval_evaluations WHERE experiment_id=%s ORDER BY created_at DESC,id", (identifier,))
            return [{**row, "evaluation_id": str(row["evaluation_id"])} for row in cursor.fetchall()]

    def list_experiments(self, *, limit=100, offset=0, **filters):
        clauses, values = [], []
        for key in ("case_name", "scheme_name", "commit", "status"):
            if filters.get(key):
                clauses.append("record->>%s = %s")
                values.extend((key, filters[key]))
        for key, operator in (("started_after", ">="), ("started_before", "<=")):
            if filters.get(key):
                clauses.append(f"(record->>'started_at')::timestamptz {operator} %s")
                values.append(filters[key])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = self.connection.execute("SELECT count(*) FROM eval_experiments" + where, values).fetchone()[0]
        rows = self.connection.execute("SELECT id FROM eval_experiments" + where +
            " ORDER BY created_at DESC,id LIMIT %s OFFSET %s", [*values, limit, offset]).fetchall()
        return {"items": [self.get_experiment(str(row[0])) for row in rows], "total": total}

    def cleanup_guard(self, identifier: str):
        record = self.get_experiment(identifier)
        refine_id = record.get("refine_id")
        shared = []
        if refine_id:
            shared = [str(row[0]) for row in self.connection.execute(
                "SELECT id FROM eval_experiments WHERE id<>%s AND NOT cleaned AND record->>'refine_id'=%s",
                (identifier, refine_id),
            ).fetchall()]
        return {"archived": True, "shared_refine_experiments": shared, "can_delete_refine": not shared}

    def mark_cleaned(self, identifier: str):
        self.get_experiment(identifier)
        self.connection.execute("UPDATE eval_experiments SET cleaned=true WHERE id=%s", (identifier,))

    def map_features(self, identifier: str, bbox: tuple, limit: int, evaluation_id=None):
        record = self.get_experiment(identifier)
        evaluation_id = evaluation_id or record["latest_evaluation_id"]
        details = []
        if evaluation_id:
            row = self.connection.execute("SELECT matches FROM eval_evaluations WHERE id=%s AND experiment_id=%s",
                                          (evaluation_id, identifier)).fetchone()
            if row is None:
                raise KeyError(evaluation_id)
            details = row[0]
        labels = {}
        for detail in details:
            if detail.get("prediction_id") is not None:
                labels["prediction", detail["prediction_id"]] = (detail["classification"], detail.get("truth_id"))
            if detail.get("truth_id") is not None:
                labels["truth", detail["truth_id"]] = (detail["classification"], detail.get("prediction_id"))
        rows = self.connection.execute(
            "SELECT feature,layer,count(*) OVER () FROM (SELECT feature,geom,'prediction' AS layer FROM eval_predictions "
            "WHERE experiment_id=%s UNION ALL SELECT feature,geom,'truth' AS layer FROM eval_truths "
            "WHERE evaluation_id=%s) features WHERE ST_Intersects(geom,ST_MakeEnvelope(%s,%s,%s,%s,4326)) "
            "ORDER BY layer,feature->>'id' LIMIT %s", (identifier, evaluation_id, *bbox, limit + 1),
        ).fetchall()
        result = []
        for feature, layer, _ in rows[:limit]:
            classification, matched = labels.get((layer, feature["id"]), (None, None))
            result.append({**feature, "properties": {**feature.get("properties", {}),
                          "id": feature["id"], "layer": layer, "classification": classification, "matched_id": matched}})
        region = record.get("case_snapshot", {}).get("geometry")
        if region and self.connection.execute(
            "SELECT ST_Intersects(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326),"
            "ST_MakeEnvelope(%s,%s,%s,%s,4326))", (json.dumps(region), *bbox)
        ).fetchone()[0]:
            result.append({"type": "Feature", "id": "region", "geometry": region,
                           "properties": {"id": "region", "layer": "region", "classification": None}})
        return {"type": "FeatureCollection", "features": result,
                "metadata": {"has_more": len(rows) > limit, "truncated": len(rows) > limit,
                             "total": rows[0][2] if rows else 0}}


def _reject_secrets(value):
    """Prevent connection secrets from leaking through an experiment snapshot."""
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in {"password", "postgres_password", "secret", "token", "dsn"}:
                raise ValueError("experiment snapshot must not contain credentials")
            _reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secrets(item)
