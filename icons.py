# -*- coding: utf-8 -*-
"""Bundled PNG icons used by the custom layout items and property metadata."""

from __future__ import annotations

import os

from qgis.PyQt.QtGui import QIcon


_PLUGIN_DIR = os.path.dirname(__file__)
_RESOURCE_DIR = os.path.join(_PLUGIN_DIR, "resources")
_ICON_CACHE = {}


def _resource_icon(filename):
    icon = _ICON_CACHE.get(filename)
    if icon is None:
        icon = QIcon(os.path.join(_RESOURCE_DIR, filename))
        _ICON_CACHE[filename] = icon
    # QIcon copies are implicitly shared, so callers cannot mutate the cached
    # wrapper while all uses still share the same decoded icon data.
    return QIcon(icon)


def spline_icon():
    return _resource_icon("spline.png")


def polygon_icon():
    return _resource_icon("polygon.png")


def edit_spline_icon():
    return _resource_icon("edit-spline.png")


def node_tool_icon():
    return edit_spline_icon()


def plugin_icon():
    return polygon_icon()
