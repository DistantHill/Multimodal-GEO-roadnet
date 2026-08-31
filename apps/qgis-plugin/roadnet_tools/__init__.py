"""Roadnet Tools QGIS plugin."""


def classFactory(iface):  # QGIS plugin API naming
    from .plugin import RoadnetToolsPlugin

    return RoadnetToolsPlugin(iface)
