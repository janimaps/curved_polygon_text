"""
layout_item_polygon_text.py — Polygon Shaped Text Box layout item.
"""


import hashlib
import re

from qgis.core import (
    QgsLayoutItem, QgsLayoutItemRegistry, QgsReadWriteContext,
    QgsTextFormat, QgsLayoutMeasurement, Qgis, QgsTextRenderer,
    QgsTextDocument, QgsTextDocumentMetrics, QgsTextBlock,
    QgsTextFragment, QgsTextCharacterFormat, QgsTextBlockFormat, QgsMargins,
    QgsRenderContext,
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
    text_format_allows_html, segments_slice_to_html, text_format_base_font,
    normalised_text_format_font,
)
from .bezier import (
    build_bezier_path, flatten_bezier, clone_handles, empty_handles, straight_handles,
    bounded_polygon_handles, ensure_closed_handles,
    handle_scene_records, nearest_segment, split_segment,
    convert_segment_to_curve, convert_segment_to_straight,
    serialise_handles, deserialise_handles, apply_node_mode,
)
from .icons import polygon_item_icon
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


class _CompositionUnitContext:
    """Expose fixed composition-unit conversions to the polygon renderer."""

    def __init__(self, source_context, destination_to_composition):
        self._source_context = source_context
        self._factor = float(destination_to_composition)

    def convertToPainterUnits(self, *args):
        return float(self._source_context.convertToPainterUnits(*args)) * self._factor


def _render_pixels_unit():
    """Resolve QGIS' pixel render unit across supported API generations."""
    try:
        from qgis.core import QgsUnitTypes
        return QgsUnitTypes.RenderUnit.RenderPixels
    except Exception:
        try:
            return Qgis.RenderUnit.Pixels
        except Exception:
            return None


def _fixed_polygon_paint_format(text_format, render_ctx,
                                composition_scale=16.0):
    """Freeze a text format to composition-space pixels for final painting.

    QgsTextRenderer normally resolves point/mm sizes against its live painter
    context.  The polygon row plan is deliberately created at a fixed scale,
    therefore final painting must use the equivalent fixed pixel size too.
    The outer painter subsequently applies preview/export zoom as a transform.
    """
    try:
        result = normalised_text_format_font(text_format)
        font = text_format_base_font(result)
        unit = _format_size_unit(result)
        unit_scale = _format_size_map_unit_scale(result)
        size_value = float(result.size())
        display_scale = max(float(render_ctx.scaleFactor() or 1.0), 1.0e-9)
        comp_context = _CompositionUnitContext(
            render_ctx, float(composition_scale) / display_scale)
        fixed_font = render_font(
            font, composition_scale, comp_context, unit, unit_scale,
            size_value)
        try:
            if unit_scale is not None:
                size_px = float(comp_context.convertToPainterUnits(
                    size_value, unit, unit_scale))
            else:
                size_px = float(comp_context.convertToPainterUnits(
                    size_value, unit))
        except Exception:
            size_px = max(1.0, float(fixed_font.pointSizeF()) * 96.0 / 72.0)
        fixed_font.setPixelSize(max(1, int(round(size_px))))
        result.setFont(fixed_font)
        pixel_unit = _render_pixels_unit()
        if pixel_unit is not None:
            result.setSizeUnit(pixel_unit)
        result.setSize(max(1.0, size_px))
        return result
    except Exception:
        return text_format


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


_HTML_SEMANTICS_RE = re.compile(
    r"<\s*/?\s*[A-Za-z][A-Za-z0-9:_-]*(?:\s+[^<>]*?)?/?>"
    r"|&(?:#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]+);"
)


def _has_html_semantics(value):
    """Return whether an input needs a rich-document rendering path.

    A mode toggle alone is not rich formatting.  Tags and character entities
    change the text stream or its presentation and therefore remain on the
    HTML path; ordinary text can use the exact plain-text compositor.
    """
    try:
        return bool(_HTML_SEMANTICS_RE.search(str(value or "")))
    except (TypeError, ValueError):
        return True


def _html_single_flow_text(value):
    """Mirror Render-as-HTML's collapse of ordinary source whitespace."""
    try:
        return re.sub(r"[\t\n\r\f\v ]+", " ", str(value or ""))
    except (TypeError, ValueError):
        return str(value or "")


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



def _line_safe_spans(points, y_top, y_bottom, pad_px, hm_px, extra_x=0.0):
    """Return every horizontal interior span safe for the full line band.

    A concave polygon can intersect one horizontal row in multiple disjoint
    regions.  We retain all regions which stay inside the polygon throughout
    the complete painted line height.  This allows one visual text row to flow
    left-to-right through multiple lobes/columns instead of discarding all but
    the widest lobe.
    """
    if y_bottom < y_top:
        y_top, y_bottom = y_bottom, y_top

    bbox = QPolygonF(points).boundingRect()
    eps = 0.01
    y_top = max(bbox.top() + eps, y_top)
    y_bottom = min(bbox.bottom() - eps, y_bottom)
    if y_bottom < y_top:
        return []

    if y_bottom - y_top <= eps:
        sample_ys = [y_top]
    else:
        sample_count = 5
        sample_ys = [
            y_top + (y_bottom - y_top) * i / (sample_count - 1)
            for i in range(sample_count)
        ]

        # Include polygon vertices inside the row band so a narrow neck or
        # lobe transition cannot be missed between the regular samples.
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
            return []

        if current is None:
            current = spans
            continue

        overlaps = []
        for cur_left, cur_right in current:
            for left, right in spans:
                il = max(cur_left, left)
                ir = min(cur_right, right)
                if ir - il > 1.0:
                    overlaps.append((il, ir))
        if not overlaps:
            return []

        # Intersections can occasionally touch/duplicate at sampled vertices.
        # Merge only truly overlapping intervals; keep genuine polygon gaps.
        overlaps.sort(key=lambda span: span[0])
        merged = []
        for left, right in overlaps:
            if merged and left <= merged[-1][1] + eps:
                merged[-1] = (merged[-1][0], max(merged[-1][1], right))
            else:
                merged.append((left, right))
        current = merged

    return sorted(current or [], key=lambda span: span[0])


def _line_safe_span(points, y_top, y_bottom, pad_px, hm_px, extra_x=0.0):
    """Backward-compatible helper returning the widest safe line span."""
    spans = _line_safe_spans(
        points, y_top, y_bottom, pad_px, hm_px, extra_x)
    if not spans:
        return None
    return max(spans, key=lambda span: span[1] - span[0])

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

def _zero_horizontal_rich_document_margins(document):
    """Return a copy of a rich text document with horizontal block margins removed."""
    result = QgsTextDocument()
    for block in document:
        block_format = QgsTextBlockFormat(block.blockFormat())
        margins = block_format.margins()
        block_format.setMargins(
            QgsMargins(0.0, margins.top(), 0.0, margins.bottom())
        )
        target_block = QgsTextBlock()
        target_block.setBlockFormat(block_format)
        for fragment in block:
            target_block.append(fragment)
        result.append(target_block)
    return result


def _measure_rich_fragment_width(render_ctx, text_format, rich_fragment):
    """Measure one rich fragment using QGIS' document metrics path used for painting.

    The returned width is in painter units and is based on the same resolved
    QgsTextDocument metrics pipeline used by the rich text renderer. Horizontal
    block margins are removed because the geometric compositor owns the exact
    placement of each fragment.
    """
    value = str(rich_fragment or "")
    if not value:
        return 0.0

    try:
        measure_format = normalised_text_format_font(text_format)
    except Exception:
        measure_format = text_format

    try:
        measure_format.setAllowHtmlFormatting(True)
    except Exception:
        record_suppressed_exception()

    try:
        measure_format.updateDataDefinedProperties(render_ctx)
    except Exception:
        record_suppressed_exception()

    document = QgsTextDocument.fromTextAndFormat([value], measure_format)
    document = _zero_horizontal_rich_document_margins(document)
    scale_factor = QgsTextRenderer.calculateScaleFactorForFormat(
        render_ctx, measure_format
    )
    metrics = QgsTextDocumentMetrics.calculateMetrics(
        document, measure_format, render_ctx, scale_factor
    )

    mode = getattr(getattr(Qgis, "TextLayoutMode", None), "Rectangle", None)
    orientation = getattr(getattr(Qgis, "TextOrientation", None), "Horizontal", None)
    if mode is None or orientation is None:
        return 0.0

    size = metrics.documentSize(mode, orientation)
    return max(0.0, float(size.width()))


def _preserve_html_spaces(rich_html):
    """Replace literal spaces in HTML text nodes with non-breaking spaces.

    Rich-document measurement must preserve source whitespace when a fragment
    is measured independently from its neighbouring words.  Attribute values
    (including font-family names) are left untouched.
    """
    value = str(rich_html or "")
    if not value:
        return value

    def _replace(match):
        return ">" + match.group(1).replace(" ", "\u00a0") + "<"

    return re.sub(r">([^<>]*)<", _replace, value)


def _rich_words_and_spaces(item, render_ctx, text_start, plain_line,
                           text_format, segments, rich_content=True,
                           base_font=None, base_color=None):
    """Return QGIS-measured rich words and inter-word whitespace widths."""
    line_text = str(plain_line or "")
    matches = list(re.finditer(r"\S+", line_text))
    if not matches:
        return [], 0.0

    segments = segments or []
    # Render-as-HTML composes in fixed painter units. Its word fragments must
    # use that same resolved QGIS base font, not Qt's implicit document font.
    if base_font is None or base_color is None:
        base_font = item._text_format.font()
        base_color = item._text_format.color()
    words = []
    total_word_width = 0.0
    total_space_width = 0.0
    for index, match in enumerate(matches):
        word_start = int(text_start) + match.start()
        word_len = match.end() - match.start()
        if rich_content:
            word_html = segments_slice_to_html(
                segments, word_start, word_len, base_font, base_color)
            if not word_html:
                word_html = _html_escape(match.group(0), quote=False)
            word_width = _measure_rich_fragment_width(
                render_ctx, text_format, word_html)
        else:
            word_html = match.group(0)
            try:
                word_width = float(QgsTextRenderer.textWidth(
                    render_ctx, text_format, [word_html]))
            except Exception:
                word_width = 0.0
        if word_width <= 0.0:
            return [], 0.0

        words.append((word_html, word_width))
        total_word_width += word_width

        if index + 1 < len(matches):
            gap_text = line_text[match.end():matches[index + 1].start()]
            if rich_content:
                gap_html = segments_slice_to_html(
                    segments, int(text_start) + match.end(), len(gap_text),
                    base_font, base_color)
                if not gap_html:
                    gap_html = _html_escape(gap_text, quote=False)
                gap_html = _preserve_html_spaces(gap_html)
                gap_width = _measure_rich_fragment_width(
                    render_ctx, text_format, gap_html) if gap_html else 0.0
            else:
                gap_html = gap_text
                try:
                    gap_width = float(QgsTextRenderer.textWidth(
                        render_ctx, text_format, [gap_html])) if gap_html else 0.0
                except Exception:
                    gap_width = 0.0
            total_space_width += max(0.0, float(gap_width))

    return words, total_space_width


