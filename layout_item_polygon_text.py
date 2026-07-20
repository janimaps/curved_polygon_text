"""
layout_item_polygon_text.py — Polygon Shaped Text Box layout item.
"""


import hashlib

from qgis.core import (
    QgsLayoutItem, QgsLayoutItemRegistry, QgsReadWriteContext,
    QgsTextFormat, QgsLayoutMeasurement, Qgis,
    QgsTextDocument, QgsTextDocumentMetrics, QgsTextBlock,
    QgsTextFragment, QgsTextCharacterFormat,
)

from .compat import (
    QtGui, QtCore, QtWidgets, QPointF, QRectF, QPolygonF, QFont, QColor, QPen, QBrush,
    QTextOption,
    AA_ANTIALIASING, AA_TEXT_ANTIALIASING, NO_BRUSH, NO_PEN,
)
from .text_engine import (
    evaluate_expressions, extract_segments, extract_qgis_html_segments,
    segments_to_plain_and_formats,
    polygon_scanline_spans, widest_span, point_segment_distance,
    render_font, strip_html, apply_capitalization, resolve_qgs_halign,
    text_format_allows_html, segments_slice_to_html,
)
from .icons import polygon_icon
from .keep_alive import keep_alive
from .reliability import record_suppressed_exception

from html import escape as _html_escape

POLYGON_TEXT_ITEM_TYPE = QgsLayoutItemRegistry.ItemType.PluginItem + 1002


def _is_item_selection_change(change):
    """Return True for Qt 5/6 selection change notifications."""
    owners = [QtWidgets.QGraphicsItem]
    scoped = getattr(QtWidgets.QGraphicsItem, "GraphicsItemChange", None)
    if scoped is not None:
        owners.append(scoped)
    for owner in owners:
        for name in ("ItemSelectedChange", "ItemSelectedHasChanged"):
            try:
                if change == getattr(owner, name):
                    return True
            except Exception:
                record_suppressed_exception()
    try:
        return "selected" in str(change).lower()
    except Exception:
        return False


def _is_layout_preview_render(item):
    """Return True only while QGIS is painting the Layout Designer view."""
    try:
        layout = item.layout()
        return bool(layout and layout.renderContext().isPreviewRender())
    except Exception:
        return False


# ------------------------------------------------------------------ helpers
def _make_default_text_format():
    tf = QgsTextFormat()
    tf.setFont(QFont())
    tf.setSize(10.0)
    try:
        from qgis.core import QgsUnitTypes
        tf.setSizeUnit(QgsUnitTypes.RenderUnit.RenderPoints)
    except Exception:
        try:
            from qgis.core import Qgis
            tf.setSizeUnit(Qgis.RenderUnit.Points)
        except Exception:
            record_suppressed_exception()
    tf.setColor(QColor(20, 20, 20))
    return tf


def _apply_design_metrics_to_layout(layout):
    """Prefer design metrics so wrapping stays stable across zoom."""
    try:
        opt = QTextOption()
        wrap = getattr(QTextOption, "WrapAtWordBoundaryOrAnywhere", None)
        if wrap is not None:
            try:
                opt.setWrapMode(wrap)
            except Exception:
                record_suppressed_exception()
        if hasattr(opt, "setUseDesignMetrics"):
            try:
                opt.setUseDesignMetrics(True)
            except Exception:
                record_suppressed_exception()
        if hasattr(layout, "setTextOption"):
            layout.setTextOption(opt)
    except Exception:
        record_suppressed_exception()


# -------------------------------------------------------- text format XML
_TEXT_FORMAT_XML_TAGS = (
    "QgsTextFormat",
    "text-format",
    "textFormat",
    "text-style",
    "textStyle",
    "textformat",
)


def _readwrite_context(context):
    """Use QGIS' active read/write context when available."""
    return context if context is not None else QgsReadWriteContext()


def _text_format_signature(text_format, context=None):
    """Return a stable in-memory signature for QgsTextFormat changes."""
    try:
        from .compat import QtXml
    except Exception:
        QtXml = None

    if QtXml is None:
        try:
            font = text_format.font()
            return (
                font.family(), float(font.pointSizeF()), float(font.pixelSize()),
                bool(font.bold()), bool(font.italic()), bool(font.underline()),
                bool(font.strikeOut()), bool(font.overline()),
                float(text_format.size()) if hasattr(text_format, 'size') else 0.0,
                bool(text_format.allowHtmlFormatting())
                if hasattr(text_format, 'allowHtmlFormatting') else False,
            )
        except Exception:
            return repr(text_format)

    try:
        doc = QtXml.QDomDocument("fmt_sig")
        root = doc.createElement("sig")
        doc.appendChild(root)
        _append_text_format_to_element(
            root, doc, context, text_format, "sigFmt")
        return hashlib.blake2b(
            doc.toString().encode("utf-8"), digest_size=16).digest()
    except Exception:
        return repr(text_format)


def _append_text_format_to_element(element, document, context, text_format,
                                   wrapper_tag):
    """Persist the full QgsTextFormat under a stable plugin-owned wrapper.

    QgsTextFormat.writeXml() has used different element names across QGIS
    versions/builds. Wrapping the returned QGIS element gives the plugin a
    stable place to read from while still letting QGIS serialize all
    buffer/background/shadow/effect details.
    """
    try:
        fmt_elem = text_format.writeXml(document, _readwrite_context(context))
    except TypeError:
        # Some bindings are stricter about the context type.
        fmt_elem = text_format.writeXml(document, QgsReadWriteContext())
    except Exception:
        return

    try:
        if fmt_elem.isNull():
            return
    except Exception:
        record_suppressed_exception()

    try:
        wrapper = document.createElement(wrapper_tag)
        wrapper.appendChild(fmt_elem)
        element.appendChild(wrapper)
    except Exception:
        try:
            element.appendChild(fmt_elem)
        except Exception:
            record_suppressed_exception()


def _text_format_xml_candidates(element, wrapper_tags):
    """Return possible QgsTextFormat XML elements, newest to oldest."""
    candidates = []

    def add(candidate):
        try:
            if candidate.isNull():
                return
        except Exception:
            record_suppressed_exception()
        candidates.append(candidate)

    # New stable wrapper written by this plugin.
    for wrapper_tag in wrapper_tags:
        try:
            wrapper = element.firstChildElement(wrapper_tag)
            if not wrapper.isNull():
                child = wrapper.firstChildElement()
                if not child.isNull():
                    add(child)
        except Exception:
            record_suppressed_exception()

    # Legacy direct children, including the earlier hard-coded tag and QGIS'
    # own possible QgsTextFormat element names.
    for tag in _TEXT_FORMAT_XML_TAGS:
        try:
            child = element.firstChildElement(tag)
            if not child.isNull():
                add(child)
        except Exception:
            record_suppressed_exception()

    # Last-chance compatibility for layouts copied/saved by older development
    # builds where the QGIS format element was appended directly with a tag name
    # we do not know.
    try:
        child = element.firstChildElement()
        if not child.isNull():
            tag = child.tagName()
            if tag in wrapper_tags:
                nested = child.firstChildElement()
                if not nested.isNull():
                    add(nested)
            else:
                add(child)
    except Exception:
        record_suppressed_exception()

    return candidates


def _read_text_format_from_element(element, context, fallback_format,
                                   wrapper_tags):
    """Restore a full QgsTextFormat from plugin XML."""
    for fmt_elem in _text_format_xml_candidates(element, wrapper_tags):
        try:
            fmt = QgsTextFormat(fallback_format)
        except Exception:
            try:
                fmt = QgsTextFormat()
            except Exception:
                fmt = _make_default_text_format()

        try:
            result = fmt.readXml(fmt_elem, _readwrite_context(context))
            if result is False:
                continue
            if isinstance(result, QgsTextFormat):
                fmt = result
            return fmt
        except TypeError:
            # Defensive support for bindings where readXml is exposed as a
            # static-style constructor.
            try:
                result = QgsTextFormat.readXml(
                    fmt_elem, _readwrite_context(context))
                if isinstance(result, QgsTextFormat):
                    return result
            except Exception:
                record_suppressed_exception()
        except Exception:
            record_suppressed_exception()

    return None



def _format_size_unit(text_format):
    try:
        return text_format.sizeUnit()
    except Exception:
        return None


def _format_size_map_unit_scale(text_format):
    try:
        return text_format.sizeMapUnitScale()
    except Exception:
        return None


def _convert_value_to_painter_units(render_ctx, value, unit=None,
                                    map_unit_scale=None, fallback_scale=1.0):
    try:
        value = float(value)
    except Exception:
        return 0.0

    if render_ctx is not None and unit is not None:
        converter = getattr(render_ctx, "convertToPainterUnits", None)
        if converter is not None:
            try:
                if map_unit_scale is not None:
                    return float(converter(value, unit, map_unit_scale))
                return float(converter(value, unit))
            except TypeError:
                try:
                    return float(converter(value, unit))
                except Exception:
                    record_suppressed_exception()
            except Exception:
                record_suppressed_exception()
    return value * fallback_scale


def _setting_distance_to_painter_units(render_ctx, settings, value_method,
                                       unit_method, map_unit_scale_method,
                                       fallback_scale):
    try:
        value = float(getattr(settings, value_method)())
    except Exception:
        return 0.0

    unit = None
    map_unit_scale = None
    try:
        unit = getattr(settings, unit_method)()
    except Exception:
        record_suppressed_exception()
    try:
        map_unit_scale = getattr(settings, map_unit_scale_method)()
    except Exception:
        record_suppressed_exception()

    return _convert_value_to_painter_units(
        render_ctx, value, unit, map_unit_scale, fallback_scale)


