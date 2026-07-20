"""
node_edit_tool.py

A QgsLayoutViewTool that lets the user interactively edit the node
list of our custom items (LayoutItemSplineText / LayoutItemPolygonText)
directly on the Layout Designer canvas:

  * Left-press + drag a node   -> move it
  * Double left-click on the outline -> insert a new node there
  * Right-click an existing node      -> delete it (subject to the
                                          item's minimum node count)

Only the currently *selected* layout item is eligible for node
editing, mirroring how QGIS's native node-editing tools behave (select
the shape first, then edit it). Node moves/inserts/deletes are wrapped
in the item's own beginCommand()/endCommand()/cancelCommand() so they
integrate with the layout's normal Ctrl+Z undo history.
"""
import math

from qgis.gui import QgsLayoutViewTool

from .compat import (
    LEFT_BUTTON, RIGHT_BUTTON, MIDDLE_BUTTON,
    CURSOR_CROSS, CURSOR_CLOSED_HAND,
)
from .layout_item_spline_text import LayoutItemSplineText
from .layout_item_polygon_text import LayoutItemPolygonText

NODE_ITEM_TYPES = (LayoutItemSplineText, LayoutItemPolygonText)
HIT_TOLERANCE_MM = 3.0


class NodeEditTool(QgsLayoutViewTool):

    def __init__(self, view):
        super().__init__(view, "Edit Curve / Polygon Nodes")
        self._view = view
        self.setCursor(CURSOR_CROSS)
        self._drag_item = None
        self._drag_index = -1
        self._pan_active = False
        self._pan_last_pos = None

    # ------------------------------------------------------------- helpers
    def _active_node_item(self):
        layout = self.layout()
        if layout is None:
            return None
        for item in layout.selectedLayoutItems():
            if isinstance(item, NODE_ITEM_TYPES):
                return item
        return None

    @staticmethod
    def _node_at(item, scene_pos):
        for i, p in enumerate(item.nodeScenePositions()):
            if math.hypot(p.x() - scene_pos.x(), p.y() - scene_pos.y()) <= HIT_TOLERANCE_MM:
                return i
        return -1

    @staticmethod
    def _event_view_pos(event):
        position = getattr(event, "position", None)
        return position() if callable(position) else event.pos()

    # ------------------------------------------------ QgsLayoutViewTool API
    def layoutPressEvent(self, event):
        if event.button() == MIDDLE_BUTTON:
            self._pan_active = True
            self._pan_last_pos = self._event_view_pos(event)
            self.setCursor(CURSOR_CLOSED_HAND)
            self._view.viewport().setCursor(CURSOR_CLOSED_HAND)
            return

        item = self._active_node_item()
        if item is None:
            return
        scene_pos = event.layoutPoint()

        if event.button() == LEFT_BUTTON:
            idx = self._node_at(item, scene_pos)
            if idx >= 0:
                item.beginCommand("Move Node")
                self._drag_item = item
                self._drag_index = idx

        elif event.button() == RIGHT_BUTTON:
            idx = self._node_at(item, scene_pos)
            if idx >= 0:
                item.beginCommand("Delete Node")
                if item.removeNodeAt(idx):
                    item.endCommand()
                else:
                    item.cancelCommand()

    def layoutMoveEvent(self, event):
        if self._pan_active and self._pan_last_pos is not None:
            current_pos = self._event_view_pos(event)
            delta = current_pos - self._pan_last_pos
            horizontal = self._view.horizontalScrollBar()
            vertical = self._view.verticalScrollBar()
            horizontal.setValue(horizontal.value() - int(round(delta.x())))
            vertical.setValue(vertical.value() - int(round(delta.y())))
            self._pan_last_pos = current_pos
            return

        if self._drag_item is not None and self._drag_index >= 0:
            self._drag_item.setNodeAtScenePos(self._drag_index, event.layoutPoint())

    def layoutReleaseEvent(self, event):
        if event.button() == MIDDLE_BUTTON and self._pan_active:
            self._pan_active = False
            self._pan_last_pos = None
            self.setCursor(CURSOR_CROSS)
            self._view.viewport().setCursor(CURSOR_CROSS)
            return

        if self._drag_item is not None:
            self._drag_item.endCommand()
        self._drag_item = None
        self._drag_index = -1

    def layoutDoubleClickEvent(self, event):
        item = self._active_node_item()
        if item is None:
            return
        scene_pos = event.layoutPoint()
        item.beginCommand("Insert Node")
        if isinstance(item, LayoutItemSplineText):
            item.insertNodeNearestSegment(scene_pos)
        else:
            item.insertNodeNearestEdge(scene_pos)
        item.endCommand()

    def deactivate(self):
        # Make sure an in-progress drag can't leak past tool switches.
        if self._drag_item is not None:
            self._drag_item.cancelCommand()
        self._drag_item = None
        self._drag_index = -1
        self._pan_active = False
        self._pan_last_pos = None
        super().deactivate()
