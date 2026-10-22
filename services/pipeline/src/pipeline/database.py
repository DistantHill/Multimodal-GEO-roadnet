"""Optional atomic publication of final, self-contained human-facing features.

Only intersections and centerlines are published. Intermediate points, events,
clips, trial metadata and configurations remain file artifacts.
"""
from __future__ import annotations

import json
import math
import os
from typing import Any

import numpy as np
from pyproj import CRS, Transformer
from shapely import transform
from shapely.geometry import shape


def _rows(features: list[dict], kind: str, processing_crs: str) -> list[tuple]:
    expected = "Point" if kind == "intersections" else "LineString"
    metric = CRS(processing_crs)
    if not metric.is_projected or any(abs(axis.unit_conversion_factor - 1) > 1e-9 for axis in metric.axis_info[:2]):
        raise ValueError("Publication input must use a projected CRS with metre axes")
    project = Transformer.from_crs(metric, "EPSG:4326", always_xy=True).transform
    rows, seen = [], set()
    for feature in features:
        if feature.get("type") != "Feature":
            raise ValueError(f"{kind} publication expects GeoJSON Features")
        props = dict(feature.get("properties") or {})
        identifier = feature.get("id", props.get("id"))
        if identifier is None or isinstance(identifier, bool) or not str(identifier):
            raise ValueError(f"{kind} feature is missing a primary id")
        identifier = str(identifier)
        if props.get("id") is not None and str(props["id"]) != identifier:
            raise ValueError("Feature id and properties.id disagree")
        if identifier in seen:
            raise ValueError(f"Duplicate primary id in {kind}: {identifier}")
        seen.add(identifier)
        required = ["run_id", "refine_id", "detect_id"]
        if kind == "centerlines":
            required.append("fit_id")
        for key in required:
            if not isinstance(props.get(key), str) or not props[key]:
                raise ValueError(f"{kind} feature {identifier} requires {key}")
        geometry = shape(feature.get("geometry"))
        if geometry.geom_type != expected or geometry.is_empty or not geometry.is_valid:
            raise ValueError(f"{kind} requires nonempty, valid {expected} geometry")
        if geometry.has_z:
            raise ValueError("Publication requires two-dimensional metric geometry")
        coordinates = list(geometry.coords)
        if any(not all(math.isfinite(value) for value in coordinate) for coordinate in coordinates):
            raise ValueError("Publication geometry must have finite coordinates")
        projected = transform(geometry, lambda xy: np.column_stack(project(xy[:, 0], xy[:, 1])))
        if projected.is_empty or not projected.is_valid or any(
            not all(math.isfinite(value) for value in coordinate)
            or not -180 <= coordinate[0] <= 180 or not -90 <= coordinate[1] <= 90
            for coordinate in projected.coords
        ):
            raise ValueError("Publication geometry could not be transformed to EPSG:4326")
        props.setdefault("source_geometry_crs", metric.to_string())
        # Fail before starting a transaction if properties are not valid JSON.
        json.dumps(props, allow_nan=False)
        rows.append((identifier, props["run_id"], props["refine_id"], props["detect_id"],
                     props.get("fit_id"), props, bytes(projected.wkb)))
    return rows


def publish(config: dict, intersections: list[dict], centerlines: list[dict]) -> dict[str, Any]:
    """Publish both layers in one transaction; an existing primary key is an error.

    The caller must supply metric GeoJSON with lineage attributes. PostGIS must
    already be installed and visible on the connection's search_path. No server
    credentials are returned, persisted or included in raised database errors.
    """
    database = config.get("database", {})
    if not database.get("enabled", False):
        return {"status": "DISABLED"}
    schema = database.get("schema", "roadnet_workbench")
    if not isinstance(schema, str) or not schema or len(schema.encode()) > 63 or "\x00" in schema:
        raise ValueError("database.schema must be a nonempty PostgreSQL identifier of at most 63 bytes")
    env_name = database.get("dsn_env", "ROADNET_RESULT_DSN")
    if not isinstance(env_name, str) or not env_name or "\x00" in env_name:
        raise ValueError("database.dsn_env must name the connection-string environment variable")
    dsn = os.environ.get(env_name)
    if not dsn:
        raise ValueError(f"Set the {env_name} environment variable before publishing final features")
    try:
        import psycopg
        from psycopg import sql
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise RuntimeError("PostgreSQL publication requires the pipeline's postgres extra") from exc
    metric = config["input"]["processing_crs"]
    layers = {
        "intersections": _rows(intersections, "intersections", metric),
        "centerlines": _rows(centerlines, "centerlines", metric),
    }
    try:
        with psycopg.connect(dsn) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgis')")
                    if not cursor.fetchone()[0]:
                        raise ValueError("PostGIS must already be installed in the result database")
                    cursor.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
                    for layer, rows in layers.items():
                        # Geometry type is from a fixed internal allowlist, never user SQL.
                        geom_type = sql.SQL("Point" if layer == "intersections" else "LineString")
                        table = sql.Identifier(schema, layer)
                        cursor.execute(sql.SQL("""
                            CREATE TABLE IF NOT EXISTS {} (
                                id TEXT PRIMARY KEY,
                                run_id TEXT NOT NULL,
                                refine_id TEXT NOT NULL,
                                detect_id TEXT NOT NULL,
                                fit_id TEXT,
                                properties JSONB NOT NULL,
                                geom geometry({}, 4326) NOT NULL
                            )
                        """).format(table, geom_type))
                        cursor.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} USING GIST (geom)")
                                       .format(sql.Identifier(f"{layer}_geom_gist"), table))
                        statement = sql.SQL("""
                            INSERT INTO {} (id, run_id, refine_id, detect_id, fit_id, properties, geom)
                            VALUES (%s, %s, %s, %s, %s, %s, ST_GeomFromWKB(%s, 4326))
                        """).format(table)
                        values = [(*row[:5], Jsonb(row[5]), row[6]) for row in rows]
                        if values:
                            cursor.executemany(statement, values)
    except psycopg.errors.UniqueViolation:
        raise RuntimeError(
            "Publication found an existing result primary id; both layers were rolled back. "
            "Use a new run identity instead of overwriting existing results."
        ) from None
    except psycopg.Error as exc:
        raise RuntimeError(
            f"Final-feature publication failed ({type(exc).__name__}); the transaction was rolled back. "
            "Check the configured connection, schema permissions and PostGIS availability."
        ) from None
    return {"status": "PUBLISHED", "schema": schema, "crs": "EPSG:4326",
            "tables": {name: len(rows) for name, rows in layers.items()}}