def _setting_size_to_painter_units(render_ctx, settings, value_method,
                                   unit_method, map_unit_scale_method,
                                   fallback_scale):
    try:
        raw = getattr(settings, value_method)()
    except Exception:
        return 0.0, 0.0

    try:
        x_val = raw.width()
        y_val = raw.height()
    except Exception:
        try:
            x_val = raw.x()
            y_val = raw.y()
        except Exception:
            try:
                x_val, y_val = raw
            except Exception:
                return 0.0, 0.0

    unit = None
    map_unit_scale = None
    try:
        unit = getattr(settings, unit_method)()
    except Exception:
        record_suppressed_exception()
    try:
        map_unit_scale = getattr(settings, map_unit_scale_method)()
    except Exception:
        record_suppressed_exception()

    return (
        _convert_value_to_painter_units(
            render_ctx, x_val, unit, map_unit_scale, fallback_scale),
        _convert_value_to_painter_units(
            render_ctx, y_val, unit, map_unit_scale, fallback_scale),
    )


def _text_visual_padding(render_ctx, text_format, scale_factor,
                         font_metrics=None, sample_text="",
                         additional_font_samples=None):
    """Return extra X/Y painter-space padding required by text effects.

    Polygon text is clipped to the user-drawn polygon.  If wrapping is based
    only on the baseline scanline, glyphs near slanted/narrow boundaries can be
    valid at their midpoint but still get clipped at their ascenders,
    descenders, buffers, backgrounds or shadows.  This padding lets the layout
    test reserve the same visual envelope that will be painted.
    """
    sf = scale_factor or 1.0
    antialias_pad = 0.25 * sf
    glyph_overhang_x = antialias_pad
    component_pad_x = 0.0
    component_pad_y = 0.0
    shadow_pad_x = 0.0
    shadow_pad_y = 0.0

    # Advance widths deliberately exclude italic/oblique ink overhang.  Find
    # the largest per-glyph ink excess so light-italic and condensed-italic
    # fonts receive the same safe polygon inset as their actual painted shape.
    metric_samples = []
    if font_metrics is not None:
        metric_samples.append((font_metrics, sample_text))
    metric_samples.extend(additional_font_samples or [])
    for sample_metrics, metric_text in metric_samples:
        try:
            chars = set(str(metric_text or ""))
            chars.update("WMAfgjy.,;()[]")
            for ch in chars:
                if ch in ("\n", "\r", "\u2028"):
                    continue
                bounds = sample_metrics.boundingRect(ch)
                advance = float(sample_metrics.horizontalAdvance(ch))
                left_overhang = max(0.0, -float(bounds.left()))
                right_overhang = max(
                    0.0, float(bounds.right()) - advance)
                glyph_overhang_x = max(
                    glyph_overhang_x, left_overhang, right_overhang)
        except Exception:
            record_suppressed_exception()

    try:
        buf = text_format.buffer()
        if buf.enabled():
            bsz = _setting_distance_to_painter_units(
                render_ctx, buf, "size", "sizeUnit",
                "sizeMapUnitScale", scale_factor)
            component_pad_x = max(component_pad_x, abs(bsz))
            component_pad_y = max(component_pad_y, abs(bsz))
    except Exception:
        record_suppressed_exception()

    try:
        bg = text_format.background()
        if bg.enabled():
            sx, sy = _setting_size_to_painter_units(
                render_ctx, bg, "size", "sizeUnit",
                "sizeMapUnitScale", scale_factor)
            ox, oy = _setting_size_to_painter_units(
                render_ctx, bg, "offset", "offsetUnit",
                "offsetMapUnitScale", scale_factor)
            sw = _setting_distance_to_painter_units(
                render_ctx, bg, "strokeWidth", "strokeWidthUnit",
                "strokeWidthMapUnitScale", scale_factor)
            component_pad_x = max(
                component_pad_x, abs(sx) + abs(ox) + abs(sw))
            component_pad_y = max(
                component_pad_y, abs(sy) + abs(oy) + abs(sw))
    except Exception:
        record_suppressed_exception()

    try:
        shd = text_format.shadow()
        if shd.enabled():
            d = _setting_distance_to_painter_units(
                render_ctx, shd, "offsetDistance", "offsetUnit",
                "offsetMapUnitScale", scale_factor)
            b = _setting_distance_to_painter_units(
                render_ctx, shd, "blurRadius", "blurRadiusUnit",
                "blurRadiusMapUnitScale", scale_factor)
            shadow_pad_x = max(shadow_pad_x, abs(d) + abs(b))
            shadow_pad_y = max(shadow_pad_y, abs(d) + abs(b))
    except Exception:
        record_suppressed_exception()

    return (
        glyph_overhang_x + component_pad_x + shadow_pad_x,
        antialias_pad + component_pad_y + shadow_pad_y,
    )


def _polygon_clip_path(qpoly):
    path = QtGui.QPainterPath()
    path.addPolygon(qpoly)
    path.closeSubpath()
    return path


def _frame_width_in_painter_units(item, scale_factor):
    """Return the native Layout Frame stroke width in painter units."""
    try:
        width = item.layout().convertToLayoutUnits(item.frameStrokeWidth())
    except Exception:
        try:
            width = item.frameStrokeWidth().length()
        except Exception:
            width = 0.0
    return max(0.0, float(width) * float(scale_factor or 1.0))



def _line_safe_span(points, y_top, y_bottom, pad_px, hm_px, extra_x=0.0):
    """Return the widest horizontal span that stays inside the polygon.

    The renderer samples several scanlines across the full painted line height
    and intersects them to avoid choosing a span that is valid only at the
    baseline but clips at ascenders/descenders.  The span is measured in the
    same painter units as the supplied points.
    """
    if y_bottom < y_top:
        y_top, y_bottom = y_bottom, y_top

    bbox = QPolygonF(points).boundingRect()
    eps = 0.01
    y_top = max(bbox.top() + eps, y_top)
    y_bottom = min(bbox.bottom() - eps, y_bottom)
    if y_bottom < y_top:
        return None

    if y_bottom - y_top <= eps:
        sample_ys = [y_top]
    else:
        sample_count = 5
        sample_ys = [
            y_top + (y_bottom - y_top) * i / (sample_count - 1)
            for i in range(sample_count)
        ]

        # A fixed set of scanlines can miss a polygon vertex between samples.
        # That is normally hidden by the unused space on left/center/right
        # aligned rows, but justified rows and their backgrounds occupy the
        # complete calculated span.  Sample every vertex inside the painted
        # band, plus a tiny point on either side, so the returned intersection
        # reflects the true narrowest span throughout the row height.
        vertex_eps = max(eps, (y_bottom - y_top) * 1.0e-6)
        for point in points:
            vertex_y = float(point.y())
            if y_top < vertex_y < y_bottom:
                sample_ys.extend((
                    max(y_top, vertex_y - vertex_eps),
                    vertex_y,
                    min(y_bottom, vertex_y + vertex_eps),
                ))
        sample_ys = sorted(set(sample_ys))

    current = None
    for y in sample_ys:
        spans = []
        for left, right in polygon_scanline_spans(points, y):
            left = left + pad_px + hm_px + extra_x
            right = right - pad_px - hm_px - extra_x
            if right - left > 1.0:
                spans.append((left, right))

        if not spans:
            return None

        if current is None:
            current = max(spans, key=lambda s: s[1] - s[0])
            continue

        overlaps = []
        for left, right in spans:
            il = max(current[0], left)
            ir = min(current[1], right)
            if ir - il > 1.0:
                overlaps.append((il, ir))
        if not overlaps:
            return None
        current = max(overlaps, key=lambda s: s[1] - s[0])

    return current

def _copy_text_format_without_effects(text_format):
    """Clone the Text/Formatting tabs without the excluded effect tabs.

    Used by the polygon item's explicit Render as HTML mode, which should
    retain the core font and formatting controls, including opacity, but not
    the buffer/background/shadow component stack.
    """
    try:
        fmt = QgsTextFormat(text_format)
    except Exception:
        return text_format

    for getter_name, setter_name in (
        ("buffer", "setBuffer"),
        ("mask", "setMask"),
        ("background", "setBackground"),
        ("shadow", "setShadow"),
    ):
        try:
            component = getattr(fmt, getter_name)()
            try:
                neutral_component = type(component)()
            except Exception:
                neutral_component = component
            if hasattr(neutral_component, "setEnabled"):
                try:
                    neutral_component.setEnabled(False)
                except Exception:
                    record_suppressed_exception()
            setter = getattr(fmt, setter_name, None)
            if callable(setter):
                try:
                    setter(neutral_component)
                except Exception:
                    record_suppressed_exception()
        except Exception:
            record_suppressed_exception()
    return fmt


def _copy_text_format_without_background_shadow(text_format):
    """Clone a format while leaving row text/buffer effects intact.

    Polygon rows are rendered separately to follow changing scan-line spans.
    Background and its shadow are instead rendered once for the composed text
    block, preventing one independent background shape per wrapped row.
    """
    try:
        fmt = QgsTextFormat(text_format)
    except Exception:
        return text_format

    for getter_name, setter_name in (
        ("background", "setBackground"),
        ("shadow", "setShadow"),
    ):
        try:
            component = getattr(fmt, getter_name)()
            try:
                neutral_component = type(component)()
            except Exception:
                neutral_component = component
            if hasattr(neutral_component, "setEnabled"):
                neutral_component.setEnabled(False)
            setter = getattr(fmt, setter_name, None)
            if callable(setter):
                setter(neutral_component)
        except Exception:
            record_suppressed_exception()
    return fmt



def _clip_safe_text_rect(lx, ly, lw, lh):
    """Return a draw rectangle with a little extra room on the right/bottom.

    The polygon clip path is applied separately by the caller.  We avoid
    shifting the origin upward or leftward so top/left alignment remains
    visually pinned, and only expand the paint rectangle where a small amount
    of slack helps preserve glyph overhangs and HTML effects.
    """
    lw = float(lw)
    lh = float(lh)
    # Keep only a very small cushion for antialiasing.  The layout itself
    # is made conservative enough to avoid border touching/chopping, so the
    # paint rect should not add extra right-side room that could reintroduce
    # edge overflow.
    right_pad  = max(1.0, 0.01 * lw)
    bottom_pad = max(1.0, 0.02 * lh)
    try:
        return QRectF(lx, ly, lw + right_pad, lh + bottom_pad)
    except Exception:
        try:
            from qgis.PyQt.QtCore import QRectF as _QRectF
            return _QRectF(lx, ly, lw + right_pad, lh + bottom_pad)
        except Exception:
            return None

