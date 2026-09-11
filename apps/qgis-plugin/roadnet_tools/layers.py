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

    groups = geometry_groups(parsed.feature_collection)
    if not groups:
        groups = [("None", [])]
    groups.sort(key=lambda item: _GEOMETRY_PRIORITY.get(item[0], 99))

    layers: list[QgsVectorLayer] = []
    for index, (geometry_type, features) in enumerate(groups):
        name = OUTPUT_LAYER_NAME if index == 0 else f"tmp — {geometry_type}"
        layer = _make_memory_layer(name, geometry_type, features, crs)
        layer.setCustomProperty(OUTPUT_PROPERTY, True)
        layer.setCustomProperty("roadnet_tools/operation", operation)
        layer.setCustomProperty(
            "roadnet_tools/metadata",
            json.dumps(parsed.metadata, ensure_ascii=False, default=str),
        )
        _apply_default_style(layer, operation)
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
                geometry, ensure_ascii=False, separators=(",", ":")
            )
            qgis_geometry = QgsJsonUtils.geometryFromGeoJson(encoded)
            if qgis_geometry.isNull():
                raise ContractError(f"QGIS could not parse output geometry {index}.")
            qgis_feature.setGeometry(qgis_geometry)

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


def _apply_default_style(layer: QgsVectorLayer, operation: str) -> None:
    if operation == "replay" and layer.fields().indexFromName("replay_role") >= 0:
        categories = []
        for role, color, width, label in (
            ("context", "#9ca3af", 0.4, "其他 segment"),
            ("highlight", "#e33f8e", 1.0, "直接参与 segment"),
        ):
            symbol = QgsSymbol.defaultSymbol(layer.geometryType())
            symbol.setColor(QColor(color))
            if hasattr(symbol, "setWidth"):
                symbol.setWidth(width)
            categories.append(QgsRendererCategory(role, symbol, label))
        layer.setRenderer(QgsCategorizedSymbolRenderer("replay_role", categories))
        return
    renderer = layer.renderer()
    symbol = renderer.symbol() if renderer is not None else None
    if symbol is None:
        return
    symbol.setColor(QColor("#2563eb" if operation == "replay" else "#f97316"))
    if hasattr(symbol, "setWidth"):
        symbol.setWidth(0.8)
    if hasattr(symbol, "setSize"):
        symbol.setSize(3.0)
