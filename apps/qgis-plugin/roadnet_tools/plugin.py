"""QGIS plugin entry point for replay and clip map operations."""

from __future__ import annotations

import os
from typing import Any

from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
from qgis.core import NULL, Qgis, QgsDataSourceUri, QgsVectorLayer, QgsWkbTypes
from qgis.core import QgsRectangle, QgsCoordinateTransform, QgsProject

from .client import BackendClient
from .database_browser import DatabaseBrowserDialog
from .core import (
    ContractError,
    SelectionRecord,
    build_clip_payload,
    build_replay_payload,
    endpoint_url,
    parse_geojson_response,
)
from .layers import replace_output_layers
from .settings import PluginSettings, SettingsDialog


MENU_NAME = "&Roadnet Tools"


class RoadnetToolsPlugin:
    def __init__(self, iface: Any):
        self.iface = iface
        self._actions: list[QAction] = []
        self._toolbar = None
        self._database_browser = None
        self._client = BackendClient()
        self._client.request_started.connect(self._on_request_started)
        self._client.request_succeeded.connect(self._on_request_succeeded)
        self._client.request_failed.connect(self._on_request_failed)

    def initGui(self) -> None:  # QGIS plugin API naming
        self._toolbar = self.iface.addToolBar("Roadnet Tools")
        self._toolbar.setObjectName("RoadnetToolsToolbar")

        icon_path = os.path.join(os.path.dirname(__file__), "icon.svg")
        icon = QIcon(icon_path)
        self._add_action(
            icon,
            "Replay selected points",
            "Replay trajectories through the selected raw points",
            self.replay_selection,
        )
        self._add_action(
            icon,
            "Clip between intersections",
            "Clip trajectories between exactly two selected intersections",
            self.clip_selection,
        )
        self._add_action(
            icon,
            "Browse databases",
            "浏览数据库、表名和估算行数",
            self.show_database_browser,
        )
        self._add_action(
            icon,
            "Roadnet Tools settings",
            "Configure the Roadnet backend and endpoint paths",
            self.show_settings,
        )

    def unload(self) -> None:
        if self._database_browser is not None:
            self._database_browser.close()
            self._database_browser.deleteLater()
        for action in self._actions:
            self.iface.removePluginVectorMenu(MENU_NAME, action)
            self.iface.removeToolBarIcon(action)
        self._actions.clear()
        if self._toolbar is not None:
            self._toolbar.deleteLater()
            self._toolbar = None
        for signal, slot in (
            (self._client.request_started, self._on_request_started),
            (self._client.request_succeeded, self._on_request_succeeded),
            (self._client.request_failed, self._on_request_failed),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._client.cancel_all()

    def replay_selection(self) -> None:
        try:
            layer = self.iface.activeLayer()
            intersection_layer = (
                isinstance(layer, QgsVectorLayer)
                and layer.providerType() == "postgres"
                and QgsDataSourceUri(layer.source()).table() == "intersections"
            )
            records = self._selected_point_records(allow_polygon=intersection_layer)
            payload = build_replay_payload(records, intersection_layer=intersection_layer)
            payload["database_name"] = self._selected_database_name()
            settings = PluginSettings.load()
            url = endpoint_url(settings.backend_url, settings.replay_path)
        except ContractError as exc:
            self._warn(str(exc))
            return
        self._client.post_json("replay", url, payload)

    def clip_selection(self) -> None:
        try:
            records = self._selected_point_records()
            payload = build_clip_payload(records)
            payload["database_name"] = self._selected_database_name()
            settings = PluginSettings.load()
            url = endpoint_url(settings.backend_url, settings.clip_path)
        except ContractError as exc:
            self._warn(str(exc))
            return
        self._client.post_json("clip", url, payload)

    def show_settings(self) -> None:
        try:
            dialog = SettingsDialog(self.iface.mainWindow())
        except ContractError as exc:
            self._critical(
                f"Saved settings are invalid: {exc} Reset them in QGIS Settings if needed."
            )
            return
        dialog.exec_()

    def show_database_browser(self) -> None:
        if self._database_browser is not None:
            self._database_browser.close()
            self._database_browser.deleteLater()
        try:
            self._database_browser = DatabaseBrowserDialog(self.iface.mainWindow(), self._critical)
        except ContractError as exc:
            self._critical(str(exc))
            return
        self._database_browser.show()

    def _add_action(
        self,
        icon: QIcon,
        text: str,
        status_tip: str,
        callback: Any,
    ) -> None:
        action = QAction(icon, text, self.iface.mainWindow())
        action.setStatusTip(status_tip)
        action.triggered.connect(callback)
        self.iface.addPluginToVectorMenu(MENU_NAME, action)
        self._toolbar.addAction(action)
        self._actions.append(action)

    def _selected_database_name(self) -> str:
        layer = self.iface.activeLayer()
        if not isinstance(layer, QgsVectorLayer) or layer.providerType() != "postgres":
            raise ContractError("无法确认图层数据库。请选择 PostgreSQL 图层，并确认 database 是否对应。")
        database_name = QgsDataSourceUri(layer.source()).database()
        if not database_name:
            raise ContractError("图层连接中缺少 database name，请在 PostgreSQL 连接设置中明确指定数据库。")
        return database_name

    def _selected_point_records(self, *, allow_polygon: bool = False) -> list[SelectionRecord]:
        layer = self.iface.activeLayer()
        if not isinstance(layer, QgsVectorLayer):
            raise ContractError("Make a point vector layer active, then select its features.")
        if not allow_polygon and QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PointGeometry:
            raise ContractError("The active layer must be a point vector layer.")

        selected = layer.selectedFeatures()
        if not selected:
            raise ContractError(
                "Select points in the active layer with QGIS point or rectangle selection first."
            )

        fields = layer.fields()
        records: list[SelectionRecord] = []
        for feature in selected:
            properties = {
                field.name(): _python_value(feature.attribute(field.name()))
                for field in fields
            }
            records.append(SelectionRecord(properties, feature.id()))
        return records

    def _on_request_started(self, operation: str) -> None:
        self.iface.messageBar().pushMessage(
            "Roadnet Tools",
            f"{operation.title()} request sent…",
            level=Qgis.Info,
            duration=2,
        )

    def _on_request_succeeded(self, operation: str, payload: Any) -> None:
        try:
            parsed = parse_geojson_response(payload)
            layers, feature_count = replace_output_layers(parsed, operation)
        except (ContractError, TypeError, ValueError) as exc:
            self._critical(f"Could not draw the {operation} response: {exc}")
            return

        if feature_count:
            extent = QgsRectangle()
            for layer in layers:
                transform = QgsCoordinateTransform(layer.crs(), self.iface.mapCanvas().mapSettings().destinationCrs(), QgsProject.instance())
                classified = layer.fields().indexFromName("replay_role") >= 0
                for feature in layer.getFeatures():
                    if classified and feature["replay_role"] == "context":
                        continue
                    if not feature.hasGeometry():
                        continue
                    bounds = transform.transformBoundingBox(feature.geometry().boundingBox())
                    if extent.isNull():
                        extent = QgsRectangle(bounds)
                    else:
                        extent.combineExtentWith(bounds)
            if not extent.isEmpty():
                self.iface.mapCanvas().setExtent(extent)
            self.iface.mapCanvas().refresh()
            self.iface.messageBar().pushMessage(
                "Roadnet Tools",
                f"{operation.title()} loaded {feature_count} feature(s) into tmp.",
                level=Qgis.Success,
                duration=5,
            )
        else:
            self._warn(
                f"{operation.title()} completed but returned no features; tmp is now empty."
            )

    def _on_request_failed(self, operation: str, message: str) -> None:
        self._critical(
            f"{operation.title()} failed: {message}. The previous tmp result was kept."
        )

    def _warn(self, message: str) -> None:
        self.iface.messageBar().pushMessage(
            "Roadnet Tools", message, level=Qgis.Warning, duration=7
        )

    def _critical(self, message: str) -> None:
        self.iface.messageBar().pushMessage(
            "Roadnet Tools", message, level=Qgis.Critical, duration=10
        )


def _python_value(value: Any) -> Any:
    if value is None or value is NULL:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
