"""Exercise actual QGIS geometry and symbols when the QGIS runtime is installed."""
import os
import unittest
from pathlib import Path
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from qgis.core import QgsApplication, QgsProject, QgsWkbTypes
except ImportError:
    QgsApplication = None


@unittest.skipIf(QgsApplication is None, "QGIS runtime is not installed")
class ReplayGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = tempfile.TemporaryDirectory(prefix="roadnet-qgis-test-")
        os.environ["QGIS_AUTH_DB_DIR_PATH"] = cls.profile.name
        cls.app = QgsApplication.instance() or QgsApplication([], False, cls.profile.name)
        cls.app.initQgis()

    def tearDown(self):
        QgsProject.instance().clear()

    def test_segments_are_drawable_with_classified_opacity_and_color(self):
        from roadnet_tools.core import parse_geojson_response
        from roadnet_tools.layers import replace_output_layers
        features = [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[108, 34], [108.1, 34.1]]},
                     "properties": {"replay_role": role, "source_row_keys": ["1", "2"]}}
                    for role in ("context", "highlight")]
        layers, count = replace_output_layers(parse_geojson_response({"type": "FeatureCollection", "features": features}), "replay")
        self.assertEqual(count, 2)
        layer = layers[0]
        self.assertEqual(layer.geometryType(), QgsWkbTypes.LineGeometry)
        self.assertTrue(all(f.hasGeometry() and not f.geometry().isEmpty() for f in layer.getFeatures()))
        categories = {c.value(): c.symbol().clone() for c in layer.renderer().categories()}
        self.assertEqual(categories["context"].opacity(), 0.5)
        self.assertEqual(categories["highlight"].color().name(), "#ff00ff")
        self.assertEqual(categories["highlight"].opacity(), 1.0)

    def test_polygon_selection_reaches_both_replay_and_clip(self):
        from unittest.mock import Mock, patch
        from qgis.core import QgsVectorLayer, QgsFeature, QgsGeometry
        from roadnet_tools.plugin import RoadnetToolsPlugin
        from roadnet_tools.settings import PluginSettings

        class IntersectionLayer(QgsVectorLayer):
            def providerType(self):
                return "postgres"

            def source(self):
                return 'dbname=roadnet table="public"."intersections" (geom)'

        layer = IntersectionLayer("Polygon?crs=EPSG:4326&field=id:integer&field=detect_id:string", "intersections", "memory")
        for identifier in (101, 202):
            feature = QgsFeature(layer.fields())
            feature.setAttributes([identifier, "12345678-1234-5678-1234-567812345678"])
            feature.setGeometry(QgsGeometry.fromWkt("POLYGON((108 34,109 34,109 35,108 34))"))
            layer.dataProvider().addFeatures([feature])
        layer.selectAll()
        plugin = RoadnetToolsPlugin.__new__(RoadnetToolsPlugin)
        plugin.iface = Mock()
        plugin.iface.activeLayer.return_value = layer
        plugin._client = Mock()
        plugin._warn = Mock()
        settings = Mock(backend_url="http://localhost:8000", replay_path="/api/v1/map/replay", clip_path="/api/v1/map/clip")
        with patch.object(PluginSettings, "load", return_value=settings):
            plugin.replay_selection()
            plugin.clip_selection()
        plugin._warn.assert_not_called()
        self.assertEqual(plugin._client.post_json.call_count, 2)
        for call in plugin._client.post_json.call_args_list:
            self.assertEqual(set(call.args[2]["intersection_ids"]), {101, 202})
            self.assertEqual(call.args[2]["database_name"], "roadnet")

    def test_intersection_replay_splits_semantic_layers_with_same_geometry(self):
        from roadnet_tools.core import ContractError, parse_geojson_response
        from roadnet_tools.layers import replace_output_layers
        definitions = [
            {"id": "turn_events", "geometry_type": "Point"},
            {"id": "cross_points", "geometry_type": "Point"},
            {"id": "segments", "geometry_type": "LineString"},
            {"id": "subtracks", "geometry_type": "LineString"},
        ]
        features = []
        for name, role in (("turn_events", "event"), ("cross_points", "event"),
                           ("subtracks", "context"), ("segments", "highlight")):
            geometry = {"type": "Point", "coordinates": [108, 34]} if role == "event" else {
                "type": "LineString", "coordinates": [[108, 34], [108.1, 34.1]]}
            features.append({"type": "Feature", "geometry": geometry,
                             "properties": {"replay_layer": name, "replay_role": role}})
        payload = {"type": "FeatureCollection", "features": features, "metadata": {"replay_layers": definitions}}
        layers, count = replace_output_layers(parse_geojson_response(payload), "replay")
        self.assertEqual(count, 4)
        self.assertEqual([layer.name() for layer in layers],
                         ["tmp.turn_events", "tmp.cross_points", "tmp.segments", "tmp.subtracks"])
        self.assertTrue(all(f.hasGeometry() for layer in layers for f in layer.getFeatures()))
        self.assertEqual(layers[0].renderer().symbol().color().name(), "#2563eb")
        self.assertEqual(layers[1].renderer().symbol().color().name(), "#f97316")
        old_ids = {layer.id() for layer in layers}
        payload["features"] = [{**features[0], "properties": {"replay_layer": "unknown"}}]
        with self.assertRaises(ContractError):
            replace_output_layers(parse_geojson_response(payload), "replay")
        self.assertTrue(old_ids <= set(QgsProject.instance().mapLayers()))
        payload["features"] = []
        new_layers, count = replace_output_layers(parse_geojson_response(payload), "replay")
        self.assertEqual(count, 0)
        self.assertEqual(len(new_layers), 4)
        self.assertFalse(old_ids & set(QgsProject.instance().mapLayers()))
