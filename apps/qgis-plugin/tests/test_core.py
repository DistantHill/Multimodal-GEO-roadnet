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
    def test_replay_intersection_uses_database_id_not_feature_id(self):
        self.assertEqual(build_replay_payload(
            [record({"id": 42, "detect_id": DETECT_ID}, 999)], intersection_layer=True,
        ), {"intersection_id": 42, "detect_id": DETECT_ID})

    def test_replay_intersection_rejects_missing_ids(self):
        with self.assertRaises(ContractError):
            build_replay_payload([record({})], intersection_layer=True)

    def test_replay_two_intersections_uses_database_ids(self):
        records = [record({"id": i, "detect_id": DETECT_ID}, i + 100) for i in (1, 2)]
        self.assertEqual(build_replay_payload(records, intersection_layer=True),
                         {"intersection_ids": [1, 2], "detect_id": DETECT_ID})
        self.assertEqual(build_clip_payload(records),
                         {"intersection_ids": [1, 2], "detect_id": DETECT_ID})

    def test_event_and_injected_entity_ids_never_become_source_keys(self):
        for table, field in (("tracks_cross", "tracks_cross_ids"),
                             ("turn_evnets_gemo", "turn_event_ids")):
            payload = build_replay_payload([record({"id": 7, "detect_id": DETECT_ID})], source_table=table)
            self.assertEqual(payload, {field: [7], "detect_id": DETECT_ID})
        payload = build_replay_payload([record({"id": 7, "refine_id": DETECT_ID})], source_table="injected_points")
        self.assertEqual(payload, {"injected_point_ids": [7], "refine_id": DETECT_ID})

    def test_replay_prefers_resolved_ids_and_requires_detect(self):
        payload = build_replay_payload(
            [
                record({"resolved_point_id": 10, "source_row_key": "a", "detect_id": DETECT_ID}),
                record({"resolved_point_id": "11", "detect_id": DETECT_ID}, 2),
                record({"resolved_point_id": 10, "detect_id": DETECT_ID}, 3),
            ]
        )
        self.assertEqual(
            payload, {"resolved_point_ids": [10, 11], "detect_id": DETECT_ID}
        )

    def test_replay_serializes_source_keys_and_may_omit_detect_id(self):
        payload = build_replay_payload(
            [record({"source_row_key": 1}), record({"database_id": "a:2"}, 2)]
        )
        self.assertEqual(payload, {"source_row_keys": ["1", "a:2"]})

    def test_replay_rejects_resolved_ids_without_one_detect(self):
        with self.assertRaisesRegex(ContractError, "requires one common"):
            build_replay_payload([record({"resolved_point_id": 1})])

    def test_replay_rejects_mixed_identity_domains(self):
        with self.assertRaisesRegex(ContractError, "cannot mix"):
            build_replay_payload([
                record({"resolved_point_id": 1, "detect_id": DETECT_ID}),
                record({"source_row_key": "2"}, 2),
            ])

    def test_replay_reports_features_without_database_ids(self):
        with self.assertRaisesRegex(ContractError, "missing on feature IDs: 9"):
            build_replay_payload([record({"detect_id": "a"}, 9)])

    def test_replay_rejects_non_positive_or_non_integer_resolved_ids(self):
        for point_id in (0, -1, "abc", 1.5, True):
            with self.subTest(point_id=point_id), self.assertRaises(ContractError):
                build_replay_payload([
                    record({"resolved_point_id": point_id, "detect_id": DETECT_ID})
                ])

    def test_clip_uses_two_distinct_intersection_ids_and_common_detect_id(self):
        payload = build_clip_payload(
            [
                record({"intersection_id": 101, "detect_id": DETECT_ID}, 70),
                record({"intersection_id": 202, "detect_id": DETECT_ID}, 120),
            ]
        )
        self.assertEqual(
            payload,
            {"detect_id": DETECT_ID, "intersection_ids": [101, 202]},
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
            build_clip_payload([record({"intersection_id": 1, "detect_id": DETECT_ID})])

    def test_clip_rejects_duplicate_intersections(self):
        with self.assertRaisesRegex(ContractError, "distinct"):
            build_clip_payload(
                [
                    record({"intersection_id": 1, "detect_id": DETECT_ID}, 10),
                    record({"intersection_id": 1, "detect_id": DETECT_ID}, 11),
                ]
            )

    def test_clip_rejects_non_positive_intersection_ids(self):
        for intersection_id in (0, -1):
            with (
                self.subTest(intersection_id=intersection_id),
                self.assertRaises(ContractError),
            ):
                build_clip_payload(
                    [
                        record({
                            "intersection_id": intersection_id,
                            "detect_id": DETECT_ID,
                        }),
                        record({"intersection_id": 2, "detect_id": DETECT_ID}, 2),
                    ]
                )

    def test_clip_requires_a_common_detect_id(self):
        with self.assertRaisesRegex(ContractError, "common 'detect_id'"):
            build_clip_payload(
                [
                    record({"intersection_id": 1, "detect_id": "a"}),
                    record({"intersection_id": 2, "detect_id": "b"}, 2),
                ]
            )

    def test_clip_rejects_an_invalid_detect_uuid(self):
        with self.assertRaisesRegex(ContractError, "valid UUID"):
            build_clip_payload(
                [
                    record({"intersection_id": 1, "detect_id": "not-a-uuid"}),
                    record({"intersection_id": 2, "detect_id": "not-a-uuid"}, 2),
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