def _compose_rich_line_commands(item, render_ctx, text_start, plain_line,
                                row_x, row_y, row_width, text_format,
                                segments, line_height, left_align,
                                alignment, rich_content=True,
                                base_font=None, base_color=None):
    """Compose a rich-text line from independently measured word fragments.

    The same compositor is used for left, center, right, and justify rich
    text.  Words are measured through the exact QgsTextDocument metrics path
    used to paint each fragment, avoiding a second line-level rich-document
    wrapping/layout pass which can disagree with QTextLayout for condensed
    fonts.
    """
    words, natural_space_width = _rich_words_and_spaces(
        item, render_ctx, text_start, plain_line, text_format, segments,
        rich_content=rich_content, base_font=base_font,
        base_color=base_color)
    if not words:
        return None

    available = max(0.0, float(row_width))
    word_total = sum(width for _html, width in words)
    justify = alignment == "justify" and len(words) >= 2

    if justify:
        remaining = available - word_total
        tolerance = max(0.5, available * 0.001)
        if remaining < -tolerance:
            return None
        gap = max(0.0, remaining / (len(words) - 1))
        start_x = float(row_x)
        gap_widths = [gap] * (len(words) - 1)
    else:
        natural_total = word_total + natural_space_width
        if alignment == "right":
            start_x = float(row_x) + max(0.0, available - natural_total)
        elif alignment == "center":
            start_x = float(row_x) + max(0.0, (available - natural_total) / 2.0)
        else:
            start_x = float(row_x)

        if len(words) > 1:
            line_text = str(plain_line or "")
            matches = list(re.finditer(r"\S+", line_text))
            gap_widths = []
            if base_font is None or base_color is None:
                base_font = item._text_format.font()
                base_color = item._text_format.color()
            segments = segments or []
            for index in range(len(matches) - 1):
                gap_text = line_text[
                    matches[index].end():matches[index + 1].start()]
                if rich_content:
                    gap_html = segments_slice_to_html(
                        segments, int(text_start) + matches[index].end(),
                        len(gap_text), base_font, base_color)
                    gap_html = _preserve_html_spaces(
                        gap_html or _html_escape(gap_text, quote=False))
                    if gap_html and not gap_html.strip():
                        # HTML collapses a whitespace-only fragment to no visible
                        # advance. Preserve the source whitespace explicitly so
                        # non-justify rich text retains normal inter-word gaps.
                        gap_html = "&nbsp;" * len(gap_text)
                    try:
                        gap_width = _measure_rich_fragment_width(
                            render_ctx, text_format, gap_html) if gap_html else 0.0
                    except Exception:
                        gap_width = 0.0
                else:
                    try:
                        gap_width = float(QgsTextRenderer.textWidth(
                            render_ctx, text_format, [gap_text])) if gap_text else 0.0
                    except Exception:
                        gap_width = 0.0
                gap_widths.append(max(0.0, float(gap_width)))
        else:
            gap_widths = []

    commands = []
    x = start_x
    for index, (word_html, word_width) in enumerate(words):
        commands.append({
            "rect": (x, row_y, word_width, line_height),
            "alignment": left_align,
            "text": word_html,
            "format": text_format,
            "translate": (0.0, 0.0),
            "scale": (1.0, 1.0),
            "context_boost": 1.0,
            "zero_horizontal_margins": True,
        })
        if index + 1 < len(words):
            x += word_width + gap_widths[index]
    return commands


def _lock_rich_commands_to_row_advance(commands, origin_x, target_width):
    """Apply the plain-text row transform to a sequence of rich fragments.

    Plain polygon text is not aligned by asking the painter to re-evaluate an
    alignment flag.  The compositor fixes a row's left origin from the
    QTextLayout result, then applies one horizontal transform so the final
    paint advance matches that row.  Rich text must do exactly the same: its
    document fragments can have subtly different metrics, but they are still
    parts of one already-aligned row.  Scaling each command about the shared
    row origin retains inline formatting while keeping left/centre/right
    geometrically identical to the normal-text path.

    Justified rows deliberately do not call this helper.  Their individual
    word positions already define the authoritative full-span layout.
    """
    if not commands:
        return commands
    try:
        origin_x = float(origin_x)
        target_width = float(target_width)
        if target_width <= 0.0:
            return commands
        first_x = min(float(command["rect"][0]) for command in commands)
        last_x = max(
            float(command["rect"][0]) + float(command["rect"][2])
            for command in commands)
        natural_width = last_x - first_x
        if natural_width <= 0.0:
            return commands
        scale = max(0.25, min(4.0, target_width / natural_width))
        locked = []
        for command in commands:
            result = dict(command)
            x, y, width, height = result["rect"]
            # The fragment compositor starts non-justified lines at origin_x.
            # Use first_x defensively so the transformation remains stable if
            # a future rich-text feature introduces a leading run offset.
            result["rect"] = (
                (float(x) - first_x) / scale, y, width, height)
            result["translate"] = (origin_x, 0.0)
            result["scale"] = (scale, 1.0)
            locked.append(result)
        return locked
    except (KeyError, TypeError, ValueError, IndexError):
        return commands