def _wrap_html_for_justify(html_text, plain_text, target_width_px, font,
                           measured_width_px=None,
                           css_spacing_factor=1.0,
                           css_spacing_unit="px",
                           edge_allowance_ratio=0.0):
    """Wrap a line of HTML with CSS word-spacing so it visually justifies."""
    try:
        plain = (plain_text or "").strip()
    except Exception:
        plain = ""
    if not plain or " " not in plain:
        return html_text, False

    if measured_width_px is not None:
        try:
            base_width = float(measured_width_px)
        except Exception:
            return html_text, False
    else:
        try:
            fm = QtGui.QFontMetricsF(font)
            base_width = float(fm.horizontalAdvance(plain))
        except Exception:
            return html_text, False

    # The target is already the safe scan-line span used by left and right
    # alignment. Glyph overhang and antialiasing are accounted for when that
    # span is composed, so justification must use the complete width here.
    target_width = float(target_width_px)
    edge_allowance = (
        max(0.0, float(edge_allowance_ratio)) * target_width)
    safe_target_width = max(0.0, target_width - edge_allowance)
    extra = safe_target_width - base_width
    if extra <= 0.5:
        return html_text, False

    gap_count = plain.count(" ")
    if gap_count <= 0:
        return html_text, False

    word_spacing = max(0.0, extra / gap_count)
    if word_spacing <= 0.01:
        return html_text, False

    try:
        css_word_spacing = word_spacing * float(css_spacing_factor)
    except Exception:
        css_word_spacing = word_spacing

    wrapped = (
        f'<div style="word-spacing:{css_word_spacing:.5f}{css_spacing_unit}; '
        f'text-align:justify; white-space:pre-wrap;">'
        f'{html_text}'
        f'</div>'
    )
    return wrapped, True


