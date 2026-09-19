# -*- coding: utf-8 -*-
"""Persistent recovery support for custom Curved/Polygon Text layout items.

The backup is deliberately stored as a QgsLayout custom property. QGIS core
serializes layout custom properties even when this plugin is not installed, so
saving a project while the custom item types are unavailable does not destroy
our recovery copy.
"""

from __future__ import annotations

import json

from qgis.core import QgsProject, QgsReadWriteContext
from qgis.PyQt.QtCore import QTimer

try:
    from qgis.PyQt import sip as _sip
except ImportError:  # pragma: no cover - fallback for unusual bindings.
    try:
        import sip as _sip
    except ImportError:  # pragma: no cover
        _sip = None
from qgis.PyQt.QtXml import QDomDocument

from .layout_item_polygon_text import LayoutItemPolygonText
from .layout_item_spline_text import LayoutItemSplineText
from .reliability import record_suppressed_exception


RECOVERY_PROPERTY = "curved_polygon_text/recovery_v1"
RECOVERY_SCHEMA = 1
_SNAPSHOT_LAYOUT_IDS = set()
_ITEM_WRITE_SNAPSHOT_IDS = set()
_ITEM_WRITE_SNAPSHOT_CLEAR_SCHEDULED = False
_SCHEDULED_LAYOUT_NAMES = set()


def _is_live_qt_object(obj):
    """Return False when a Python wrapper no longer owns a live Qt object.

    Calling methods on a SIP wrapper whose C++ object has already been deleted
    can terminate QGIS before Python has a chance to raise an exception.  The
    recovery code therefore checks object lifetime before touching a layout.
    """
    if obj is None:
        return False
    if _sip is None:
        return True
    try:
        return not bool(_sip.isdeleted(obj))
    except (TypeError, RuntimeError):
        return False


def _custom_items(layout):
    """Return live plugin items in *layout*, without relying on registry state."""
    if not _is_live_qt_object(layout):
        return []
    try:
        scene_items = layout.items()
    except (RuntimeError, TypeError):
        scene_items = []
    return [
        item for item in scene_items
        if isinstance(item, (LayoutItemPolygonText, LayoutItemSplineText))
    ]


def _item_kind(item):
    if isinstance(item, LayoutItemPolygonText):
        return "polygon"
    if isinstance(item, LayoutItemSplineText):
        return "spline"
    return ""


def _item_uuid(item):
    try:
        return str(item.uuid())
    except Exception:
        return ""


def _serialize_full_item(item):
    """Serialize the complete QgsLayoutItem XML, including base item geometry."""
    doc = QDomDocument("CurvedPolygonTextRecovery")
    root = doc.createElement("RecoveryItem")
    doc.appendChild(root)
    try:
        ok = item.writeXml(root, doc, QgsReadWriteContext())
        if ok is False:
            return ""
        return doc.toString(-1)
    except Exception:
        record_suppressed_exception()
        return ""


def _clear_item_write_snapshot_coalescing():
    """Clear the per-event serialization coalescing state."""
    global _ITEM_WRITE_SNAPSHOT_CLEAR_SCHEDULED
    _ITEM_WRITE_SNAPSHOT_IDS.clear()
    _ITEM_WRITE_SNAPSHOT_CLEAR_SCHEDULED = False


def _mark_item_write_snapshot(layout):
    """Coalesce duplicate snapshots caused by one layout serialization."""
    global _ITEM_WRITE_SNAPSHOT_CLEAR_SCHEDULED
    key = id(layout)
    if key in _ITEM_WRITE_SNAPSHOT_IDS:
        return False
    _ITEM_WRITE_SNAPSHOT_IDS.add(key)
    if not _ITEM_WRITE_SNAPSHOT_CLEAR_SCHEDULED:
        _ITEM_WRITE_SNAPSHOT_CLEAR_SCHEDULED = True
        QTimer.singleShot(0, _clear_item_write_snapshot_coalescing)
    return True


def snapshot_layout(layout):
    """Refresh the durable manifest/recovery copy for one live layout.

    This function intentionally remains immediate for normal recovery calls.
    Only item-serialization requests are coalesced by ``snapshot_item_layout``
    below, so edits and lifecycle events cannot be accidentally suppressed.

    The re-entrancy guard prevents the nested item.writeXml() calls used to
    construct recovery records from recursing.
    """
    if not _is_live_qt_object(layout):
        return
    key = id(layout)
    if key in _SNAPSHOT_LAYOUT_IDS:
        return
    _SNAPSHOT_LAYOUT_IDS.add(key)
    try:
        records = []
        for item in _custom_items(layout):
            xml = _serialize_full_item(item)
            if not xml:
                continue
            records.append({
                "uuid": _item_uuid(item),
                "kind": _item_kind(item),
                "xml": xml,
            })

        payload = {
            "schema": RECOVERY_SCHEMA,
            "items": records,
        }
        try:
            layout.setCustomProperty(
                RECOVERY_PROPERTY,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )
        except Exception:
            record_suppressed_exception()
    finally:
        _SNAPSHOT_LAYOUT_IDS.discard(key)


