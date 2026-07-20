"""Low-noise diagnostics for optional QGIS API fallbacks.

Rendering and GUI integration deliberately recover from unavailable optional
methods across supported QGIS/Qt versions.  Record each failing call site at
most once at Python's debug logging level, so these recoveries remain
observable during development without flooding the QGIS log or changing the
established fallback behavior.
"""

from __future__ import annotations

import logging
import sys


_LOGGER = logging.getLogger("curved_polygon_text")
_REPORTED_CALL_SITES = set()
_MAX_REPORTED_CALL_SITES = 512


def record_suppressed_exception():
    """Record the active exception once and allow the caller to recover."""
    exc_type, exc_value, exc_traceback = sys.exc_info()
    if exc_type is None:
        return

    try:
        frame = sys._getframe(1)
        key = (frame.f_code.co_filename, frame.f_lineno, exc_type)
    except (AttributeError, ValueError):
        key = ("unknown", 0, exc_type)

    if key in _REPORTED_CALL_SITES:
        return
    if len(_REPORTED_CALL_SITES) >= _MAX_REPORTED_CALL_SITES:
        return
    _REPORTED_CALL_SITES.add(key)
    _LOGGER.debug(
        "Recovered from optional API failure at %s:%s",
        key[0], key[1],
        exc_info=(exc_type, exc_value, exc_traceback),
    )