class LayoutItemPolygonText(QgsLayoutItem):

    ALIGN_LEFT_, ALIGN_CENTER_, ALIGN_RIGHT_, ALIGN_JUSTIFY_ = range(4)
    VALIGN_TOP_, VALIGN_MIDDLE_, VALIGN_BOTTOM_ = range(3)

    def __init__(self, layout):
        super().__init__(layout)
        self._text = (
            "Polygon shaped text box. Activate the \u201cEdit Curve/Polygon "
            "Nodes\u201d tool, then drag a corner to warp this paragraph into "
            "any shape \u2014 banners, shields, speech bubbles, you name it."
        )
        self._text_format = _make_default_text_format()
        # Monotonic cache token.  Serializing QgsTextFormat to XML on every
        # paint was comparatively expensive and allocated several temporary
        # DOM objects even when the format had not changed.
        self._text_format_revision = 0
        self._allow_html  = False  # Render as HTML checkbox state
        self._padding    = 0.0   # mm, uniform polygon inset; default matches native no-padding behavior
        self._h_margin   = 0.0   # mm, extra left margin applied per scan-line
        self._v_margin   = 0.0   # mm, extra top margin before text starts
        self._h_align    = self.ALIGN_LEFT_
        self._v_align    = self.VALIGN_TOP_
        # Repurpose QGIS' native Layout Frame controls for the polygon path.
        self.setFrameEnabled(True)
        self.setFrameStrokeColor(QColor(150, 150, 150))
        self.setFrameStrokeWidth(QgsLayoutMeasurement(0.2))
        self._nodes = [
            QPointF(0.0, 0.0), QPointF(1.0, 0.0),
            QPointF(1.0, 1.0), QPointF(0.0, 1.0),
        ]
        # Transient cache for the layout plan.  The cache key excludes zoom so
        # line breaks stay pinned once composed.
        self._layout_cache_key = None
        self._layout_cache = None

    # ---------------------------------------------------------------- identity
    def type(self):        return POLYGON_TEXT_ITEM_TYPE
    def icon(self):        return polygon_icon()
    def displayName(self): return "Polygon Text"

    def estimatedFrameBleed(self):
        """Include the edit-time node circles in the item's paint bounds."""
        try:
            inherited = float(super().estimatedFrameBleed())
        except Exception:
            inherited = 0.0
        return max(inherited, 1.65)

    def boundingRect(self):
        """Report the complete edit-handle area to QGraphicsScene.

        QgsLayoutItem's C++ bounding rectangle can remain limited to rect()
        for this custom Python item even when estimatedFrameBleed() is
        overridden.  Explicitly uniting both rectangles prevents the scene's
        system clip from cutting boundary-node circles in half.
        """
        try:
            bounds = QRectF(super().boundingRect())
        except Exception:
            bounds = QRectF(self.rect())
        handle_bounds = QRectF(self.rect())
        handle_bounds.adjust(-1.65, -1.65, 1.65, 1.65)
        return bounds.united(handle_bounds)

    def shape(self):
        """Use the real polygon for initial selection.

        Once selected, QGIS' normal rectangular item shape is restored so its
        standard move, resize and rotation handles retain their full behavior.
        """
        if self.isSelected():
            return super().shape()

        rect = self.rect()
        points = QPolygonF([
            QPointF(node.x() * rect.width(), node.y() * rect.height())
            for node in self._nodes
        ])
        if len(points) < 3:
            return super().shape()

        path = QtGui.QPainterPath()
        path.addPolygon(points)
        path.closeSubpath()
        return path

    def _request_selection_repaint(self):
        """Clear complete node handles immediately after selection changes."""
        try:
            self.update()
        except Exception:
            record_suppressed_exception()
        try:
            scene = self.scene()
        except Exception:
            scene = None
        if scene is None:
            return
        try:
            scene.update()
        except Exception:
            record_suppressed_exception()
        try:
            for view in scene.views():
                view.viewport().update()
        except Exception:
            record_suppressed_exception()
        try:
            QtCore.QTimer.singleShot(0, scene.update)
        except Exception:
            record_suppressed_exception()

    def itemChange(self, change, value):
        selection_change = _is_item_selection_change(change)
        if selection_change:
            self._request_selection_repaint()
        try:
            result = super().itemChange(change, value)
        except Exception:
            result = value
        if selection_change:
            self._request_selection_repaint()
        return result

    # --------------------------------------------------------- node access
    def nodeScenePositions(self):
        rect = self.rect()
        return [
            self.mapToScene(QPointF(n.x()*rect.width(), n.y()*rect.height()))
            for n in self._nodes
        ]

    def setNodeAtScenePos(self, index, scene_pos):
        if not (0 <= index < len(self._nodes)):
            return
        # Get ALL nodes in scene coordinates, update the dragged one.
        all_scene = self.nodeScenePositions()
        all_scene[index] = scene_pos
        # Compute the bounding rect of all new scene positions.
        xs = [p.x() for p in all_scene]
        ys = [p.y() for p in all_scene]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        new_w = max(max_x - min_x, 5.0)   # 5 mm minimum
        new_h = max(max_y - min_y, 5.0)
        # Resize the item to fit the new node positions.
        self.attemptSetSceneRect(QRectF(min_x, min_y, new_w, new_h))
        # Compute normalised fractions directly from the requested bounds
        # (not from self.rect(), which may not yet reflect the resize).
        self._nodes = [
            QPointF(
                min(max((p.x() - min_x) / new_w, 0.0), 1.0),
                min(max((p.y() - min_y) / new_h, 0.0), 1.0),
            )
            for p in all_scene
        ]
        self.update()

    def insertNodeNearestEdge(self, scene_pos):
        rect = self.rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return
        local = self.mapFromScene(scene_pos)
        new_pt = QPointF(
            min(max(local.x()/rect.width(),  0.0), 1.0),
            min(max(local.y()/rect.height(), 0.0), 1.0),
        )
        n = len(self._nodes)
        best_i, best_d = 1, None
        for i in range(n):
            d = point_segment_distance(
                new_pt, self._nodes[i], self._nodes[(i+1) % n])
            if best_d is None or d < best_d:
                best_d, best_i = d, i+1
        self._nodes.insert(best_i % (n+1), new_pt)
        self.update()

    def removeNodeAt(self, index):
        if len(self._nodes) <= 3:
            return False
        if 0 <= index < len(self._nodes):
            # Work in scene coordinates so shrinking the item extent cannot
            # move or rescale any of the surviving polygon vertices.
            remaining_scene = self.nodeScenePositions()
            del remaining_scene[index]

            xs = [point.x() for point in remaining_scene]
            ys = [point.y() for point in remaining_scene]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            new_w = max(max_x - min_x, 5.0)
            new_h = max(max_y - min_y, 5.0)

            self.attemptSetSceneRect(QRectF(min_x, min_y, new_w, new_h))
            self._nodes = [
                QPointF(
                    min(max((point.x() - min_x) / new_w, 0.0), 1.0),
                    min(max((point.y() - min_y) / new_h, 0.0), 1.0),
                )
                for point in remaining_scene
            ]
            self._request_selection_repaint()
            return True
        return False

    def setNodesFromScenePoints(self, scene_points):
        rect = self.rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return
        nodes = []
        for p in scene_points:
            local = self.mapFromScene(p)
            nodes.append(QPointF(
                min(max(local.x()/rect.width(),  0.0), 1.0),
                min(max(local.y()/rect.height(), 0.0), 1.0),
            ))
        if len(nodes) >= 3:
            self._nodes = nodes
            self.update()

    def setNodesFromSceneBounds(self, scene_points, scene_rect):
        if scene_rect.width() <= 0 or scene_rect.height() <= 0:
            return
        nodes = []
        for p in scene_points:
            nodes.append(QPointF(
                min(max((p.x()-scene_rect.left())/scene_rect.width(),  0.0), 1.0),
                min(max((p.y()-scene_rect.top())/scene_rect.height(), 0.0), 1.0),
            ))
        if len(nodes) >= 3:
            self._nodes = nodes
            self.update()

    # --------------------------------------------------------- properties
    def text(self):             return self._text
    def setText(self, v):       self._text = v or ""; self.update()

    def allowHtml(self):
        return self._allow_html

    def setAllowHtml(self, v):
        # Plugin-level "Render as HTML" mode.  Keep it separate from the
        # QgsTextFormat/native Font dialog "Allow HTML formatting" flag.
        new_value = bool(v)
        if new_value != self._allow_html:
            self._layout_cache_key = None
            self._layout_cache = None
        self._allow_html = new_value
        self.update()

    def textFormat(self):
        return QgsTextFormat(self._text_format)
    def setTextFormat(self, fmt):
        self._text_format = QgsTextFormat(fmt)
        self._text_format_revision += 1
        self.update()

    def padding(self):          return self._padding
    def setPadding(self, v):    self._padding = max(0.0, float(v)); self.update()

    def hMargin(self):          return self._h_margin
    def setHMargin(self, v):    self._h_margin = float(v); self.update()

    def vMargin(self):          return self._v_margin
    def setVMargin(self, v):    self._v_margin = float(v); self.update()

    def horizontalAlignment(self):      return self._h_align
    def setHorizontalAlignment(self, v): self._h_align = v; self.update()

    def verticalAlignment(self):        return self._v_align
    def setVerticalAlignment(self, v):  self._v_align = v; self.update()


    def _base_font_and_color(self):
        """Return (QFont with point size, QColor) from the stored text format."""
        f = QFont(self._text_format.font())
        size = self._text_format.size()
        if size > 0:
            f.setPointSizeF(size)
        elif f.pointSizeF() <= 0 and f.pixelSize() <= 0:
            f.setPointSizeF(10.0)
        try:
            if self._text_format.forcedBold():
                f.setBold(True)
        except Exception:
            record_suppressed_exception()
        try:
            if self._text_format.forcedItalic():
                f.setItalic(True)
        except Exception:
            record_suppressed_exception()
        return f, QColor(self._text_format.color())



    def _layout_signature(self, resolved_text, render_html=False, inline_html=False):
        """Build a zoom-independent signature for the current polygon text plan."""
        rect = self.rect()

        def _safe_float(v, nd=6):
            try:
                return round(float(v), nd)
            except Exception:
                return 0.0

        nodes_sig = tuple((_safe_float(n.x(), 6), _safe_float(n.y(), 6)) for n in self._nodes)
        text_sig = hashlib.blake2b(
            (resolved_text or "").encode("utf-8"),
            digest_size=16).digest()

        if render_html:
            return (
                "render_html", _safe_float(rect.width(), 6),
                _safe_float(rect.height(), 6), nodes_sig, text_sig,
                int(self._h_align), int(self._v_align),
                _safe_float(self._padding, 6),
                _safe_float(self._h_margin, 6),
                _safe_float(self._v_margin, 6),
                self._text_format_revision,
            )

        sig = (
            _safe_float(rect.width(), 6),
            _safe_float(rect.height(), 6),
            nodes_sig,
            text_sig,
            bool(render_html),
            bool(inline_html),
            int(self._h_align),
            int(self._v_align),
            _safe_float(self._padding, 6),
            _safe_float(self._h_margin, 6),
            _safe_float(self._v_margin, 6),
            self._text_format_revision,
        )
        return sig

    # -------------------------------------------------------------- rendering
    # -------------------------------------------------------------- rendering
    def draw(self, context):
        """
        Renders to painter-pixel space using QgsTextRenderer for all text
        effects (buffer, shadow, background, opacity, case transformation,
        HTML). Scan-line polygon wrapping determines per-line position and
        width; QgsTextRenderer renders each line with full effect support.
        """
        painter = context.renderContext().painter()
        if painter is None:
            return
        scale_factor = context.renderContext().scaleFactor() or 1.0
        render_ctx   = context.renderContext()
        is_preview_render = _is_layout_preview_render(self)
        painter.save()
        try:
            painter.setRenderHint(AA_ANTIALIASING, True)
            painter.setRenderHint(AA_TEXT_ANTIALIASING, True)

            rect = self.rect()
            poly_layout = [
                QPointF(n.x() * rect.width(), n.y() * rect.height())
                for n in self._nodes
            ]
            poly_px = [
                QPointF(p.x() * scale_factor, p.y() * scale_factor)
                for p in poly_layout
            ]
            qpoly  = QPolygonF(poly_px)
            # Stored values are layout millimetres; convert them to the same
            # painter coordinate system as the zoom-scaled polygon.
            pad_px = self._padding * scale_factor
            hm_px  = self._h_margin * scale_factor
            vm_px  = self._v_margin * scale_factor

            # ── Background (polygon-shaped, not bounding rect) ────────
            # Painted here instead of in drawBackground() so we can fill
            # the exact polygon shape the user drew rather than the
            # full item bounding rect that QGIS would normally fill.
            try:
                if self.hasBackground():
                    bg_col = self.backgroundColor()
                    painter.save()
                    painter.setBrush(QBrush(bg_col))
                    painter.setPen(QPen(NO_PEN))
                    painter.drawPolygon(qpoly)
                    painter.restore()
            except Exception:
                record_suppressed_exception()

            if self.frameEnabled():
                pen = QPen(
                    self.frameStrokeColor(),
                    _frame_width_in_painter_units(self, scale_factor))
                try:
                    pen.setJoinStyle(self.frameJoinStyle())
                except Exception:
                    record_suppressed_exception()
                painter.setPen(pen)
                painter.setBrush(QBrush(NO_BRUSH))
                painter.drawPolygon(qpoly)

            painter.save()
            try:
                painter.setClipPath(_polygon_clip_path(qpoly))
                resolved = evaluate_expressions(self._text, self)
                if resolved.strip():
                    previous_text_render_format = None
                    outline_format = None
                    try:
                        previous_text_render_format = render_ctx.textRenderFormat()
                    except Exception:
                        record_suppressed_exception()
                    try:
                        scoped = getattr(Qgis, "TextRenderFormat", None)
                        if scoped is not None:
                            outline_format = getattr(scoped, "AlwaysOutlines", None)
                        if outline_format is None:
                            outline_format = getattr(Qgis, "TextFormatAlwaysOutlines", None)
                        if outline_format is not None:
                            render_ctx.setTextRenderFormat(outline_format)
                        self._draw_wrapped_text(
                            render_ctx, poly_px, resolved,
                            pad_px, hm_px, vm_px, scale_factor)
                    finally:
                        if previous_text_render_format is not None:
                            try:
                                render_ctx.setTextRenderFormat(
                                    previous_text_render_format)
                            except Exception:
                                record_suppressed_exception()
            finally:
                painter.restore()

            if is_preview_render and self.isSelected():
                # QgsLayoutItem supplies a clip matching rect(), which cuts
                # boundary-node circles in half even though frame bleed makes
                # their paint bounds visible to the scene.  Lift that clip
                # only for these edit-time handles; polygon/text clipping has
                # already completed above and exports never draw the handles.
                painter.save()
                try:
                    painter.setClipping(False)
                    painter.setPen(QPen(
                        QColor(40, 140, 90), 0.25 * scale_factor))
                    painter.setBrush(QColor(255, 255, 255))
                    r = 1.4 * scale_factor
                    for pt in poly_px:
                        painter.drawEllipse(pt, r, r)
                finally:
                    painter.restore()
        finally:
            painter.restore()

    def _padded_path(self, qpoly, pad_px):
        path = QtGui.QPainterPath()
        if pad_px <= 0 or len(qpoly) < 3:
            path.addPolygon(qpoly)
            path.closeSubpath()
            return path
        centroid = QPointF(
            sum(p.x() for p in qpoly) / len(qpoly),
            sum(p.y() for p in qpoly) / len(qpoly),
        )
        bbox   = qpoly.boundingRect()
        span   = max(bbox.width(), bbox.height(), 1.0)
        shrink = max(0.0, 1.0 - (2 * pad_px / span))
        shrunk = QPolygonF([
            QPointF(centroid.x() + (p.x() - centroid.x()) * shrink,
                     centroid.y() + (p.y() - centroid.y()) * shrink)
            for p in qpoly
        ])
        path.addPolygon(shrunk)
        path.closeSubpath()
        return path



    def _store_polygon_layout_cache(self, cache_key, mode, payload, scale_factor):
        sf = float(scale_factor or 1.0)
        if sf <= 0.0:
            sf = 1.0
        inv = 1.0 / sf
        normalized = []
        for item in payload.get("positioned", []):
            try:
                if len(item) == 4:
                    line_plain, lx, ly, lw = item
                    normalized.append((line_plain, float(lx) * inv, float(ly) * inv, float(lw) * inv))
                elif len(item) == 6:
                    line_plain, lx, ly, lw, paragraph_final, blank = item
                    normalized.append((line_plain, float(lx) * inv, float(ly) * inv,
                                       float(lw) * inv, bool(paragraph_final), bool(blank)))
                elif len(item) == 5:
                    ts, tl, lx, ly, lw = item
                    normalized.append((ts, tl, float(lx) * inv, float(ly) * inv, float(lw) * inv))
                elif len(item) == 7:
                    ts, tl, lx, ly, lw, paragraph_final, blank = item
                    normalized.append((ts, tl, float(lx) * inv, float(ly) * inv,
                                       float(lw) * inv, bool(paragraph_final), bool(blank)))
                elif len(item) == 8:
                    ts, tl, lx, ly, lw, paragraph_final, blank, natural_width = item
                    normalized.append((
                        ts, tl, float(lx) * inv, float(ly) * inv,
                        float(lw) * inv, bool(paragraph_final), bool(blank),
                        float(natural_width) * inv))
                else:
                    normalized.append(tuple(item))
            except Exception:
                normalized.append(tuple(item))
        cache = {
            "mode": mode,
            "lh": float(payload.get("lh", 0.0)) * inv,
            "html_segments": payload.get("html_segments", []),
            "html_plain": payload.get("html_plain", ""),
            "positioned": normalized,
        }
        self._layout_cache_key = cache_key
        self._layout_cache = cache

    def _render_polygon_cached_layout(self, render_ctx, scale_factor, cache):
        try:
            from qgis.core import QgsTextRenderer
        except ImportError:
            return

        h_align_enum = resolve_qgs_halign(self._h_align)
        left_align = resolve_qgs_halign(self.ALIGN_LEFT_)
        if h_align_enum is None or left_align is None:
            return

        sf = float(scale_factor or 1.0)
        if sf <= 0.0:
            sf = 1.0

        def _fixed_width_transform(text_value, text_fmt, origin_x,
                                   rendered_value=None,
                                   target_width_override=None,
                                   render_html_mode=False,
                                   apply_transform=True):
            """Lock a rendered row to its fixed composition-space width."""
            painter = render_ctx.painter()
            if painter is None or not text_value:
                return painter, 1.0, origin_x
            try:
                base_font, _ = self._base_font_and_color()
                canonical_font = render_font(
                    base_font, 16.0, None,
                    _format_size_unit(self._text_format),
                    _format_size_map_unit_scale(self._text_format),
                    self._text_format.size())
                if target_width_override is not None:
                    target_width = float(target_width_override)
                else:
                    target_width = (
                        float(QtGui.QFontMetricsF(canonical_font).horizontalAdvance(
                            str(text_value))) / 16.0 * sf
                    )
                actual_text = (
                    rendered_value if rendered_value is not None
                    else text_value
                )
                actual_width = float(QgsTextRenderer.textWidth(
                    render_ctx, text_fmt, [actual_text]))
                if target_width <= 0.0 or actual_width <= 0.0:
                    return painter, 1.0, origin_x
                if target_width_override is not None:
                    # Justification CSS can differ substantially between
                    # preview scales; the explicit target is authoritative.
                    width_scale = max(
                        0.25, min(4.0, target_width / actual_width))
                else:
                    width_scale = max(
                        0.75, min(1.25, target_width / actual_width))
                if abs(width_scale - 1.0) < 0.001:
                    return painter, 1.0, origin_x
                if apply_transform:
                    painter.save()
                    painter.translate(origin_x, 0.0)
                    painter.scale(width_scale, 1.0)
                return painter, width_scale, 0.0
            except Exception:
                return painter, 1.0, origin_x

        def _justify_preview_boost(max_boost=4.0):
            """Supersample justified rows only when preview is below 100%."""
            try:
                app = getattr(QtGui, "QGuiApplication", None)
                screen = app.primaryScreen() if app is not None else None
                dpi = float(screen.logicalDotsPerInch()) if screen else 96.0
                reference_sf = max(1.0, dpi / 25.4)
                return max(1.0, min(float(max_boost), reference_sf / sf))
            except Exception:
                return 1.0

        def _fixed_composition_justify(html_text, plain_text, target_width,
                                       render_boost=1.0,
                                       measured_width=None,
                                       render_html_mode=False):
            """Calculate word spacing only in fixed composition space."""
            try:
                base_font, _ = self._base_font_and_color()
                canonical_font = render_font(
                    base_font, 16.0, None,
                    _format_size_unit(self._text_format),
                    _format_size_map_unit_scale(self._text_format),
                    self._text_format.size())
                canonical_target = float(target_width) / sf * 16.0
                canonical_measured = None
                if measured_width is not None:
                    canonical_measured = (
                        float(measured_width) / sf * 16.0)
                return _wrap_html_for_justify(
                    html_text, plain_text, canonical_target, canonical_font,
                    measured_width_px=canonical_measured,
                    css_spacing_factor=(
                        72.0 / (25.4 * 16.0) * float(render_boost)),
                    css_spacing_unit="pt",
                    edge_allowance_ratio=0.0)
            except Exception:
                return html_text, False

        mode = cache.get("mode", "plain")
        lh = float(cache.get("lh", 0.0)) * sf
        positioned = cache.get("positioned", [])

        def _disable_format_component(fmt, getter_name, setter_name):
            try:
                component = getattr(fmt, getter_name)()
                try:
                    neutral = type(component)()
                except Exception:
                    neutral = component
                if hasattr(neutral, "setEnabled"):
                    neutral.setEnabled(False)
                getattr(fmt, setter_name)(neutral)
            except Exception:
                record_suppressed_exception()

        def _component_format(text_format, component_name):
            """Create a public-API format for one global rendering pass."""
            try:
                fmt = QgsTextFormat(text_format)
            except Exception:
                return text_format
            if component_name == "background":
                # Preserve the configured background (and its shadow), but
                # prevent the invisible carrier text from painting a buffer.
                _disable_format_component(fmt, "buffer", "setBuffer")
                transparent = QColor(fmt.color())
                transparent.setAlpha(0)
                try:
                    fmt.setColor(transparent)
                except Exception:
                    record_suppressed_exception()
            elif component_name == "buffer":
                _disable_format_component(fmt, "background", "setBackground")
                transparent = QColor(fmt.color())
                transparent.setAlpha(0)
                try:
                    fmt.setColor(transparent)
                except Exception:
                    record_suppressed_exception()
            elif component_name == "text_shadow":
                # This preliminary pass supplies a shadow when neither a
                # background nor buffer exists. Its visible text is harmless:
                # every final glyph is repainted in the global text pass.
                _disable_format_component(fmt, "background", "setBackground")
                _disable_format_component(fmt, "buffer", "setBuffer")
            else:
                _disable_format_component(fmt, "background", "setBackground")
                _disable_format_component(fmt, "buffer", "setBuffer")
                _disable_format_component(fmt, "shadow", "setShadow")
            return fmt

        def _transparent_document(document):
            """Clone a document with every fragment's fill made transparent."""
            transparent = QColor(0, 0, 0, 0)
            result = QgsTextDocument()
            for block_index in range(document.size()):
                source_block = document.at(block_index)
                target_block = QgsTextBlock()
                try:
                    target_block.setBlockFormat(source_block.blockFormat())
                except Exception:
                    record_suppressed_exception()
                for fragment_index in range(source_block.size()):
                    source_fragment = source_block.at(fragment_index)
                    try:
                        character_format = QgsTextCharacterFormat(
                            source_fragment.characterFormat())
                    except Exception:
                        character_format = source_fragment.characterFormat()
                    try:
                        character_format.setTextColor(transparent)
                    except Exception:
                        record_suppressed_exception()
                    target_block.append(QgsTextFragment(
                        source_fragment.text(), character_format))
                result.append(target_block)
            return result

        def _draw_document(rect, alignment, text, text_format,
                           component_name):
            """Render one row through QgsTextDocument's public pipeline."""
            component_format = _component_format(
                text_format, component_name)
            try:
                component_format.updateDataDefinedProperties(render_ctx)
            except Exception:
                record_suppressed_exception()
            document = QgsTextDocument.fromTextAndFormat(
                [text], component_format)
            if component_name in ("background", "buffer"):
                document = _transparent_document(document)
            text_scale = QgsTextRenderer.calculateScaleFactorForFormat(
                render_ctx, component_format)
            metrics = QgsTextDocumentMetrics.calculateMetrics(
                document, component_format, render_ctx, text_scale)
            QgsTextRenderer.drawDocument(
                rect, component_format, metrics.document(), metrics,
                render_ctx, alignment)

        def _paint_row_component(command, component_name):
            painter = render_ctx.painter()
            if painter is None:
                return
            tx, ty = command.get("translate", (0.0, 0.0))
            sx, sy = command.get("scale", (1.0, 1.0))
            context_boost = float(command.get("context_boost", 1.0))
            previous_context_scale = None
            painter.save()
            try:
                if tx or ty:
                    painter.translate(tx, ty)
                if abs(sx - 1.0) >= 0.001 or abs(sy - 1.0) >= 0.001:
                    painter.scale(sx, sy)
                if context_boost > 1.001:
                    try:
                        previous_context_scale = float(render_ctx.scaleFactor())
                        render_ctx.setScaleFactor(
                            previous_context_scale * context_boost)
                    except Exception:
                        previous_context_scale = None

                x, y, width, height = command["rect"]
                rect = QRectF(x, y, width, height)
                _draw_document(
                    rect, command["alignment"], command["text"],
                    command["format"], component_name)
            except Exception:
                record_suppressed_exception()
            finally:
                if previous_context_scale is not None:
                    try:
                        render_ctx.setScaleFactor(previous_context_scale)
                    except Exception:
                        record_suppressed_exception()
                painter.restore()

        def _paint_buffer_then_text(commands):
            # Buffers may merge with adjacent buffers, but all glyph fills are
            # painted afterward, so no later row's buffer can cover text.
            for component_name in ("buffer", "text"):
                for command in commands:
                    _paint_row_component(command, component_name)

        def _background_is_enabled():
            if mode == "render_html":
                return False
            try:
                return bool(self._text_format.background().enabled())
            except Exception:
                return False

        background_enabled = _background_is_enabled()

        def _component_is_enabled(component_name):
            if mode == "render_html":
                return False
            try:
                return bool(getattr(
                    self._text_format, component_name)().enabled())
            except Exception:
                return False

        buffer_enabled = _component_is_enabled("buffer")
        shadow_enabled = _component_is_enabled("shadow")
        # QGIS associates Lowest shadows with the lowest visible component and
        # emits each row's shadow together with that row's source component.
        # Split that source into a preliminary global pass so a later row's
        # shadow cannot cover any earlier background or buffer.
        global_shadow_source = None
        if shadow_enabled:
            if background_enabled:
                global_shadow_source = "background"
            elif buffer_enabled:
                global_shadow_source = "buffer"

        if background_enabled:
            row_base_format = _copy_text_format_without_background_shadow(
                self._text_format)
        elif global_shadow_source:
            try:
                row_base_format = QgsTextFormat(self._text_format)
                _disable_format_component(
                    row_base_format, "shadow", "setShadow")
            except Exception:
                row_base_format = self._text_format
        else:
            row_base_format = self._text_format

        def _paint_background_buffer_text(commands):
            # QGIS cannot emit a shadow without its associated component.  For
            # overlapping polygon buffers, render all shadow/source pairs as
            # one preliminary layer, then repaint every buffer afterward.  The
            # second, shadow-free buffer layer forms the authoritative combined
            # buffer and covers source buffers and shadows from every row.
            if global_shadow_source:
                for command in commands:
                    shadow_command = dict(command)
                    try:
                        shadow_format = QgsTextFormat(command["format"])
                        if global_shadow_source == "background":
                            shadow_format.setBackground(
                                self._text_format.background())
                        else:
                            shadow_format.setBuffer(
                                self._text_format.buffer())
                        shadow_format.setShadow(self._text_format.shadow())
                    except Exception:
                        shadow_format = self._text_format
                    shadow_command["format"] = shadow_format
                    _paint_row_component(
                        shadow_command, global_shadow_source)
            elif shadow_enabled:
                # With no background/buffer, text itself is the shadow source.
                # Emit all row shadows before any authoritative buffer/text
                # layer so later shadows can never cover earlier final glyphs.
                for command in commands:
                    _paint_row_component(command, "text_shadow")

            # Every background uses the same final row rectangle and painter
            # transform as its text. Paint the complete background layer next
            # so overlapping background shapes can never cover glyphs.
            if background_enabled:
                for command in commands:
                    background_command = dict(command)
                    # Start from the row's effective format so justified HTML
                    # spacing, supersampled size and native inline formatting
                    # use identical metrics. Restore the background only; its
                    # shadow was emitted in the preliminary global layer.
                    try:
                        background_format = QgsTextFormat(command["format"])
                        background_format.setBackground(
                            self._text_format.background())
                    except Exception:
                        background_format = self._text_format
                    background_command["format"] = background_format
                    _paint_row_component(background_command, "background")
            _paint_buffer_then_text(commands)

        if mode == "plain":
            row_commands = []
            n_lines = len(positioned)
            justified_fmt = None
            for i, item in enumerate(positioned):
                if len(item) not in (4, 6):
                    continue
                if len(item) == 6:
                    line_plain, lx, ly, lw, paragraph_final, blank = item
                else:
                    line_plain, lx, ly, lw = item
                    paragraph_final = (i == n_lines - 1)
                    blank = not str(line_plain).strip()
                if blank:
                    continue
                lx = float(lx) * sf
                ly = float(ly) * sf
                lw = float(lw) * sf
                justify_this_line = (
                    self._h_align == self.ALIGN_JUSTIFY_
                    and not paragraph_final and " " in str(line_plain).strip()
                )
                # Row origins are pre-aligned in fixed composition space.
                eff_align = left_align
                if justify_this_line:
                    justify_boost = _justify_preview_boost()
                    if justified_fmt is None:
                        try:
                            justified_fmt = QgsTextFormat(row_base_format)
                        except Exception:
                            justified_fmt = row_base_format
                        try:
                            justified_fmt.setAllowHtmlFormatting(True)
                        except Exception:
                            record_suppressed_exception()
                    source_line_html = _html_escape(
                        str(line_plain), quote=False)
                    measured_plain_width = None
                    try:
                        measured_plain_width = QgsTextRenderer.textWidth(
                            render_ctx, justified_fmt,
                            [source_line_html])
                    except Exception:
                        record_suppressed_exception()
                    line_to_draw, _ = _fixed_composition_justify(
                        source_line_html,
                        str(line_plain), lw, justify_boost,
                        measured_plain_width)
                    if justify_boost > 1.001:
                        try:
                            format_to_draw = QgsTextFormat(justified_fmt)
                            format_to_draw.setSize(
                                justified_fmt.size() * justify_boost)
                        except Exception:
                            format_to_draw = justified_fmt
                    else:
                        format_to_draw = justified_fmt

                    # Qt/QGIS can resolve CSS word-spacing slightly
                    # differently from the canonical measurement. Measure the
                    # completed native row and feed the residual width back
                    # into spacing only, leaving glyphs and margins untouched.
                    spacing_target = lw
                    for _correction in range(3):
                        try:
                            painted_width = float(QgsTextRenderer.textWidth(
                                render_ctx, format_to_draw,
                                [line_to_draw])) / max(justify_boost, 1.0)
                        except Exception:
                            break
                        residual = lw - painted_width
                        if abs(residual) <= max(0.25, lw * 0.0005):
                            break
                        spacing_target += residual
                        line_to_draw, applied = _fixed_composition_justify(
                            source_line_html, str(line_plain),
                            spacing_target, justify_boost,
                            measured_plain_width)
                        if not applied:
                            break
                else:
                    line_to_draw = line_plain
                    format_to_draw = row_base_format
                _justify_w = lw
                if justify_this_line:
                    # Justification must alter spaces only.  Scaling the
                    # painter changes glyph proportions and makes regular
                    # fonts appear inconsistently condensed.
                    width_scale = 1.0
                    draw_lx = lx
                    draw_ly = ly
                    draw_width = _justify_w
                    draw_height = lh
                    translate = (0.0, 0.0)
                    painter_scale = (1.0, 1.0)
                    if justify_boost > 1.001:
                        width_scale = justify_boost
                        translate = (lx, ly)
                        painter_scale = (
                            1.0 / justify_boost, 1.0 / justify_boost)
                        draw_lx = 0.0
                        draw_ly = 0.0
                        draw_width = _justify_w * justify_boost
                        draw_height = lh * justify_boost
                else:
                    width_painter, width_scale, draw_lx = _fixed_width_transform(
                        line_plain, format_to_draw, lx,
                        apply_transform=False)
                    draw_ly = ly
                    draw_width = _justify_w / width_scale
                    draw_height = lh
                    translate = (
                        (lx, 0.0) if abs(width_scale - 1.0) >= 0.001
                        else (0.0, 0.0))
                    painter_scale = (width_scale, 1.0)
                row_commands.append({
                    "rect": (draw_lx, draw_ly, draw_width, draw_height),
                    "alignment": eff_align,
                    "text": line_to_draw,
                    "format": format_to_draw,
                    "translate": translate,
                    "scale": painter_scale,
                })
            _paint_background_buffer_text(row_commands)
            return

        html_segments = cache.get("html_segments", [])
        html_plain = cache.get("html_plain", "")
        if mode == "render_html":
            draw_fmt = _copy_text_format_without_effects(self._text_format)
        else:
            try:
                draw_fmt = QgsTextFormat(row_base_format)
            except Exception:
                draw_fmt = row_base_format
        try:
            draw_fmt.setAllowHtmlFormatting(True)
        except Exception:
            record_suppressed_exception()

        row_commands = []
        n_lines = len(positioned)
        for i, item in enumerate(positioned):
            if len(item) not in (5, 7, 8):
                continue
            if len(item) == 8:
                ts, tl, lx, ly, lw, paragraph_final, blank, natural_width = item
            elif len(item) == 7:
                ts, tl, lx, ly, lw, paragraph_final, blank = item
                natural_width = None
            else:
                ts, tl, lx, ly, lw = item
                paragraph_final = (i == n_lines - 1)
                blank = False
                natural_width = None
            lx = float(lx) * sf
            ly = float(ly) * sf
            lw = float(lw) * sf
            if natural_width is not None:
                natural_width = float(natural_width) * sf
            if mode == "render_html":
                slice_font = self._text_format.font()
                slice_color = self._text_format.color()
            else:
                slice_font = self._text_format.font()
                slice_color = self._text_format.color()
            line_html = segments_slice_to_html(
                html_segments, ts, tl, slice_font, slice_color)
            source_line_html = line_html
            line_plain = html_plain[ts:ts+tl].rstrip("\n\u2028")
            if blank or (not line_html and not line_plain.strip()):
                continue
            justify_this_line = (
                self._h_align == self.ALIGN_JUSTIFY_
                and not paragraph_final and " " in line_plain.strip()
            )
            # Row origins are pre-aligned in fixed composition space.
            eff_align = left_align
            if justify_this_line:
                justify_boost = (
                    _justify_preview_boost(16.0)
                    if mode == "render_html"
                    else _justify_preview_boost()
                )
                # Render-as-HTML supersampling is performed by temporarily
                # increasing the render-context scale, never by copying or
                # resizing its QgsTextFormat.  Consequently its physical CSS
                # spacing itself must not be multiplied here.
                spacing_boost = (
                    1.0 if mode == "render_html" else justify_boost
                )
                measured_html_width = None
                try:
                    measured_html_width = QgsTextRenderer.textWidth(
                        render_ctx, draw_fmt, [line_html])
                except Exception:
                    record_suppressed_exception()
                line_html, _ = _fixed_composition_justify(
                    line_html, line_plain, lw, spacing_boost,
                    measured_html_width,
                    render_html_mode=(mode == "render_html"))
                eff_align = left_align
                if justify_boost > 1.001 and mode != "render_html":
                    try:
                        row_draw_fmt = QgsTextFormat(draw_fmt)
                        row_draw_fmt.setSize(
                            draw_fmt.size() * justify_boost)
                        # Some QGIS builds do not preserve this flag when a
                        # temporary boosted format is copied from the plugin's
                        # Render as HTML format.  Set it explicitly so this
                        # mode never depends on the native Allow HTML option
                        # having been visited/toggled first.
                        if mode == "render_html":
                            row_draw_fmt.setAllowHtmlFormatting(True)
                    except Exception:
                        row_draw_fmt = draw_fmt
                else:
                    row_draw_fmt = draw_fmt

                if mode != "render_html":
                    spacing_target = lw
                    for _correction in range(3):
                        try:
                            painted_width = float(QgsTextRenderer.textWidth(
                                render_ctx, row_draw_fmt,
                                [line_html])) / max(justify_boost, 1.0)
                        except Exception:
                            break
                        residual = lw - painted_width
                        if abs(residual) <= max(0.25, lw * 0.0005):
                            break
                        spacing_target += residual
                        line_html, applied = _fixed_composition_justify(
                            source_line_html, line_plain,
                            spacing_target, spacing_boost,
                            measured_html_width,
                            render_html_mode=False)
                        if not applied:
                            break
            else:
                justify_boost = 1.0
                row_draw_fmt = draw_fmt
            _justify_w = lw
            if justify_this_line:
                width_scale = 1.0
                draw_lx = lx
                draw_ly = ly
                draw_width = _justify_w
                draw_height = lh
                translate = (0.0, 0.0)
                painter_scale = (1.0, 1.0)
                if justify_boost > 1.001:
                    width_scale = justify_boost
                    translate = (lx, ly)
                    painter_scale = (
                        1.0 / justify_boost, 1.0 / justify_boost)
                    draw_lx = 0.0
                    draw_ly = 0.0
                    draw_width = _justify_w * justify_boost
                    draw_height = lh * justify_boost
            else:
                if mode == "render_html":
                    width_painter, width_scale, draw_lx = (
                        _fixed_width_transform(
                            line_plain, draw_fmt, lx,
                            rendered_value=line_html,
                            target_width_override=(
                                natural_width
                                if natural_width is not None
                                and natural_width > 0.0
                                else None),
                            render_html_mode=True,
                            apply_transform=False)
                    )
                else:
                    width_painter, width_scale, draw_lx = (
                        _fixed_width_transform(
                            line_plain, draw_fmt, lx,
                            rendered_value=(
                                line_html if mode == "inline_html" else None),
                            target_width_override=(
                                natural_width
                                if mode == "inline_html"
                                and natural_width is not None
                                and natural_width > 0.0
                                else None),
                            apply_transform=False)
                    )
                draw_ly = ly
                draw_width = _justify_w / width_scale
                draw_height = lh
                translate = (
                    (lx, 0.0) if abs(width_scale - 1.0) >= 0.001
                    else (0.0, 0.0))
                painter_scale = (width_scale, 1.0)

            row_commands.append({
                "rect": (draw_lx, draw_ly, draw_width, draw_height),
                "alignment": eff_align,
                "text": line_html,
                "format": row_draw_fmt,
                "translate": translate,
                "scale": painter_scale,
                "context_boost": (
                    justify_boost
                    if justify_this_line and mode == "render_html"
                    else 1.0),
            })

        _paint_background_buffer_text(row_commands)

    def _draw_wrapped_text(self, render_ctx, points, resolved_text,
                            pad_px, hm_px, vm_px, scale_factor):
        """
        Phase 1 - Measurement: QTextLayout on plain/case-applied text
        determines per-line breaks using the scan-line polygon algorithm.
        Phase 2 - Rendering:
          • normal text uses QgsTextRenderer, preserving QGIS text effects;
          • native Allow HTML formatting also uses QgsTextRenderer, but each
            wrapped line is rebuilt as simple inline HTML first;
          • plugin Render as HTML stays separate and uses Qt rich-text drawing,
            intentionally not the QGIS effects stack.
        """
        try:
            from qgis.core import QgsTextRenderer
        except ImportError:
            return

        # Compose in one fixed, zoom-independent painter space.  QGIS' native
        # layout items keep their content geometry in layout units and only
        # transform it for the destination painter.  Re-running QTextLayout at
        # the current preview zoom makes line breaks depend on screen pixels.
        display_scale_factor = max(float(scale_factor or 1.0), 1.0e-9)
        composition_scale_factor = 16.0  # high-resolution units per layout mm
        composition_ratio = composition_scale_factor / display_scale_factor
        points = [
            QPointF(p.x() * composition_ratio, p.y() * composition_ratio)
            for p in points
        ]
        pad_px *= composition_ratio
        hm_px *= composition_ratio
        vm_px *= composition_ratio
        scale_factor = composition_scale_factor

        render_html = self._allow_html
        inline_html = (
            not render_html
            and text_format_allows_html(self._text_format)
        )

        base_font, base_color = self._base_font_and_color()
        fmt_size_unit = _format_size_unit(self._text_format)
        fmt_size_scale = _format_size_map_unit_scale(self._text_format)
        bfpx = render_font(
            base_font, scale_factor, None,
            fmt_size_unit, fmt_size_scale, self._text_format.size())
        fm   = QtGui.QFontMetricsF(bfpx)

        cap = None
        try:
            cap = self._text_format.capitalization()
        except Exception:
            cap = None

        cache_key = self._layout_signature(resolved_text, render_html, inline_html)
        if cache_key == self._layout_cache_key and self._layout_cache is not None:
            try:
                self._render_polygon_cached_layout(
                    render_ctx, display_scale_factor, self._layout_cache)
            except Exception:
                record_suppressed_exception()
            return

        html_segments = []
        html_plain = ""
        html_formats = []
        html_block_spacing = {}
        if render_html:
            html_segments = extract_segments(
                resolved_text, True, base_font, base_color,
                block_spacing_out=html_block_spacing)
            html_plain, html_formats = segments_to_plain_and_formats(
                html_segments, scale_factor, None,
                fmt_size_unit, fmt_size_scale)
        elif inline_html:
            html_segments = extract_qgis_html_segments(
                resolved_text, self._text_format, base_font, base_color)
            html_plain, html_formats = segments_to_plain_and_formats(
                html_segments, scale_factor, None,
                fmt_size_unit, fmt_size_scale)

        if render_html or inline_html:
            plain_measure = html_plain
            if render_html:
                # QgsTextRenderer applies QgsTextFormat capitalization after
                # parsing native inline HTML.  Measure the same visible case
                # here; otherwise uppercase/title-case rows wrap using the
                # narrower source text and their final glyph is clipped.
                case_measure = apply_capitalization(plain_measure, cap)
                # Segment offsets address the original HTML character stream.
                # The supported case transforms normally preserve length; for
                # rare Unicode expansions, retain the original stream so its
                # formatting ranges cannot become misaligned.
                if len(case_measure) == len(plain_measure):
                    plain_measure = case_measure
        else:
            plain_measure = apply_capitalization(resolved_text, cap)

        # QTextLayout does not consistently treat LF/CRLF as a forced line
        # break (the behaviour varies between Qt versions).  U+2028 is Qt's
        # explicit in-paragraph line separator and has the same string length,
        # so segment/range offsets remain valid for the HTML rendering paths.
        # This also preserves consecutive newlines as genuinely empty lines.
        plain_measure = (plain_measure or "").replace("\r\n", "\n").replace("\r", "\n")
        plain_measure = plain_measure.replace("\n", "\u2028")
        if render_html or inline_html:
            html_plain = plain_measure

        line_height_mult = 1.0
        try:
            line_height_mult = max(0.5, self._text_format.lineHeight())
        except Exception:
            line_height_mult = 1.0
        lh = max(fm.height() * line_height_mult, 1.0)
        inline_font_samples = []
        if inline_html:
            for fmt_range in html_formats:
                try:
                    sample = plain_measure[
                        fmt_range.start:fmt_range.start + fmt_range.length]
                    inline_font_samples.append((
                        QtGui.QFontMetricsF(fmt_range.format.font()), sample))
                except Exception:
                    record_suppressed_exception()
        # Text structure is independent of paint effects. Buffer, background
        # and shadow must adopt the composed rows and must never cause text to
        # move or rewrap when their settings are toggled.
        visual_format = _copy_text_format_without_effects(self._text_format)
        visual_pad_x, visual_pad_y = _text_visual_padding(
            None, visual_format, scale_factor, fm, plain_measure,
            inline_font_samples)

        # QTextDocument resolves CSS/default block margins in its HTML pixel
        # coordinate system.  QGIS' layout HTML renderer converts that system
        # using the same adjustment factors before painting in layout units.
        # Keep those margins as vertical compositor geometry so headings and
        # paragraphs retain their native spacing without inserting fake text
        # rows or changing the polygon wrapping algorithm.
        html_leading = 0.0
        html_trailing = 0.0
        html_break_advances = {}
        if render_html and html_block_spacing:
            html_adjustment = 3.77
            try:
                qt_version = tuple(
                    int(part) for part in QtCore.QT_VERSION_STR.split(".")[:2]
                )
                if qt_version >= (6, 7):
                    html_adjustment = 4.18
            except Exception:
                record_suppressed_exception()
            html_margin_scale = composition_scale_factor / html_adjustment
            html_leading = max(
                0.0,
                float(html_block_spacing.get("leading", 0.0))
                * html_margin_scale,
            )
            html_trailing = max(
                0.0,
                float(html_block_spacing.get("trailing", 0.0))
                * html_margin_scale,
            )
            for offset, advance in html_block_spacing.get("breaks", {}).items():
                try:
                    html_break_advances[int(offset)] = max(
                        0.0, float(advance) * html_margin_scale)
                except Exception:
                    record_suppressed_exception()

        bbox         = QPolygonF(points).boundingRect()
        # Treat margins as a true inner content box: both left/right and
        # top/bottom margins reduce the usable area equally.
        avail_top    = bbox.top()    + pad_px + vm_px
        avail_bottom = bbox.bottom() - pad_px - vm_px
        inner_left   = bbox.left()   + pad_px + hm_px
        inner_right  = bbox.right()  - pad_px - hm_px
        if avail_bottom <= avail_top or inner_right <= inner_left:
            return

        def _compose_rows(candidate_y):
            """Lay out once for measurement and rendering.

            Row tuples are (start, length, left, y, width, paragraph_final,
            blank, formatted_natural_width).  Forced separators are retained
            as explicit blank rows, so bottom alignment cannot measure fewer
            rows than the renderer later consumes.
            """
            layout = QtGui.QTextLayout(plain_measure, bfpx)
            _apply_design_metrics_to_layout(layout)
            if html_formats:
                layout.setFormats(html_formats)
            rows = []
            py = max(avail_top, float(candidate_y)) + html_leading
            complete = False
            layout.beginLayout()
            for _safety in range(5000):
                qline = layout.createLine()
                if not qline.isValid():
                    complete = True
                    break
                ph = max(qline.height(), lh)
                cy = py
                chosen = None
                for _ in range(60):
                    if cy + ph > avail_bottom + 0.01:
                        break
                    span = _line_safe_span(
                        points, cy - visual_pad_y,
                        cy + ph + visual_pad_y,
                        pad_px, hm_px, visual_pad_x)
                    if span and span[1] - span[0] > 1.0:
                        width = span[1] - span[0]
                        qline.setLineWidth(width)
                        chosen = (span[0], width, cy)
                        if qline.naturalTextWidth() <= width + 0.5:
                            break
                    cy += ph
                if chosen is None:
                    break
                left, width, row_y = chosen
                # QTextLine decides its wrapped text range from the assigned
                # width.  Reading textLength before setLineWidth makes the
                # first line consume the entire document as one clipped row.
                ts = qline.textStart()
                raw_len = qline.textLength()
                tl = raw_len
                forced = False
                while tl > 0 and plain_measure[ts + tl - 1:ts + tl] in ("\n", "\u2028"):
                    forced = True
                    tl -= 1
                block_advance = 0.0
                if forced and raw_len > tl:
                    block_advance = sum(
                        html_break_advances.get(offset, 0.0)
                        for offset in range(ts + tl, ts + raw_len)
                    )
                blank = not plain_measure[ts:ts + tl].strip()
                rows.append((
                    ts, tl, left, row_y, width, forced, blank,
                    max(0.0, float(qline.naturalTextWidth()))))
                py = (
                    row_y
                    + max(qline.height(), lh) * line_height_mult
                    + block_advance
                )
            layout.endLayout()
            if complete:
                py += html_trailing
                if py > avail_bottom + 0.01:
                    complete = False
            if complete and rows:
                last = list(rows[-1])
                last[5] = True
                rows[-1] = tuple(last)
            return rows, complete, py

        # Find the lowest start which still composes every row.  This uses the
        # exact same records later rendered and therefore includes blank lines.
        start_y = avail_top
        if self._v_align == self.VALIGN_BOTTOM_:
            low, high = avail_top, avail_bottom
            best = avail_top
            for _ in range(18):
                candidate = (low + high) / 2.0
                _rows, fits, _end = _compose_rows(candidate)
                if fits:
                    best = candidate
                    low = candidate
                else:
                    high = candidate
            start_y = best
        elif self._v_align == self.VALIGN_MIDDLE_:
            top_rows, top_fits, top_end = _compose_rows(avail_top)
            if top_fits:
                desired = avail_top + max(0.0, (avail_bottom - top_end) / 2.0)
                middle_rows, middle_fits, _ = _compose_rows(desired)
                start_y = desired if middle_fits else avail_top

        composed_rows, composed_complete, _composed_end = _compose_rows(start_y)

        def _rows_with_fixed_origins(rows):
            """Resolve horizontal alignment once in composition space.

            QgsTextRenderer otherwise recalculates right/center origins from
            zoom-rounded glyph metrics.  Persisting an explicit left origin
            makes the row translate/scale as a rigid layout object.
            """
            fixed = []
            for (ts, tl, lx, ly, lw, paragraph_final, blank,
                 formatted_natural) in rows:
                line_text = plain_measure[ts:ts + tl]
                natural = (
                    max(0.0, float(formatted_natural))
                    if render_html or inline_html
                    else max(0.0, float(fm.horizontalAdvance(line_text)))
                )
                justify_row = (
                    self._h_align == self.ALIGN_JUSTIFY_
                    and not paragraph_final and " " in line_text.strip()
                )
                if justify_row:
                    draw_x, draw_w = lx, lw
                elif self._h_align == self.ALIGN_RIGHT_:
                    draw_x = lx + max(0.0, lw - natural)
                    draw_w = min(lw, natural + 2.0 * visual_pad_x)
                elif self._h_align == self.ALIGN_CENTER_:
                    draw_x = lx + max(0.0, (lw - natural) / 2.0)
                    draw_w = min(lw, natural + 2.0 * visual_pad_x)
                else:
                    draw_x = lx
                    draw_w = min(lw, natural + 2.0 * visual_pad_x)
                fixed.append((ts, tl, draw_x, ly, draw_w,
                              paragraph_final, blank, formatted_natural))
            return fixed

        composed_rows = _rows_with_fixed_origins(composed_rows)

        h_align_enum = resolve_qgs_halign(self._h_align)
        left_align   = resolve_qgs_halign(self.ALIGN_LEFT_)
        if h_align_enum is None or left_align is None:
            return

        if render_html:
            # ── Render as HTML path ───────────────────────────────────────
            # Richer HTML/document-like rendering, intentionally separate from
            # QGIS font effects.  We still use the same scan-line wrapping,
            # but render line-by-line so top alignment stays stable and the
            # line origin is explicit.
            positioned_html = list(composed_rows)

            self._store_polygon_layout_cache(
                cache_key, "render_html", {
                    "lh": lh,
                    "positioned": positioned_html,
                    "html_segments": html_segments,
                    "html_plain": html_plain,
                }, scale_factor)

            self._render_polygon_cached_layout(
                render_ctx, display_scale_factor, self._layout_cache)
            return

        if inline_html:
            # ── Native Allow HTML formatting path ────────────────────────
            # Use QTextLayout only for wrapping/line placement.  Render the
            # resulting line HTML through QgsTextRenderer so buffer, shadow,
            # background, opacity and draw effects still behave like QGIS'
            # own Add Label item.
            positioned_html = list(composed_rows)

            self._store_polygon_layout_cache(
                cache_key, "inline_html", {
                    "lh": lh,
                    "positioned": positioned_html,
                    "html_segments": html_segments,
                    "html_plain": html_plain,
                }, scale_factor)

            self._render_polygon_cached_layout(
                render_ctx, display_scale_factor, self._layout_cache)
            return

        # ── Non-HTML path (QgsTextRenderer) ───────────────────────────
        # Applies buffer, shadow, background, opacity, case.
        positioned = [
            (plain_measure[ts:ts + tl], lx, ly, lw, paragraph_final, blank)
            for (ts, tl, lx, ly, lw, paragraph_final, blank,
                 _formatted_natural) in composed_rows
        ]

        self._store_polygon_layout_cache(
            cache_key, "plain", {
                "lh": lh,
                "positioned": positioned,
            }, scale_factor)

        self._render_polygon_cached_layout(
            render_ctx, display_scale_factor, self._layout_cache)
        return



    # ---- QgsLayoutItem overrides ----------------------------------------
    def drawBackground(self, context):
        """Intentionally a no-op: the polygon-shaped background is painted
        inside draw() directly, before text, so we can guarantee it fills
        exactly the polygon outline and not the full item bounding rect.
        If we let the QGIS framework call this to paint the background, it
        draws the bounding rect regardless of our polygon shape."""
        pass  # background is handled in draw()

    def drawFrame(self, context):
        """The native Frame is painted on the polygon path inside draw()."""
        pass
    # ------------------------------------------------------------ persistence
    def writePropertiesToElement(self, element, document, context):
        element.setAttribute("polyText",     self._text)
        element.setAttribute("polyHtml", "1" if self._allow_html else "0")
        element.setAttribute("polyPadding",  str(self._padding))
        element.setAttribute("polyHMargin",  str(self._h_margin))
        element.setAttribute("polyVMargin",  str(self._v_margin))
        element.setAttribute("polyHAlign",   str(self._h_align))
        element.setAttribute("polyVAlign",   str(self._v_align))
        node_str = ";".join(f"{n.x():.6f},{n.y():.6f}" for n in self._nodes)
        element.setAttribute("polyNodes", node_str)
        # Persist full text format (font, color, size, buffer, shadow, …)
        _append_text_format_to_element(
            element, document, context, self._text_format, "polyTextFormat")
        return True

    def readPropertiesFromElement(self, element, document, context):
        self._text       = element.attribute("polyText", self._text)
        # Plugin-level Render as HTML flag.  Native Allow HTML formatting is
        # stored independently inside QgsTextFormat.
        self._allow_html = element.attribute("polyHtml", "0") == "1"
        for attr, field, default in [
            ("polyPadding",  "_padding",      0.0),
            ("polyHMargin",  "_h_margin",     0.0),
            ("polyVMargin",  "_v_margin",     0.0),
        ]:
            v = element.attribute(attr, "")
            setattr(self, field, float(v) if v else default)
        self._h_align = int(element.attribute("polyHAlign", "0") or "0")
        self._v_align = int(element.attribute("polyVAlign", "0") or "0")
        # Read text format (new wrapped format plus legacy direct QGIS tags)
        fmt = _read_text_format_from_element(
            element, context, self._text_format, ("polyTextFormat",))
        if fmt is not None:
            self._text_format = fmt
        else:
            # Backward-compat: read old individual font/color attributes
            fam  = element.attribute("polyFontFamily", "")
            sz   = element.attribute("polyFontSize",   "10.0")
            bold = element.attribute("polyFontBold",   "0") == "1"
            ital = element.attribute("polyFontItalic", "0") == "1"
            col  = element.attribute("polyColor", "#141414")
            f = QFont(fam) if fam else QFont(); f.setPointSizeF(float(sz) if sz else 10.0)
            f.setBold(bold); f.setItalic(ital)
            self._text_format = _make_default_text_format()
            self._text_format.setFont(f)
            self._text_format.setSize(float(sz) if sz else 10.0)
            self._text_format.setColor(QColor(col))
        self._text_format_revision += 1
        self._layout_cache_key = None
        self._layout_cache = None

        node_str = element.attribute("polyNodes", "")
        if node_str:
            nodes = []
            for pair in node_str.split(";"):
                if not pair: continue
                xs, ys = pair.split(",")
                nodes.append(QPointF(float(xs), float(ys)))
            if len(nodes) >= 3:
                self._nodes = nodes
        return True

    def clone(self):
        from qgis.PyQt.QtXml import QDomDocument
        item = LayoutItemPolygonText(self.layout())
        doc  = QDomDocument()
        elem = doc.createElement("clonedPolyText")
        self.writePropertiesToElement(elem, doc, QgsReadWriteContext())
        item.readPropertiesFromElement(elem, doc, QgsReadWriteContext())
        # QGIS copy/paste may use clone() directly.  Keep a direct deep copy of
        # the text format too, so buffer/background/shadow/effects survive even
        # if a binding-specific XML round trip skips part of QgsTextFormat.
        try:
            item._text_format = QgsTextFormat(self._text_format)
            item._text_format_revision += 1
        except Exception:
            record_suppressed_exception()
        try:
            item.setFrameEnabled(self.frameEnabled())
            item.setFrameStrokeColor(self.frameStrokeColor())
            item.setFrameStrokeWidth(self.frameStrokeWidth())
            item.setFrameJoinStyle(self.frameJoinStyle())
        except Exception:
            record_suppressed_exception()
        return keep_alive(item)
