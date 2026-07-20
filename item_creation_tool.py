"""
item_creation_tool.py

A QgsLayoutViewTool that creates a new Curved Spline Text or Polygon
Shaped Text Box item by letting the user click each vertex in turn --
the same sketching workflow QGIS's native Add Polygon / Add Polyline
shape tools use -- rather than the default "drag out one rectangle"
flow most layout items use.

  * Left-click       -> place the next vertex
  * Double-click      -> place a final vertex and finish
  * Right-click       -> finish without placing another vertex
  * Enter / Return    -> finish (keyboard alternative)
  * Escape            -> cancel and discard the in-progress sketch

Why this is a *separate, self-built* tool rather than QGIS's native
multi-click item creation tool (QgsLayoutViewToolAddNodeItem): that
native tool is hard-wired to QgsLayoutNodesItem (it casts the newly
created item to that type internally to call its node-management
methods). Our items are deliberately plain QgsLayoutItem subclasses
with their own node storage (see the design note in
layout_item_spline_text.py), so we get the equivalent click-to-sketch
UX by building our own tool against that same storage, using only
stable, version-safe QgsLayoutViewTool / QGraphicsScene primitives --
notably, this mirrors what QGIS's own C++ implementation does
internally for its rubber band (a plain QGraphicsPathItem/
QGraphicsPolygonItem added directly to the QgsLayout scene and removed
once the sketch is finished or cancelled).
"""
from qgis.gui import QgsLayoutViewTool
from qgis.PyQt.QtWidgets import QGraphicsPathItem

from .compat import (
    QtGui, QPen, QBrush, QColor, QRectF, NO_BRUSH, DASH_LINE,
    LEFT_BUTTON, RIGHT_BUTTON, CURSOR_CROSS,
    KEY_RETURN, KEY_ENTER, KEY_ESCAPE,
)
from .reliability import record_suppressed_exception

RUBBER_BAND_PEN = QPen(QColor(40, 90, 200), 0.5, DASH_LINE)
RUBBER_BAND_FILL = QColor(40, 90, 200, 40)
NODE_MARKER_RADIUS_MM = 1.2