def _compose_justify_commands(item, render_ctx, text_start, plain_line,
                              row_x, row_y, row_width, text_format,
                              segments, line_height, left_align):
    """Build the single authoritative Polygon Text justification layout."""
    return _compose_rich_line_commands(
        item, render_ctx, text_start, plain_line, row_x, row_y, row_width,
        text_format, segments, line_height, left_align, "justify",
        rich_content=False)



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
        # v1.0.2: cubic Bezier handles are present on every polygon
        # anchor. Their default collinear positions reproduce the original
        # straight polygon exactly while making every edge immediately editable.
        self._bezier_handles = bounded_polygon_handles(self._nodes)
        # Transient edit-state only.  This is deliberately not serialized.
        self._active_node_index = -1
        self._active_handle = None
        # Transient cache for the layout plan.  The cache key excludes zoom so
        # line breaks stay pinned once composed.
        self._layout_cache_key = None
        self._layout_cache = None
        # Keep a few immutable final paint plans so mode switches do not force
        # a fresh advance/justification calculation for unchanged content.
        self._frozen_paint_plans = {}
        # One completed preview picture. It contains no scene or painter
        # references and is replaced (not accumulated) when invalidated.
        self._effect_preview_picture = None

    # ---------------------------------------------------------------- identity
    def type(self):        return POLYGON_TEXT_ITEM_TYPE
    def icon(self):        return polygon_item_icon()
    def displayName(self): return "Polygon Text"

    def estimatedFrameBleed(self):
        """Include the edit-time node circles in the item's paint bounds."""
        try:
            inherited = float(super().estimatedFrameBleed())
        except Exception:
            inherited = 0.0
        return max(inherited, 1.65)

    def boundingRect(self):
        """Report the item plus all editable Bezier geometry in the fixed frame.

        Polygon node/handle coordinates are intentionally allowed outside the
        item rect while editing.  The item rect is *not* used as a moving
        normalisation frame, so crossing x=0/y=0 cannot reinterpret the closed
        path.  Include anchors as well as controls so a node moved outside the
        current rect remains inside the QGraphics bounding box.
        """
        try:
            bounds = QRectF(super().boundingRect())
        except Exception:
            bounds = QRectF(self.rect())

        rect = QRectF(self.rect())
        geometry_bounds = QRectF(rect)
        width = rect.width()
        height = rect.height()
        if width <= 0.0 or height <= 0.0:
            geometry_bounds.adjust(-1.65, -1.65, 1.65, 1.65)
            return bounds.united(geometry_bounds)

        try:
            for node in self._nodes:
                geometry_bounds = geometry_bounds.united(
                    QRectF(node.x() * width, node.y() * height, 0.0, 0.0))
            handles = clone_handles(self._bezier_handles, len(self._nodes))
            defaults = straight_handles(self._nodes, closed=True)
            for i, rec in enumerate(handles):
                for kind in ("in", "out"):
                    hp = rec.get(kind)
                    if hp is None and i < len(defaults):
                        hp = defaults[i].get(kind)
                    if hp is not None:
                        geometry_bounds = geometry_bounds.united(
                            QRectF(hp.x() * width, hp.y() * height, 0.0, 0.0))
        except Exception:
            record_suppressed_exception()

        geometry_bounds.adjust(-1.65, -1.65, 1.65, 1.65)
        return bounds.united(geometry_bounds)

    def shape(self):
        """Use the real polygon for initial selection.

        Once selected, QGIS' normal rectangular item shape is restored so its
        standard move, resize and rotation handles retain their full behavior.
        """
        if self.isSelected():
            return super().shape()

        points, handles = self._local_bezier_geometry()
        if len(points) < 3:
            return super().shape()
        return build_bezier_path(points, handles, closed=True)

    def _request_selection_repaint(self):
        """Request a normal item repaint after selection changes.

        Calling ``QGraphicsScene.update()`` or a viewport update from within
        ``itemChange(ItemSelectedChange)`` can recursively enter Qt's scene
        foreground painting while a layout sketch is completing. ``update()``
        is queued by QGraphicsItem and is sufficient to refresh node handles.
        """
        try:
            self.update()
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
    def setActiveNodeIndex(self, index):
        """Highlight the node currently targeted by the node-edit tool."""
        index = int(index) if index is not None else -1
        if index < 0 or index >= len(self._nodes):
            index = -1
        if index != self._active_node_index:
            self._active_node_index = index
            self.update()

    def activeNodeIndex(self):
        return self._active_node_index

    def setActiveHandle(self, index=None, kind=None):
        value = None
        if index is not None and kind in ("in", "out") and 0 <= int(index) < len(self._nodes):
            value = (int(index), kind)
        if value != self._active_handle:
            self._active_handle = value
            self.update()

    def activeHandle(self):
        return self._active_handle

    def _local_bezier_geometry(self):
        rect = self.rect()
        points = [QPointF(n.x() * rect.width(), n.y() * rect.height()) for n in self._nodes]
        handles = clone_handles(self._bezier_handles, len(self._nodes))
        # Do not mutate live geometry during paint.  Legacy files may lack a
        # control, but the rendering fallback is a local copy only.
        defaults = straight_handles(self._nodes, closed=True)
        for i, rec in enumerate(handles):
            for kind in ("in", "out"):
                pt = rec.get(kind)
                if pt is None and i < len(defaults):
                    pt = defaults[i].get(kind)
                if pt is not None:
                    rec[kind] = QPointF(pt.x() * rect.width(), pt.y() * rect.height())
        return points, handles

    def handleScenePositions(self):
        handles = clone_handles(self._bezier_handles, len(self._nodes))
        defaults = straight_handles(self._nodes, closed=True)
        for i, rec in enumerate(handles):
            for kind in ("in", "out"):
                if rec.get(kind) is None and i < len(defaults):
                    rec[kind] = defaults[i].get(kind)
        return handle_scene_records(self, self._nodes, handles)

    def nodeMode(self, index):
        if 0 <= index < len(self._bezier_handles):
            return self._bezier_handles[index].get("mode", "corner")
        return "corner"

    def _refresh_bezier_geometry_bounds(self):
        """Refresh painting and QGraphics bounds without changing the frame.

        The polygon's editable geometry lives in one stable local coordinate
        frame.  QGraphics derives its paint bounds from :meth:`boundingRect`, so
        changing a handle/node only requires a geometry-change notification and
        repaint; there is no reason to rewrite every node against a new rect.
        """
        old_rect = None
        try:
            old_rect = QRectF(self.sceneBoundingRect())
            old_rect.adjust(-6.0, -6.0, 6.0, 6.0)
        except (RuntimeError, TypeError):
            old_rect = None
        try:
            self.prepareGeometryChange()
        except (AttributeError, RuntimeError):
            record_suppressed_exception()
        self._layout_cache_key = None
        self._layout_cache = None
        self.update()
        try:
            scene = self.scene()
        except RuntimeError:
            scene = None
        if scene is not None:
            try:
                new_rect = QRectF(self.sceneBoundingRect())
                new_rect.adjust(-6.0, -6.0, 6.0, 6.0)
            except (RuntimeError, TypeError):
                new_rect = None
            dirty = None
            for candidate in (old_rect, new_rect):
                if candidate is not None and candidate.isValid() and not candidate.isNull():
                    dirty = QRectF(candidate) if dirty is None else dirty.united(candidate)
            try:
                scene.update(dirty) if dirty is not None else scene.update()
            except RuntimeError:
                record_suppressed_exception()
            try:
                for view in scene.views():
                    view.viewport().update()
            except RuntimeError:
                record_suppressed_exception()

    def setNodeMode(self, index, mode):
        if not (0 <= index < len(self._nodes)) or mode not in ("corner", "smooth", "symmetric"):
            return False
        self._bezier_handles = ensure_closed_handles(
            self._nodes, self._bezier_handles)
        self._bezier_handles, changed = apply_node_mode(
            self._nodes, self._bezier_handles, index, mode, closed=True)
        if not changed:
            return False
        self._tighten_geometry_frame()
        return True

    def resetNodeHandles(self, index):
        if not (0 <= index < len(self._nodes)):
            return False
        self._bezier_handles = ensure_closed_handles(
            self._nodes, self._bezier_handles)
        defaults = bounded_polygon_handles(self._nodes)
        self._bezier_handles[index]["in"] = defaults[index]["in"]
        self._bezier_handles[index]["out"] = defaults[index]["out"]
        self._bezier_handles[index]["mode"] = "corner"
        self._tighten_geometry_frame()
        return True

    def nearestBezierSegment(self, scene_pos):
        local = self.mapFromScene(scene_pos)
        points, handles = self._local_bezier_geometry()
        return nearest_segment(local, points, handles, closed=True)

    def convertSegmentToCurve(self, index):
        self._bezier_handles = convert_segment_to_curve(
            self._nodes, self._bezier_handles, index, closed=True)
        bounded = bounded_polygon_handles(self._nodes)
        if 0 <= index < len(self._nodes):
            j = (index + 1) % len(self._nodes)
            # Only constrain the newly-created controls. Existing custom
            # controls are preserved exactly.
            if self._bezier_handles[index].get("mode", "corner") == "corner":
                self._bezier_handles[index]["out"] = bounded[index]["out"]
            if self._bezier_handles[j].get("mode", "corner") == "corner":
                self._bezier_handles[j]["in"] = bounded[j]["in"]
        self._layout_cache_key = None
        self._layout_cache = None
        self._tighten_geometry_frame()
        return True

    def convertSegmentToStraight(self, index):
        if not (0 <= index < len(self._nodes)):
            return False
        self._bezier_handles = ensure_closed_handles(
            self._nodes, self._bezier_handles)
        j = (index + 1) % len(self._nodes)
        a, b = self._nodes[index], self._nodes[j]
        self._bezier_handles[index]["out"] = QPointF(
            a.x() + (b.x() - a.x()) / 3.0,
            a.y() + (b.y() - a.y()) / 3.0)
        self._bezier_handles[j]["in"] = QPointF(
            a.x() + 2.0 * (b.x() - a.x()) / 3.0,
            a.y() + 2.0 * (b.y() - a.y()) / 3.0)
        self._layout_cache_key = None
        self._layout_cache = None
        self._tighten_geometry_frame()
        return True

    def _scene_geometry(self):
        anchors = self.nodeScenePositions()
        handle_map = {(i, kind): pt for i, kind, pt in self.handleScenePositions()}
        return anchors, handle_map

    def _apply_scene_geometry(self, anchors, handle_map):
        """Resize the QGIS item while preserving every Bezier point in scene space."""
        all_points = list(anchors) + list(handle_map.values())
        if not all_points:
            return

        xs = [p.x() for p in all_points]
        ys = [p.y() for p in all_points]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)

        # This is the reference build's fixed edit margin. It keeps the
        # visual node/handle affordances inside the QGraphics bounds without
        # accumulating unused space as the shape is edited.
        edit_margin = 1.75
        min_x -= edit_margin
        min_y -= edit_margin
        max_x += edit_margin
        max_y += edit_margin
        new_w = max(max_x - min_x, 5.0)
        new_h = max(max_y - min_y, 5.0)

        old_rect = QRectF(self.sceneBoundingRect())
        target_rect = QRectF(min_x, min_y, new_w, new_h)
        try:
            self.attemptSetSceneRect(target_rect)
        except Exception:
            record_suppressed_exception()
            return

        # The snapshot, not derived controls or node modes, is authoritative
        # after the frame change. Thus the closed seam cannot be recalculated
        # differently from any other segment.
        local_rect = QRectF(self.rect())
        if local_rect.width() <= 0.0 or local_rect.height() <= 0.0:
            return
        nodes = []
        for point in anchors:
            local = self.mapFromScene(point)
            nodes.append(QPointF(
                local.x() / local_rect.width(),
                local.y() / local_rect.height()))

        hs = clone_handles(self._bezier_handles, len(nodes))
        for i, rec in enumerate(hs):
            for kind in ("in", "out"):
                scene_handle = handle_map.get((i, kind))
                if scene_handle is None:
                    rec[kind] = None
                    continue
                local = self.mapFromScene(scene_handle)
                rec[kind] = QPointF(
                    local.x() / local_rect.width(),
                    local.y() / local_rect.height())
        self._nodes = nodes
        self._bezier_handles = hs
        self._layout_cache_key = None
        self._layout_cache = None

        try:
            new_rect = QRectF(self.sceneBoundingRect())
            dirty = old_rect.united(new_rect)
            self.scene().update(dirty) if self.scene() is not None else self.update()
        except (AttributeError, RuntimeError):
            record_suppressed_exception()

    def _tighten_geometry_frame(self):
        """Fit the item to nodes and handles from an invariant scene snapshot."""
        try:
            anchors = self.nodeScenePositions()
            handle_map = {
                (i, kind): point
                for i, kind, point in self.handleScenePositions()
            }
        except (AttributeError, RuntimeError):
            record_suppressed_exception()
            return
        if len(anchors) < 3:
            return
        self._apply_scene_geometry(anchors, handle_map)
        self._refresh_bezier_geometry_bounds()

    def setBezierHandleAtScenePos(self, index, kind, scene_pos, independent=False, constrain=False):
        if not (0 <= index < len(self._nodes)) or kind not in ("in", "out"):
            return

        rect = QRectF(self.rect())
        if rect.width() <= 0.0 or rect.height() <= 0.0:
            return

        anchors, handle_map = self._scene_geometry()
        anchor = anchors[index]
        target = QPointF(scene_pos)
        if constrain:
            dx, dy = target.x() - anchor.x(), target.y() - anchor.y()
            radius = (dx * dx + dy * dy) ** 0.5
            if radius > 0:
                import math
                step = math.pi / 12.0
                angle = round(math.atan2(dy, dx) / step) * step
                target = QPointF(anchor.x() + math.cos(angle) * radius,
                                 anchor.y() + math.sin(angle) * radius)

        mode = self.nodeMode(index)
        opposite = "out" if kind == "in" else "in"
        handle_map[(index, kind)] = target
        if not independent and mode in ("smooth", "symmetric"):
            old_other = handle_map.get((index, opposite))
            vx, vy = target.x() - anchor.x(), target.y() - anchor.y()
            length = (vx * vx + vy * vy) ** 0.5
            if length > 1e-9:
                if mode == "symmetric" or old_other is None:
                    other_len = length
                else:
                    other_len = ((old_other.x() - anchor.x()) ** 2 +
                                 (old_other.y() - anchor.y()) ** 2) ** 0.5
                handle_map[(index, opposite)] = QPointF(
                    anchor.x() - vx / length * other_len,
                    anchor.y() - vy / length * other_len)

        try:
            self.prepareGeometryChange()
        except (AttributeError, RuntimeError):
            record_suppressed_exception()

        local_handles = clone_handles(self._bezier_handles, len(self._nodes))
        for handle_kind in (kind, opposite):
            scene_handle = handle_map.get((index, handle_kind))
            if scene_handle is None:
                continue
            local = self.mapFromScene(scene_handle)
            local_handles[index][handle_kind] = QPointF(
                local.x() / rect.width(), local.y() / rect.height())
        self._bezier_handles = local_handles
        self._tighten_geometry_frame()

    def nodeScenePositions(self):
        rect = self.rect()
        return [
            self.mapToScene(QPointF(n.x()*rect.width(), n.y()*rect.height()))
            for n in self._nodes
        ]

    def setNodeAtScenePos(self, index, scene_pos):
        """Move one anchor and keep the item frame tight to its geometry."""
        if not (0 <= index < len(self._nodes)):
            return
        rect = QRectF(self.rect())
        if rect.width() <= 0.0 or rect.height() <= 0.0:
            return

        try:
            self.prepareGeometryChange()
        except (AttributeError, RuntimeError):
            record_suppressed_exception()

        local = self.mapFromScene(QPointF(scene_pos))
        dx_norm = local.x() / rect.width() - self._nodes[index].x()
        dy_norm = local.y() / rect.height() - self._nodes[index].y()
        self._nodes[index] = QPointF(
            local.x() / rect.width(), local.y() / rect.height())

        # Move this node's own handles with the anchor in the current frame.
        rec = self._bezier_handles[index] if index < len(self._bezier_handles) else None
        if isinstance(rec, dict):
            for kind in ("in", "out"):
                handle = rec.get(kind)
                if handle is not None:
                    rec[kind] = QPointF(
                        handle.x() + dx_norm, handle.y() + dy_norm)

        self._tighten_geometry_frame()

    def insertNodeNearestEdge(self, scene_pos):
        rect = self.rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return
        local = self.mapFromScene(scene_pos)
        norm_local = QPointF(local.x() / rect.width(), local.y() / rect.height())
        seg_index, t, _nearest, _distance = nearest_segment(
            norm_local, self._nodes, self._bezier_handles, closed=True)
        if seg_index < 0:
            return
        self._nodes, self._bezier_handles, _new_index = split_segment(
            self._nodes, self._bezier_handles, seg_index, t, closed=True)
        self._layout_cache_key = None
        self._layout_cache = None
        self._tighten_geometry_frame()

    def removeNodeAt(self, index):
        if len(self._nodes) <= 3 or not (0 <= index < len(self._nodes)):
            return False
        anchors, handle_map = self._scene_geometry()
        del anchors[index]
        new_map = {}
        for (i, kind), pt in handle_map.items():
            if i == index:
                continue
            new_i = i - 1 if i > index else i
            new_map[(new_i, kind)] = pt
        hs = clone_handles(self._bezier_handles, len(self._nodes))
        del hs[index]
        new_hs = clone_handles(hs, len(anchors))
        for (i, kind), pt in new_map.items():
            if 0 <= i < len(new_hs):
                local = self.mapFromScene(pt)
                rect = QRectF(self.rect())
                if rect.width() > 0.0 and rect.height() > 0.0:
                    new_hs[i][kind] = QPointF(
                        local.x() / rect.width(), local.y() / rect.height())
        self._bezier_handles = new_hs
        rect = QRectF(self.rect())
        if rect.width() > 0.0 and rect.height() > 0.0:
            self._nodes = [
                QPointF(
                    self.mapFromScene(p).x() / rect.width(),
                    self.mapFromScene(p).y() / rect.height(),
                ) for p in anchors
            ]
        self._layout_cache_key = None
        self._layout_cache = None
        self._tighten_geometry_frame()
        self._request_selection_repaint()
        return True

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
            self._bezier_handles = bounded_polygon_handles(self._nodes)
            self._layout_cache_key = None
            self._layout_cache = None
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
            self._bezier_handles = bounded_polygon_handles(self._nodes)
            self._layout_cache_key = None
            self._layout_cache = None
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
        return text_format_base_font(self._text_format), QColor(self._text_format.color())

    def _has_picture_cacheable_effect(self):
        """Return whether a preview effect benefits from QPicture replay."""
        try:
            # QGIS buffers are stroked glyph geometry. Recording those paths
            # is slower than the native direct buffer renderer.
            if self._text_format.buffer().enabled():
                return False
        except Exception:
            record_suppressed_exception()
        settings = [self._text_format]
        for name in ("background", "shadow"):
            try:
                component = getattr(self._text_format, name)()
                settings.append(component)
                if component.enabled():
                    return True
            except Exception:
                record_suppressed_exception()
        for owner in settings:
            for getter_name in (
                    "paintEffect", "paintEffectStack", "effect",
                    "effectStack", "drawEffect", "drawEffects"):
                try:
                    effect = getattr(owner, getter_name)()
                    if effect is not None and bool(effect.enabled()):
                        return True
                except AttributeError:
                    continue
                except Exception:
                    record_suppressed_exception()
        return False

    def _effect_preview_picture_key(self, resolved_text):
        """Return a conservative, zoom-independent preview cache key."""
        try:
            inline_html = text_format_allows_html(self._text_format)
        except Exception:
            inline_html = False
        return (
            "effect-picture-isolated-v1",
            self._layout_signature(
                resolved_text, self._allow_html, inline_html),
            self._text_format_revision,
        )

    def _disable_preview_item_cache(self):
        """Disable Qt item caching for QGIS' transformed layout painter."""
        owner = QtWidgets.QGraphicsItem
        scoped = getattr(owner, "CacheMode", None)
        no_cache = getattr(scoped, "NoCache", None)
        if no_cache is None:
            no_cache = getattr(owner, "NoCache", None)
        if no_cache is None:
            return
        try:
            if self.cacheMode() == no_cache:
                return
            # QGIS applies its own layout-unit/preview transform. Qt's item
            # coordinate cache replays it as a second transform and can move
            # polygon content, so use only the explicit text-picture cache.
            self.setCacheMode(no_cache)
        except Exception:
            record_suppressed_exception()



    def _layout_signature(self, resolved_text, render_html=False,
                          inline_html=False, render_html_plain=False,
                          plain_compatible=False):
        """Build a zoom-independent signature for the current polygon text plan."""
        rect = self.rect()

        def _safe_float(v, nd=6):
            try:
                return round(float(v), nd)
            except Exception:
                return 0.0

        nodes_sig = tuple((_safe_float(n.x(), 6), _safe_float(n.y(), 6)) for n in self._nodes)
        bezier_sig = serialise_handles(self._bezier_handles)
        text_sig = hashlib.blake2b(
            (resolved_text or "").encode("utf-8"),
            digest_size=16).digest()

        if plain_compatible:
            # The native Allow HTML flag changes QgsTextFormat's revision even
            # when there are no tags to render. Key compatible text by the
            # actual font/layout properties instead, so its frozen plan can be
            # reused across all three mode toggles.
            font, _color = self._base_font_and_color()
            try:
                font_sig = font.toString()
            except Exception:
                font_sig = repr(font)
            try:
                size_unit = str(self._text_format.sizeUnit())
            except Exception:
                size_unit = ""
            try:
                line_height = float(self._text_format.lineHeight())
            except Exception:
                line_height = 1.0
            try:
                capitalization = str(self._text_format.capitalization())
            except Exception:
                capitalization = ""
            return (
                "plain_compatible", _safe_float(rect.width(), 6),
                _safe_float(rect.height(), 6), nodes_sig, bezier_sig, text_sig,
                font_sig, _safe_float(self._text_format.size(), 6), size_unit,
                _safe_float(line_height, 6), capitalization,
                int(self._h_align), int(self._v_align),
                _safe_float(self._padding, 6),
                _safe_float(self._h_margin, 6),
                _safe_float(self._v_margin, 6),
            )

        if render_html:
            return (
                "render_html", _safe_float(rect.width(), 6),
                _safe_float(rect.height(), 6), nodes_sig, bezier_sig, text_sig,
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
            bezier_sig,
            text_sig,
            bool(render_html),
            bool(inline_html),
            bool(render_html_plain),
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
        # Do not mutate QGraphicsItem cache state from inside draw().
        # QGIS/Qt may be traversing the scene's paint/cache machinery at this
        # point; v1.0.1 left the item cache state untouched during painting.
        painter.save()
        try:
            painter.setRenderHint(AA_ANTIALIASING, True)
            painter.setRenderHint(AA_TEXT_ANTIALIASING, True)

            rect = self.rect()
            anchor_layout, handle_layout = self._local_bezier_geometry()
            # The text wrapping engine works on a polygon. Curved boundaries
            # are adaptively flattened at sub-millimetre tolerance, while the
            # editable geometry remains exact cubic Bezier data.
            poly_layout = flatten_bezier(
                anchor_layout, handle_layout, closed=True, tolerance=0.15)
            poly_px = [QPointF(p.x() * scale_factor, p.y() * scale_factor)
                       for p in poly_layout]
            qpoly  = QPolygonF(poly_px)
            anchor_px = [QPointF(p.x() * scale_factor, p.y() * scale_factor)
                         for p in anchor_layout]
            handle_px = clone_handles(handle_layout, len(anchor_layout))
            for rec in handle_px:
                for kind in ("in", "out"):
                    hp = rec.get(kind)
                    if hp is not None:
                        rec[kind] = QPointF(hp.x() * scale_factor, hp.y() * scale_factor)
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
            elif is_preview_render and self.isSelected():
                # Keep the editable polygon boundary visible even when the
                # user has disabled the exported/printed frame.  This is an
                # edit-only guide: it deliberately ignores any stored custom
                # frame colour/width while the frame is disabled.
                guide_pen = QPen(QColor(150, 150, 150), 0.20 * scale_factor)
                painter.setPen(guide_pen)
                painter.setBrush(QBrush(NO_BRUSH))
                painter.drawPolygon(qpoly)

            painter.save()
            try:
                painter.setClipPath(_polygon_clip_path(qpoly))
                resolved = evaluate_expressions(self._text, self)
                if resolved.strip():
                    previous_text_render_format = None
                    previous_render_context_scale = None
                    outline_format = None
                    try:
                        previous_text_render_format = render_ctx.textRenderFormat()
                    except Exception:
                        record_suppressed_exception()
                    try:
                        # Rich polygon composition temporarily replaces this
                        # shared QGIS context value with its fixed metric scale.
                        # Keep the entry value here so every paint exit,
                        # including an interrupted HTML/justify draw, restores
                        # the context observed by the next layout item.
                        previous_render_context_scale = float(
                            render_ctx.scaleFactor())
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
                        # QPicture replay is deliberately disabled. Even with
                        # a cloned QgsRenderContext, recording Qt/QGIS paint
                        # effects during an active Layout scene paint has
                        # caused native access violations on QGIS 4 / Qt 6.
                        # Direct rendering is the supported, stable path.
                        picture_key = None
                        cached_picture = None
                        if picture_key is not None:
                            try:
                                cached = self._effect_preview_picture
                                if cached is not None and cached[0] == picture_key:
                                    cached_picture = cached[1]
                            except Exception:
                                record_suppressed_exception()
                        if cached_picture is not None:
                            painter.save()
                            try:
                                painter.scale(scale_factor, scale_factor)
                                painter.drawPicture(0, 0, cached_picture)
                            finally:
                                painter.restore()
                        elif picture_key is None:
                            self._draw_wrapped_text(
                                render_ctx, poly_px, resolved,
                                pad_px, hm_px, vm_px, scale_factor)
                        else:
                            # Record through a *cloned* render context. The
                            # live layout context is never given another
                            # painter, which is the unsafe v1.0.34 behaviour.
                            picture = QtGui.QPicture()
                            recording_painter = QtGui.QPainter(picture)
                            recorded = False
                            isolated_context = None
                            try:
                                isolated_context = QgsRenderContext(render_ctx)
                                isolated_context.setPainter(recording_painter)
                                recording_painter.setClipPath(
                                    _polygon_clip_path(QPolygonF(poly_layout)))
                                self._draw_wrapped_text(
                                    isolated_context, poly_layout, resolved,
                                    self._padding, self._h_margin,
                                    self._v_margin, 1.0)
                                recorded = True
                            except Exception:
                                record_suppressed_exception()
                            finally:
                                # End before retaining the picture. The cloned
                                # context and its temporary painter are then
                                # discarded together, without touching the
                                # live QGIS render context.
                                try:
                                    recording_painter.end()
                                except Exception:
                                    record_suppressed_exception()
                                isolated_context = None
                            if recorded:
                                painter.save()
                                try:
                                    painter.scale(scale_factor, scale_factor)
                                    painter.drawPicture(0, 0, picture)
                                finally:
                                    painter.restore()
                                try:
                                    bounds = picture.boundingRect()
                                    pixels = max(0, bounds.width()) * max(
                                        0, bounds.height())
                                    if pixels <= 24_000_000:
                                        self._effect_preview_picture = (
                                            picture_key, picture)
                                except Exception:
                                    record_suppressed_exception()
                            else:
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
                        if previous_render_context_scale is not None:
                            try:
                                render_ctx.setScaleFactor(
                                    previous_render_context_scale)
                            except Exception:
                                record_suppressed_exception()
            finally:
                painter.restore()

            if is_preview_render and self.isSelected():
                painter.save()
                try:
                    painter.setClipping(False)
                    self._draw_bezier_handles(
                        painter, anchor_px, handle_px, scale_factor)
                finally:
                    painter.restore()

        finally:
            painter.restore()

    def _draw_bezier_handles(self, painter, anchors_px, handles_px, scale_factor):
        normal_pen = QPen(QColor(40, 140, 90), 0.25 * scale_factor)
        active_pen = QPen(QColor(190, 85, 0), 0.35 * scale_factor)
        arm_pen = QPen(QColor(95, 140, 115), 0.20 * scale_factor)
        handle_pen = QPen(QColor(55, 125, 95), 0.22 * scale_factor)
        active_handle_pen = QPen(QColor(190, 85, 0), 0.32 * scale_factor)
        normal_brush = QColor(255, 255, 255)
        active_brush = QColor(255, 170, 45)
        handle_brush = QColor(225, 245, 235)
        r = 1.4 * scale_factor
        active_r = 1.7 * scale_factor
        hr = 1.0 * scale_factor
        active_hr = 1.25 * scale_factor
        handles_px = clone_handles(handles_px, len(anchors_px))
        for index, anchor in enumerate(anchors_px):
            rec = handles_px[index]
            for kind in ("in", "out"):
                hp = rec.get(kind)
                if hp is None:
                    continue
                painter.setPen(arm_pen)
                painter.setBrush(normal_brush)
                painter.drawLine(anchor, hp)
                if self._active_handle == (index, kind):
                    painter.setPen(active_handle_pen)
                    painter.setBrush(active_brush)
                    painter.drawRect(QRectF(hp.x() - active_hr, hp.y() - active_hr,
                                            2 * active_hr, 2 * active_hr))
                else:
                    painter.setPen(handle_pen)
                    painter.setBrush(handle_brush)
                    painter.drawRect(QRectF(hp.x() - hr, hp.y() - hr,
                                            2 * hr, 2 * hr))
        for index, pt in enumerate(anchors_px):
            if index == self._active_node_index:
                painter.setPen(active_pen)
                painter.setBrush(active_brush)
                painter.drawEllipse(pt, active_r, active_r)
            else:
                painter.setPen(normal_pen)
                painter.setBrush(normal_brush)
                painter.drawEllipse(pt, r, r)

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
                elif len(item) == 10:
                    (ts, tl, lx, ly, lw, paragraph_final, blank,
                     natural_width, span_x, span_width) = item
                    normalized.append((
                        ts, tl, float(lx) * inv, float(ly) * inv,
                        float(lw) * inv, bool(paragraph_final), bool(blank),
                        float(natural_width) * inv, float(span_x) * inv,
                        float(span_width) * inv))
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
            "paint_plan_key": cache_key,
            "lh": float(payload.get("lh", 0.0)) * inv,
            "html_segments": payload.get("html_segments", []),
            "html_plain": payload.get("html_plain", ""),
            # Ratio between QGIS' final painted advance and the QTextLayout
            # measurement font.  It is normally ~1.0, but some condensed/
            # narrow faces resolve with noticeably different advances.
            "metric_width_scale": float(
                payload.get("metric_width_scale", 1.0) or 1.0),
            "positioned": normalized,
        }
        self._layout_cache_key = cache_key
        self._layout_cache = cache

    def _render_polygon_cached_layout(self, render_ctx, scale_factor, cache,
                                      mode_override=None):
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

        # Compose and paint in one fixed text-metric space. The preview/export
        # zoom is represented only by the painter transform, so it cannot
        # alter glyph advances or cause an already-wrapped row to reflow.
        composition_sf = 16.0
        composition_painter = render_ctx.painter()
        previous_context_scale = None
        composition_saved = False
        if composition_painter is not None:
            try:
                previous_context_scale = float(render_ctx.scaleFactor())
                composition_painter.save()
                composition_saved = True
                composition_painter.scale(
                    sf / composition_sf, sf / composition_sf)
                render_ctx.setScaleFactor(composition_sf)
                sf = composition_sf
            except Exception:
                if composition_saved:
                    composition_painter.restore()
                composition_saved = False
                previous_context_scale = None

        def _restore_composition_space():
            if not composition_saved:
                return
            try:
                if previous_context_scale is not None:
                    render_ctx.setScaleFactor(previous_context_scale)
            finally:
                composition_painter.restore()

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
                        * max(0.5, min(1.5, float(
                            cache.get("metric_width_scale", 1.0) or 1.0)))
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

        mode = mode_override or cache.get("mode", "plain")
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

        def _zero_horizontal_block_margins(document):
            """Remove HTML block side margins from single-word documents.

            Geometric justification supplies the exact x coordinate for each
            word. QGIS HTML parsing can attach block margins to a reconstructed
            fragment, which otherwise makes a right-aligned final word stop
            short of the authoritative polygon edge. Keep vertical margins
            intact, but force the horizontal margins to zero.
            """
            result = QgsTextDocument()
            for block in document:
                try:
                    block_format = QgsTextBlockFormat(block.blockFormat())
                    margins = block_format.margins()
                    block_format.setMargins(
                        QgsMargins(0.0, margins.top(), 0.0, margins.bottom()))
                    target_block = QgsTextBlock()
                    target_block.setBlockFormat(block_format)
                    for fragment in block:
                        target_block.append(fragment)
                    result.append(target_block)
                except Exception:
                    record_suppressed_exception()
                    return document
            return result

        def _draw_document(rect, alignment, text, text_format,
                           component_name, zero_horizontal_margins=False):
            """Render one row through QgsTextDocument's public pipeline."""
            component_format = _component_format(
                text_format, component_name)
            try:
                component_format.updateDataDefinedProperties(render_ctx)
            except Exception:
                record_suppressed_exception()
            document = QgsTextDocument.fromTextAndFormat(
                [text], component_format)
            if zero_horizontal_margins:
                document = _zero_horizontal_block_margins(document)
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
                    command["format"], component_name,
                    bool(command.get("zero_horizontal_margins", False)))
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
            if mode in ("render_html", "plain_render_html"):
                return False
            try:
                return bool(self._text_format.background().enabled())
            except Exception:
                return False

        background_enabled = _background_is_enabled()

        def _component_is_enabled(component_name):
            if mode in ("render_html", "plain_render_html"):
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

        if mode == "plain_render_html":
            # Preserve Render-as-HTML's no-effects contract while sharing the
            # exact plain glyph placement for markup-free content.
            row_base_format = _copy_text_format_without_effects(
                self._text_format)
        elif background_enabled:
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
        # This is deliberately done for plain rows too.  It makes the QFont
        # inherited by every renderer agree with QgsTextFormat.size(), rather
        # than allowing rich documents to use a stale independent point size.
        row_base_format = normalised_text_format_font(row_base_format)
        row_base_format = _fixed_polygon_paint_format(
            row_base_format, render_ctx)

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
                # Justification places words independently in order to fill a
                # changing polygon span.  Replaying the background for each
                # word makes a tiled highlight.  A marked carrier is one
                # transparent, span-locked line document, so QGIS creates the
                # same continuous background that it creates for left/centre/
                # right aligned rows while the actual words remain justified.
                carriers = [
                    command["background_carrier"] for command in commands
                    if isinstance(command.get("background_carrier"), dict)
                ]
                if carriers:
                    # QGIS text backgrounds follow individual fragments and
                    # omit whitespace. A justified row is deliberately split
                    # into word fragments, so use one geometric strip for the
                    # authoritative row span instead of replaying tiled runs.
                    try:
                        settings = self._text_format.background()
                        fill = QColor(settings.fillColor())
                    except Exception:
                        settings = None
                        fill = QColor()
                    if not fill.isValid():
                        try:
                            fill = QColor(settings.color())
                        except Exception:
                            fill = QColor(255, 255, 255, 0)
                    for carrier in carriers:
                        painter = render_ctx.painter()
                        if painter is None:
                            continue
                        tx, ty = carrier.get("translate", (0.0, 0.0))
                        sx, sy = carrier.get("scale", (1.0, 1.0))
                        x, y, width, height = carrier["rect"]
                        painter.save()
                        try:
                            if tx or ty:
                                painter.translate(tx, ty)
                            if (abs(sx - 1.0) >= 0.001
                                    or abs(sy - 1.0) >= 0.001):
                                painter.scale(sx, sy)
                            painter.setBrush(QBrush(fill))
                            # The QGIS text-background stroke has its own
                            # enable flag, which is not consistently exposed
                            # by older bindings. Never infer a visible border
                            # solely from the stored stroke colour.
                            painter.setPen(QPen(NO_PEN))
                            painter.drawRect(QRectF(x, y, width, height))
                        except Exception:
                            record_suppressed_exception()
                        finally:
                            painter.restore()
                else:
                    for command in commands:
                        background_command = dict(command)
                        try:
                            background_format = QgsTextFormat(command["format"])
                            background_format.setBackground(
                                self._text_format.background())
                        except Exception:
                            background_format = self._text_format
                        background_command["format"] = background_format
                        _paint_row_component(background_command, "background")
            _paint_buffer_then_text(commands)

        def _thaw_command_plan(kind, text_format):
            """Return an immutable, composition-space text paint plan."""
            plan = cache.get(f"{kind}_command_plan")
            if not isinstance(plan, dict):
                plan_key = cache.get("paint_plan_key")
                plan = self._frozen_paint_plans.get(plan_key)
            if not isinstance(plan, dict):
                return None
            try:
                if plan.get("kind") != kind:
                    return None
                plan_scale = float(plan.get("scale", 0.0))
                if abs(plan_scale - sf) > 1.0e-6:
                    return None
                commands = []
                for rec in plan.get("commands", []):
                    if len(rec) == 12:
                        (text, x, y, width, height, tx, ty, sx, sy,
                         zero_margins, context_boost, carrier_rec) = rec
                    else:
                        (text, x, y, width, height, tx, ty, sx, sy,
                         zero_margins, context_boost) = rec
                        carrier_rec = None
                    command = {
                        "rect": (float(x) * sf, float(y) * sf,
                                 float(width) * sf, float(height) * sf),
                        "alignment": left_align,
                        "text": text,
                        "format": text_format,
                        "translate": (float(tx) * sf, float(ty) * sf),
                        "scale": (float(sx), float(sy)),
                        "zero_horizontal_margins": bool(zero_margins),
                        "context_boost": float(context_boost),
                    }
                    if carrier_rec is not None:
                        (carrier_text, cx, cy, cwidth, cheight,
                         ctx, cty, csx, csy, carrier_zero_margins,
                         carrier_context_boost) = carrier_rec
                        command["background_carrier"] = {
                            "rect": (float(cx) * sf, float(cy) * sf,
                                     float(cwidth) * sf,
                                     float(cheight) * sf),
                            "alignment": left_align,
                            "text": carrier_text,
                            "format": text_format,
                            "translate": (float(ctx) * sf,
                                          float(cty) * sf),
                            "scale": (float(csx), float(csy)),
                            "zero_horizontal_margins": bool(
                                carrier_zero_margins),
                            "context_boost": float(carrier_context_boost),
                        }
                    commands.append(command)
                return commands
            except (TypeError, ValueError, KeyError):
                return None

        def _freeze_command_plan(commands, kind):
            """Persist final glyph advances, including justified word offsets."""
            if sf <= 0.0:
                return
            frozen = []
            try:
                for command in commands:
                    x, y, width, height = command["rect"]
                    tx, ty = command.get("translate", (0.0, 0.0))
                    sx, sy = command.get("scale", (1.0, 1.0))
                    carrier = command.get("background_carrier")
                    carrier_rec = None
                    if isinstance(carrier, dict):
                        cx, cy, cwidth, cheight = carrier["rect"]
                        ctx, cty = carrier.get("translate", (0.0, 0.0))
                        csx, csy = carrier.get("scale", (1.0, 1.0))
                        carrier_rec = (
                            carrier["text"], float(cx) / sf,
                            float(cy) / sf, float(cwidth) / sf,
                            float(cheight) / sf, float(ctx) / sf,
                            float(cty) / sf, float(csx), float(csy),
                            bool(carrier.get(
                                "zero_horizontal_margins", False)),
                            float(carrier.get("context_boost", 1.0)),
                        )
                    frozen.append((
                        command["text"], float(x) / sf, float(y) / sf,
                        float(width) / sf, float(height) / sf,
                        float(tx) / sf, float(ty) / sf,
                        float(sx), float(sy),
                        bool(command.get("zero_horizontal_margins", False)),
                        float(command.get("context_boost", 1.0)),
                        carrier_rec,
                    ))
                plan = {
                    "kind": kind, "scale": float(sf),
                    "commands": tuple(frozen)}
                cache[f"{kind}_command_plan"] = plan
                plan_key = cache.get("paint_plan_key")
                if plan_key is not None:
                    if (plan_key not in self._frozen_paint_plans
                            and len(self._frozen_paint_plans) >= 12):
                        self._frozen_paint_plans.clear()
                    self._frozen_paint_plans[plan_key] = plan
            except (TypeError, ValueError, KeyError):
                # A plan is an optimisation only; rendering continues through
                # the normal path if a binding supplies an unusual value.
                return

        def _attach_justify_row_background(command, text, text_format,
                                           row_x, row_y, row_width):
            """Attach a continuous background for every justified row.

            QGIS intentionally leaves the paragraph-final and one-word rows
            un-justified. They still belong to a justified paragraph and must
            retain a background, using their natural text width rather than
            the full scan-line span.
            """
            if (not background_enabled
                    or self._h_align != self.ALIGN_JUSTIFY_):
                return
            try:
                width = max(0.0, float(row_width))
            except (TypeError, ValueError):
                width = 0.0
            if width <= 0.0:
                return
            command["background_carrier"] = {
                "rect": (float(row_x), float(row_y), width, lh),
                "alignment": left_align,
                "text": text,
                "format": text_format,
                "translate": (0.0, 0.0),
                "scale": (1.0, 1.0),
                "context_boost": 1.0,
                "zero_horizontal_margins": True,
            }

        if mode in ("plain", "plain_render_html"):
            row_commands = _thaw_command_plan("plain", row_base_format)
            if row_commands is None:
                row_commands = []
                n_lines = len(positioned)
                for i, item in enumerate(positioned):
                    if len(item) not in (4, 6, 10):
                        continue
                    if len(item) == 10:
                        (line_plain, lx, ly, lw, paragraph_final, blank,
                         natural_width, _span_x, _span_width) = item
                    elif len(item) == 6:
                        line_plain, lx, ly, lw, paragraph_final, blank = item
                        natural_width = None
                    else:
                        line_plain, lx, ly, lw = item
                        paragraph_final = (i == n_lines - 1)
                        blank = not str(line_plain).strip()
                        natural_width = None
                    if blank:
                        continue
                    lx = float(lx) * sf
                    ly = float(ly) * sf
                    lw = float(lw) * sf
                    justify_this_line = (
                        self._h_align == self.ALIGN_JUSTIFY_
                        and not paragraph_final and " " in str(line_plain).strip()
                    )
                    if justify_this_line:
                        geometric_commands = _compose_justify_commands(
                            self, render_ctx, 0, str(line_plain), lx, ly, lw,
                            row_base_format, [], lh, left_align)
                        if geometric_commands is not None:
                            if background_enabled:
                                (_painter, background_scale,
                                 background_x) = _fixed_width_transform(
                                    line_plain, row_base_format, lx,
                                    target_width_override=lw,
                                    apply_transform=False)
                                geometric_commands[0]["background_carrier"] = {
                                    "rect": (background_x, ly,
                                             lw / background_scale, lh),
                                    "alignment": left_align,
                                    "text": line_plain,
                                    "format": row_base_format,
                                    "translate": (
                                        (lx, 0.0) if abs(
                                            background_scale - 1.0) >= 0.001
                                        else (0.0, 0.0)),
                                    "scale": (background_scale, 1.0),
                                }
                            row_commands.extend(geometric_commands)
                            continue

                    _painter, width_scale, draw_lx = _fixed_width_transform(
                        line_plain, row_base_format, lx, apply_transform=False)
                    translate = (
                        (lx, 0.0) if abs(width_scale - 1.0) >= 0.001
                        else (0.0, 0.0))
                    row_commands.append({
                        "rect": (draw_lx, ly, lw / width_scale, lh),
                        "alignment": left_align,
                        "text": line_plain,
                        "format": row_base_format,
                        "translate": translate,
                        "scale": (width_scale, 1.0),
                    })
                    if self._h_align == self.ALIGN_JUSTIFY_:
                        try:
                            carrier_width = float(natural_width) * sf
                        except (TypeError, ValueError):
                            carrier_width = 0.0
                        if carrier_width <= 0.0:
                            try:
                                carrier_width = float(QgsTextRenderer.textWidth(
                                    render_ctx, row_base_format,
                                    [str(line_plain)]))
                            except Exception:
                                carrier_width = lw
                        _attach_justify_row_background(
                            row_commands[-1], line_plain, row_base_format,
                            lx, ly, carrier_width)
                _freeze_command_plan(row_commands, "plain")
            _paint_background_buffer_text(row_commands)
            _restore_composition_space()
            return

        html_segments = cache.get("html_segments", [])
        html_plain = cache.get("html_plain", "")
        if mode == "render_html":
            draw_fmt = _fixed_polygon_paint_format(
                _copy_text_format_without_effects(self._text_format),
                render_ctx)
        else:
            try:
                draw_fmt = _fixed_polygon_paint_format(
                    row_base_format, render_ctx)
            except Exception:
                draw_fmt = row_base_format
        try:
            draw_fmt.setAllowHtmlFormatting(True)
        except Exception:
            record_suppressed_exception()

        frozen_rich_commands = _thaw_command_plan("rich", draw_fmt)
        if frozen_rich_commands is not None:
            _paint_background_buffer_text(frozen_rich_commands)
            _restore_composition_space()
            return


        row_commands = []
        n_lines = len(positioned)
        for i, item in enumerate(positioned):
            if len(item) not in (5, 7, 8, 10):
                continue
            if len(item) == 10:
                (ts, tl, lx, ly, lw, paragraph_final, blank,
                 natural_width, span_x, span_width) = item
            elif len(item) == 8:
                ts, tl, lx, ly, lw, paragraph_final, blank, natural_width = item
                span_x, span_width = lx, lw
            elif len(item) == 7:
                ts, tl, lx, ly, lw, paragraph_final, blank = item
                natural_width = None
                span_x, span_width = lx, lw
            else:
                ts, tl, lx, ly, lw = item
                paragraph_final = (i == n_lines - 1)
                blank = False
                natural_width = None
                span_x, span_width = lx, lw
            lx = float(lx) * sf
            ly = float(ly) * sf
            lw = float(lw) * sf
            span_x = float(span_x) * sf
            span_width = float(span_width) * sf
            if natural_width is not None:
                natural_width = float(natural_width) * sf
            # Every HTML mode serializes inherited runs against the canonical
            # QGIS font. Explicit fragment styles remain in the segments, but
            # untagged text now shares Plain Text's font and spacing engine.
            slice_font, slice_color = self._base_font_and_color()
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

            if mode in ("render_html", "inline_html"):
                if not justify_this_line:
                    # A non-justified row is one continuous text run. Drawing
                    # it word-by-word makes every inter-word space an isolated
                    # QTextDocument fragment, which changes kerning and word
                    # spacing compared with normal plain text. Draw the whole
                    # already-wrapped line through one rich document instead.
                    # _rows_with_fixed_origins() has already resolved the
                    # left/centre/right origin in fixed composition space,
                    # just as it does for normal plain text. Do not ask the
                    # rich-document painter to resolve the alignment again
                    # against its live preview-scale metrics: that makes the
                    # selected alignment depend on the zoom level. Paint from
                    # the cached origin with left alignment instead.
                    # The QGIS rich document shapes the same characters with
                    # slightly different advances from QTextLayout.  The row
                    # plan is authoritative for wrapping and horizontal
                    # alignment, so scale the document to its planned advance
                    # while retaining native styling inside the row.
                    _painter, width_scale, draw_lx = _fixed_width_transform(
                        line_plain, draw_fmt, lx,
                        rendered_value=line_html,
                        target_width_override=natural_width,
                        render_html_mode=(mode == "render_html"),
                        apply_transform=False)
                    translate = (
                        (lx, 0.0) if abs(width_scale - 1.0) >= 0.001
                        else (0.0, 0.0))
                    row_commands.append({
                        "rect": (draw_lx, ly, lw / width_scale, lh),
                        "alignment": left_align,
                        "text": line_html,
                        "format": draw_fmt,
                        "translate": translate,
                        "scale": (width_scale, 1.0),
                        "context_boost": 1.0,
                        "zero_horizontal_margins": True,
                    })
                    if self._h_align == self.ALIGN_JUSTIFY_:
                        carrier_width = natural_width
                        if carrier_width is None or carrier_width <= 0.0:
                            try:
                                carrier_width = float(QgsTextRenderer.textWidth(
                                    render_ctx, draw_fmt, [line_html]))
                            except Exception:
                                carrier_width = lw
                        _attach_justify_row_background(
                            row_commands[-1], line_html, draw_fmt,
                            lx, ly, carrier_width)
                    continue

                # All rich modes use one shared word/run compositor. It
                # measures each fragment with the same QgsTextDocument metrics
                # pipeline used by _draw_document(), then places the fragments
                # explicitly. This prevents the rich renderer from re-wrapping
                # a line with condensed-font metrics after QTextLayout has
                # already selected its polygon span.
                if justify_this_line:
                    # Justification is inherently a fragment layout: words
                    # are distributed across the complete safe polygon span.
                    rich_commands = _compose_rich_line_commands(
                        self, render_ctx, ts, line_plain,
                        span_x, ly, span_width, draw_fmt, html_segments,
                        lh, left_align, "justify",
                        base_font=slice_font, base_color=slice_color)
                if rich_commands is not None:
                    if background_enabled:
                        (_painter, background_scale,
                         background_x) = _fixed_width_transform(
                            line_plain, draw_fmt, span_x,
                            rendered_value=line_html,
                            target_width_override=span_width,
                            render_html_mode=(mode == "render_html"),
                            apply_transform=False)
                        rich_commands[0]["background_carrier"] = {
                            "rect": (background_x, ly,
                                     span_width / background_scale, lh),
                            "alignment": left_align,
                            "text": line_html,
                            "format": draw_fmt,
                            "translate": (
                                (span_x, 0.0) if abs(
                                    background_scale - 1.0) >= 0.001
                                else (0.0, 0.0)),
                            "scale": (background_scale, 1.0),
                            "context_boost": 1.0,
                            "zero_horizontal_margins": True,
                        }
                    row_commands.extend(rich_commands)
                    continue

                # Safe fallback when a rich fragment cannot be measured.
                eff_align = left_align
                _justify_w = lw
                width_painter, width_scale, draw_lx = _fixed_width_transform(
                    line_plain, draw_fmt, lx,
                    rendered_value=line_html,
                    target_width_override=None,
                    render_html_mode=(mode == "render_html"),
                    apply_transform=False)
                draw_width = _justify_w / width_scale
            else:
                # Non-rich path retains the existing renderer behavior.
                eff_align = left_align
                _justify_w = lw
                width_painter, width_scale, draw_lx = (
                    _fixed_width_transform(
                        line_plain, draw_fmt, lx,
                        rendered_value=None,
                        target_width_override=None,
                        render_html_mode=False,
                        apply_transform=False)
                )
                draw_width = _justify_w / width_scale
            draw_ly = ly
            draw_height = lh
            translate = (
                (lx, 0.0) if abs(width_scale - 1.0) >= 0.001
                else (0.0, 0.0))
            painter_scale = (width_scale, 1.0)

            row_commands.append({
                "rect": (draw_lx, draw_ly, draw_width, draw_height),
                "alignment": eff_align,
                "text": line_html,
                "format": draw_fmt,
                "translate": translate,
                "scale": painter_scale,
                "context_boost": 1.0,
            })

        _freeze_command_plan(row_commands, "rich")
        _paint_background_buffer_text(row_commands)
        _restore_composition_space()

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

        requested_render_html = self._allow_html
        requested_inline_html = (
            not requested_render_html
            and text_format_allows_html(self._text_format)
        )
        plain_compatible = not _has_html_semantics(resolved_text)

        # A mode switch must not move plain single-flow text.  Route it
        # through the same QTextLayout/QgsTextRenderer geometry as ordinary
        # plain text unless the input actually asks for HTML semantics.
        # Render-as-HTML still collapses ordinary source newlines/whitespace;
        # only explicit HTML breaks and blocks take the rich path.
        render_html_plain = requested_render_html and plain_compatible
        render_html = requested_render_html and not plain_compatible
        inline_html = requested_inline_html and not plain_compatible
        layout_text = (
            _html_single_flow_text(resolved_text)
            if render_html_plain else resolved_text
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

        cache_key = self._layout_signature(
            layout_text, render_html, inline_html, render_html_plain,
            plain_compatible=plain_compatible)
        if cache_key == self._layout_cache_key and self._layout_cache is not None:
            try:
                self._render_polygon_cached_layout(
                    render_ctx, display_scale_factor, self._layout_cache,
                    mode_override=(
                        "plain_render_html" if render_html_plain else None))
            except Exception:
                record_suppressed_exception()
            return

        html_segments = []
        html_plain = ""
        html_formats = []
        html_block_spacing = {}
        if render_html:
            # Preserve only explicit HTML font attributes over the canonical
            # QGIS font. This applies to justification as well: otherwise Qt
            # reintroduces a scale-dependent default font for its word runs.
            html_segments = extract_segments(
                layout_text, True, base_font, base_color,
                overlay_base_font=True,
                block_spacing_out=html_block_spacing)
            html_plain, html_formats = segments_to_plain_and_formats(
                html_segments, scale_factor, None,
                fmt_size_unit, fmt_size_scale)
        elif inline_html:
            # Supply QGIS' parser with the same canonical font that creates
            # the plain QTextLayout row plan.  It prevents inherited QGIS
            # defaults from reintroducing a stale document point size.
            html_segments = extract_qgis_html_segments(
                layout_text, normalised_text_format_font(self._text_format),
                base_font, base_color)
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
            plain_measure = apply_capitalization(layout_text, cap)

        # QTextLayout does not consistently treat LF/CRLF as a forced line
        # break (the behaviour varies between Qt versions).  U+2028 is Qt's
        # explicit in-paragraph line separator and has the same string length,
        # so segment/range offsets remain valid for the HTML rendering paths.
        # This also preserves consecutive newlines as genuinely empty lines.
        plain_measure = (plain_measure or "").replace("\r\n", "\n").replace("\r", "\n")
        plain_measure = plain_measure.replace("\n", "\u2028")
        if render_html or inline_html:
            html_plain = plain_measure

        # Calibrate QTextLayout's horizontal advances against the same
        # QgsTextRenderer which performs the final paint.  Certain condensed
        # and narrow faces resolve to slightly different advances in Qt's
        # shaping/layout path than in QGIS' renderer.  A single width ratio is
        # enough to keep wrapping, right/center alignment and painting in the
        # same metric space without stretching the rendered glyphs.
        metric_width_scale = 1.0
        try:
            # One base-font calibration governs every row.  HTML formatting is
            # applied by QTextLayout format ranges afterwards, so a tagged run
            # cannot change the safe-span calculation for untagged text that
            # precedes it in the same paragraph.
            metric_sample = " ".join(
                str(plain_measure).replace("\u2028", " ").split())
            if len(metric_sample) > 240:
                metric_sample = metric_sample[:240].rsplit(" ", 1)[0]
            if metric_sample:
                display_font = render_font(
                    base_font, display_scale_factor, None,
                    fmt_size_unit, fmt_size_scale, self._text_format.size())
                probe = QtGui.QTextLayout(metric_sample, display_font)
                _apply_design_metrics_to_layout(probe)
                probe.beginLayout()
                probe_line = probe.createLine()
                qt_probe_width = 0.0
                if probe_line.isValid():
                    probe_line.setLineWidth(1000000.0)
                    qt_probe_width = float(probe_line.naturalTextWidth())
                probe.endLayout()
                qgis_probe_width = float(QgsTextRenderer.textWidth(
                    render_ctx, self._text_format, [metric_sample]))
                if qt_probe_width > 0.01 and qgis_probe_width > 0.01:
                    ratio = qgis_probe_width / qt_probe_width
                    if 0.65 <= ratio <= 1.35:
                        metric_width_scale = ratio
        except Exception:
            metric_width_scale = 1.0

        line_height_mult = 1.0
        try:
            line_height_mult = max(0.5, self._text_format.lineHeight())
        except Exception:
            line_height_mult = 1.0
        lh = max(fm.height() * line_height_mult, 1.0)
        inline_font_samples = []
        if render_html or inline_html:
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

        html_list_ranges = (
            html_block_spacing.get("list_ranges", []) if render_html else []
        )

        def _list_insets_for_offset(offset):
            """Return (first-line inset, continuation inset) for an HTML list.

            QTextDocument stores list indentation as block metadata, not as
            characters.  Our polygon compositor flattens the document in order
            to wrap against a variable-width polygon, so reproduce that block
            indentation geometrically instead of inserting leading whitespace
            which QgsTextRenderer may collapse at row starts.
            """
            for info in html_list_ranges:
                try:
                    start = int(info.get("start", -1))
                    end = int(info.get("end", -1))
                    current_offset = int(offset)
                    level = max(1, int(info.get("level", 1)))
                except (TypeError, ValueError, AttributeError):
                    start = end = current_offset = -1
                    level = 1

                if start <= current_offset < end:
                    marker = str(info.get("marker", ""))
                    # Qt's default QTextDocument list indent is generous
                    # (roughly a couple of ems per nesting level).  Express
                    # it through the active font metrics so it scales with
                    # the QGIS text size and render context.
                    em = max(1.0, float(fm.horizontalAdvance("M")))
                    base = 2.0 * em * level
                    hanging = max(
                        0.75 * em,
                        float(fm.horizontalAdvance(marker + " "))
                    )
                    return start, base, base + hanging
            return None

        # Use exactly the same safe scan-line span for justification as for
        # left/right alignment.  _text_visual_padding() already supplies the
        # true ink/effect allowance, so adding a second justify-only inset
        # creates the visible right-side margin which right alignment does not
        # have.
        justify_edge_inset = 0.0

        def _compose_rows(candidate_y):
            """Lay out once for measurement and rendering.

            A single physical row may contain multiple disjoint polygon spans.
            In that case consecutive QTextLine fragments are placed at the same
            y coordinate, left-to-right, so text continues through every valid
            interior region before advancing to the next visual row.

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
                first_line = layout.createLine()
                if not first_line.isValid():
                    complete = True
                    break

                probe_height = max(first_line.height(), lh)
                cy = py
                safe_spans = []
                for _ in range(60):
                    if cy + probe_height > avail_bottom + 0.01:
                        break
                    safe_spans = _line_safe_spans(
                        points, cy - visual_pad_y,
                        cy + probe_height + visual_pad_y,
                        pad_px, hm_px,
                        visual_pad_x + justify_edge_inset)
                    if safe_spans:
                        break
                    cy += probe_height

                if not safe_spans:
                    break

                row_y = cy
                row_height = probe_height
                block_advance = 0.0
                line_for_span = first_line
                row_finished = False
                document_finished = False

                for span_index, span in enumerate(safe_spans):
                    if span_index > 0:
                        line_for_span = layout.createLine()
                        if not line_for_span.isValid():
                            complete = True
                            document_finished = True
                            break

                    row_height = max(
                        row_height, max(line_for_span.height(), lh))

                    left, right = span
                    line_start = line_for_span.textStart()
                    list_insets = _list_insets_for_offset(line_start)
                    # List paragraph indentation belongs at the beginning of
                    # the visual row.  A second lobe on that same row is a
                    # continuation of the line, not a new indented line.
                    if list_insets is not None and span_index == 0:
                        list_start, first_inset, continuation_inset = list_insets
                        left += (
                            first_inset if line_start == list_start
                            else continuation_inset
                        )

                    width = right - left
                    if width <= 1.0:
                        # Do not consume the QTextLine on an unusably narrow
                        # first span.  Try the next region with the same line.
                        if span_index == 0:
                            continue
                        break

                    # QTextLayout works in its own advance metric space.
                    # Convert the physical polygon span into that space so the
                    # line break predicts the final QgsTextRenderer width.
                    line_for_span.setLineWidth(
                        width / max(0.65, min(1.35, metric_width_scale)))

                    ts = line_for_span.textStart()
                    raw_len = line_for_span.textLength()
                    tl = raw_len
                    forced = False
                    while (
                        tl > 0
                        and plain_measure[ts + tl - 1:ts + tl]
                        in ("\n", "\u2028")
                    ):
                        forced = True
                        tl -= 1

                    if forced and raw_len > tl:
                        block_advance = sum(
                            html_break_advances.get(offset, 0.0)
                            for offset in range(ts + tl, ts + raw_len)
                        )

                    blank = not plain_measure[ts:ts + tl].strip()
                    rows.append((
                        ts, tl, left, row_y, width, forced, blank,
                        max(0.0, float(line_for_span.naturalTextWidth())
                            * metric_width_scale)))

                    if forced:
                        row_finished = True
                        break

                py = (
                    row_y
                    + row_height * line_height_mult
                    + block_advance
                )

                if document_finished:
                    break
                if row_finished:
                    continue

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
                # formatted_natural has already been calibrated into the
                # final renderer's metric space.  Use it for every mode so
                # condensed/narrow fonts do not acquire a separate alignment
                # estimate here.
                natural = max(0.0, float(formatted_natural))
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
                # Keep the unmodified safe scan-line span as well.  The
                # rich renderer measures fragments with QgsTextDocument,
                # whereas QTextLayout supplies ``natural`` above.  Passing
                # only the pre-shifted row rectangle to rich alignment would
                # apply center/right positioning against two different metric
                # systems.  Justification already retained this full span,
                # which is why it was the only stable rich-text alignment.
                fixed.append((ts, tl, draw_x, ly, draw_w,
                              paragraph_final, blank, formatted_natural,
                              lx, lw))
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
                    "metric_width_scale": metric_width_scale,
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
                    "metric_width_scale": metric_width_scale,
                }, scale_factor)

            self._render_polygon_cached_layout(
                render_ctx, display_scale_factor, self._layout_cache)
            return

        # ── Non-HTML path (QgsTextRenderer) ───────────────────────────
        # Applies buffer, shadow, background, opacity, case.
        positioned = [
            (plain_measure[ts:ts + tl], lx, ly, lw, paragraph_final, blank)
            for (ts, tl, lx, ly, lw, paragraph_final, blank,
                 _formatted_natural, _span_x, _span_width) in composed_rows
        ]

        self._store_polygon_layout_cache(
            cache_key,
            "plain", {
                "lh": lh,
                "positioned": positioned,
                "metric_width_scale": metric_width_scale,
            }, scale_factor)

        self._render_polygon_cached_layout(
            render_ctx, display_scale_factor, self._layout_cache,
            mode_override=(
                "plain_render_html" if render_html_plain else None))
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
        # Refresh the redundant layout-level recovery manifest while QGIS is
        # writing this item. QgsLayout writes its custom properties after its
        # items, so this backup is included in the same project save/autosave.
        try:
            from .recovery import snapshot_item_layout
            snapshot_item_layout(self)
        except Exception:
            record_suppressed_exception()
        element.setAttribute("polyText",     self._text)
        element.setAttribute("polyHtml", "1" if self._allow_html else "0")
        element.setAttribute("polyPadding",  str(self._padding))
        element.setAttribute("polyHMargin",  str(self._h_margin))
        element.setAttribute("polyVMargin",  str(self._v_margin))
        element.setAttribute("polyHAlign",   str(self._h_align))
        element.setAttribute("polyVAlign",   str(self._v_align))
        node_str = ";".join(f"{n.x():.6f},{n.y():.6f}" for n in self._nodes)
        element.setAttribute("polyNodes", node_str)
        element.setAttribute("polyBezier", serialise_handles(self._bezier_handles))
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
        bezier_text = element.attribute("polyBezier", "")
        loaded_handles = deserialise_handles(bezier_text, len(self._nodes))
        self._bezier_handles = ensure_closed_handles(
            self._nodes, loaded_handles if loaded_handles is not None
            else bounded_polygon_handles(self._nodes))
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
