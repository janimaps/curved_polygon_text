"""
gui_widgets.py — Item Properties panels for Curved Spline Text and
Polygon Shaped Text Box, structured to match the native Add Label panel.

  Main Properties (collapsible)
    • text input
    • Allow HTML formatting
    • Insert Expression…

  Appearance (collapsible)
    • Font   — QgsFontButton, opens the full Label Font dialog
    • Horizontal margin
    • Vertical margin
    • Horizontal alignment
    • [Polygon only] Vertical alignment

  [Embedded QgsLayoutItemPropertiesWidget]
    • Position and Size / Rotation / Frame (path outline) / Background /
      Item ID / Variables
"""
from ..compat import QtWidgets, QtCore
from ..layout_item_spline_text  import LayoutItemSplineText,  ALIGN_LEFT_, ALIGN_CENTER_, ALIGN_RIGHT_, ALIGN_JUSTIFY_
from ..layout_item_polygon_text import LayoutItemPolygonText
from ..reliability import record_suppressed_exception

from qgis.gui import QgsLayoutItemBaseWidget
from qgis.core import QgsTextFormat


# ------------------------------------------------------------------ helpers
def _group(title, parent):
    """QgsCollapsibleGroupBox with a plain QGroupBox fallback."""
    try:
        from qgis.gui import QgsCollapsibleGroupBox
        return QgsCollapsibleGroupBox(title, parent)
    except Exception:
        return QtWidgets.QGroupBox(title, parent)


def _font_button_mode():
    """Return the scoped text-renderer mode used by QGIS 3.44 and QGIS 4."""
    try:
        from qgis.gui import QgsFontButton
        return QgsFontButton.Mode.ModeTextRenderer
    except Exception:
        return None


def _make_font_button(parent):
    """Return a QgsFontButton (ModeTextRenderer) or None if unavailable."""
    try:
        from qgis.gui import QgsFontButton
        btn  = QgsFontButton(parent, "Font")
        mode = _font_button_mode()
        if mode is not None:
            btn.setMode(mode)
        return btn
    except Exception:
        return None


def _reset_scrollbars_to_top(widget):
    """Reset scroll areas inside/around a properties widget to their top."""
    if widget is None:
        return

    def _set_top(scroll_area):
        try:
            bar = scroll_area.verticalScrollBar()
            if bar is not None:
                bar.setValue(bar.minimum())
        except Exception:
            record_suppressed_exception()

    try:
        for area in widget.findChildren(QtWidgets.QAbstractScrollArea):
            _set_top(area)
    except Exception:
        record_suppressed_exception()

    try:
        parent = widget.parentWidget()
    except Exception:
        parent = None
    guard = 0
    while parent is not None and guard < 32:
        guard += 1
        try:
            if isinstance(parent, QtWidgets.QAbstractScrollArea):
                _set_top(parent)
        except Exception:
            record_suppressed_exception()
        try:
            parent = parent.parentWidget()
        except Exception:
            break


def _schedule_scrollbars_to_top(widget, delays=(0, 50, 150)):
    for delay in delays:
        try:
            QtCore.QTimer.singleShot(
                int(delay), lambda w=widget: _reset_scrollbars_to_top(w))
        except Exception:
            _reset_scrollbars_to_top(widget)


def _event_type(name):
    """Resolve QEvent values across the Qt 5 and Qt 6 enum layouts."""
    try:
        return getattr(QtCore.QEvent, name)
    except AttributeError:
        return getattr(QtCore.QEvent.Type, name)


_MOUSE_BUTTON_RELEASE = _event_type("MouseButtonRelease")


def _h_margin_form_label():  return "Horizontal margin"
def _v_margin_form_label():  return "Vertical margin"