class NodeItemCreationTool(QgsLayoutViewTool):
    """
    Generic click-to-sketch creation tool, parametrised per item type so
    one class serves both the spline (open polyline, min 2 points) and
    polygon (closed shape, min 3 points) items.
    """

    def __init__(self, view, tool_name, item_class, min_nodes, closed,
                 default_size_mm=5.0, on_finished=None, on_item_created=None):
        super().__init__(view, tool_name)
        self.setCursor(CURSOR_CROSS)
        self._item_class = item_class
        self._min_nodes = min_nodes
        self._closed = closed
        self._default_size_mm = default_size_mm
        self._on_finished = on_finished  # optional callable(view) after success/cancel
        self._on_item_created = on_item_created  # optional callable(item), see layout_plugin._keep_alive

        self._points = []          # committed vertices, scene (layout) coords
        self._rubber_item = None   # temporary QGraphicsItem preview, added to the layout scene

    # ------------------------------------------------------------ lifecycle
    def activate(self):
        self._points = []
        super().activate()

    def deactivate(self):
        self._clear_rubber_band()
        self._points = []
        super().deactivate()

    # --------------------------------------------------------------- events
    def layoutPressEvent(self, event):
        if event.button() == RIGHT_BUTTON:
            self._finish()
            return
        if event.button() == LEFT_BUTTON:
            self._points.append(event.layoutPoint())
            self._update_rubber_band(event.layoutPoint())

    def layoutMoveEvent(self, event):
        self._update_rubber_band(event.layoutPoint())

    def layoutDoubleClickEvent(self, event):
        # Qt delivers a normal press for the second click of a double
        # click before the double-click event itself, so the "closing"
        # click has already been appended as a vertex by
        # layoutPressEvent(); drop it so it isn't duplicated.
        if self._points:
            self._points.pop()
        self._finish()

    def keyPressEvent(self, event):
        if event.key() in (KEY_RETURN, KEY_ENTER):
            self._finish()
        elif event.key() == KEY_ESCAPE:
            self._cancel()

    # ----------------------------------------------------------------- core
    def _finish(self):
        if len(self._points) < self._min_nodes:
            # Not enough vertices yet -- ignore rather than discard the
            # user's progress; they can keep clicking or press Escape.
            return

        layout = self.layout()
        if layout is None:
            self._cancel()
            return

        points = list(self._points)
        xs = [p.x() for p in points]
        ys = [p.y() for p in points]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        width = max(max_x - min_x, self._default_size_mm)
        height = max(max_y - min_y, self._default_size_mm)

        item = self._item_class(layout)
        layout.addLayoutItem(item)
        if self._on_item_created:
            # Must happen before anything else gets a chance to round-trip
            # this item through C++ and back (see the keep-alive
            # explanation in layout_plugin.py) -- registering it first is
            # cheap insurance against that happening during any of the
            # calls below.
            self._on_item_created(item)

        scene_rect = QRectF(min_x, min_y, width, height)
        try:
            # attemptSetSceneRect() takes a plain QRectF directly in the
            # layout's native (mm) coordinate system -- the same system
            # event.layoutPoint() already gave us -- so there's no unit
            # enum to resolve at all (QGIS 4.x relocated those from
            # QgsUnitTypes into the new Qgis.LayoutUnit class).
            item.attemptSetSceneRect(scene_rect)
        except Exception as exc:  # noqa: BLE001
            from qgis.core import QgsMessageLog, Qgis
            QgsMessageLog.logMessage(
                f"Could not position new item to match the sketched points "
                f"(it was still created, just at its default position/size): {exc}",
                "Curved/Polygon Text", Qgis.MessageLevel.Warning)

        item.setNodesFromSceneBounds(points, scene_rect)
        item.setSelected(True)

        self._clear_rubber_band()
        self._points = []

        if self._on_finished:
            self._on_finished(self.view())

    def _cancel(self):
        self._clear_rubber_band()
        self._points = []
        if self._on_finished:
            self._on_finished(self.view())

    # ----------------------------------------------------------- rubber band
    def _update_rubber_band(self, live_point):
        layout = self.layout()
        if layout is None or not self._points:
            return

        preview_points = list(self._points)
        if live_point is not None:
            preview_points = preview_points + [live_point]

        path = QtGui.QPainterPath()
        path.moveTo(preview_points[0])
        for pt in preview_points[1:]:
            path.lineTo(pt)
        if self._closed and len(preview_points) > 2:
            path.closeSubpath()

        # Mark each already-committed vertex with a small circle so the
        # in-progress sketch stays clearly visible even for short/thin
        # open paths (e.g. early in a Curved Spline Text sketch), where a
        # plain stroke alone can be easy to miss on screen.
        for pt in self._points:
            path.addEllipse(pt, NODE_MARKER_RADIUS_MM, NODE_MARKER_RADIUS_MM)

        if self._rubber_item is None:
            self._rubber_item = QGraphicsPathItem()
            self._rubber_item.setPen(RUBBER_BAND_PEN)
            # NOTE: QGraphicsItem.setBrush() (unlike QPainter.setBrush())
            # has no overload accepting a bare Qt.BrushStyle in PyQt6 --
            # only QBrush/QColor/etc -- so NO_BRUSH must be wrapped
            # explicitly here rather than passed directly.
            self._rubber_item.setBrush(QBrush(RUBBER_BAND_FILL) if self._closed else QBrush(NO_BRUSH))
            self._rubber_item.setZValue(1000)
            layout.addItem(self._rubber_item)

        self._rubber_item.setPath(path)

    def _clear_rubber_band(self):
        if self._rubber_item is not None:
            layout = self.layout()
            if layout is not None:
                try:
                    layout.removeItem(self._rubber_item)
                except Exception:
                    record_suppressed_exception()
            self._rubber_item = None
