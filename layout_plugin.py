# -*- coding: utf-8 -*-
"""QGIS plugin entry point for Curved & Polygon Text layout items.

The item implementations and layout view tools live in their own modules.  This
module is responsible for the QGIS lifecycle only:

* register the two custom layout item types and their property panels;
* add three actions to the lower QGIS Layout Designer action toolbar;
* remove those actions cleanly when the plugin is disabled.
"""

from __future__ import annotations

import os
import sys
import traceback

from qgis.core import (
    QgsApplication,
    QgsLayoutItemAbstractMetadata,
    QgsMessageLog,
    Qgis,
)
from qgis.gui import (
    QgsGui,
    QgsLayoutDesignerInterface,
    QgsLayoutItemAbstractGuiMetadata,
)

from qgis.PyQt.QtCore import QObject, QEvent, QTimer
from qgis.PyQt.QtGui import QIcon

try:
    # Qt 6 / PyQt 6 location.
    from qgis.PyQt.QtGui import QAction
except ImportError:  # pragma: no cover - Qt 5 / PyQt 5 location.
    from qgis.PyQt.QtWidgets import QAction

from qgis.PyQt.QtWidgets import QApplication, QToolBar, QWidget

from .gui.gui_widgets import PolygonTextPropertiesWidget, SplineTextPropertiesWidget
from .item_creation_tool import NodeItemCreationTool
from .keep_alive import keep_alive
from .reliability import record_suppressed_exception
from .layout_item_polygon_text import LayoutItemPolygonText, POLYGON_TEXT_ITEM_TYPE
from .layout_item_spline_text import LayoutItemSplineText, SPLINE_TEXT_ITEM_TYPE
from .node_edit_tool import NodeEditTool
from .icons import edit_spline_icon, polygon_icon, spline_icon


LOG_TAG = "Curved/Polygon Text"
NODE_GROUP_ID = "nodes"  # QGIS core's existing node-item group.

ACTION_DEFINITIONS = (
    (
        "curved_polygon_text_add_spline",
        "Add Curved Spline Text",
        "resources/spline.png",
        "Click each point of the curve, then double-click, right-click, "
        "or press Enter to finish. Escape cancels.",
        "spline",
    ),
    (
        "curved_polygon_text_add_polygon",
        "Add Polygon Shaped Text Box",
        "resources/polygon.png",
        "Click each corner of the polygon, then double-click, right-click, "
        "or press Enter to finish. Escape cancels.",
        "polygon",
    ),
    (
        "curved_polygon_text_edit_nodes",
        "Edit Curve / Polygon Nodes",
        "resources/edit-spline.png",
        "Drag the nodes of a selected Curved Spline Text or Polygon Shaped "
        "Text Box item. Double-click the outline to add a node; right-click "
        "a node to remove it.",
        "edit",
    ),
)
ACTION_OBJECT_NAMES = tuple(definition[0] for definition in ACTION_DEFINITIONS)


def _qevent_type(name):
    """Return a QEvent enum value in both Qt 5 and Qt 6 bindings."""
    if hasattr(QEvent, name):
        return getattr(QEvent, name)
    return getattr(QEvent.Type, name)


_EVENT_SHOW = _qevent_type("Show")
_EVENT_WINDOW_ACTIVATE = _qevent_type("WindowActivate")


def _no_creation_tools_flag():
    """Resolve QgsLayoutItemAbstractGuiMetadata.FlagNoCreationTools safely."""
    try:
        value = getattr(QgsLayoutItemAbstractGuiMetadata, "FlagNoCreationTools", None)
        if value is not None and not isinstance(value, type):
            return value

        flag_class = getattr(QgsLayoutItemAbstractGuiMetadata, "Flag", None)
        if flag_class is not None:
            value = getattr(flag_class, "FlagNoCreationTools", None)
            if value is not None:
                return value
    except Exception:
        record_suppressed_exception()

    # Safe fallback: the item may appear in QGIS' item toolbox, but startup will
    # not fail on a changed enum location.
    return 0


# ---- Custom layout item metadata ---------------------------------------------


class _SplineTextItemMetadata(QgsLayoutItemAbstractMetadata):
    def __init__(self):
        super().__init__(SPLINE_TEXT_ITEM_TYPE, "Spline Text")

    def createItem(self, layout):
        return keep_alive(LayoutItemSplineText(layout))


class _PolygonTextItemMetadata(QgsLayoutItemAbstractMetadata):
    def __init__(self):
        super().__init__(POLYGON_TEXT_ITEM_TYPE, "Polygon Text")

    def createItem(self, layout):
        return keep_alive(LayoutItemPolygonText(layout))


class _SplineTextGuiMetadata(QgsLayoutItemAbstractGuiMetadata):
    def __init__(self):
        super().__init__(
            SPLINE_TEXT_ITEM_TYPE,
            "Spline Text",
            NODE_GROUP_ID,
            False,
            _no_creation_tools_flag(),
        )

    def createItem(self, layout):
        return keep_alive(LayoutItemSplineText(layout))

    def createItemWidget(self, item):
        return SplineTextPropertiesWidget(None, item)

    def creationIcon(self):
        return spline_icon()


