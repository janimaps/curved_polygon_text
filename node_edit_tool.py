"""Interactive anchor/control-handle editor for custom layout text items.

v1.0.2 adds cubic Bezier control handles shared by Spline Text and Polygon
Text.  Anchors remain fully backwards compatible with the v1.0.1 node model.
"""
import math

from qgis.gui import QgsLayoutViewTool

from .compat import (
    QtCore, QtWidgets, QtGui, ALIGN_LEFT, ALIGN_RIGHT,
    LEFT_BUTTON, RIGHT_BUTTON, MIDDLE_BUTTON,
    CURSOR_CROSS, CURSOR_CLOSED_HAND,
    CTRL_MODIFIER, SHIFT_MODIFIER, ALT_MODIFIER, META_MODIFIER,
)
from .layout_item_spline_text import LayoutItemSplineText
from .layout_item_polygon_text import LayoutItemPolygonText

NODE_ITEM_TYPES = (LayoutItemSplineText, LayoutItemPolygonText)
HIT_TOLERANCE_MM = 3.0
HANDLE_HIT_TOLERANCE_MM = 2.5
SEGMENT_MENU_TOLERANCE_MM = 4.0


class NodeEditTool(QgsLayoutViewTool):

    def __init__(self, view):
        super().__init__(view, "Edit Curve / Polygon Nodes")
        self._view = view
        self.setCursor(CURSOR_CROSS)
        self._drag_item = None
        self._drag_index = -1
        self._drag_handle = None
        self._highlight_item = None
        self._highlight_index = -1
        self._highlight_handle = None
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
    def _handle_at(item, scene_pos):
        getter = getattr(item, "handleScenePositions", None)
        if not callable(getter):
            return None
        for index, kind, p in getter():
            if math.hypot(p.x() - scene_pos.x(), p.y() - scene_pos.y()) <= HANDLE_HIT_TOLERANCE_MM:
                return index, kind
        return None

    @staticmethod
    def _event_view_pos(event):
        position = getattr(event, "position", None)
        return position() if callable(position) else event.pos()

    @staticmethod
    def _modifier_active(event, modifier):
        try:
            return bool(event.modifiers() & modifier)
        except (AttributeError, TypeError):
            return False

    def _global_menu_pos(self, event):
        screen_pos = getattr(event, "screenPos", None)
        if callable(screen_pos):
            try:
                return screen_pos()
            except RuntimeError:
                screen_pos = None
        view_pos = self._event_view_pos(event)
        to_point = getattr(view_pos, "toPoint", None)
        if callable(to_point):
            view_pos = to_point()
        return self._view.mapToGlobal(view_pos)

    @staticmethod
    def _exec_menu(menu, global_pos):
        executor = getattr(menu, "exec", None)
        if callable(executor):
            return executor(global_pos)
        executor = getattr(menu, "exec_", None)
        return executor(global_pos) if callable(executor) else None

    def _set_highlight(self, item, index=-1, handle=None):
        target_handle = handle if item is not None else None
        if (item is self._highlight_item and index == self._highlight_index
                and target_handle == self._highlight_handle):
            return
        if self._highlight_item is not None:
            try:
                setter = getattr(self._highlight_item, "setActiveNodeIndex", None)
                if callable(setter):
                    setter(-1)
                hsetter = getattr(self._highlight_item, "setActiveHandle", None)
                if callable(hsetter):
                    hsetter(None, None)
            except RuntimeError:
                self._highlight_item = None

        self._highlight_item = item
        self._highlight_index = index if item is not None else -1
        self._highlight_handle = target_handle
        if item is None:
            return
        try:
            setter = getattr(item, "setActiveNodeIndex", None)
            if callable(setter):
                setter(index if target_handle is None else -1)
            hsetter = getattr(item, "setActiveHandle", None)
            if callable(hsetter):
                if target_handle is None:
                    hsetter(None, None)
                else:
                    hsetter(target_handle[0], target_handle[1])
        except RuntimeError:
            self._highlight_item = None
            self._highlight_index = -1
            self._highlight_handle = None

    @staticmethod
    def _layout_direction(right_to_left=False):
        """Return a Qt5/Qt6 compatible layout-direction enum."""
        qt = QtCore.Qt
        name = "RightToLeft" if right_to_left else "LeftToRight"
        value = getattr(qt, name, None)
        if value is not None and not isinstance(value, type):
            return value
        enum_cls = getattr(qt, "LayoutDirection", None)
        return getattr(enum_cls, name) if enum_cls is not None else value

    def _node_global_pos(self, item, index, event):
        """Return the edited node position in global screen pixels."""
        try:
            positions = item.nodeScenePositions()
            if 0 <= index < len(positions):
                view_pos = self._view.mapFromScene(positions[index])
                return self._view.viewport().mapToGlobal(view_pos)
        except (AttributeError, IndexError, RuntimeError, TypeError):
            return self._global_menu_pos(event)
        return self._global_menu_pos(event)

    @staticmethod
    def _anchor_extent_rect(scene_positions):
        """Bounds of the actual anchor geometry, excluding Bezier handles."""
        if not scene_positions:
            return None
        xs = [point.x() for point in scene_positions]
        ys = [point.y() for point in scene_positions]
        left, right = min(xs), max(xs)
        top, bottom = min(ys), max(ys)
        return QtCore.QRectF(left, top, right - left, bottom - top)

    @staticmethod
    def _available_screen_rect(global_pos):
        """Return the available desktop geometry containing global_pos."""
        app = QtGui.QGuiApplication.instance()
        screen = None
        if app is not None:
            screen_at = getattr(app, "screenAt", None)
            if callable(screen_at):
                try:
                    screen = screen_at(global_pos)
                except (RuntimeError, TypeError):
                    screen = None
            if screen is None:
                try:
                    screen = app.primaryScreen()
                except (AttributeError, RuntimeError):
                    screen = None
        if screen is not None:
            try:
                return screen.availableGeometry()
            except RuntimeError:
                return None
        return None

    def _anchor_popup_preferences(self, item, index, event):
        """Return preferred side, horizontal content alignment and node pixel."""
        node_global = self._node_global_pos(item, index, event)
        preferred = "right"
        align_right = False

        try:
            scene_positions = item.nodeScenePositions()
            node_scene = scene_positions[index]
        except (AttributeError, IndexError, RuntimeError, TypeError):
            return preferred, align_right, node_global

        rect = self._anchor_extent_rect(scene_positions)
        if rect is None:
            return preferred, align_right, node_global

        distances = {
            "left": abs(node_scene.x() - rect.left()),
            "right": abs(rect.right() - node_scene.x()),
            "top": abs(node_scene.y() - rect.top()),
            "bottom": abs(rect.bottom() - node_scene.y()),
        }
        preferred = min(distances, key=distances.get)

        if preferred == "left":
            align_right = True
        elif preferred == "right":
            align_right = False
        else:
            # Top/bottom popups always use left-aligned contents with
            # radio indicators on the left.
            align_right = False
        return preferred, align_right, node_global

    @staticmethod
    def _popup_candidate(side, anchor, size, gap=10):
        """Top-left global point for a popup placed outward from anchor."""
        width = int(size.width())
        height = int(size.height())
        ax = int(anchor.x())
        ay = int(anchor.y())
        if side == "left":
            return QtCore.QPoint(ax - width - gap, ay - height // 2)
        if side == "right":
            return QtCore.QPoint(ax + gap, ay - height // 2)
        if side == "top":
            return QtCore.QPoint(ax - width // 2, ay - height - gap)
        return QtCore.QPoint(ax - width // 2, ay + gap)

    @staticmethod
    def _rect_fits_screen(top_left, size, screen_rect, margin=4):
        if screen_rect is None:
            return True
        candidate = QtCore.QRect(top_left, size)
        safe = screen_rect.adjusted(margin, margin, -margin, -margin)
        return safe.contains(candidate)

    @staticmethod
    def _clamp_popup(top_left, size, screen_rect, margin=4):
        if screen_rect is None:
            return top_left
        safe = screen_rect.adjusted(margin, margin, -margin, -margin)
        x = min(max(top_left.x(), safe.left()), max(safe.left(), safe.right() - size.width() + 1))
        y = min(max(top_left.y(), safe.top()), max(safe.top(), safe.bottom() - size.height() + 1))
        return QtCore.QPoint(int(x), int(y))

    def _adaptive_menu_position(self, menu, item, index, event):
        """Choose an outward popup side with a screen-edge fallback."""
        preferred, _align_right, node_global = self._anchor_popup_preferences(
            item, index, event)
        size_hint = menu.sizeHint()
        # The anchor popup uses a deliberately fixed width. QMenu.sizeHint() can
        # still report a wider native action-column width, especially for the
        # mirrored left-side popup. If that wider hint is used for positioning,
        # the menu is shifted too far away from the node even though the actual
        # visible popup is narrower. Use the fixed width for geometry and retain
        # only the native height hint.
        fixed_width = int(menu.minimumWidth())
        if fixed_width > 0 and fixed_width == int(menu.maximumWidth()):
            size = QtCore.QSize(fixed_width, int(size_hint.height()))
        else:
            size = size_hint
        screen_rect = self._available_screen_rect(node_global)

        opposite = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
        if preferred in ("left", "right"):
            fallbacks = [preferred, opposite[preferred], "top", "bottom"]
        else:
            fallbacks = [preferred, opposite[preferred], "left", "right"]

        for side in fallbacks:
            point = self._popup_candidate(side, node_global, size)
            if self._rect_fits_screen(point, size, screen_rect):
                return point, side

        point = self._popup_candidate(preferred, node_global, size)
        return self._clamp_popup(point, size, screen_rect), preferred

    @staticmethod
    def _set_anchor_menu_alignment(menu, header, radios, align_right, action_buttons=None):
        """Mirror heading/text and radio side for left-opening popups."""
        direction = NodeEditTool._layout_direction(right_to_left=align_right)
        alignment = ALIGN_RIGHT if align_right else ALIGN_LEFT
        # Keep QMenu itself left-to-right. Reversing the whole menu also
        # reverses Qt's internal action-column geometry, which can leave an
        # asymmetric trailing gutter or clip the right-aligned widgets.
        # Only the custom content widgets are mirrored below.
        menu_direction_set = True
        header_alignment_set = True
        try:
            header.setAlignment(alignment)
        except (AttributeError, TypeError):
            header_alignment_set = False
        for radio in radios:
            radio_direction_set = True
            try:
                radio.setLayoutDirection(direction)
            except (AttributeError, TypeError):
                radio_direction_set = False
            if not radio_direction_set:
                radio.setProperty("nodeModeRightAligned", bool(align_right))
        for button in action_buttons or []:
            button.setStyleSheet(
                "QPushButton { border: none; padding: 4px 8px; text-align: %s; } "
                "QPushButton:hover { background: palette(highlight); color: palette(highlighted-text); }"
                % ("right" if align_right else "left")
            )
        if not menu_direction_set:
            menu.setProperty("nodeModeRightAligned", bool(align_right))
        if not header_alignment_set:
            header.setProperty("nodeModeRightAligned", bool(align_right))

    @staticmethod
    def _tighten_anchor_menu_width(menu, header, radios, action_buttons, indicator_w):
        """Size the custom node-mode popup to its real visible contents."""
        def text_width(widget, text):
            metrics = QtGui.QFontMetrics(widget.font())
            advance = getattr(metrics, "horizontalAdvance", None)
            if callable(advance):
                return int(advance(text))
            return int(metrics.width(text))

        # Match the explicit 8 px left/right row padding used by the widgets.
        horizontal_padding = 16
        radio_gap = 6
        widths = [
            text_width(header, header.text()) + horizontal_padding,
        ]
        for radio in radios:
            widths.append(
                text_width(radio, radio.text())
                + int(indicator_w)
                + radio_gap
                + horizontal_padding
            )
        for button in action_buttons or []:
            widths.append(text_width(button, button.text()) + horizontal_padding)

        row_width = max(widths) if widths else 1
        # One pixel on either side is enough for the menu frame; forcing the
        # width prevents QMenu from reserving its normal unused action columns.
        menu_width = row_width + 2
        for widget in [header] + list(radios) + list(action_buttons or []):
            widget.setFixedWidth(row_width)
        menu.setMinimumWidth(menu_width)
        menu.setMaximumWidth(menu_width)
        menu.setContentsMargins(0, 0, 0, 0)

    def _show_anchor_menu(self, item, index, event):
        menu = QtWidgets.QMenu(self._view)

        # Keep a clear, non-interactive heading above the first divider.
        header_action = QtWidgets.QWidgetAction(menu)
        header = QtWidgets.QLabel("Node Mode", menu)
        header_font = header.font()
        header_font.setBold(True)
        header.setFont(header_font)
        header.setContentsMargins(8, 4, 8, 3)
        header_action.setDefaultWidget(header)
        menu.addAction(header_action)
        menu.addSeparator()

        current_mode = "corner"
        getter = getattr(item, "nodeMode", None)
        if callable(getter):
            try:
                current_mode = str(getter(index) or "corner")
            except RuntimeError:
                current_mode = "corner"

        # Use real radio widgets so the indicator can switch sides together
        # with the menu's adaptive left/right alignment.
        button_group = QtWidgets.QButtonGroup(menu)
        button_group.setExclusive(True)
        selected_mode = {"value": None}
        radios = []

        style = menu.style()
        try:
            qstyle = QtWidgets.QStyle
            width_metric = getattr(qstyle, "PM_ExclusiveIndicatorWidth", None)
            height_metric = getattr(qstyle, "PM_ExclusiveIndicatorHeight", None)
            if width_metric is None:
                width_metric = qstyle.PixelMetric.PM_ExclusiveIndicatorWidth
            if height_metric is None:
                height_metric = qstyle.PixelMetric.PM_ExclusiveIndicatorHeight
            base_w = int(style.pixelMetric(width_metric))
            base_h = int(style.pixelMetric(height_metric))
        except (AttributeError, TypeError, RuntimeError):
            base_w, base_h = 13, 13
        indicator_w = max(1, int(round(base_w * 1.1)))
        indicator_h = max(1, int(round(base_h * 1.1)))

        def choose_mode(mode_name):
            selected_mode["value"] = mode_name
            menu.close()

        for mode_name, label in (
            ("smooth", "Smooth"),
            ("symmetric", "Symmetric"),
            ("corner", "Corner"),
        ):
            widget_action = QtWidgets.QWidgetAction(menu)
            radio = QtWidgets.QRadioButton(label, menu)
            radio.setChecked(current_mode == mode_name)
            radio.setContentsMargins(8, 1, 8, 1)
            radio.setStyleSheet(
                "QRadioButton::indicator { width: %dpx; height: %dpx; }"
                % (indicator_w, indicator_h)
            )
            if current_mode == mode_name:
                radio_font = radio.font()
                radio_font.setBold(True)
                radio.setFont(radio_font)
            radio.clicked.connect(lambda checked=False, m=mode_name: choose_mode(m))
            button_group.addButton(radio)
            radios.append(radio)
            widget_action.setDefaultWidget(radio)
            menu.addAction(widget_action)

        action_choice = {"value": None}

        def choose_action(action_name):
            action_choice["value"] = action_name
            menu.close()

        menu.addSeparator()
        reset_action = QtWidgets.QWidgetAction(menu)
        reset_button = QtWidgets.QPushButton("Reset Handles", menu)
        reset_button.setFlat(True)
        reset_button.clicked.connect(lambda checked=False: choose_action("reset"))
        reset_action.setDefaultWidget(reset_button)
        menu.addAction(reset_action)
        menu.addSeparator()
        delete_action = QtWidgets.QWidgetAction(menu)
        delete_button = QtWidgets.QPushButton("Delete Node", menu)
        delete_button.setFlat(True)
        delete_button.clicked.connect(lambda checked=False: choose_action("delete"))
        delete_action.setDefaultWidget(delete_button)
        menu.addAction(delete_action)
        action_buttons = [reset_button, delete_button]
        self._tighten_anchor_menu_width(
            menu, header, radios, action_buttons, indicator_w)

        preferred, preferred_align_right, _node_global = self._anchor_popup_preferences(
            item, index, event)
        self._set_anchor_menu_alignment(
            menu, header, radios, preferred_align_right, action_buttons)
        global_pos, actual_side = self._adaptive_menu_position(menu, item, index, event)

        # A screen-edge fallback may place the popup on the opposite horizontal
        # side. Mirror the contents again so the text/radios face the node.
        if actual_side == "left":
            actual_align_right = True
        elif actual_side == "right":
            actual_align_right = False
        else:
            # Top/bottom popups always keep left-aligned contents and
            # radio indicators on the left, regardless of screen fallback.
            actual_align_right = False
        if actual_align_right != preferred_align_right:
            self._set_anchor_menu_alignment(menu, header, radios, actual_align_right, action_buttons)
            # Layout direction can change the size hint slightly.
            global_pos, _actual_side = self._adaptive_menu_position(menu, item, index, event)

        chosen = self._exec_menu(menu, global_pos)
        chosen_mode = selected_mode["value"]
        chosen_action = action_choice["value"]
        if chosen is None and chosen_mode is None and chosen_action is None:
            return

        item.beginCommand("Edit Bezier Node")
        changed = False
        if chosen_mode == "smooth":
            changed = item.setNodeMode(index, "smooth")
        elif chosen_mode == "symmetric":
            changed = item.setNodeMode(index, "symmetric")
        elif chosen_mode == "corner":
            changed = item.setNodeMode(index, "corner")
        elif chosen_action == "reset":
            changed = item.resetNodeHandles(index)
        elif chosen_action == "delete":
            changed = item.removeNodeAt(index)
        if changed:
            item.endCommand()
        else:
            item.cancelCommand()

    def _show_segment_menu(self, item, segment_index, event):
        menu = QtWidgets.QMenu(self._view)
        curve = menu.addAction("Convert Segment to Curve")
        straight = menu.addAction("Convert Segment to Straight")
        chosen = self._exec_menu(menu, self._global_menu_pos(event))
        if chosen is None:
            return
        item.beginCommand("Convert Bezier Segment")
        changed = False
        if chosen is curve:
            changed = item.convertSegmentToCurve(segment_index)
        elif chosen is straight:
            changed = item.convertSegmentToStraight(segment_index)
        if changed:
            item.endCommand()
        else:
            item.cancelCommand()

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
            handle = self._handle_at(item, scene_pos)
            if handle is not None:
                self._set_highlight(item, -1, handle)
                item.beginCommand("Move Bezier Handle")
                self._drag_item = item
                self._drag_index = handle[0]
                self._drag_handle = handle[1]
                return
            idx = self._node_at(item, scene_pos)
            if idx >= 0:
                self._set_highlight(item, idx, None)
                item.beginCommand("Move Node")
                self._drag_item = item
                self._drag_index = idx
                self._drag_handle = None

        elif event.button() == RIGHT_BUTTON:
            handle = self._handle_at(item, scene_pos)
            idx = handle[0] if handle is not None else self._node_at(item, scene_pos)
            if idx >= 0:
                self._set_highlight(item, idx, None)
                self._show_anchor_menu(item, idx, event)
                return
            nearest = item.nearestBezierSegment(scene_pos)
            if nearest and nearest[0] >= 0 and nearest[3] <= SEGMENT_MENU_TOLERANCE_MM:
                self._show_segment_menu(item, nearest[0], event)

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
            if self._drag_handle is not None:
                independent = (self._modifier_active(event, CTRL_MODIFIER)
                               or self._modifier_active(event, ALT_MODIFIER)
                               or self._modifier_active(event, META_MODIFIER))
                constrain = self._modifier_active(event, SHIFT_MODIFIER)
                self._set_highlight(
                    self._drag_item, -1, (self._drag_index, self._drag_handle))
                self._drag_item.setBezierHandleAtScenePos(
                    self._drag_index, self._drag_handle, event.layoutPoint(),
                    independent=independent, constrain=constrain)
            else:
                self._set_highlight(self._drag_item, self._drag_index, None)
                self._drag_item.setNodeAtScenePos(
                    self._drag_index, event.layoutPoint())
            return

        item = self._active_node_item()
        if item is None:
            self._set_highlight(None, -1, None)
            return
        handle = self._handle_at(item, event.layoutPoint())
        if handle is not None:
            self._set_highlight(item, -1, handle)
            return
        idx = self._node_at(item, event.layoutPoint())
        self._set_highlight(item, idx, None) if idx >= 0 else self._set_highlight(None, -1, None)

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
        self._drag_handle = None

    def layoutDoubleClickEvent(self, event):
        item = self._active_node_item()
        if item is None:
            return
        scene_pos = event.layoutPoint()
        item.beginCommand("Insert Bezier Node")
        if isinstance(item, LayoutItemSplineText):
            item.insertNodeNearestSegment(scene_pos)
        else:
            item.insertNodeNearestEdge(scene_pos)
        item.endCommand()

    def deactivate(self):
        if self._drag_item is not None:
            self._drag_item.cancelCommand()
        self._drag_item = None
        self._drag_index = -1
        self._drag_handle = None
        self._set_highlight(None, -1, None)
        self._pan_active = False
        self._pan_last_pos = None
        super().deactivate()
