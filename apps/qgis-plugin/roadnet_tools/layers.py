"""Render backend GeoJSON into replaceable QGIS memory layers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsJsonUtils,
    QgsLayerTreeLayer,
    QgsProject,
    QgsVectorLayer,
    QgsCategorizedSymbolRenderer,
    QgsRendererCategory,
    QgsSymbol,
)

from .core import ContractError, ParsedGeoJson, geometry_groups


OUTPUT_GROUP_NAME = "tmp"
OUTPUT_LAYER_NAME = "tmp"
OUTPUT_PROPERTY = "roadnet_tools/output"
_GEOMETRY_TYPES = {
    "Point",
    "MultiPoint",
    "LineString",
    "MultiLineString",
    "Polygon",
    "MultiPolygon",
    "GeometryCollection",
    "None",
}
_GEOMETRY_PRIORITY = {
    "LineString": 0,
    "MultiLineString": 1,
    "Polygon": 2,
    "MultiPolygon": 3,
    "Point": 4,
    "MultiPoint": 5,
    "GeometryCollection": 6,
    "None": 7,
}


def replace_output_layers(
    parsed: ParsedGeoJson, operation: str
) -> tuple[list[QgsVectorLayer], int]:
    """Atomically replace plugin-owned output with new in-memory layers."""

    crs = QgsCoordinateReferenceSystem(parsed.crs)
    if not crs.isValid():
        raise ContractError(f"Backend returned an invalid CRS: {parsed.crs!r}.")

    groups = _output_groups(parsed)
    layers: list[QgsVectorLayer] = []
    for name, geometry_type, features, replay_layer in groups:
        layer = _make_memory_layer(name, geometry_type, features, crs)
        layer.setCustomProperty(OUTPUT_PROPERTY, True)
        layer.setCustomProperty("roadnet_tools/operation", operation)
        layer.setCustomProperty(
            "roadnet_tools/metadata",
            json.dumps(parsed.metadata, ensure_ascii=False, default=str),
        )
        layer.setCustomProperty("roadnet_tools/replay_layer", replay_layer or "")
        _apply_default_style(layer, operation, replay_layer=replay_layer)
        layers.append(layer)

    project = QgsProject.instance()
    root = project.layerTreeRoot()
    output_group = root.findGroup(OUTPUT_GROUP_NAME)
    if output_group is None:
        output_group = root.addGroup(OUTPUT_GROUP_NAME)
    output_group.setCustomProperty(OUTPUT_PROPERTY, True)

    old_layer_ids = [
        node.layerId()
        for node in output_group.children()
        if isinstance(node, QgsLayerTreeLayer)
        and node.layer() is not None
        and bool(node.layer().customProperty(OUTPUT_PROPERTY, False))
    ]

    # Build and validate everything before this commit point so a bad response
    # never destroys the last useful result.
    for layer_id in old_layer_ids:
        project.removeMapLayer(layer_id)
    for layer in layers:
        project.addMapLayer(layer, False)
        output_group.addLayer(layer)
    output_group.setExpanded(True)

    feature_count = sum(layer.featureCount() for layer in layers)
    return layers, feature_count


def _output_groups(parsed: ParsedGeoJson):
    definitions = parsed.metadata.get("replay_layers")
    if definitions is None:
        groups = geometry_groups(parsed.feature_collection) or [("None", [])]
        groups.sort(key=lambda item: _GEOMETRY_PRIORITY.get(item[0], 99))
        return [(OUTPUT_LAYER_NAME if index == 0 else f"tmp — {kind}", kind, features, None)
                for index, (kind, features) in enumerate(groups)]
    allowed = {"turn_events", "cross_points", "subtracks", "segments"}
    if not isinstance(definitions, list) or not definitions:
        raise ContractError("Backend returned invalid replay layer definitions.")
    features_by_layer = {}
    for definition in definitions:
        if not isinstance(definition, dict):
            raise ContractError("Backend returned invalid replay layer definitions.")
        identifier = definition.get("id")
        if identifier not in allowed or identifier in features_by_layer:
            raise ContractError("Backend returned unknown or duplicate replay layers.")
        if definition.get("geometry_type") not in _GEOMETRY_TYPES:
            raise ContractError("Backend returned an invalid replay geometry type.")
        features_by_layer[identifier] = []
    for feature in parsed.feature_collection["features"]:
        identifier = (feature.get("properties") or {}).get("replay_layer")
        if identifier not in features_by_layer:
            raise ContractError("Replay feature has no matching output layer.")
        features_by_layer[identifier].append(feature)
    groups = []
    for definition in definitions:
        identifier = definition["id"]
        geometry = geometry_groups({"features": features_by_layer[identifier]})
        if not geometry:
            geometry = [(definition["geometry_type"], [])]
        for kind, features in geometry:
            name = f"tmp.{identifier}" if len(geometry) == 1 else f"tmp.{identifier} — {kind}"
            groups.append((name, kind, features, identifier))
    return groups


def _make_memory_layer(
    name: str,
    geometry_type: str,
    geojson_features: list[Mapping[str, Any]],
    crs: QgsCoordinateReferenceSystem,
) -> QgsVectorLayer:
    if geometry_type not in _GEOMETRY_TYPES:
        raise ContractError(f"Unsupported output geometry type: {geometry_type!r}.")

    uri = geometry_type if geometry_type == "None" else f"{geometry_type}?crs={crs.authid()}"
    layer = QgsVectorLayer(uri, name, "memory")
    if not layer.isValid():
        raise ContractError(
            f"QGIS could not create a memory layer for {geometry_type} geometry."
        )
    layer.setCrs(crs)

    property_names = _property_names(geojson_features)
    property_types = {
        key: _field_type(
            [
                (feature.get("properties") or {}).get(key)
                for feature in geojson_features
            ]
        )
        for key in property_names
    }

    provider = layer.dataProvider()
    if property_names:
        if not provider.addAttributes(
            [QgsField(key, property_types[key]) for key in property_names]
        ):
            raise ContractError("QGIS could not create fields for the tmp layer.")
        layer.updateFields()

    qgis_features: list[QgsFeature] = []
    for index, geojson_feature in enumerate(geojson_features):
        qgis_feature = QgsFeature(layer.fields())
        geometry = geojson_feature.get("geometry")
        if geometry is not None:
            encoded = json.dumps(
                {"type": "Feature", "geometry": geometry, "properties": {}},
                ensure_ascii=False, separators=(",", ":")
            )
            parsed_features = QgsJsonUtils.stringToFeatureList(encoded)
            if len(parsed_features) != 1 or not parsed_features[0].hasGeometry():
                raise ContractError(f"QGIS could not parse output geometry {index}.")
            qgis_feature.setGeometry(parsed_features[0].geometry())

        properties = geojson_feature.get("properties") or {}
        qgis_feature.setAttributes(
            [
                _field_value(properties.get(key), property_types[key])
                for key in property_names
            ]
        )
        qgis_features.append(qgis_feature)

    if qgis_features:
        result = provider.addFeatures(qgis_features)
        succeeded = result[0] if isinstance(result, tuple) else result
        if not succeeded:
            raise ContractError("QGIS could not add backend features to the tmp layer.")
        layer.updateExtents()
    return layer


def _property_names(features: list[Mapping[str, Any]]) -> list[str]:
    names: list[str] = []
    for feature in features:
        properties = feature.get("properties") or {}
        for raw_name in properties:
            name = str(raw_name)
            if name not in names:
                names.append(name)
    return names


def _field_type(values: list[Any]):
    non_null = [value for value in values if value is not None]
    if non_null and all(isinstance(value, bool) for value in non_null):
        return QVariant.Bool
    if non_null and all(
        isinstance(value, int) and not isinstance(value, bool) for value in non_null
    ):
        return QVariant.LongLong
    if non_null and all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in non_null
    ):
        return QVariant.Double
    return QVariant.String


def _field_value(value: Any, field_type: Any) -> Any:
    if value is None:
        return None
    if field_type == QVariant.String:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return str(value)
    return value


def _apply_default_style(layer: QgsVectorLayer, operation: str, *, replay_layer: str | None = None) -> None:
    if (operation == "replay" and replay_layer not in {"turn_events", "cross_points"}
            and layer.fields().indexFromName("replay_role") >= 0):
        categories = []
        for role, color, width, label in (
            ("context", "#808080", 0.4, "其他 segment"),
            ("highlight", "#ff00ff", 1.0, "直接参与 segment"),
        ):
            symbol = QgsSymbol.defaultSymbol(layer.geometryType())
            symbol.setColor(QColor(color))
            symbol.setOpacity(0.5 if role == "context" else 1.0)
            if hasattr(symbol, "setWidth"):
                symbol.setWidth(width)
            for index in range(symbol.symbolLayerCount()):
                symbol.symbolLayer(index).setRenderingPass(1 if role == "highlight" else 0)
            categories.append(QgsRendererCategory(role, symbol, label))
        renderer = QgsCategorizedSymbolRenderer("replay_role", categories)
        renderer.setUsingSymbolLevels(True)
        layer.setRenderer(renderer)
        return
    renderer = layer.renderer()
    symbol = renderer.symbol() if renderer is not None else None
    if symbol is None:
        return
    event_colors = {"turn_events": "#2563eb", "cross_points": "#f97316"}
    symbol.setColor(QColor(event_colors.get(replay_layer, "#2563eb" if operation == "replay" else "#f97316")))
    if hasattr(symbol, "setWidth"):
        symbol.setWidth(0.8)
    if hasattr(symbol, "setSize"):
        symbol.setSize(3.0)
