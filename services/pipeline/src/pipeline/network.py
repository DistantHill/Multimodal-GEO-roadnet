"""Assemble final physical-road nodes/edges from already fitted feature records."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .artifacts import key, seal_stage, write_features, write_geojson


def run_network(config: dict, run_id: str, directory: Path, intersections: list[dict],
                centerlines: list[dict], monitor) -> tuple[dict, dict]:
    directory.mkdir(parents=True, exist_ok=True)
    network_id = key(run_id, "network")
    crs = config["input"]["processing_crs"]
    by_id = {row["id"]: row for row in intersections}
    if len(by_id) != len(intersections):
        raise ValueError("Duplicate intersection primary keys")
    degree: Counter = Counter()
    edges = []
    pairs = set()
    with monitor.stage("network") as span:
        with span.phase("compute"):
            for centerline in centerlines:
                properties = centerline["properties"]
                if properties["variant"] != "combined":
                    continue
                start, end = properties["from_intersection_id"], properties["to_intersection_id"]
                if start not in by_id or end not in by_id:
                    raise ValueError("Centerline references an unknown intersection")
                pair = tuple(sorted((start, end)))
                if pair in pairs or start == end:
                    raise ValueError("Duplicate/self-loop physical-road pair is unsupported")
                pairs.add(pair)
                edge_id = key(network_id, centerline["id"])
                edges.append({"type": "Feature", "id": edge_id, "geometry": centerline["geometry"],
                              "properties": {**properties, "edge_id": edge_id,
                                             "network_id": network_id, "directed": False}})
                degree[start] += 1
                degree[end] += 1
            nodes = [{**row, "properties": {**row["properties"], "network_id": network_id,
                                             "degree": degree[row["id"]]}} for row in intersections
                     if config["network"]["include_isolated_nodes"] or degree[row["id"]] > 0]
        with span.phase("write"):
            write_features(directory / "nodes.parquet", nodes, crs, "Point")
            write_features(directory / "edges.parquet", edges, crs, "LineString")
            if config["output"]["geojson"]:
                write_geojson(directory / "nodes.geojson", nodes, crs)
                write_geojson(directory / "edges.geojson", edges, crs)
        span.add_counts(nodes=len(nodes), edges=len(edges), isolated_nodes=sum(degree[row["id"]] == 0 for row in nodes))
        with span.phase("write"):
            manifest = seal_stage(directory, "network", network_id,
                          {"fit_id": key(run_id, "fit"), "detect_id": key(run_id, "detect")},
                          {"nodes": (directory / "nodes.parquet", "id"),
                           "edges": (directory / "edges.parquet", "id")}, config["network"])
    return manifest, {"nodes": nodes, "edges": edges}
