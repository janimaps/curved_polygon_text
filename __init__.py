# -*- coding: utf-8 -*-
"""QGIS plugin entry point."""


def classFactory(iface):  # noqa: N802 - QGIS-mandated name
    """Load the Curved & Polygon Text plugin class."""
    from .layout_plugin import CurvedPolygonTextPlugin
    return CurvedPolygonTextPlugin(iface)