class _PolygonTextGuiMetadata(QgsLayoutItemAbstractGuiMetadata):
    def __init__(self):
        super().__init__(
            POLYGON_TEXT_ITEM_TYPE,
            "Polygon Text",
            NODE_GROUP_ID,
            False,
            _no_creation_tools_flag(),
        )

    def createItem(self, layout):
        return keep_alive(LayoutItemPolygonText(layout))

    def createItemWidget(self, item):
        return PolygonTextPropertiesWidget(None, item)

    def creationIcon(self):
        return polygon_icon()


# ---- Plugin implementation ----------------------------------------------------


class CurvedPolygonTextPlugin(QObject):
    """QGIS plugin implementation."""

    def __init__(self, iface):
        super().__init__()
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        icon_factories = {
            "spline": spline_icon,
            "polygon": polygon_icon,
            "edit": edit_spline_icon,
        }
        self.icons = {
            object_name: icon_factories[tool_kind]()
            for object_name, _text, _icon_file, _whats_this, tool_kind
            in ACTION_DEFINITIONS
        }

        self._records = {}
        self._designer_signal = None
        self._designer_closing_signal = None
        self._scan_scheduled = False
        self._event_filter_installed = False

        self._item_metadata = []
        self._gui_metadata = []
        self._loaded = False

    # ---- QGIS plugin lifecycle -------------------------------------------------

    def initGui(self):
        """Called by QGIS when the plugin is enabled."""
        self._loaded = True
        self._register_layout_items()
        self._connect_layout_designer_signals()
        self._install_event_filter()
        self._schedule_scan()

    def unload(self):
        """Called by QGIS when the plugin is disabled or unloaded."""
        self._loaded = False
        self._disconnect_layout_designer_signals()
        self._remove_event_filter()
        self._remove_all_actions()
        self._unregister_layout_items()

    # ---- Layout item registration ---------------------------------------------

    def _register_layout_items(self):
        core_registry = QgsApplication.layoutItemRegistry()
        self._item_metadata = [_SplineTextItemMetadata(), _PolygonTextItemMetadata()]

        for metadata in self._item_metadata:
            try:
                core_registry.addLayoutItemType(metadata)
            except Exception as exc:  # noqa: BLE001
                self._log(
                    f"Could not register layout item type {metadata.type()}: {exc}",
                    Qgis.MessageLevel.Warning,
                    exc_info=True,
                )

        gui_registry = QgsGui.layoutItemGuiRegistry()
        self._gui_metadata = [_SplineTextGuiMetadata(), _PolygonTextGuiMetadata()]

        for metadata in self._gui_metadata:
            try:
                gui_registry.addLayoutItemGuiMetadata(metadata)
            except Exception as exc:  # noqa: BLE001
                self._log(
                    f"Could not register layout item GUI metadata {metadata.type()}: {exc}",
                    Qgis.MessageLevel.Warning,
                    exc_info=True,
                )

    def _unregister_layout_items(self):
        gui_registry = QgsGui.layoutItemGuiRegistry()
        for metadata in self._gui_metadata:
            try:
                gui_registry.removeLayoutItemGuiMetadata(metadata)
            except Exception:
                record_suppressed_exception()
        self._gui_metadata = []

        core_registry = QgsApplication.layoutItemRegistry()
        for metadata in self._item_metadata:
            try:
                # Available in newer QGIS builds. On older 3.x builds this may
                # not exist; leaving the core type registered until QGIS exits is
                # preferable to raising during plugin unload.
                core_registry.removeLayoutItemType(metadata)
            except Exception:
                record_suppressed_exception()
        self._item_metadata = []

    # ---- Signal/event wiring ---------------------------------------------------

    def _connect_layout_designer_signals(self):
        opened_signal = getattr(self.iface, "layoutDesignerOpened", None)
        if opened_signal is not None:
            try:
                opened_signal.connect(self._on_layout_designer_opened)
                self._designer_signal = opened_signal
            except Exception:
                self._designer_signal = None

        closing_signal = getattr(self.iface, "layoutDesignerWillBeClosed", None)
        if closing_signal is not None:
            try:
                closing_signal.connect(self._on_layout_designer_closing)
                self._designer_closing_signal = closing_signal
            except Exception:
                self._designer_closing_signal = None

    def _disconnect_layout_designer_signals(self):
        if self._designer_signal is not None:
            try:
                self._designer_signal.disconnect(self._on_layout_designer_opened)
            except Exception:
                record_suppressed_exception()
        self._designer_signal = None

        if self._designer_closing_signal is not None:
            try:
                self._designer_closing_signal.disconnect(self._on_layout_designer_closing)
            except Exception:
                record_suppressed_exception()
        self._designer_closing_signal = None

    def _install_event_filter(self):
        """Watch for Layout Designer windows as a fallback.

        This covers already-open Layout Designer windows, project changes, and
        Qt/QGIS signal timing differences without polling.
        """
        # On macOS, application-wide Python event filters can crash inside
        # SIP's QObject subclass conversion while unrelated dialogs (notably
        # Plugin Manager) dispatch events. Layout Designer discovery does not
        # require this fallback: QgisInterface.layoutDesignerOpened handles
        # new designers and the scheduled openLayoutDesigners scan handles
        # designers which were already open when the plugin was enabled.
        if sys.platform == "darwin":
            return

        app = QApplication.instance()
        if app is None or self._event_filter_installed:
            return

        try:
            app.installEventFilter(self)
            self._event_filter_installed = True
        except Exception:
            self._event_filter_installed = False

    def _remove_event_filter(self):
        app = QApplication.instance()
        if app is None or not self._event_filter_installed:
            return

        try:
            app.removeEventFilter(self)
        except Exception:
            record_suppressed_exception()
        self._event_filter_installed = False

    def eventFilter(self, obj, event):  # noqa: N802 - Qt method name
        """Schedule a scan when Layout Designer windows are shown/activated."""
        if not self._loaded:
            return False

        try:
            event_type = event.type()
        except Exception:
            return False

        if event_type in (_EVENT_SHOW, _EVENT_WINDOW_ACTIVATE):
            # QApplication sends these events for every dialog and child
            # widget.  Only a top-level Layout Designer window can require
            # toolbar installation, so ignore unrelated UI activity.
            window = obj if isinstance(obj, QWidget) else None
            try:
                if window is not None and not window.isWindow():
                    window = None
            except Exception:
                window = None
            if self._looks_like_layout_designer_window(window):
                self._schedule_scan()

        return False

    def _schedule_scan(self):
        if self._scan_scheduled:
            return

        self._scan_scheduled = True
        QTimer.singleShot(0, self._run_scheduled_scan)

    def _run_scheduled_scan(self):
        self._scan_scheduled = False
        if self._loaded:
            self._install_on_existing_layout_designers()

    # ---- Main behavior ---------------------------------------------------------

    def _on_layout_designer_opened(self, designer):
        """Slot for iface.layoutDesignerOpened."""
        # Let QGIS finish constructing the Layout Designer window/toolbars.
        QTimer.singleShot(0, lambda: self._install_on_designer(designer))

    def _on_layout_designer_closing(self, designer):
        """Remove records for a Layout Designer that is closing."""
        for key, record in list(self._records.items()):
            if self._same_designer(record.get("designer"), designer):
                self._remove_record(key)

    def _install_on_existing_layout_designers(self):
        """Install the toolbar buttons on every open Layout Designer."""
        # Prefer QGIS' public designer interfaces when available. This gives the
        # actions a direct path to designer.view() for tool activation.
        getter = getattr(self.iface, "openLayoutDesigners", None)
        if callable(getter):
            try:
                for designer in list(getter() or []):
                    self._install_on_designer(designer)
            except Exception:
                record_suppressed_exception()

        app = QApplication.instance()
        if app is None:
            return

        try:
            top_level_widgets = app.topLevelWidgets()
        except Exception:
            return

        # Scan top-level windows that look like Layout Designer windows and
        # add the same actions there.
        for widget in top_level_widgets:
            if self._looks_like_layout_designer_window(widget):
                self._install_on_window(widget)

    def _install_on_designer(self, designer):
        """Install on a QgsLayoutDesignerInterface instance."""
        if not self._loaded or designer is None:
            return

        window = self._designer_window(designer)
        key = self._record_key(designer, window)
        if key in self._records:
            self._update_record_designer(key, designer)
            return

        toolbars = self._designer_layout_toolbars(designer, window)
        if toolbars:
            self._install_actions(
                target=designer,
                window=window,
                designer=designer,
                toolbars=toolbars,
            )
            return

        # Last-resort public API fallback: some QgsLayoutDesignerInterface
        # versions expose addAction/removeAction.
        add_action = getattr(designer, "addAction", None)
        if callable(add_action):
            parent = window if window is not None else self._safe_main_window()
            actions, entries = self._new_actions(parent, key, designer, window)
            added_actions = []

            for action in actions:
                try:
                    add_action(action)
                    added_actions.append(action)
                except Exception:
                    self._safe_delete_action(action)

            if added_actions:
                self._store_record(
                    key=key,
                    target=designer,
                    window=window,
                    designer=designer,
                    actions=added_actions,
                    entries=entries,
                    toolbars=[],
                    added_to_designer=True,
                )

    def _install_on_window(self, window):
        """Install on a Layout Designer top-level QWidget."""
        if not self._loaded or window is None:
            return

        designer = self._designer_from_window(window)
        key = self._record_key(designer if designer is not None else window, window)
        if key in self._records:
            if designer is not None:
                self._update_record_designer(key, designer)
            return

        toolbars = self._window_layout_toolbars(window)
        if not toolbars:
            return

        self._install_actions(
            target=designer if designer is not None else window,
            window=window,
            designer=designer,
            toolbars=toolbars,
        )

    def _install_actions(self, target, window, designer, toolbars):
        """Create QActions and add them to the target Layout toolbar(s)."""
        if not self._loaded:
            return

        key = self._record_key(target, window)
        if key in self._records:
            self._update_record_designer(key, designer)
            return

        parent = window if window is not None else self._safe_main_window()
        actions, entries = self._new_actions(parent, key, designer, window)

        added_toolbars = []
        for toolbar in toolbars:
            if toolbar is None:
                continue

            self._remove_orphaned_plugin_actions(toolbar)

            toolbar_had_action_added = False
            for action in actions:
                if self._toolbar_has_action(toolbar, action.objectName()):
                    continue

                try:
                    toolbar.addAction(action)
                    toolbar_had_action_added = True
                except Exception:
                    record_suppressed_exception()

            if toolbar_had_action_added:
                added_toolbars.append(toolbar)

        if added_toolbars:
            self._store_record(
                key=key,
                target=target,
                window=window,
                designer=designer,
                actions=actions,
                entries=entries,
                toolbars=added_toolbars,
                added_to_designer=False,
            )
        else:
            for action in actions:
                self._safe_delete_action(action)

    def _new_actions(self, parent, record_key, designer, window):
        actions = []
        entries = {}

        for object_name, text, _icon_file, whats_this, tool_kind in ACTION_DEFINITIONS:
            icon = self.icons.get(object_name, QIcon())
            action = QAction(icon, text, parent)
            action.setObjectName(object_name)
            action.setCheckable(True)
            if tool_kind == "edit":
                action.setToolTip(
                    f"<b>{text}</b><br>"
                    "<i>Use this tool to drag curve or polygon nodes, "
                    "double-click the outline to add a point, or right-click "
                    "a point to remove it.</i>"
                )
            elif tool_kind == "spline":
                action.setToolTip(
                    f"<b>{text}</b><br>"
                    "<i>Use the \u201cEdit Curve / Polygon Nodes\u201d toolbar tool "
                    "to drag the curve, double-click it to add a point, or "
                    "right-click a point to remove it.</i>"
                )
            elif tool_kind == "polygon":
                action.setToolTip(
                    f"<b>{text}</b><br>"
                    "<i>Use the \u201cEdit Curve / Polygon Nodes\u201d toolbar tool "
                    "to drag the polygon, double-click it to add a point, or "
                    "right-click a point to remove it.</i>"
                )
            else:
                action.setToolTip(text)
            action.setStatusTip(text)
            action.setWhatsThis(whats_this)

            entry = {
                "object_name": object_name,
                "text": text,
                "tool_kind": tool_kind,
                "action": action,
                "tool": None,
                "view": None,
                "sync_fn": None,
                "designer": designer,
                "window": window,
            }
            entries[object_name] = entry

            action.toggled.connect(
                lambda checked, key=record_key, name=object_name:
                    self._on_action_toggled(key, name, checked)
            )

            actions.append(action)

        return actions, entries

    def _on_action_toggled(self, record_key, object_name, checked):
        if not checked:
            return

        record = self._records.get(record_key)
        if record is None:
            return

        entry = record.get("entries", {}).get(object_name)
        if entry is None:
            return

        self._activate_entry(record_key, record, entry)

    # ---- Tool activation -------------------------------------------------------

    def _activate_entry(self, record_key, record, entry):
        tool, view = self._ensure_tool(record_key, record, entry)
        action = entry.get("action")

        if tool is None or view is None:
            self._safe_set_checked(action, False)
            return

        try:
            view.setTool(tool)
        except Exception as exc:  # noqa: BLE001
            self._log(
                f"Failed to activate layout tool '{entry.get('text', 'unknown')}': {exc}",
                Qgis.MessageLevel.Critical,
                exc_info=True,
            )
            self._safe_set_checked(action, False)

    def _ensure_tool(self, record_key, record, entry):
        tool = entry.get("tool")
        view = entry.get("view")
        if tool is not None and view is not None:
            return tool, view

        designer = self._record_designer(record)
        view = self._designer_view(designer)
        if view is None:
            view = self._window_layout_view(record.get("window"))

        if view is None:
            self._log(
                f"Layout Designer view is not ready for '{entry.get('text', 'unknown')}'. "
                "The toolbar button remains available; try clicking it again.",
                Qgis.MessageLevel.Warning,
            )
            return None, None

        try:
            tool = self._create_tool(entry["tool_kind"], view, record_key)
        except Exception as exc:  # noqa: BLE001
            self._log(
                f"Failed to create layout tool '{entry.get('text', 'unknown')}': {exc}",
                Qgis.MessageLevel.Critical,
                exc_info=True,
            )
            return None, None

        action = entry.get("action")

        def _sync_checked(active_tool, a=action, t=tool):
            self._safe_set_checked(a, active_tool is t)

        sync_fn = None
        try:
            view.toolSet.connect(_sync_checked)
            sync_fn = _sync_checked
        except Exception:
            sync_fn = None

        entry["tool"] = tool
        entry["view"] = view
        entry["sync_fn"] = sync_fn
        entry["designer"] = designer
        return tool, view

    def _create_tool(self, tool_kind, view, record_key):
        if tool_kind == "spline":
            return NodeItemCreationTool(
                view,
                "Add Curved Spline Text",
                LayoutItemSplineText,
                min_nodes=2,
                closed=False,
                on_finished=lambda _view, key=record_key: self._activate_select_tool_for_record(key),
                on_item_created=keep_alive,
            )

        if tool_kind == "polygon":
            return NodeItemCreationTool(
                view,
                "Add Polygon Shaped Text Box",
                LayoutItemPolygonText,
                min_nodes=3,
                closed=True,
                on_finished=lambda _view, key=record_key: self._activate_select_tool_for_record(key),
                on_item_created=keep_alive,
            )

        return NodeEditTool(view)

    def _activate_select_tool_for_record(self, record_key):
        """Return to the native Select/Move Item tool after creating an item.

        QGIS may finish its own creation-tool cleanup after our custom tool's
        _finish() returns, so the select-tool handoff is applied immediately and
        then repeated on the next event-loop turns.  This prevents the Layout
        Designer from settling on the neighbouring "Move Item Content" tool.
        """
        self._activate_select_tool_for_record_now(record_key)
        for delay in (0, 50, 150):
            try:
                QTimer.singleShot(
                    delay,
                    lambda key=record_key: self._activate_select_tool_for_record_now(key),
                )
            except Exception:
                record_suppressed_exception()

    def _activate_select_tool_for_record_now(self, record_key):
        record = self._records.get(record_key)
        if record is None:
            return
        if self._native_select_move_action_active(
                self._record_designer(record), record):
            return
        self._activate_select_tool(self._record_designer(record), record)

    def _native_select_move_action_active(self, designer, record=None):
        """Return True when QGIS' native Select/Move Item action is active."""
        cached_action = record.get("native_select_action") if record else None
        if cached_action is not None:
            try:
                return bool(
                    cached_action.isCheckable() and cached_action.isChecked())
            except Exception:
                if record is not None:
                    record.pop("native_select_action", None)
        for action in self._native_select_move_actions(designer, record):
            try:
                if action.isCheckable() and action.isChecked():
                    if record is not None:
                        record["native_select_action"] = action
                    return True
            except Exception:
                record_suppressed_exception()
        return False

    def _activate_select_tool(self, designer, record=None):
        """Switch back to QGIS' standard Select/Move Item tool after sketching."""
        if designer is None and record is None:
            return

        # First prefer the actual QAction already owned by the Layout Designer.
        # It is the safest route because it matches the exact tool button QGIS
        # shows in this installation.  We explicitly avoid any action mentioning
        # "content", which is the adjacent Move Item Content tool.
        if self._trigger_native_select_move_action(designer, record):
            return

        # Fallback to the public designer enum API.  The content-move enum is
        # intentionally not used because it activates the neighbouring tool.
        candidate_enums = []
        standard_tool = None
        try:
            standard_tool = getattr(QgsLayoutDesignerInterface, "StandardTool", None)
        except Exception:
            standard_tool = None

        def _add_tool_candidate(name):
            try:
                candidate_enums.append(getattr(QgsLayoutDesignerInterface, name, None))
            except Exception:
                record_suppressed_exception()
            if standard_tool is not None:
                try:
                    candidate_enums.append(getattr(standard_tool, name, None))
                except Exception:
                    record_suppressed_exception()

        for attr_name in (
            "ToolSelect",
            "ToolSelectItem",
            "ToolSelectItems",
            "ToolSelectMoveItem",
            "ToolSelectMoveItems",
            "ToolMoveItem",
            "ToolMoveItems",
        ):
            _add_tool_candidate(attr_name)

        activate_tool = getattr(designer, "activateTool", None)
        if not callable(activate_tool):
            return

        seen = set()
        for tool_enum in candidate_enums:
            if tool_enum is None:
                continue
            marker = repr(tool_enum)
            if marker in seen:
                continue
            seen.add(marker)
            if "content" in marker.lower():
                continue
            try:
                activate_tool(tool_enum)
                return
            except Exception:
                record_suppressed_exception()

    def _trigger_native_select_move_action(self, designer, record=None):
        """Trigger the Layout Designer QAction for Select/Move Item if found."""
        for action in self._native_select_move_actions(designer, record):
            try:
                action.trigger()
                if record is not None:
                    record["native_select_action"] = action
                return True
            except Exception:
                record_suppressed_exception()
        return False

    def _native_select_move_actions(self, designer, record=None):
        """Yield unique native Select/Move Item actions for a designer."""
        actions = []
        for window in self._candidate_designer_windows(designer, record):
            try:
                actions.extend(window.findChildren(QAction))
            except Exception:
                record_suppressed_exception()
            try:
                for toolbar in window.findChildren(QToolBar):
                    try:
                        actions.extend(toolbar.actions())
                    except Exception:
                        record_suppressed_exception()
            except Exception:
                record_suppressed_exception()

        seen = set()
        for action in actions:
            if action is None:
                continue
            key = id(action)
            if key in seen:
                continue
            seen.add(key)

            if self._looks_like_native_select_move_action(action):
                yield action

    def _candidate_designer_windows(self, designer, record=None):
        windows = []
        if record is not None:
            window = record.get("window")
            if isinstance(window, QWidget):
                windows.append(window)
            target = record.get("target")
            if isinstance(target, QWidget):
                windows.append(target)

        window = self._designer_window(designer)
        if isinstance(window, QWidget):
            windows.append(window)

        try:
            app = QApplication.instance()
            if app is not None:
                active = app.activeWindow()
                if isinstance(active, QWidget):
                    windows.append(active)
        except Exception:
            record_suppressed_exception()

        return self._unique_widgets(windows)

    def _looks_like_native_select_move_action(self, action):
        text = " ".join(
            [
                self._safe_widget_text(action, "objectName"),
                self._safe_widget_text(action, "text"),
                self._safe_widget_text(action, "toolTip"),
                self._safe_widget_text(action, "statusTip"),
                self._safe_widget_text(action, "whatsThis"),
            ]
        ).lower()

        compact = "".join(ch for ch in text if ch.isalnum())
        if not text:
            return False

        # Never trigger the custom plugin actions or the neighbouring content
        # tool.  The requested target is the normal Select/Move Item arrow.
        plugin_action_names = {
            "".join(ch for ch in name.lower() if ch.isalnum())
            for name in ACTION_OBJECT_NAMES
        }
        if any(name in compact for name in plugin_action_names):
            return False
        if "content" in text or "node" in text or "nodes" in text:
            return False

        return (
            ("select" in text and "item" in text)
            or ("select" in text and "move" in text)
            or "selectmoveitem" in compact
            or "selectmoveitems" in compact
        )

    # ---- Finding Layout Designer windows/toolbars -----------------------------

    def _designer_window(self, designer):
        for attr_name in ("window", "parentWidget"):
            method = getattr(designer, attr_name, None)
            if callable(method):
                try:
                    widget = method()
                    if isinstance(widget, QWidget):
                        return widget
                except Exception:
                    record_suppressed_exception()

        if isinstance(designer, QWidget):
            return designer

        return None

    def _designer_layout_toolbars(self, designer, window):
        """Find the lower Layout Designer action toolbar first."""
        toolbars = self._designer_action_toolbars(designer)
        if toolbars:
            return toolbars

        if window is not None:
            toolbars = self._window_layout_toolbars(window)
            if toolbars:
                return toolbars

        # Final compatibility fallback for unusual QGIS builds which expose a
        # toolbar from the designer interface but whose QWidget tree cannot be
        # inspected. This may land on the upper toolbar, but it keeps the plugin
        # usable instead of failing silently.
        return self._designer_legacy_layout_toolbars(designer)

    def _designer_action_toolbars(self, designer):
        """Return action-toolbar objects exposed directly by the designer API."""
        toolbars = []

        for method_name in (
            "actionToolbar",
            "actionToolBar",
            "actionsToolbar",
            "actionsToolBar",
            "layoutActionToolbar",
            "layoutActionToolBar",
            "layoutActionsToolbar",
            "layoutActionsToolBar",
        ):
            method = getattr(designer, method_name, None)
            if not callable(method):
                continue

            try:
                toolbar = method()
                if isinstance(toolbar, QToolBar):
                    toolbars.append(toolbar)
            except Exception:
                record_suppressed_exception()

        return self._unique_widgets(toolbars)

    def _designer_legacy_layout_toolbars(self, designer):
        """Return the upper layout toolbar as a last-resort compatibility path."""
        toolbars = []

        for method_name in (
            "layoutToolbar",
            "layoutToolBar",
            "layoutToolsToolbar",
            "layoutToolsToolBar",
        ):
            method = getattr(designer, method_name, None)
            if not callable(method):
                continue

            try:
                toolbar = method()
                if isinstance(toolbar, QToolBar):
                    toolbars.append(toolbar)
            except Exception:
                record_suppressed_exception()

        return self._unique_widgets(toolbars)

    def _window_layout_toolbars(self, window):
        """Return the preferred lower toolbar for a Layout Designer window.

        Preference order:

        1. a named "Actions" toolbar, excluding Atlas/Report toolbars;
        2. the right-most toolbar on the second horizontal toolbar row;
        3. a compatible layout-toolbar search as a fallback for changed QGIS
           builds.
        """
        try:
            toolbars = list(window.findChildren(QToolBar))
        except Exception:
            return []

        if not toolbars:
            return []

        usable = self._usable_horizontal_toolbars(window, toolbars)

        action_toolbars = [
            toolbar for toolbar in usable
            if self._looks_like_action_toolbar(toolbar)
        ]
        if action_toolbars:
            second_row = self._second_toolbar_row(usable, window)
            action_toolbars_on_second_row = [
                toolbar for toolbar in action_toolbars
                if toolbar in second_row
            ]
            if action_toolbars_on_second_row:
                return [self._rightmost_toolbar(action_toolbars_on_second_row, window)]
            return [self._rightmost_toolbar(action_toolbars, window)]

        second_row = self._second_toolbar_row(usable, window)
        if second_row:
            return [self._rightmost_toolbar(second_row, window)]

        # Compatibility fallback. Keep this last so a normal QGIS Layout
        # Designer chooses the lower/action toolbar first.
        preferred = []
        secondary = []

        for toolbar in usable or toolbars:
            text = self._toolbar_search_text(toolbar)

            if "layout" in text and "atlas" not in text and "report" not in text:
                preferred.append(toolbar)
            elif "toolbar" in text or "tool bar" in text:
                secondary.append(toolbar)

        if preferred:
            return [self._rightmost_toolbar(preferred, window)]

        if secondary:
            return [secondary[0]]

        return [toolbars[0]]

    def _usable_horizontal_toolbars(self, window, toolbars):
        """Return toolbar candidates suitable for adding layout-edit actions."""
        visible = []
        hidden_or_not_ready = []

        for toolbar in toolbars:
            if not isinstance(toolbar, QToolBar):
                continue

            text = self._toolbar_search_text(toolbar)
            if self._excluded_layout_toolbar_text(text):
                continue

            if not self._toolbar_is_probably_horizontal(toolbar):
                continue

            if self._toolbar_is_visible(toolbar, window):
                visible.append(toolbar)
            else:
                hidden_or_not_ready.append(toolbar)

        # Prefer currently visible toolbars, but do not fail during early
        # Layout Designer construction when Qt may not yet report visibility.
        return self._unique_widgets(visible or hidden_or_not_ready)

    @staticmethod
    def _excluded_layout_toolbar_text(text):
        compact = "".join(ch for ch in text if ch.isalnum())
        return (
            "atlas" in text
            or "report" in text
            or "atlastoolbar" in compact
            or "reporttoolbar" in compact
        )

    def _looks_like_action_toolbar(self, toolbar):
        text = self._toolbar_search_text(toolbar)
        compact = "".join(ch for ch in text if ch.isalnum())

        if self._excluded_layout_toolbar_text(text):
            return False

        return (
            ("action" in text and ("toolbar" in text or "tool bar" in text))
            or "actiontoolbar" in compact
            or "actionstoolbar" in compact
            or "layoutactiontoolbar" in compact
            or "layoutactionstoolbar" in compact
        )

    @staticmethod
    def _toolbar_is_probably_horizontal(toolbar):
        try:
            geometry = toolbar.geometry()
            width = geometry.width()
            height = geometry.height()
            if width > 0 and height > 0:
                return width >= height
        except Exception:
            record_suppressed_exception()

        return True

    @staticmethod
    def _toolbar_is_visible(toolbar, window):
        try:
            if toolbar.isVisible():
                return True
        except Exception:
            record_suppressed_exception()

        try:
            return toolbar.isVisibleTo(window)
        except Exception:
            return False

    def _second_toolbar_row(self, toolbars, window):
        """Return all toolbars on the second horizontal toolbar row."""
        positioned = []

        for toolbar in toolbars:
            if not self._toolbar_is_probably_horizontal(toolbar):
                continue

            try:
                geometry = toolbar.geometry()
                height = max(1, geometry.height())
            except Exception:
                height = 24

            x_pos, y_pos = self._toolbar_position(toolbar, window)
            positioned.append((y_pos, x_pos, height, toolbar))

        if len(positioned) < 2:
            return []

        positioned.sort(key=lambda entry: (entry[0], entry[1]))
        tolerance = max(8, min(entry[2] for entry in positioned) // 2)

        rows = []
        for y_pos, x_pos, _height, toolbar in positioned:
            for row in rows:
                if abs(y_pos - row["y"]) <= tolerance:
                    row["items"].append((x_pos, toolbar))
                    break
            else:
                rows.append({"y": y_pos, "items": [(x_pos, toolbar)]})

        rows.sort(key=lambda row: row["y"])
        if len(rows) < 2:
            return []

        return [toolbar for _x_pos, toolbar in sorted(rows[1]["items"])]

    def _rightmost_toolbar(self, toolbars, window):
        """Choose a single toolbar to avoid adding duplicate button groups."""
        if not toolbars:
            return None

        return max(
            toolbars,
            key=lambda toolbar: self._toolbar_position(toolbar, window),
        )

    @staticmethod
    def _toolbar_position(toolbar, window):
        try:
            point = toolbar.mapTo(window, toolbar.rect().topLeft())
            return point.x(), point.y()
        except Exception:
            record_suppressed_exception()

        try:
            geometry = toolbar.geometry()
            return geometry.x(), geometry.y()
        except Exception:
            return 0, 0

    def _looks_like_layout_designer_window(self, widget):
        if widget is None or not isinstance(widget, QWidget):
            return False

        try:
            if not widget.isWindow():
                return False
        except Exception:
            return False

        haystack = " ".join(
            [
                self._safe_text(type(widget).__name__),
                self._safe_widget_text(widget, "objectName"),
                self._safe_widget_text(widget, "windowTitle"),
            ]
        ).lower()

        if "layoutdesigner" in haystack or "layout designer" in haystack:
            return True

        has_layout_title = (
            "print layout" in haystack
            or "layout" in haystack
            or "report" in haystack
        )
        if not has_layout_title:
            return False

        try:
            return bool(widget.findChildren(QToolBar))
        except Exception:
            return False

    def _designer_from_window(self, window):
        """Best-effort lookup of a QgsLayoutDesignerInterface from a window."""
        designer = self._designer_from_object(window)
        if designer is not None:
            return designer

        try:
            children = window.findChildren(QObject)
        except Exception:
            children = []

        for child in children:
            designer = self._designer_from_object(child)
            if designer is not None:
                return designer

        return None

    @staticmethod
    def _designer_from_object(obj):
        if CurvedPolygonTextPlugin._looks_like_designer(obj):
            return obj

        if obj is None:
            return None

        for attr_name in (
            "designerInterface",
            "layoutDesignerInterface",
            "layoutDesigner",
            "designer",
        ):
            accessor = getattr(obj, attr_name, None)
            if not callable(accessor):
                continue

            try:
                candidate = accessor()
            except Exception:
                candidate = None

            if CurvedPolygonTextPlugin._looks_like_designer(candidate):
                return candidate

        return None

    @staticmethod
    def _looks_like_designer(obj):
        return (
            obj is not None
            and callable(getattr(obj, "view", None))
            and (
                callable(getattr(obj, "activateTool", None))
                or callable(getattr(obj, "layoutToolbar", None))
                or callable(getattr(obj, "layoutToolBar", None))
            )
        )

    @staticmethod
    def _designer_view(designer):
        if designer is None:
            return None

        view_method = getattr(designer, "view", None)
        if not callable(view_method):
            return None

        try:
            return view_method()
        except Exception:
            return None

    def _window_layout_view(self, window):
        """Last-resort view lookup when only a Layout Designer QWidget is known."""
        if window is None:
            return None

        try:
            candidates = window.findChildren(QWidget)
        except Exception:
            return None

        for candidate in candidates:
            if (
                callable(getattr(candidate, "setTool", None))
                and hasattr(candidate, "toolSet")
            ):
                return candidate

        return None

    def _record_designer(self, record):
        designer = record.get("designer")
        if self._looks_like_designer(designer):
            return designer

        target = record.get("target")
        designer = self._designer_from_object(target)
        if designer is None:
            designer = self._designer_from_window(record.get("window"))

        if designer is not None:
            record["designer"] = designer
            for entry in record.get("entries", {}).values():
                entry["designer"] = designer

        return designer

    # ---- Action/toolbar inspection helpers ------------------------------------

    @staticmethod
    def _toolbar_has_action(toolbar, object_name):
        try:
            return any(action.objectName() == object_name for action in toolbar.actions())
        except Exception:
            return False

    def _toolbar_search_text(self, toolbar):
        return " ".join(
            [
                self._safe_widget_text(toolbar, "objectName"),
                self._safe_widget_text(toolbar, "windowTitle"),
                self._safe_widget_text(toolbar, "toolTip"),
            ]
        ).lower()

    def _remove_orphaned_plugin_actions(self, toolbar):
        """Remove old actions with this plugin's object names before adding ours.

        Normal enable/disable uses _remove_all_actions(). This small extra guard
        prevents a stale action left by a previous failed reload from blocking a
        fresh, functional action.
        """
        try:
            actions = list(toolbar.actions())
        except Exception:
            return

        for action in actions:
            try:
                if action.objectName() in ACTION_OBJECT_NAMES:
                    toolbar.removeAction(action)
                    action.deleteLater()
            except Exception:
                record_suppressed_exception()

    @staticmethod
    def _safe_text(value):
        try:
            return str(value or "")
        except Exception:
            return ""

    def _safe_widget_text(self, widget, method_name):
        method = getattr(widget, method_name, None)
        if not callable(method):
            return ""

        try:
            return self._safe_text(method())
        except Exception:
            return ""

    @staticmethod
    def _unique_widgets(widgets):
        unique = []
        seen = set()
        for widget in widgets:
            key = id(widget)
            if key not in seen:
                unique.append(widget)
                seen.add(key)
        return unique

    # ---- Records and cleanup ---------------------------------------------------

    @staticmethod
    def _record_key(target, window=None):
        if window is not None:
            return ("window", id(window))
        return ("target", id(target))

    def _update_record_designer(self, key, designer):
        if designer is None:
            return

        record = self._records.get(key)
        if record is None:
            return

        record["designer"] = designer
        for entry in record.get("entries", {}).values():
            entry["designer"] = designer

    def _store_record(
        self,
        key,
        target,
        window,
        designer,
        actions,
        entries,
        toolbars,
        added_to_designer,
    ):
        self._records[key] = {
            "target": target,
            "window": window,
            "designer": designer,
            "actions": list(actions),
            "entries": dict(entries),
            "toolbars": list(toolbars),
            "added_to_designer": bool(added_to_designer),
        }

        destroyed_source = window if window is not None else target
        destroyed_signal = getattr(destroyed_source, "destroyed", None)
        if destroyed_signal is not None:
            try:
                destroyed_signal.connect(lambda *_args, record_key=key: self._forget_record(record_key))
            except Exception:
                record_suppressed_exception()

    def _forget_record(self, key):
        self._records.pop(key, None)

    def _remove_all_actions(self):
        for key in list(self._records.keys()):
            self._remove_record(key)

    def _remove_record(self, key):
        record = self._records.pop(key, None)
        if not record:
            return

        for entry in record.get("entries", {}).values():
            view = entry.get("view")
            sync_fn = entry.get("sync_fn")
            if view is not None and sync_fn is not None:
                try:
                    view.toolSet.disconnect(sync_fn)
                except Exception:
                    record_suppressed_exception()

            tool = entry.get("tool")
            self._safe_delete_action(tool)

        for action in record.get("actions", []):
            for toolbar in record.get("toolbars", []):
                try:
                    toolbar.removeAction(action)
                except Exception:
                    record_suppressed_exception()

            if record.get("added_to_designer"):
                target = record.get("target")
                remove_action = getattr(target, "removeAction", None)
                if callable(remove_action):
                    try:
                        remove_action(action)
                    except Exception:
                        record_suppressed_exception()

            self._safe_delete_action(action)

    @staticmethod
    def _safe_delete_action(action):
        if action is None:
            return
        try:
            action.deleteLater()
        except Exception:
            record_suppressed_exception()

    @staticmethod
    def _safe_set_checked(action, checked):
        if action is None:
            return
        try:
            if action.isChecked() != checked:
                action.setChecked(checked)
        except Exception:
            record_suppressed_exception()

    def _safe_main_window(self):
        main_window = getattr(self.iface, "mainWindow", None)
        if callable(main_window):
            try:
                return main_window()
            except Exception:
                record_suppressed_exception()
        return None

    @staticmethod
    def _same_designer(a, b):
        if a is None or b is None:
            return False

        try:
            if a is b or a == b:
                return True
        except Exception:
            record_suppressed_exception()

        try:
            aw = a.window()
            bw = b.window()
            return aw is bw or aw == bw
        except Exception:
            return False

    # ---- Diagnostics -----------------------------------------------------------

    @staticmethod
    def _log(msg, level=Qgis.MessageLevel.Warning, exc_info=False):
        if exc_info:
            tb = traceback.format_exc()
            if tb and tb.strip() and tb.strip() != "NoneType: None":
                msg = f"{msg}\n{tb}"

        try:
            QgsMessageLog.logMessage(msg, LOG_TAG, level)
        except Exception:
            record_suppressed_exception()
