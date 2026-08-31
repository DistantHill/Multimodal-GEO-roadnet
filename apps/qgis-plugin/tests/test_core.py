from __future__ import annotations

import sys
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from roadnet_tools.core import (  # noqa: E402
    ContractError,
    SelectionRecord,
    build_clip_payload,
    build_replay_payload,
    endpoint_url,
    geometry_groups,
    normalize_backend_url,
    normalize_endpoint_path,
    parse_geojson_response,
)

DETECT_ID = "12345678-1234-5678-1234-567812345678"


def record(properties, feature_id=1):
    return SelectionRecord(properties, feature_id)


def feature(geometry_type="Point", coordinates=None, properties=None):
    if coordinates is None:
        coordinates = [120.1, 30.2]
    return {
        "type": "Feature",
        "geometry": {"type": geometry_type, "coordinates": coordinates},
        "properties": properties or {},
    }


class RequestContractTests(unittest.TestCase):
    def test_replay_uses_id_then_database_id_and_deduplicates(self):
        payload = build_replay_payload(
            [
                record({"id": 10, "database_id": 999, "detect_id": DETECT_ID}),
                record({"database_id": "11", "detect_id": DETECT_ID}, 2),
                record({"ID": 10, "detect_id": DETECT_ID}, 3),
            ]
        )
        self.assertEqual(
            payload, {"point_ids": [10, 11], "detect_id": DETECT_ID}
        )

    def test_replay_omits_detect_id_when_it_is_not_common(self):
        payload = build_replay_payload(
            [record({"id": 1, "detect_id": "a"}), record({"id": 2}, 2)]
        )
        self.assertEqual(payload, {"point_ids": [1, 2]})

    def test_replay_reports_features_without_database_ids(self):
        with self.assertRaisesRegex(ContractError, "missing on feature IDs: 9"):
            build_replay_payload([record({"detect_id": "a"}, 9)])

    def test_replay_rejects_non_positive_or_non_integer_ids(self):
        for point_id in (0, -1, "abc", 1.5, True):
            with self.subTest(point_id=point_id), self.assertRaises(ContractError):
                build_replay_payload([record({"id": point_id})])

    def test_clip_uses_two_distinct_cluster_ids_and_common_detect_id(self):
        payload = build_clip_payload(
            [
                record({"cluster_id": 7, "detect_id": DETECT_ID}, 70),
                record({"cluster_id": 12, "detect_id": DETECT_ID}, 120),
            ]
        )
        self.assertEqual(
            payload,
            {"detect_id": DETECT_ID, "intersection_ids": [7, 12]},
        )

    def test_clip_falls_back_to_qgis_feature_id(self):
        payload = build_clip_payload(
            [record({"detect_id": DETECT_ID}, 101), record({"detect_id": DETECT_ID}, 202)]
        )
        self.assertEqual(
            payload, {"detect_id": DETECT_ID, "intersection_ids": [101, 202]}
        )

    def test_clip_requires_exactly_two_features(self):
        with self.assertRaisesRegex(ContractError, "exactly two"):
            build_clip_payload([record({"cluster_id": 1, "detect_id": DETECT_ID})])

    def test_clip_rejects_duplicate_intersections(self):
        with self.assertRaisesRegex(ContractError, "distinct"):
            build_clip_payload(
                [
                    record({"cluster_id": 1, "detect_id": DETECT_ID}, 10),
                    record({"cluster_id": 1, "detect_id": DETECT_ID}, 11),
                ]
            )

    def test_clip_requires_a_common_detect_id(self):
        with self.assertRaisesRegex(ContractError, "common 'detect_id'"):
            build_clip_payload(
                [
                    record({"cluster_id": 1, "detect_id": "a"}),
                    record({"cluster_id": 2, "detect_id": "b"}, 2),
                ]
            )

    def test_clip_rejects_an_invalid_detect_uuid(self):
        with self.assertRaisesRegex(ContractError, "valid UUID"):
            build_clip_payload(
                [
                    record({"cluster_id": 1, "detect_id": "not-a-uuid"}),
                    record({"cluster_id": 2, "detect_id": "not-a-uuid"}, 2),
                ]
            )


class EndpointTests(unittest.TestCase):
    def test_endpoint_defaults_can_be_joined(self):
        self.assertEqual(
            endpoint_url("http://localhost:8000/", "api/v1/map/replay"),
            "http://localhost:8000/api/v1/map/replay",
        )

    def test_backend_url_may_include_a_reverse_proxy_prefix(self):
        self.assertEqual(
            endpoint_url("https://maps.example/roadnet", "/api/v1/map/clip"),
            "https://maps.example/roadnet/api/v1/map/clip",
        )

    def test_invalid_urls_and_endpoint_queries_are_rejected(self):
        for value in ("localhost:8000", "file:///tmp/socket", ""):
            with self.subTest(value=value), self.assertRaises(ContractError):
                normalize_backend_url(value)
        with self.assertRaises(ContractError):
            normalize_endpoint_path("/replay?token=secret")


class GeoJsonResponseTests(unittest.TestCase):
    def test_direct_feature_collection_defaults_to_epsg_4326(self):
        parsed = parse_geojson_response(
            {"type": "FeatureCollection", "features": [feature()]}
        )
        self.assertEqual(parsed.crs, "EPSG:4326")
        self.assertEqual(len(parsed.feature_collection["features"]), 1)

    def test_envelope_preserves_metadata_and_crs(self):
        parsed = parse_geojson_response(
            {
                "data": {"type": "FeatureCollection", "features": []},
                "crs": "epsg:3857",
                "meta": {"vehicles": 3},
            }
        )
        self.assertEqual(parsed.crs, "EPSG:3857")
        self.assertEqual(parsed.metadata, {"vehicles": 3})

    def test_named_crs_and_epsg_urn_are_supported(self):
        parsed = parse_geojson_response(
            {
                "type": "FeatureCollection",
                "features": [],
                "crs": {
                    "type": "name",
                    "properties": {"name": "urn:ogc:def:crs:EPSG::4326"},
                },
            }
        )
        self.assertEqual(parsed.crs, "EPSG:4326")

    def test_malformed_collection_is_rejected(self):
        bad_payloads = [
            [],
            {},
            {"type": "FeatureCollection", "features": {}},
            {"type": "FeatureCollection", "features": [{}]},
            {
                "type": "FeatureCollection",
                "features": [feature("CircularString")],
            },
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload), self.assertRaises(ContractError):
                parse_geojson_response(payload)

    def test_geometry_groups_preserve_first_seen_order(self):
        collection = {
            "type": "FeatureCollection",
            "features": [
                feature("Point"),
                feature("LineString", [[0, 0], [1, 1]]),
                feature("Point", [2, 2]),
            ],
        }
        groups = geometry_groups(collection)
        self.assertEqual([name for name, _ in groups], ["Point", "LineString"])
        self.assertEqual(len(groups[0][1]), 2)


if __name__ == "__main__":
    unittest.main()