def snapshot_layout_by_name(layout_name):
    """Resolve a layout afresh and snapshot it only if it is still alive.

    This deliberately accepts a plain string instead of a QgsLayout wrapper so
    delayed callbacks never retain a C++ scene object beyond its lifetime.
    """
    if not layout_name:
        return
    try:
        manager = QgsProject.instance().layoutManager()
        layout = manager.layoutByName(str(layout_name)) if manager is not None else None
    except (RuntimeError, TypeError):
        return
    if not _is_live_qt_object(layout):
        return
    snapshot_layout(layout)


def schedule_layout_snapshot(layout_name):
    """Queue a deletion-manifest refresh without retaining a layout wrapper.

    Multiple item destructions can occur as one QGIS operation.  Keep at most
    one pending callback per layout name so a batch deletion causes one
    recovery rebuild rather than one rebuild per destroyed item.
    """
    name = str(layout_name or "")
    if not name or name in _SCHEDULED_LAYOUT_NAMES:
        return
    _SCHEDULED_LAYOUT_NAMES.add(name)

    def _run(name=name):
        _SCHEDULED_LAYOUT_NAMES.discard(name)
        snapshot_layout_by_name(name)

    QTimer.singleShot(0, _run)


def snapshot_item_layout(item):
    """Refresh the owning layout, coalescing duplicate item-write requests."""
    try:
        layout = item.layout()
    except Exception:
        layout = None
    if not _is_live_qt_object(layout):
        return
    # QGIS may invoke writePropertiesToElement() once per custom item while
    # serializing one layout. Each call used to rebuild the complete manifest,
    # producing O(n^2) work. One snapshot is sufficient for that serialization
    # burst because the project-level write checkpoint also performs an
    # authoritative full snapshot before the project is written.
    if not _mark_item_write_snapshot(layout):
        return
    snapshot_layout(layout)


def snapshot_project(project):
    """Refresh all layout backups at an authoritative project checkpoint."""
    if project is None:
        return
    try:
        manager = project.layoutManager()
        layouts = manager.layouts() if manager is not None else []
    except Exception:
        layouts = []
    for layout in layouts:
        snapshot_layout(layout)


def _parse_payload(layout):
    if not _is_live_qt_object(layout):
        return None
    try:
        raw = layout.customProperty(RECOVERY_PROPERTY, "")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        raw = str(raw)
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        record_suppressed_exception()
        return None
    if not isinstance(data, dict) or data.get("schema") != RECOVERY_SCHEMA:
        return None
    items = data.get("items", [])
    return items if isinstance(items, list) else None


def _first_item_element(doc):
    root = doc.documentElement()
    if root.isNull():
        return root
    child = root.firstChildElement()
    return child


def _restore_record(layout, record):
    xml = record.get("xml", "")
    if not xml:
        return None

    doc = QDomDocument()
    try:
        result = doc.setContent(str(xml))
        if isinstance(result, tuple) and result and not bool(result[0]):
            return None
        if result is False:
            return None
    except Exception:
        record_suppressed_exception()
        return None

    root = doc.documentElement()
    if root.isNull():
        return None

    # Use QGIS' own item restoration path. It consults the registered layout
    # item metadata, restores base geometry/UUID/z-order and invokes the custom
    # readPropertiesFromElement() implementation exactly as normal project load
    # does. This is safer than reconstructing those base properties ourselves.
    try:
        added = layout.addItemsFromXml(
            root, doc, QgsReadWriteContext(), None, False
        )
    except TypeError:
        try:
            added = layout.addItemsFromXml(root, doc, QgsReadWriteContext())
        except Exception:
            record_suppressed_exception()
            return None
    except Exception:
        record_suppressed_exception()
        return None

    if not added:
        return None
    item = added[0]
    try:
        item.update()
    except Exception:
        record_suppressed_exception()
    return item


def recover_layout(layout):
    """Restore backed-up items missing from a loaded layout.

    Returns the number of items recreated. Existing live UUIDs are never
    duplicated. An empty persisted manifest is authoritative and therefore
    restores nothing (important for intentional deletions made while the plugin
    was active and subsequently saved).
    """
    if not _is_live_qt_object(layout):
        return 0
    records = _parse_payload(layout)
    if records is None:
        return 0

    live_ids = {_item_uuid(item) for item in _custom_items(layout)}
    restored = 0

    for record in records:
        if not isinstance(record, dict):
            continue
        saved_uuid = str(record.get("uuid", ""))
        if saved_uuid and saved_uuid in live_ids:
            continue
        item = _restore_record(layout, record)
        if item is None:
            continue
        restored += 1
        current_uuid = _item_uuid(item)
        if current_uuid:
            live_ids.add(current_uuid)
        if saved_uuid:
            live_ids.add(saved_uuid)

    if restored:
        try:
            layout.refresh()
        except Exception:
            record_suppressed_exception()
    return restored


def recover_project(project):
    """Restore missing custom items in every loaded print layout."""
    if project is None:
        return 0
    try:
        manager = project.layoutManager()
        layouts = manager.layouts() if manager is not None else []
    except Exception:
        layouts = []
    restored = 0
    for layout in layouts:
        restored += recover_layout(layout)
    return restored


def schedule_recovery(project, callback=None):
    """Run after QGIS has finished deserializing the project/layouts."""
    def _run():
        count = recover_project(project)
        if callback is not None:
            callback(count)
    try:
        QTimer.singleShot(0, _run)
    except Exception:
        _run()