class _BaseTextPropertiesWidget(QgsLayoutItemBaseWidget):
    """Shared base for both item property panels."""

    ITEM_CLASS = None
    SUPPORTS_JUSTIFY = True
    DEFER_COMMON_PROPERTIES = False

    def __init__(self, parent, item):
        super().__init__(parent, item)
        self.item                 = item
        self._loading             = False
        self._font_button         = None   # QgsFontButton or None
        self._common_props_widget = None   # QgsLayoutItemPropertiesWidget
        self._common_props_host   = None
        self._loaded_text_format  = None
        self._font_dialog_reset_token = 0
        self._build_ui()
        self._load_from_item()

    # ------------------------------------------------------- QGIS API
    def setNewItem(self, item):
        if not isinstance(item, self.ITEM_CLASS):
            return False
        self.item = item
        self._load_from_item()
        if self._common_props_widget is not None:
            try:
                self._common_props_widget.setItem(item)
            except Exception:
                record_suppressed_exception()
        if self._font_button is not None:
            try:
                self._font_button.setLayer(None)
            except Exception:
                record_suppressed_exception()
        return True

    # ---------------------------------------------------------------- layout
    def _build_ui(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        # ── Main Properties ──────────────────────────────────────────
        main_grp  = _group("Main Properties", self)
        main_form = QtWidgets.QFormLayout(main_grp)

        self.text_edit = QtWidgets.QPlainTextEdit(self)
        self.text_edit.setMinimumHeight(90)
        self.text_edit.setPlaceholderText(
            "Type text here. Use [% @project_title %] or [% $page %] for "
            "dynamic values."
        )
        main_form.addRow("Text", self.text_edit)

        self.html_checkbox = QtWidgets.QCheckBox("Render as HTML", self)
        main_form.addRow("", self.html_checkbox)

        insert_btn = QtWidgets.QPushButton("Insert Expression\u2026", self)
        main_form.addRow("", insert_btn)
        outer.addWidget(main_grp)

        # ── Appearance ───────────────────────────────────────────────
        app_grp  = _group("Appearance", self)
        app_form = QtWidgets.QFormLayout(app_grp)

        # Font button (opens the full Label Font dialog)
        self._font_button = _make_font_button(self)
        if self._font_button is not None:
            app_form.addRow("Font", self._font_button)
        else:
            # Fallback: simple font combo + size + color
            self._font_combo = QtWidgets.QFontComboBox(self)
            self._size_spin  = QtWidgets.QDoubleSpinBox(self)
            self._size_spin.setRange(1, 999); self._size_spin.setSuffix(" pt")
            app_form.addRow("Base font", self._font_combo)
            app_form.addRow("Base font size", self._size_spin)
            try:
                from qgis.gui import QgsColorButton
                self._color_btn = QgsColorButton(self)
            except Exception:
                self._color_btn = None
            if self._color_btn:
                app_form.addRow("Base text color", self._color_btn)

        self.h_margin_spin = QtWidgets.QDoubleSpinBox(self)
        self.h_margin_spin.setRange(-200, 200)
        self.h_margin_spin.setDecimals(2)
        self.h_margin_spin.setSuffix(" mm")
        app_form.addRow(_h_margin_form_label(), self.h_margin_spin)

        self.v_margin_spin = QtWidgets.QDoubleSpinBox(self)
        self.v_margin_spin.setRange(-200, 200)
        self.v_margin_spin.setDecimals(2)
        self.v_margin_spin.setSuffix(" mm")
        app_form.addRow(_v_margin_form_label(), self.v_margin_spin)

        self.h_align_combo = QtWidgets.QComboBox(self)
        self.h_align_combo.addItem("Left",    ALIGN_LEFT_)
        self.h_align_combo.addItem("Center",  ALIGN_CENTER_)
        self.h_align_combo.addItem("Right",   ALIGN_RIGHT_)
        if self.SUPPORTS_JUSTIFY:
            self.h_align_combo.addItem("Justify", ALIGN_JUSTIFY_)
        app_form.addRow("Horizontal alignment", self.h_align_combo)

        self._build_extra_ui(app_form)

        outer.addWidget(app_grp)

        # Usage guidance is exposed by the node-edit toolbar tooltip instead.

        if self.DEFER_COMMON_PROPERTIES:
            # Spline-only optimization: reserve the native common-properties
            # location and construct the large QGIS widget after first paint.
            self._common_props_host = QtWidgets.QWidget(self)
            host_layout = QtWidgets.QVBoxLayout(self._common_props_host)
            host_layout.setContentsMargins(0, 0, 0, 0)
            host_layout.setSpacing(0)
            outer.addWidget(self._common_props_host)
            try:
                QtCore.QTimer.singleShot(0, self._build_common_properties_widget)
            except Exception:
                self._build_common_properties_widget()
        else:
            # Preserve the original synchronous construction path for polygon
            # text. QGIS expects this widget to be directly parented here when
            # it installs the custom item panel.
            try:
                from qgis.gui import QgsLayoutItemPropertiesWidget
                self._common_props_widget = QgsLayoutItemPropertiesWidget(
                    self, self.item)
                try:
                    self._common_props_widget.setMasterLayout(
                        self.item.layout())
                except Exception:
                    record_suppressed_exception()
                outer.addWidget(self._common_props_widget)
                self._configure_common_properties_widget()
            except Exception as exc:
                self._common_props_widget = None
                from qgis.core import QgsMessageLog, Qgis
                QgsMessageLog.logMessage(
                    f"Could not embed QgsLayoutItemPropertiesWidget: {exc}",
                    "Curved/Polygon Text", Qgis.MessageLevel.Warning)

        outer.addStretch(1)

        # ── Signals ───────────────────────────────────────────────────
        self.text_edit.textChanged.connect(self._on_text_changed)
        self.html_checkbox.toggled.connect(self._on_html_toggled)
        insert_btn.clicked.connect(self._on_insert_expression)
        self.h_margin_spin.valueChanged.connect(self._on_h_margin_changed)
        self.v_margin_spin.valueChanged.connect(self._on_v_margin_changed)
        self.h_align_combo.currentIndexChanged.connect(self._on_h_align_changed)

        if self._font_button is not None:
            self._font_button.changed.connect(self._on_font_format_changed)
            # Observe the release immediately before QgsFontButton enters its
            # modal dialog.
            # Its clicked signal may not reach us until that dialog has closed.
            self._font_button.installEventFilter(self)
        else:
            if hasattr(self, "_font_combo"):
                self._font_combo.currentFontChanged.connect(self._on_fallback_font_changed)
                self._size_spin.valueChanged.connect(self._on_fallback_font_changed)
            if hasattr(self, "_color_btn") and self._color_btn:
                self._color_btn.colorChanged.connect(self._on_fallback_color_changed)

    def _build_common_properties_widget(self):
        """Create QGIS' heavy shared properties panel once, after first paint."""
        if self._common_props_widget is not None or self.item is None:
            return
        try:
            from qgis.gui import QgsLayoutItemPropertiesWidget
            self._common_props_widget = QgsLayoutItemPropertiesWidget(
                self._common_props_host, self.item)
            try:
                self._common_props_widget.setMasterLayout(self.item.layout())
            except Exception:
                record_suppressed_exception()
            self._common_props_host.layout().addWidget(
                self._common_props_widget)
            self._configure_common_properties_widget()
        except Exception as exc:
            self._common_props_widget = None
            from qgis.core import QgsMessageLog, Qgis
            QgsMessageLog.logMessage(
                f"Could not embed QgsLayoutItemPropertiesWidget: {exc}",
                "Curved/Polygon Text", Qgis.MessageLevel.Warning)

    def _build_extra_ui(self, form):
        """Subclasses add their own Appearance rows here."""

    def _usage_hint_text(self):
        return ""

    def _configure_common_properties_widget(self):
        """Hide native sections which are not exposed in version 1.0.0."""
        self._hide_common_property_sections("Rendering")

    def _hide_common_property_sections(self, *titles):
        """Remove named collapsible groups from the embedded QGIS widget."""
        if self._common_props_widget is None:
            return
        hidden_titles = {str(title).strip().lower() for title in titles}

        def hide_sections():
            try:
                groups = self._common_props_widget.findChildren(
                    QtWidgets.QWidget)
            except Exception:
                groups = []
            for group in groups:
                try:
                    title_fn = getattr(group, "title", None)
                    title = title_fn() if callable(title_fn) else ""
                    if str(title).strip().lower() in hidden_titles:
                        group.hide()
                except Exception:
                    record_suppressed_exception()

        hide_sections()
        delays = (0,) if self.DEFER_COMMON_PROPERTIES else (0, 50, 150)
        for delay in delays:
            try:
                QtCore.QTimer.singleShot(delay, hide_sections)
            except Exception:
                record_suppressed_exception()

    # ---------------------------------------------------------------- load
    def _load_from_item(self):
        if self.item is None:
            return
        if not self.DEFER_COMMON_PROPERTIES:
            # Exact pre-optimization polygon loading path. Keep this separate
            # from spline's signal-blocked/cached property initialization.
            self._loading = True
            try:
                self.text_edit.setPlainText(self.item.text())
                self.html_checkbox.setChecked(self.item.allowHtml())

                if self._font_button is not None:
                    self._font_button.setTextFormat(self.item.textFormat())
                else:
                    font, color = self._fallback_font_color()
                    if hasattr(self, "_font_combo"):
                        self._font_combo.setCurrentFont(font)
                        self._size_spin.setValue(
                            font.pointSizeF() if font.pointSizeF() > 0 else 10.0)
                    if hasattr(self, "_color_btn") and self._color_btn:
                        self._color_btn.setColor(color)

                self.h_margin_spin.setValue(self.item.hMargin())
                self.v_margin_spin.setValue(self.item.vMargin())
                idx = self.h_align_combo.findData(
                    self.item.horizontalAlignment())
                self.h_align_combo.setCurrentIndex(max(0, idx))
                self._load_extra_from_item()
            finally:
                self._loading = False
            _schedule_scrollbars_to_top(self)
            return

        self._loading = True
        blockers = []
        try:
            for widget in (
                self.text_edit, self.html_checkbox, self._font_button,
                self.h_margin_spin, self.v_margin_spin, self.h_align_combo,
            ):
                if widget is not None:
                    try:
                        blockers.append(QtCore.QSignalBlocker(widget))
                    except Exception:
                        record_suppressed_exception()
            self.text_edit.setPlainText(self.item.text())
            self.html_checkbox.setChecked(self.item.allowHtml())

            if self._font_button is not None:
                item_format = self.item.textFormat()
                format_changed = True
                if self._loaded_text_format is not None:
                    try:
                        format_changed = item_format != self._loaded_text_format
                    except Exception:
                        format_changed = True
                if format_changed:
                    self._font_button.setTextFormat(item_format)
                    self._loaded_text_format = QgsTextFormat(item_format)
            else:
                font, color = self._fallback_font_color()
                if hasattr(self, "_font_combo"):
                    self._font_combo.setCurrentFont(font)
                    self._size_spin.setValue(
                        font.pointSizeF() if font.pointSizeF() > 0 else 10.0)
                if hasattr(self, "_color_btn") and self._color_btn:
                    self._color_btn.setColor(color)

            self.h_margin_spin.setValue(self.item.hMargin())
            self.v_margin_spin.setValue(self.item.vMargin())
            idx = self.h_align_combo.findData(self.item.horizontalAlignment())
            self.h_align_combo.setCurrentIndex(max(0, idx))

            self._load_extra_from_item()
        finally:
            blockers.clear()
            self._loading = False
        _schedule_scrollbars_to_top(
            self, (0,) if self.DEFER_COMMON_PROPERTIES else (0, 50, 150))

    def _load_extra_from_item(self):
        pass

    def _fallback_font_color(self):
        """Extract font/color for fallback display when QgsFontButton is unavailable."""
        from qgis.PyQt.QtGui import QFont as _QFont
        fmt = self.item.textFormat()
        f = _QFont()
        try:
            f = _QFont(fmt.font())
            f.setPointSizeF(fmt.size() or 10.0)
        except Exception:
            record_suppressed_exception()
        try:
            return f, fmt.color()
        except Exception:
            from ..compat import QColor
            return f, QColor(0, 0, 0)

    def _schedule_font_dialog_scroll_reset(self):
        """Reset the native font dialog once, with one readiness retry."""
        self._font_dialog_reset_token += 1
        token = self._font_dialog_reset_token

        def attempt(allow_retry):
            if token != self._font_dialog_reset_token:
                return
            if self._reset_font_dialog_scrollbars():
                return
            if allow_retry:
                try:
                    QtCore.QTimer.singleShot(60, lambda: attempt(False))
                except Exception:
                    attempt(False)

        try:
            QtCore.QTimer.singleShot(0, lambda: attempt(True))
        except Exception:
            attempt(True)

    def eventFilter(self, watched, event):
        if watched is self._font_button:
            try:
                if event.type() == _MOUSE_BUTTON_RELEASE:
                    self._schedule_font_dialog_scroll_reset()
            except Exception:
                record_suppressed_exception()
        try:
            return super().eventFilter(watched, event)
        except Exception:
            return False

    def _reset_font_dialog_scrollbars(self):
        app = QtWidgets.QApplication.instance()
        if app is None:
            return False
        try:
            owner_window = self.window()
        except Exception:
            owner_window = None

        try:
            modal = app.activeModalWidget()
        except Exception:
            modal = None
        if modal is None:
            return False
        try:
            if modal is owner_window or not modal.isVisible():
                return False
        except Exception:
            return False

        # The lookup is triggered exclusively by a QgsFontButton press, so the
        # active modal child is the native QGIS text-format dialog. Avoid the
        # former repeated scan through every top-level QGIS window.
        _reset_scrollbars_to_top(modal)
        return True

    # ---------------------------------------------------------------- handlers
    def _on_text_changed(self):
        if self._loading or self.item is None: return
        self.item.beginCommand("Change Text")
        self.item.setText(self.text_edit.toPlainText())
        self.item.endCommand()

    def _on_html_toggled(self, checked):
        if self._loading or self.item is None: return
        self.item.beginCommand("Toggle HTML")
        self.item.setAllowHtml(checked)
        # Refresh the font button display without changing its own native
        # Allow HTML formatting flag; that flag is stored separately inside
        # QgsTextFormat.
        if self._font_button is not None:
            self._loading = True
            try:
                fmt = self.item.textFormat()
                self._font_button.setTextFormat(fmt)
            except Exception:
                record_suppressed_exception()
            finally:
                self._loading = False
        self.item.endCommand()

    def _on_font_format_changed(self):
        if self._loading or self.item is None or self._font_button is None: return
        fmt = self._font_button.textFormat()
        # Keep the font dialog's native "Allow HTML formatting" flag intact.
        # It is intentionally separate from the plugin's "Render as HTML"
        # checkbox: Allow HTML keeps the QGIS buffer/background/shadow effects,
        # while Render as HTML is handled as a richer document-like mode.
        self.item.beginCommand("Change Font")
        self.item.setTextFormat(fmt)
        self.item.endCommand()
        try:
            self._loaded_text_format = QgsTextFormat(fmt)
        except Exception:
            self._loaded_text_format = None

    def _on_fallback_font_changed(self, *_):
        if self._loading or self.item is None: return
        if not hasattr(self, "_font_combo"): return
        from qgis.core import QgsTextFormat
        fmt = QgsTextFormat(self.item.textFormat())
        f = self._font_combo.currentFont()
        f.setPointSizeF(self._size_spin.value())
        fmt.setFont(f)
        fmt.setSize(self._size_spin.value())
        self.item.beginCommand("Change Font")
        self.item.setTextFormat(fmt)
        self.item.endCommand()

    def _on_fallback_color_changed(self, color):
        if self._loading or self.item is None: return
        from qgis.core import QgsTextFormat
        fmt = QgsTextFormat(self.item.textFormat())
        fmt.setColor(color)
        self.item.beginCommand("Change Color")
        self.item.setTextFormat(fmt)
        self.item.endCommand()

    def _on_h_margin_changed(self, v):
        if self._loading or self.item is None: return
        self.item.beginCommand("Change Horizontal Margin")
        self.item.setHMargin(v)
        self.item.endCommand()

    def _on_v_margin_changed(self, v):
        if self._loading or self.item is None: return
        self.item.beginCommand("Change Vertical Margin")
        self.item.setVMargin(v)
        self.item.endCommand()

    def _on_h_align_changed(self, _):
        if self._loading or self.item is None: return
        self.item.beginCommand("Change Horizontal Alignment")
        self.item.setHorizontalAlignment(self.h_align_combo.currentData())
        self.item.endCommand()

    def _on_insert_expression(self):
        if self.item is None: return
        try:
            from qgis.gui import QgsExpressionBuilderDialog
        except Exception:
            return
        ctx = self.item.createExpressionContext()
        dlg = QgsExpressionBuilderDialog(None, "", self, "generic", ctx)
        dlg.setWindowTitle("Insert Expression")
        accepted = dlg.exec()
        if accepted:
            cur = self.text_edit.textCursor()
            cur.insertText(f"[% {dlg.expressionText()} %]")
            self.text_edit.setTextCursor(cur)


# ======================================================================
class SplineTextPropertiesWidget(_BaseTextPropertiesWidget):
    ITEM_CLASS = LayoutItemSplineText
    SUPPORTS_JUSTIFY = False
    DEFER_COMMON_PROPERTIES = True

    def _build_extra_ui(self, form):
        pass

    def _load_extra_from_item(self):
        pass

    def _configure_common_properties_widget(self):
        self._hide_common_property_sections("Background", "Rendering")

    def _usage_hint_text(self):
        return ""

# ======================================================================
class PolygonTextPropertiesWidget(_BaseTextPropertiesWidget):
    ITEM_CLASS = LayoutItemPolygonText

    def _build_extra_ui(self, form):
        self.v_align_combo = QtWidgets.QComboBox(self)
        self.v_align_combo.addItem("Top",    LayoutItemPolygonText.VALIGN_TOP_)
        self.v_align_combo.addItem("Middle", LayoutItemPolygonText.VALIGN_MIDDLE_)
        self.v_align_combo.addItem("Bottom", LayoutItemPolygonText.VALIGN_BOTTOM_)
        form.addRow("Vertical alignment", self.v_align_combo)

        self.padding_spin = QtWidgets.QDoubleSpinBox(self)
        self.padding_spin.setRange(0, 100)
        self.padding_spin.setDecimals(1)
        self.padding_spin.setSuffix(" mm")
        form.addRow("Inner padding", self.padding_spin)

        self.v_align_combo.currentIndexChanged.connect(self._on_v_align_changed)
        self.padding_spin.valueChanged.connect(self._on_padding_changed)

    def _load_extra_from_item(self):
        idx = self.v_align_combo.findData(self.item.verticalAlignment())
        self.v_align_combo.setCurrentIndex(max(0, idx))
        self.padding_spin.setValue(self.item.padding())

    def _usage_hint_text(self):
        return ""

    def _on_v_align_changed(self, _):
        if self._loading or self.item is None: return
        self.item.beginCommand("Change Vertical Alignment")
        self.item.setVerticalAlignment(self.v_align_combo.currentData())
        self.item.endCommand()

    def _on_padding_changed(self, v):
        if self._loading or self.item is None: return
        self.item.beginCommand("Change Padding")
        self.item.setPadding(v)
        self.item.endCommand()
