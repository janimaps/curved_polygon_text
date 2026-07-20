"""
layout_item_spline_text.py — Curved Spline Text layout item.
"""
import hashlib
import math
from qgis.core import (
    QgsLayoutItem, QgsLayoutItemRegistry, QgsReadWriteContext,
    QgsTextFormat, QgsLayoutMeasurement,
)

from .compat import (
    QtGui, QtCore, QtWidgets, QPointF, QFont, QColor, QPen, QBrush, QRectF,
    AA_ANTIALIASING, AA_TEXT_ANTIALIASING, NO_PEN,
)
from .text_engine import (
    evaluate_expressions, extract_segments, segments_to_char_stream,
    PathLengthMapper, build_smooth_path, point_segment_distance,
    render_font, text_format_allows_html,
)
from .icons import spline_icon
from .keep_alive import keep_alive
from .reliability import record_suppressed_exception

SPLINE_TEXT_ITEM_TYPE = QgsLayoutItemRegistry.ItemType.PluginItem + 1001

ALIGN_LEFT_, ALIGN_CENTER_, ALIGN_RIGHT_, ALIGN_JUSTIFY_ = range(4)


def _is_layout_preview_render(item):
    """Return True only while QGIS is painting the Layout Designer view."""
    try:
        layout = item.layout()
        return bool(layout and layout.renderContext().isPreviewRender())
    except Exception:
        return False


class _CompositionUnitContext:
    """Normalize QGIS painter-unit conversions into a fixed drawing space."""

    def __init__(self, source_context, destination_to_composition):
        self._source_context = source_context
        self._factor = float(destination_to_composition)

    def convertToPainterUnits(self, *args):
        return (
            float(self._source_context.convertToPainterUnits(*args))
            * self._factor
        )


def _fixed_spline_font(font, scale_factor, unit_context, size_unit,
                       size_map_unit_scale, size_value):
    """Return a composition-space QFont which export DPI cannot reinterpret."""
    out = render_font(
        font, scale_factor, unit_context,
        size_unit, size_map_unit_scale, size_value)
    try:
        if size_map_unit_scale is not None:
            size_px = unit_context.convertToPainterUnits(
                size_value, size_unit, size_map_unit_scale)
        else:
            size_px = unit_context.convertToPainterUnits(
                size_value, size_unit)
    except Exception:
        try:
            size_px = float(size_value) * (25.4 / 72.0) * scale_factor
        except Exception:
            size_px = 10.0 * (25.4 / 72.0) * scale_factor
    # QFont point sizes are resolved again against the destination paint
    # device DPI. Pixel size is invariant and is subsequently scaled only by
    # the spline painter's fixed composition-to-destination transform.
    out.setPixelSize(max(1, int(round(float(size_px)))))
    return out


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


def _inline_spline_char_stream(segments, capitalization,
                               forced_bold=False, forced_italic=False):
    """Apply QGIS Font-group casing/overrides to native inline HTML runs."""
    capitalization_value = capitalization
    try:
        cap = int(capitalization)
    except Exception:
        try:
            cap = int(capitalization.value)
        except Exception:
            cap = 0
    source_stream = list(segments_to_char_stream(segments))
    source_text = "".join(ch for ch, _font, _color in source_stream)
    transformed_text = source_text
    if cap in (1, 2, 4, 1004, 1005):
        # Use QGIS' own Unicode word-boundary and title-case rules.  In
        # particular, ForceFirstLetterToCapital affects the first letter of
        # each word while leaving all remaining letters untouched, and title
        # case observes QGIS' small-word/phrase rules.
        try:
            from qgis.core import QgsStringUtils
            transformed_text = QgsStringUtils.capitalize(
                source_text, capitalization_value)
        except Exception:
            if cap == 1:
                transformed_text = source_text.upper()
            elif cap == 2:
                transformed_text = source_text.lower()

    # Case changes normally preserve the one-source-character/one-glyph
    # correspondence needed for spline placement.  Retain a safe local
    # fallback for rare Unicode expansions such as sharp-s.
    mapped_case = len(transformed_text) == len(source_text)
    word_start = True
    for index, (ch, source_font, color) in enumerate(source_stream):
        font = QFont(source_font)
        # Spline glyphs are painted one character at a time.  Leaving Qt's
        # capitalization flag on the per-glyph font makes every character look
        # like the first character of a new string (so Force First Letter turns
        # the whole line uppercase).  Casing is applied once to the continuous
        # stream below, therefore glyph fonts must paint in MixedCase mode.
        try:
            mixed_case = getattr(QFont, "MixedCase", None)
            if mixed_case is None:
                capitalization_enum = getattr(QFont, "Capitalization", None)
                mixed_case = getattr(capitalization_enum, "MixedCase", None)
            if mixed_case is not None:
                font.setCapitalization(mixed_case)
        except Exception:
            record_suppressed_exception()
        if forced_bold:
            font.setBold(True)
        if forced_italic:
            font.setItalic(True)
        if ch in ("\n", "\u2028"):
            yield ch, font, color
            word_start = True
            continue

        rendered = ch
        small = False
        if mapped_case and cap in (1, 2, 4, 1004, 1005):
            rendered = transformed_text[index]
        elif cap == 1:
            rendered = ch.upper()
        elif cap == 2:
            rendered = ch.lower()
        elif cap == 4 and word_start and ch.isalpha():
            rendered = ch.upper()
        elif cap == 5 and ch.islower():
            rendered = ch.upper()
            small = True
        elif cap == 1006 and ch.isalpha():
            rendered = ch.upper()
            small = True
        elif cap == 1004 and word_start and ch.isalpha():
            rendered = ch.upper()

        if small:
            try:
                size = float(font.pointSizeF())
                if size > 0:
                    font.setPointSizeF(size * 0.8)
                else:
                    pixel_size = int(font.pixelSize())
                    if pixel_size > 0:
                        font.setPixelSize(max(1, round(pixel_size * 0.8)))
            except Exception:
                record_suppressed_exception()
        if ch.isalpha():
            word_start = False
        elif ch.isspace() or not ch.isalnum():
            word_start = True
        yield rendered, font, color


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


def _copy_text_format_without_effects(text_format):
    """Keep Text/Formatting settings while excluding HTML-inert effects."""
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
                neutral_component.setEnabled(False)
            setter = getattr(fmt, setter_name, None)
            if callable(setter):
                setter(neutral_component)
        except Exception:
            record_suppressed_exception()
    return fmt


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



def _setting_distance_to_painter_units(render_ctx, settings, value_method,
                                       unit_method, map_unit_scale_method,
                                       fallback_scale):
    """Read a QGIS text-effect distance and convert it to painter units.

    The text-effect APIs expose distances together with render units.  QGIS'
    native text renderer uses the render context to convert those units.  The
    spline item renders glyphs manually, so it needs to do the same conversion
    itself.  If a method is unavailable on a particular QGIS version, fall back
    to the plugin's existing scale-factor behaviour.
    """
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



def _convert_value_to_painter_units(render_ctx, value, unit=None,
                                    map_unit_scale=None, fallback_scale=1.0):
    """Convert a raw QgsText* setting distance/size to painter units."""
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


def _setting_size_to_painter_units(render_ctx, settings, value_method,
                                   unit_method, map_unit_scale_method,
                                   fallback_scale):
    """Read QSizeF/QPointF-like settings such as background size/offset."""
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


def _setting_opacity(settings, fallback=1.0):
    try:
        value = float(settings.opacity())
    except Exception:
        return fallback
    # QGIS normally returns 0..1, but clamp defensively in case a future API
    # exposes a percentage-like value.
    if value > 1.0:
        value /= 100.0
    return max(0.0, min(1.0, value))


def _color_with_opacity(color, opacity):
    c = QColor(color)
    try:
        alpha = float(c.alphaF())
    except Exception:
        alpha = float(c.alpha()) / 255.0
    c.setAlphaF(max(0.0, min(1.0, alpha * opacity)))
    return c


def _paint_effect(settings):
    """Return an optional QgsPaintEffect/QgsEffectStack from text settings."""
    if settings is None:
        return None
    for name in (
        "paintEffect", "paintEffectStack", "effect", "effectStack",
        "drawEffect", "drawEffects",
    ):
        attr = None
        try:
            attr = getattr(settings, name)
        except Exception:
            record_suppressed_exception()
        if attr is None:
            continue
        eff = None
        try:
            eff = attr() if callable(attr) else attr
        except Exception:
            record_suppressed_exception()
        if eff is not None and not isinstance(eff, bool):
            return eff
    return None


def _paint_effect_enabled(effect):
    if effect is None:
        return False
    try:
        return bool(effect.enabled())
    except Exception:
        return True


def _draw_with_optional_paint_effect(render_ctx, fallback_painter, effect,
                                     draw_func, picture_cache=None,
                                     cache_key=None):
    """Draw through a QgsPaintEffect when QGIS exposes one to Python.

    QgsTextRenderer does this internally for native label/textbox items.  The
    spline item renders characters manually, so we wrap each manually-rendered
    component (background, buffer, text) in the same begin/draw/end pattern
    when the effect object is available.  If a QGIS build exposes a different
    effect API, drawing falls back to the normal painter instead of failing.
    """
    if not _paint_effect_enabled(effect):
        draw_func(fallback_painter)
        return

    if picture_cache is not None and cache_key is not None:
        try:
            cached_picture = picture_cache.get(cache_key)
            if cached_picture is not None:
                fallback_painter.drawPicture(0, 0, cached_picture)
                return
        except Exception:
            record_suppressed_exception()

        # Record the completed QGIS effect output once. QPicture preserves the
        # exact painter commands produced by the effect stack and is cheap to
        # replay during unchanged preview redraws (selection, panel opening,
        # expose events). Exports bypass this branch and are always fresh.
        picture = QtGui.QPicture()
        recording_painter = QtGui.QPainter(picture)
        original_painter = None
        ended = False
        recorded = False
        try:
            original_painter = render_ctx.painter()
            render_ctx.setPainter(recording_painter)
            begin_result = effect.begin(render_ctx)
            if hasattr(begin_result, "drawPath"):
                effect_painter = begin_result
            else:
                effect_painter = render_ctx.painter() or recording_painter
            draw_func(effect_painter)
            effect.end(render_ctx)
            ended = True
            recorded = True
        except Exception:
            if not ended:
                try:
                    effect.end(render_ctx)
                except Exception:
                    record_suppressed_exception()
        finally:
            try:
                render_ctx.setPainter(original_painter or fallback_painter)
            except Exception:
                record_suppressed_exception()
            try:
                recording_painter.end()
            except Exception:
                record_suppressed_exception()

        if recorded:
            fallback_painter.drawPicture(0, 0, picture)
            try:
                bounds = picture.boundingRect()
                if max(0, bounds.width()) * max(0, bounds.height()) <= 8_000_000:
                    picture_cache[cache_key] = picture
            except Exception:
                record_suppressed_exception()
            return

    ended = False
    try:
        begin_result = effect.begin(render_ctx)
        if hasattr(begin_result, "drawPath"):
            painter = begin_result
        else:
            painter = render_ctx.painter() or fallback_painter
        draw_func(painter)
        effect.end(render_ctx)
        ended = True
        return
    except Exception:
        if not ended:
            try:
                effect.end(render_ctx)
            except Exception:
                record_suppressed_exception()

    draw_func(fallback_painter)


def _round_join_pen(color, width):
    pen = QPen(color, width)
    try:
        pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    except AttributeError:
        pen.setJoinStyle(QtCore.Qt.RoundJoin)
        pen.setCapStyle(QtCore.Qt.RoundCap)
    except Exception:
        record_suppressed_exception()
    return pen


def _make_path_stroker(width):
    """Create one configured stroker for a complete spline render pass."""
    stroker = QtGui.QPainterPathStroker()
    stroker.setWidth(max(0.0, width))
    try:
        stroker.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        stroker.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    except AttributeError:
        stroker.setJoinStyle(QtCore.Qt.RoundJoin)
        stroker.setCapStyle(QtCore.Qt.RoundCap)
    except Exception:
        record_suppressed_exception()
    return stroker


def _make_stroked_path(path, width, stroker=None):
    """Return a stroked path, optionally reusing a pass-local stroker."""
    if stroker is None:
        stroker = _make_path_stroker(width)
    return stroker.createStroke(path)


def _graphics_item_flag(name):
    """Resolve QGraphicsItem flags across Qt5/PyQt5 and Qt6/PyQt6."""
    owners = [QtWidgets.QGraphicsItem]
    try:
        flag_owner = getattr(QtWidgets.QGraphicsItem, "GraphicsItemFlag", None)
        if flag_owner is not None:
            owners.append(flag_owner)
    except Exception:
        record_suppressed_exception()

    for owner in owners:
        value = None
        try:
            value = getattr(owner, name)
        except Exception:
            record_suppressed_exception()
        if value is not None:
            return value
    return None


def _graphics_item_change(name):
    """Resolve QGraphicsItem change enum values across Qt5 and Qt6."""
    owners = [QtWidgets.QGraphicsItem]
    try:
        change_owner = getattr(QtWidgets.QGraphicsItem, "GraphicsItemChange", None)
        if change_owner is not None:
            owners.append(change_owner)
    except Exception:
        record_suppressed_exception()

    for owner in owners:
        value = None
        try:
            value = getattr(owner, name)
        except Exception:
            record_suppressed_exception()
        if value is not None:
            return value
    return None


_GEOMETRY_CHANGE_NAMES = (
    "ItemPositionChange",
    "ItemPositionHasChanged",
    "ItemScenePositionHasChanged",
    "ItemTransformChange",
    "ItemTransformHasChanged",
    "ItemRotationChange",
    "ItemRotationHasChanged",
    "ItemScaleChange",
    "ItemScaleHasChanged",
)


def _is_item_geometry_change(change):
    """Return True when a QGraphicsItem change can leave old paint behind."""
    for name in _GEOMETRY_CHANGE_NAMES:
        try:
            if change == _graphics_item_change(name):
                return True
        except Exception:
            record_suppressed_exception()

    try:
        text = str(change).lower()
    except Exception:
        return False

    return (
        "position" in text
        or "scene position" in text
        or "transform" in text
        or "rotation" in text
        or "scale" in text
    )


def _is_item_selection_change(change):
    """Return True for Qt 5/6 selection change notifications."""
    for name in ("ItemSelectedChange", "ItemSelectedHasChanged"):
        try:
            if change == _graphics_item_change(name):
                return True
        except Exception:
            record_suppressed_exception()
    try:
        return "selected" in str(change).lower()
    except Exception:
        return False


def _transformed_glyph_path(glyph, local_path, vm_px):
    transform = QtGui.QTransform()
    transform.translate(glyph["pos"].x(), glyph["pos"].y())
    transform.rotate(glyph["angle"])
    if vm_px:
        transform.translate(0, vm_px)
    return transform.map(local_path)


def _enum_text(value):
    try:
        return str(value).lower()
    except Exception:
        return ""


def _enum_matches(value, *candidate_names):
    """Compare PyQt enum values across QGIS 3/PyQt5 and QGIS 4/PyQt6."""
    text = _enum_text(value)
    if any(name.lower() in text for name in candidate_names):
        return True

    try:
        from qgis.core import QgsTextShadowSettings as _Shadow
    except Exception:
        _Shadow = None

    if _Shadow is not None:
        owners = [_Shadow]
        for owner_name in (
            "ShadowPlacement", "ShadowType", "Placement", "Type", "DrawUnder",
        ):
            owner = getattr(_Shadow, owner_name, None)
            if owner is not None:
                owners.append(owner)
        for owner in owners:
            for name in candidate_names:
                for attr_name in (
                    name, "Shadow" + name, "Shadow" + name.capitalize(),
                    name.capitalize(),
                ):
                    try:
                        if value == getattr(owner, attr_name):
                            return True
                    except Exception:
                        record_suppressed_exception()
    return False


def _shadow_component(shadow, background_enabled, buffer_enabled):
    """Resolve which text component should cast the QGIS text shadow.

    Native QGIS labels let the shadow be drawn under text, buffer, or
    background/shape. When the component enum is not exposed in the current
    API, the lowest visible component is used.
    """
    value = None
    for method in (
        "shadowPlacement", "placement", "shadowType", "type", "drawUnder",
        "drawUnderComponent",
    ):
        candidate = None
        try:
            candidate = getattr(shadow, method)()
        except Exception:
            record_suppressed_exception()
        if candidate is None:
            continue
        value = candidate
        break

    if value is not None:
        text = _enum_text(value)
        if (
            "background" in text or "shape" in text
            or _enum_matches(value, "Background", "Shape")
        ):
            return "background" if background_enabled else (
                "buffer" if buffer_enabled else "text")
        if "buffer" in text or _enum_matches(value, "Buffer"):
            return "buffer" if buffer_enabled else "text"
        if "text" in text or _enum_matches(value, "Text"):
            return "text"
        if "lowest" in text or _enum_matches(value, "Lowest"):
            return "background" if background_enabled else (
                "buffer" if buffer_enabled else "text")

    # Sensible native-like fallback: the bottom-most enabled component should
    # cast the shadow, so increasing background X/Y or buffer size increases
    # the shadow footprint too.
    if background_enabled:
        return "background"
    if buffer_enabled:
        return "buffer"
    return "text"


def _background_is_fixed_size(background):
    try:
        value = background.sizeType()
    except Exception:
        return False

    text = _enum_text(value)
    if "fixed" in text:
        return True

    try:
        from qgis.core import QgsTextBackgroundSettings as _BG
    except Exception:
        _BG = None
    if _BG is not None:
        owners = [_BG]
        owner = getattr(_BG, "SizeType", None)
        if owner is not None:
            owners.append(owner)
        for owner in owners:
            for attr in ("SizeFixed", "Fixed"):
                try:
                    if value == getattr(owner, attr):
                        return True
                except Exception:
                    record_suppressed_exception()
    return False


def _background_shape(background):
    value = None
    for method in ("type", "shape", "shapeType"):
        try:
            value = getattr(background, method)()
            break
        except Exception:
            record_suppressed_exception()
    text = _enum_text(value)
    if "circle" in text:
        return "circle"
    if "ellipse" in text:
        return "ellipse"
    if "rounded" in text:
        return "rounded"
    if "square" in text:
        return "square"

    try:
        from qgis.core import QgsTextBackgroundSettings as _BG
    except Exception:
        _BG = None
    if _BG is not None and value is not None:
        owners = [_BG]
        owner = getattr(_BG, "ShapeType", None)
        if owner is not None:
            owners.append(owner)
        for owner in owners:
            for attr, result in (
                ("ShapeCircle", "circle"), ("Circle", "circle"),
                ("ShapeEllipse", "ellipse"), ("Ellipse", "ellipse"),
                ("ShapeRoundedRectangle", "rounded"),
                ("RoundedRectangle", "rounded"),
                ("ShapeSquare", "square"), ("Square", "square"),
            ):
                try:
                    if value == getattr(owner, attr):
                        return result
                except Exception:
                    record_suppressed_exception()
    return "rectangle"


def _background_radii_to_painter_units(render_ctx, background, fallback_scale):
    for value_method, unit_method, scale_method in (
        ("cornerRadius", "cornerRadiusUnit", "cornerRadiusMapUnitScale"),
        ("radii", "radiiUnit", "radiiMapUnitScale"),
        ("radius", "radiusUnit", "radiusMapUnitScale"),
    ):
        raw = None
        try:
            raw = getattr(background, value_method)()
        except Exception:
            record_suppressed_exception()
        if raw is None:
            continue

        unit = None
        map_unit_scale = None
        try:
            unit = getattr(background, unit_method)()
        except Exception:
            record_suppressed_exception()
        try:
            map_unit_scale = getattr(background, scale_method)()
        except Exception:
            record_suppressed_exception()

        rx = None
        ry = None
        try:
            rx = raw.width()
            ry = raw.height()
        except Exception:
            try:
                rx = raw.x()
                ry = raw.y()
            except Exception:
                try:
                    rx = ry = float(raw)
                except Exception:
                    record_suppressed_exception()
        if rx is None or ry is None:
            continue

        return (
            _convert_value_to_painter_units(
                render_ctx, rx, unit, map_unit_scale, fallback_scale),
            _convert_value_to_painter_units(
                render_ctx, ry, unit, map_unit_scale, fallback_scale),
        )
    return 0.0, 0.0


def _background_local_path(glyph, size_x, size_y, fixed_size, shape,
                           offset_x, offset_y, radius_x, radius_y):
    fm = glyph["fm"]
    try:
        sx = float(size_x)
    except Exception:
        sx = 0.0
    try:
        sy = float(size_y)
    except Exception:
        sy = 0.0

    if fixed_size:
        width = max(abs(sx), 0.25)
        height = max(abs(sy), 0.25)
        rect = QRectF(
            -width / 2.0 + offset_x,
            -fm.ascent() + (fm.height() - height) / 2.0 + offset_y,
            width,
            height,
        )
    else:
        natural_width = glyph["advance"] + 2.0 * sx
        natural_height = fm.height() + 2.0 * sy
        width = max(0.25, natural_width)
        height = max(0.25, natural_height)
        if natural_width >= 0.25:
            left = -glyph["advance"] / 2.0 - sx + offset_x
        else:
            left = -width / 2.0 + offset_x
        if natural_height >= 0.25:
            top = -fm.ascent() - sy + offset_y
        else:
            top = -fm.ascent() + (fm.height() - height) / 2.0 + offset_y
        rect = QRectF(left, top, width, height)

    # Match the continuous ribbon renderer: move the background down very
    # slightly so descenders do not sit on the lower edge.
    rect.translate(0.0, rect.height() * 0.07)

    if shape in ("square", "circle"):
        side = max(rect.width(), rect.height())
        rect = QRectF(
            rect.center().x() - side / 2.0,
            rect.center().y() - side / 2.0,
            side,
            side,
        )

    path = QtGui.QPainterPath()
    if shape in ("ellipse", "circle"):
        path.addEllipse(rect)
    elif shape == "rounded":
        path.addRoundedRect(rect, max(0.0, radius_x), max(0.0, radius_y))
    else:
        path.addRect(rect)
    return path


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


def _rotated_offset(point, angle_degrees, x_offset, y_offset):
    """Map a local text/background offset to the spline painter space."""
    if not x_offset and not y_offset:
        return QPointF(point)
    transform = QtGui.QTransform()
    transform.translate(point.x(), point.y())
    transform.rotate(angle_degrees)
    return transform.map(QPointF(x_offset, y_offset))


def _point_at_extended_distance(path, mapper, distance,
                                offset_x=0.0, offset_y=0.0):
    """Return a point on the path, allowing distances just beyond the ends.

    Font background Size X is expected to lengthen/shorten the background at
    the start and end of the text.  If the text begins close to the first spline
    node, clamping the sampled distance to 0 hides that start padding.  Native
    QGIS label backgrounds still show the padding, so for distances outside the
    path we extend along the endpoint tangent before applying the local
    background offset.
    """
    total = max(0.0, float(getattr(mapper, "total_length", 0.0) or 0.0))
    try:
        distance = float(distance)
    except Exception:
        distance = 0.0

    if total <= 0.0:
        return QPointF()

    if distance <= 0.0:
        t = 0.0
        base = path.pointAtPercent(t)
        angle = -path.angleAtPercent(t)
        return _rotated_offset(base, angle, distance + offset_x, offset_y)

    if distance >= total:
        t = 1.0
        base = path.pointAtPercent(t)
        angle = -path.angleAtPercent(t)
        return _rotated_offset(base, angle, distance - total + offset_x, offset_y)

    t = mapper.percent_at_length(distance)
    base = path.pointAtPercent(t)
    angle = -path.angleAtPercent(t)
    return _rotated_offset(base, angle, offset_x, offset_y)


def _subpath_from_distances(path, mapper, start_distance, end_distance,
                            offset_x=0.0, offset_y=0.0):
    """Sample a spline sub-path between two arc-length distances.

    This is used for continuous font backgrounds.  Drawing one rectangle per
    glyph works on gentle curves, but at tight bends the overlapping glyph
    rectangles create a visibly stepped/tattered edge.  A stroked sub-path
    follows the baseline as one continuous ribbon, so the background bends
    smoothly through sharp curves.

    Distances are deliberately *not* clamped to the spline endpoints.  The
    helper above extends along the endpoint tangent so background Size X remains
    visible even when the text starts or ends at the path boundary.
    """
    if mapper.total_length <= 0:
        return QtGui.QPainterPath()

    try:
        start_distance = float(start_distance)
        end_distance = float(end_distance)
    except Exception:
        return QtGui.QPainterPath()

    if end_distance < start_distance:
        start_distance, end_distance = end_distance, start_distance
    if end_distance - start_distance <= 0.01:
        end_distance = start_distance + 0.01

    length = max(0.01, end_distance - start_distance)
    samples = max(8, min(240, int(math.ceil(length / 3.0))))
    out = QtGui.QPainterPath()

    for i in range(samples + 1):
        dist = start_distance + length * i / samples
        pos = _point_at_extended_distance(
            path, mapper, dist, offset_x=offset_x, offset_y=offset_y)
        if i == 0:
            out.moveTo(pos)
        else:
            out.lineTo(pos)
    return out


def _make_continuous_background_path(path, mapper, glyph_plan,
                                     size_x, size_y, offset_x, offset_y):
    """Return a smooth ribbon-shaped background following the rendered text.

    Glyphs sit on the spline baseline, but most of a font's ink is above that
    baseline.  A stroke centered exactly on the spline therefore puts too much
    of the background below the letters.  Native QGIS text backgrounds are
    centered on the font metrics box instead, so we offset the ribbon's
    centerline by the font-metrics center before stroking it.

    Size X extends/reduces the ribbon along the path at both ends.  Size Y
    extends/reduces the ribbon height.  A small downward nudge keeps descenders
    such as "g" and "p" from sitting too close to the lower edge.
    """
    if not glyph_plan:
        return QtGui.QPainterPath()

    try:
        sx = float(size_x)
    except Exception:
        sx = 0.0
    try:
        sy = float(size_y)
    except Exception:
        sy = 0.0
    try:
        ox = float(offset_x)
    except Exception:
        ox = 0.0

    text_start = min(g.get("s0", 0.0) for g in glyph_plan)
    text_end = max(g.get("s1", 0.0) for g in glyph_plan)
    start_distance = text_start - sx + ox
    end_distance = text_end + sx + ox

    # If a large negative Size X would invert the background, collapse to a
    # small centred strip rather than creating an invalid path.
    if end_distance <= start_distance:
        center = (text_start + text_end) / 2.0 + ox
        half = max(0.5, (text_end - text_start + 2.0 * sx) / 2.0)
        start_distance = center - half
        end_distance = center + half

    weighted_center_sum = 0.0
    weight_sum = 0.0
    max_height = 0.0
    for g in glyph_plan:
        fm = g["fm"]
        try:
            advance = max(float(g.get("advance", 0.0)), 0.01)
        except Exception:
            advance = 1.0
        try:
            height = float(fm.height())
            center_y = -float(fm.ascent()) + height / 2.0
        except Exception:
            height = 0.0
            center_y = 0.0
        weighted_center_sum += center_y * advance
        weight_sum += advance
        max_height = max(max_height, height)

    metrics_center_y = (
        weighted_center_sum / weight_sum if weight_sum > 0.0 else 0.0
    )
    width = max(0.25, max_height + 2.0 * sy)
    descender_nudge = width * 0.07

    centerline = _subpath_from_distances(
        path, mapper, start_distance, end_distance,
        offset_x=0.0,
        offset_y=offset_y + metrics_center_y + descender_nudge)
    if centerline.elementCount() < 2:
        return QtGui.QPainterPath()

    stroker = QtGui.QPainterPathStroker()
    stroker.setWidth(width)
    try:
        stroker.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        stroker.setCapStyle(QtCore.Qt.PenCapStyle.FlatCap)
    except AttributeError:
        stroker.setJoinStyle(QtCore.Qt.RoundJoin)
        stroker.setCapStyle(QtCore.Qt.FlatCap)
    except Exception:
        record_suppressed_exception()
    return stroker.createStroke(centerline)


def _draw_paths(painter, paths, fill_color, stroke_color=None,
                stroke_width=0.0):
    painter.save()
    try:
        painter.setBrush(QBrush(fill_color))
        if stroke_color is not None and stroke_width > 0.0:
            painter.setPen(_round_join_pen(stroke_color, stroke_width))
        else:
            painter.setPen(QPen(NO_PEN))
        for path in paths:
            painter.drawPath(path)
    finally:
        painter.restore()


def _draw_buffer_paths(painter, paths, color):
    _draw_paths(painter, paths, color, None, 0.0)


def _draw_buffer_strokes(painter, paths, color, stroke_width):
    """Paint a glyph buffer directly, without materializing halo geometry."""
    painter.save()
    try:
        painter.setBrush(QBrush(color))
        painter.setPen(_round_join_pen(color, max(0.0, stroke_width)))
        for path in paths:
            painter.drawPath(path)
    finally:
        painter.restore()


def _draw_text_paths(painter, glyph_plan):
    painter.save()
    try:
        painter.setPen(QPen(NO_PEN))
        for glyph in glyph_plan:
            painter.setBrush(QBrush(glyph["color"]))
            painter.drawPath(glyph["world_path"])
    finally:
        painter.restore()


def _image_format_arg(name):
    """Return a QImage format enum for both Qt5 and Qt6 bindings."""
    value = getattr(QtGui.QImage, name, None)
    if value is not None:
        return value
    try:
        return getattr(QtGui.QImage.Format, name)
    except Exception:
        return getattr(QtGui.QImage, "Format_ARGB32_Premultiplied")


def _draw_unblurred_shadow_paths(painter, paths, shadow_dx, shadow_dy,
                                  shadow_color, shadow_opacity,
                                  stroke_width=0.0):
    color = QColor(shadow_color)
    try:
        base_alpha = float(color.alphaF())
    except Exception:
        base_alpha = float(color.alpha()) / 255.0

    try:
        shadow_opacity = float(shadow_opacity)
    except Exception:
        shadow_opacity = 1.0

    base_alpha = max(0.0, min(1.0, base_alpha * shadow_opacity))
    if base_alpha <= 0.0 or not paths:
        return

    color.setAlphaF(base_alpha)
    painter.save()
    try:
        if stroke_width > 0.0:
            painter.setPen(_round_join_pen(color, stroke_width))
        else:
            painter.setPen(QPen(NO_PEN))
        painter.setBrush(QBrush(color))
        painter.translate(shadow_dx, shadow_dy)
        for path in paths:
            painter.drawPath(path)
    finally:
        painter.restore()


def _blur_effect_and_padding(blur_radius):
    """Return Qt's blur effect and its exact required image padding."""
    fallback = max(4.0, float(blur_radius) * 3.0 + 2.0)
    try:
        effect = QtWidgets.QGraphicsBlurEffect()
        effect.setBlurRadius(max(0.0, float(blur_radius)))
        source = QRectF(0.0, 0.0, 1.0, 1.0)
        expanded = effect.boundingRectFor(source)
        expansion = max(
            source.left() - expanded.left(),
            expanded.right() - source.right(),
            source.top() - expanded.top(),
            expanded.bottom() - source.bottom(),
        )
        return max(4.0, math.ceil(expansion) + 2.0), effect
    except Exception:
        return fallback, None


def _blurred_shadow_image(source_image, blur_radius, effect=None):
    """Blur a transparent source image using Qt's native raster blur effect."""
    try:
        blur_radius = float(blur_radius)
    except Exception:
        blur_radius = 0.0
    if blur_radius <= 0.05:
        return source_image

    try:
        pixmap = QtGui.QPixmap.fromImage(source_image)
        scene = QtWidgets.QGraphicsScene()
        item = QtWidgets.QGraphicsPixmapItem(pixmap)
        if effect is None:
            effect = QtWidgets.QGraphicsBlurEffect()
            effect.setBlurRadius(max(0.0, blur_radius))
        try:
            # Qt5 and Qt6 expose this enum slightly differently; the blur
            # still works if the hint is unavailable.
            effect.setBlurHints(
                QtWidgets.QGraphicsBlurEffect.BlurHint.QualityHint)
        except AttributeError:
            try:
                effect.setBlurHints(QtWidgets.QGraphicsBlurEffect.QualityHint)
            except Exception:
                record_suppressed_exception()
        except Exception:
            record_suppressed_exception()

        item.setGraphicsEffect(effect)
        scene.addItem(item)
        width = max(1, source_image.width())
        height = max(1, source_image.height())
        scene_rect = QRectF(0.0, 0.0, float(width), float(height))
        try:
            scene.setSceneRect(scene_rect)
        except Exception:
            record_suppressed_exception()

        result = QtGui.QImage(
            width, height, _image_format_arg("Format_ARGB32_Premultiplied"))
        result.fill(0)

        qp = QtGui.QPainter(result)
        try:
            qp.setRenderHint(AA_ANTIALIASING, True)
            scene.render(qp, scene_rect, scene_rect)
        finally:
            qp.end()
        return result
    except Exception:
        # Prefer a clean hard shadow to the old repeated-glyph blur artifact
        # if Qt's graphics blur effect is unavailable in a particular build.
        return source_image


def _draw_soft_shadow_paths(painter, paths, shadow_dx, shadow_dy,
                            shadow_color, shadow_opacity, blur_radius,
                            image_cache=None, cache_key=None,
                            stroke_width=0.0):
    try:
        blur_radius = float(blur_radius)
    except Exception:
        blur_radius = 0.0

    clean_paths = [p for p in paths if p is not None and not p.isEmpty()]
    if not clean_paths:
        return

    if blur_radius <= 0.05:
        _draw_unblurred_shadow_paths(
            painter, clean_paths, shadow_dx, shadow_dy,
            shadow_color, shadow_opacity, stroke_width)
        return

    if image_cache is not None and cache_key is not None:
        try:
            cached = image_cache.get("value")
            if cached is not None and image_cache.get("key") == cache_key:
                cached_rect, cached_image = cached
                painter.save()
                try:
                    painter.drawImage(
                        QPointF(cached_rect.left(), cached_rect.top()),
                        cached_image,
                    )
                finally:
                    painter.restore()
                return
        except Exception:
            record_suppressed_exception()

    color = QColor(shadow_color)
    try:
        base_alpha = float(color.alphaF())
    except Exception:
        base_alpha = float(color.alpha()) / 255.0
    try:
        shadow_opacity = float(shadow_opacity)
    except Exception:
        shadow_opacity = 1.0
    base_alpha = max(0.0, min(1.0, base_alpha * shadow_opacity))
    if base_alpha <= 0.0:
        return
    color.setAlphaF(base_alpha)

    bounds = QRectF()
    first = True
    for path in clean_paths:
        br = path.boundingRect().translated(shadow_dx, shadow_dy)
        if stroke_width > 0.0:
            half_stroke = stroke_width / 2.0
            br.adjust(-half_stroke, -half_stroke,
                      half_stroke, half_stroke)
        if first:
            bounds = QRectF(br)
            first = False
        else:
            bounds = bounds.united(br)

    if first or bounds.isEmpty():
        return

    # Ask Qt for the blur's actual output bounds instead of retaining a fixed
    # three-radius transparent border around every temporary raster.
    pad, blur_effect = _blur_effect_and_padding(blur_radius)
    image_rect = QRectF(bounds)
    image_rect.adjust(-pad, -pad, pad, pad)
    width = max(1, int(math.ceil(image_rect.width())))
    height = max(1, int(math.ceil(image_rect.height())))

    # Prevent accidental huge temporary rasters on extreme export settings.
    # In that rare case draw a clean unblurred shadow rather than failing.
    if width * height > 36_000_000:
        _draw_unblurred_shadow_paths(
            painter, clean_paths, shadow_dx, shadow_dy,
            shadow_color, shadow_opacity, stroke_width)
        return

    image = QtGui.QImage(
        width, height, _image_format_arg("Format_ARGB32_Premultiplied"))
    image.fill(0)

    ip = QtGui.QPainter(image)
    try:
        ip.setRenderHint(AA_ANTIALIASING, True)
        if stroke_width > 0.0:
            ip.setPen(_round_join_pen(color, stroke_width))
        else:
            ip.setPen(QPen(NO_PEN))
        ip.setBrush(QBrush(color))
        ip.translate(-image_rect.left() + shadow_dx,
                     -image_rect.top() + shadow_dy)
        for path in clean_paths:
            ip.drawPath(path)
    finally:
        ip.end()

    blurred = _blurred_shadow_image(image, blur_radius, blur_effect)
    if blurred is not image:
        # Release the source raster before compositing the blurred result.
        del image

    # Retain only one reasonably sized preview/export raster. QImage is
    # implicitly shared, so drawing the cached image does not duplicate its
    # pixels. Very large effects remain uncached to avoid persistent RAM use.
    if (image_cache is not None and cache_key is not None
            and width * height <= 8_000_000):
        try:
            image_cache.clear()
            image_cache["key"] = cache_key
            image_cache["value"] = (QRectF(image_rect), blurred)
        except Exception:
            record_suppressed_exception()

    painter.save()
    try:
        painter.drawImage(QPointF(image_rect.left(), image_rect.top()), blurred)
    finally:
        painter.restore()



def _soft_shadow_kernel(blur_radius):
    """Small deterministic Gaussian-like kernel for manual soft text shadows."""
    try:
        blur_radius = float(blur_radius)
    except Exception:
        blur_radius = 0.0

    if blur_radius <= 0.05:
        return ((0.0, 0.0, 1.0),)

    # More rings for larger blur radii, capped so interactive layout editing
    # stays responsive.  Each sample weight is normalized so the center of the
    # shadow keeps roughly the configured opacity while the edges fade out.
    rings = max(2, min(5, int(math.ceil(blur_radius / 3.0))))
    sigma = max(blur_radius * 0.48, 0.35)

    samples = [(0.0, 0.0, 1.0)]
    for ring in range(1, rings + 1):
        radius = blur_radius * ring / rings
        count = 8 + (ring - 1) * 4
        weight = math.exp(-(radius * radius) / (2.0 * sigma * sigma))
        for i in range(count):
            angle = 2.0 * math.pi * i / count
            samples.append((radius * math.cos(angle),
                            radius * math.sin(angle),
                            weight))

    total = sum(weight for _x, _y, weight in samples) or 1.0
    return tuple((x, y, weight / total) for x, y, weight in samples)


def _draw_glyph_path_at(painter, glyph, x_offset, y_offset, vm_px):
    """Draw one glyph path with screen-space offsets, then glyph rotation."""
    painter.save()
    try:
        painter.translate(glyph["pos"].x() + x_offset,
                          glyph["pos"].y() + y_offset)
        painter.rotate(glyph["angle"])
        if vm_px:
            painter.translate(0, vm_px)
        painter.drawPath(glyph["path"])
    finally:
        painter.restore()


def _draw_soft_shadow_glyphs(painter, glyph_plan, vm_px,
                             shadow_dx, shadow_dy,
                             shadow_color, shadow_opacity, blur_radius):
    """Render spline glyph shadows with the configured soft blur radius."""
    color = QColor(shadow_color)
    try:
        base_alpha = float(color.alphaF())
    except Exception:
        base_alpha = float(color.alpha()) / 255.0

    try:
        shadow_opacity = float(shadow_opacity)
    except Exception:
        shadow_opacity = 1.0

    base_alpha = max(0.0, min(1.0, base_alpha * shadow_opacity))
    if base_alpha <= 0.0:
        return

    painter.save()
    try:
        painter.setPen(QPen(NO_PEN))
        for ox, oy, weight in _soft_shadow_kernel(blur_radius):
            c = QColor(color)
            c.setAlphaF(max(0.0, min(1.0, base_alpha * weight)))
            if c.alpha() <= 0:
                continue
            painter.setBrush(QBrush(c))
            for glyph in glyph_plan:
                _draw_glyph_path_at(
                    painter, glyph,
                    shadow_dx + ox, shadow_dy + oy,
                    vm_px,
                )
    finally:
        painter.restore()


class LayoutItemSplineText(QgsLayoutItem):

    ALIGN_LEFT_    = ALIGN_LEFT_
    ALIGN_CENTER_  = ALIGN_CENTER_
    ALIGN_RIGHT_   = ALIGN_RIGHT_
    ALIGN_JUSTIFY_ = ALIGN_JUSTIFY_

    def __init__(self, layout):
        super().__init__(layout)
        self._text = (
            "Curved spline text \u2014 activate the \u201cEdit Curve/Polygon "
            "Nodes\u201d tool, then drag the curve or double-click it to add points."
        )
        self._text_format = _make_default_text_format()
        self._text_format_revision = 0
        self._allow_html  = False  # Render as HTML checkbox state
        self._h_align     = ALIGN_LEFT_   # where text starts on the path
        self._h_margin    = 0.0           # mm  — start offset along path
        self._v_margin    = 0.0           # mm  — baseline offset (+↓ / -↑)
        self._letter_spacing = 0.0        # mm  — extra gap between glyphs
        self._reverse     = False
        # Repurpose QGIS' native Layout Frame controls for the spline path.
        self.setFrameEnabled(True)
        self.setFrameStrokeColor(QColor(180, 180, 180))
        self.setFrameStrokeWidth(QgsLayoutMeasurement(0.15))
        # The item background box remains off; text background/buffer effects
        # come from the Font group instead.
        self.setBackgroundEnabled(False)
        # The spline text/background/shadow can legitimately paint outside the
        # item rectangle.  Ask Qt to notify us whenever the item moves so we can
        # repaint the old outside area too; otherwise the Layout Designer can
        # leave temporary trails while dragging.
        for _flag_name in (
            "ItemSendsGeometryChanges",
            "ItemSendsScenePositionChanges",
        ):
            _flag = _graphics_item_flag(_flag_name)
            if _flag is not None:
                try:
                    self.setFlag(_flag, True)
                except Exception:
                    record_suppressed_exception()
        # Default nodes: gentle arc centred vertically in the item
        self._nodes = [
            QPointF(0.05, 0.5),
            QPointF(0.5,  0.2),
            QPointF(0.95, 0.5),
        ]
        # Cache the composed glyph layout so zoom redraws do not reflow text.
        self._layout_cache_key = None
        self._layout_cache = None
        # Single-entry effect caches avoid repeating expensive path stroking
        # and raster blurring during unchanged QGIS repaint requests. They are
        # replaced whenever the render signature or scale changes.
        self._effect_geometry_cache = {}
        self._shadow_image_cache = {}
        self._paint_effect_picture_cache = {}
        try:
            self.sizePositionChanged.connect(self._request_movement_repaint)
        except Exception:
            record_suppressed_exception()

    def _selection_text_height_mm(self):
        """Approximate the base text height in layout millimeters."""
        try:
            value = float(self._text_format.size())
        except Exception:
            value = 0.0
        try:
            font = self._text_format.font()
            font_points = float(font.pointSizeF())
        except Exception:
            font_points = 10.0
        if font_points <= 0.0:
            font_points = 10.0
        if value <= 0.0:
            value = font_points

        try:
            unit = self._text_format.sizeUnit()
        except Exception:
            unit = None

        def _unit_matches(scoped_name, legacy_name):
            candidates = []
            try:
                from qgis.core import Qgis
                candidates.append(getattr(Qgis.RenderUnit, scoped_name, None))
            except Exception:
                record_suppressed_exception()
            try:
                from qgis.core import QgsUnitTypes
                candidates.append(getattr(QgsUnitTypes, legacy_name, None))
            except Exception:
                record_suppressed_exception()
            return any(candidate is not None and unit == candidate
                       for candidate in candidates)

        if _unit_matches("Millimeters", "RenderMillimeters"):
            height_mm = value
        elif _unit_matches("Inches", "RenderInches"):
            height_mm = value * 25.4
        elif _unit_matches("Pixels", "RenderPixels"):
            # QGIS/Qt's conventional logical screen resolution.
            height_mm = value * 25.4 / 96.0
        elif _unit_matches("Percentage", "RenderPercentage"):
            height_mm = font_points * (value / 100.0) * 25.4 / 72.0
        else:
            # Points are the normal QgsTextFormat unit.  Unknown/map units use
            # the same conservative fallback because no map scale is attached
            # to a layout text item during QGraphicsScene hit testing.
            height_mm = value * 25.4 / 72.0

        return max(0.1, float(height_mm))

    def shape(self):
        """Use a text-height-scaled spline band for initial selection."""
        if self.isSelected():
            return super().shape()

        rect = self.rect()
        nodes = [
            QPointF(node.x() * rect.width(), node.y() * rect.height())
            for node in self._nodes
        ]
        if len(nodes) < 2:
            return super().shape()

        path = build_smooth_path(nodes)
        if path.isEmpty():
            return super().shape()

        # The full clickable band follows the base glyph height.  A small
        # minimum keeps very small text practical to select, while larger text
        # naturally receives a proportionally larger hit target.  Buffer and
        # shadow sizes deliberately do not inflate this area.
        hit_width = max(1.5, self._selection_text_height_mm() * 1.35)
        return _make_stroked_path(path, hit_width)

    def _request_spline_repaint(self, force_layout=False):
        """Schedule a repaint of the spline item and, when needed, layout.

        New items can be painted once before QGIS has fully propagated the
        newly requested scene rectangle.  Scheduling an immediate repaint on
        the next Qt event-loop turn makes the completed spline render appear
        without the user having to press F5.
        """
        try:
            self.update()
        except Exception:
            record_suppressed_exception()

        def _later():
            try:
                self.update()
            except Exception:
                record_suppressed_exception()
            try:
                scene = self.scene()
                if scene is not None:
                    scene.update()
                    try:
                        for view in scene.views():
                            try:
                                view.viewport().update()
                            except Exception:
                                record_suppressed_exception()
                    except Exception:
                        record_suppressed_exception()
            except Exception:
                record_suppressed_exception()
            if force_layout:
                try:
                    layout = self.layout()
                    refresh = getattr(layout, "refresh", None)
                    if refresh is not None:
                        refresh()
                except Exception:
                    record_suppressed_exception()

        try:
            QtCore.QTimer.singleShot(0, _later)
        except Exception:
            _later()

    def _expanded_scene_dirty_rect(self, extra_mm=80.0):
        """Return a generous scene rect for repainting old spline positions."""
        try:
            rect = QRectF(self.sceneBoundingRect())
        except Exception:
            return QRectF()
        try:
            extra = float(extra_mm)
        except Exception:
            extra = 80.0
        rect.adjust(-extra, -extra, extra, extra)
        return rect

    def _update_scene_dirty_area(self, *rects):
        """Invalidate old/new spline areas during node drags.

        When a node is dragged outside the current item bounds and then back in,
        QGIS may only repaint the new bounds.  Updating the union of the old
        and new scene rectangles prevents temporary trails from remaining on the
        Layout Designer canvas.
        """
        try:
            self.update()
        except Exception:
            record_suppressed_exception()

        scene = None
        try:
            scene = self.scene()
        except Exception:
            scene = None
        if scene is None:
            return

        dirty = None
        for rect in rects:
            if rect is None:
                continue
            candidate = None
            try:
                candidate = QRectF(rect)
            except Exception:
                record_suppressed_exception()
            if candidate is None:
                continue
            if candidate.isNull() or not candidate.isValid():
                continue
            dirty = candidate if dirty is None else dirty.united(candidate)

        try:
            if dirty is None:
                scene.update()
            else:
                scene.update(dirty)
        except Exception:
            try:
                scene.update()
            except Exception:
                record_suppressed_exception()

        try:
            for view in scene.views():
                try:
                    view.viewport().update()
                except Exception:
                    record_suppressed_exception()
        except Exception:
            record_suppressed_exception()

        try:
            if dirty is not None:
                saved_dirty = QRectF(dirty)

                def _later_dirty():
                    try:
                        scene.update(saved_dirty)
                    except Exception:
                        record_suppressed_exception()
                    try:
                        for view in scene.views():
                            try:
                                view.viewport().update()
                            except Exception:
                                record_suppressed_exception()
                    except Exception:
                        record_suppressed_exception()

                QtCore.QTimer.singleShot(0, _later_dirty)
            else:
                QtCore.QTimer.singleShot(0, scene.update)
        except Exception:
            record_suppressed_exception()

    def _request_movement_repaint(self):
        """Repaint the full canvas after item movement/transform changes.

        The spline renderer intentionally allows backgrounds/effects to extend
        beyond the item's own rectangle.  During interactive item moves QGIS may
        invalidate only the new item rectangle, so the outside part of the old
        paint can linger until the next canvas click.  A full scene/viewport
        update is safer here and matches the behaviour seen from native layout
        items with larger visual effects.
        """
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
                try:
                    view.viewport().update()
                except Exception:
                    record_suppressed_exception()
        except Exception:
            record_suppressed_exception()

        try:
            QtCore.QTimer.singleShot(0, scene.update)
        except Exception:
            record_suppressed_exception()

    def itemChange(self, change, value):
        repaint = (
            _is_item_geometry_change(change)
            or _is_item_selection_change(change)
        )
        if repaint:
            self._request_movement_repaint()

        try:
            result = super().itemChange(change, value)
        except Exception:
            result = value

        if repaint:
            self._request_movement_repaint()
        return result

    # ---------------------------------------------------------------- identity
    def type(self):        return SPLINE_TEXT_ITEM_TYPE
    def icon(self):        return spline_icon()
    def displayName(self): return "Spline Text"

    def estimatedFrameBleed(self):
        """Include the edit-time node circles in the item's paint bounds."""
        try:
            inherited = float(super().estimatedFrameBleed())
        except Exception:
            inherited = 0.0
        return max(inherited, 1.65)

    # --------------------------------------------------------- node access
    def nodesNormalised(self): return list(self._nodes)

    def nodeScenePositions(self):
        rect = self.rect()
        return [
            self.mapToScene(QPointF(n.x()*rect.width(), n.y()*rect.height()))
            for n in self._nodes
        ]

    def setNodeAtScenePos(self, index, scene_pos):
        if not (0 <= index < len(self._nodes)):
            return
        # Expand the bounding box so nodes can be dragged outside it,
        # mirroring QGIS native polygon/polyline node-item behaviour.
        old_dirty = self._expanded_scene_dirty_rect()
        all_scene = self.nodeScenePositions()
        all_scene[index] = scene_pos
        xs = [p.x() for p in all_scene]
        ys = [p.y() for p in all_scene]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        new_w = max(max_x - min_x, 5.0)
        new_h = max(max_y - min_y, 5.0)
        self.attemptSetSceneRect(QRectF(min_x, min_y, new_w, new_h))
        self._nodes = [
            QPointF(
                min(max((p.x() - min_x) / new_w, 0.0), 1.0),
                min(max((p.y() - min_y) / new_h, 0.0), 1.0),
            )
            for p in all_scene
        ]
        self._update_scene_dirty_area(old_dirty, self._expanded_scene_dirty_rect())

    def insertNodeNearestSegment(self, scene_pos):
        rect = self.rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return
        local   = self.mapFromScene(scene_pos)
        new_pt  = QPointF(
            min(max(local.x()/rect.width(),  0.0), 1.0),
            min(max(local.y()/rect.height(), 0.0), 1.0),
        )
        if len(self._nodes) < 2:
            self._nodes.append(new_pt)
            self.update()
            return
        best_i, best_d = 1, None
        for i in range(len(self._nodes)-1):
            d = point_segment_distance(
                new_pt, self._nodes[i], self._nodes[i+1])
            if best_d is None or d < best_d:
                best_d, best_i = d, i+1
        self._nodes.insert(best_i, new_pt)
        self.update()

    def removeNodeAt(self, index):
        if len(self._nodes) <= 2:
            return False
        if 0 <= index < len(self._nodes):
            # Preserve every surviving vertex in scene space while the item
            # rectangle contracts to the new curve bounds.
            old_dirty = self._expanded_scene_dirty_rect()
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
            self._update_scene_dirty_area(
                old_dirty, self._expanded_scene_dirty_rect())
            self._request_spline_repaint(force_layout=True)
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
        if len(nodes) >= 2:
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
        if len(nodes) >= 2:
            self._nodes = nodes
            self._request_spline_repaint(force_layout=True)

    # --------------------------------------------------------- properties
    def text(self):             return self._text
    def setText(self, v):       self._text = v or ""; self.update()

    def allowHtml(self):
        return self._allow_html

    def setAllowHtml(self, v):
        # Plugin-level "Render as HTML" mode.  Keep it separate from the
        # QgsTextFormat/native Font dialog "Allow HTML formatting" flag.
        self._allow_html = bool(v)
        self.update()

    def textFormat(self):
        return QgsTextFormat(self._text_format)
    def setTextFormat(self, fmt):
        old_dirty = self._expanded_scene_dirty_rect()
        self._text_format = QgsTextFormat(fmt)
        self._text_format_revision += 1
        # Font background Size X/Y can change the visible paint outside the
        # current item rectangle.  Repaint a generous old/new area immediately
        # so the Layout Designer preview updates while the user is editing the
        # Font dialog, without waiting for a canvas click/F5 refresh.
        self._update_scene_dirty_area(old_dirty, self._expanded_scene_dirty_rect())
        self._request_spline_repaint(force_layout=True)

    def horizontalAlignment(self):       return self._h_align
    def setHorizontalAlignment(self, v): self._h_align = v; self.update()

    def hMargin(self):          return self._h_margin
    def setHMargin(self, v):    self._h_margin = float(v); self.update()

    def vMargin(self):          return self._v_margin
    def setVMargin(self, v):    self._v_margin = float(v); self.update()

    def letterSpacing(self):        return self._letter_spacing
    def setLetterSpacing(self, v):  self._letter_spacing = float(v); self.update()

    def reversed(self):         return self._reverse
    def setReversed(self, v):   self._reverse = bool(v); self.update()

    def _base_font_and_color(self, resolve_named_style=False):
        f = QFont(self._text_format.font())
        if resolve_named_style:
            try:
                from qgis.core import QgsFontUtils
                family = f.family()
                if family:
                    try:
                        QgsFontUtils.setFontFamily(f, family)
                    except Exception:
                        f.setFamily(family)
                named_style = self._text_format.namedStyle()
                if named_style:
                    if not QgsFontUtils.updateFontViaStyle(
                            f, named_style, True):
                        f.setStyleName(named_style)
            except Exception:
                try:
                    named_style = self._text_format.namedStyle()
                    if named_style:
                        f.setStyleName(named_style)
                except Exception:
                    record_suppressed_exception()
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
        rect = self.rect()
        font = self._text_format.font()

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
            html_text_format = _copy_text_format_without_effects(
                self._text_format)
            try:
                html_text_format.setAllowHtmlFormatting(True)
            except Exception:
                record_suppressed_exception()
            return (
                "render_html", _safe_float(rect.width(), 6),
                _safe_float(rect.height(), 6), nodes_sig, text_sig,
                int(self._h_align), _safe_float(self._h_margin, 6),
                _safe_float(self._v_margin, 6),
                _safe_float(self._letter_spacing, 6), bool(self._reverse),
                _text_format_signature(html_text_format),
            )
        try:
            cap = int(self._text_format.capitalization())
        except Exception:
            try:
                cap = int(getattr(self._text_format.capitalization(), "value"))
            except Exception:
                cap = -1

        # Buffer, background and shadow settings affect only painting, never
        # glyph placement. Excluding them lets the composed spline layout stay
        # cached while an effect slider is adjusted.
        layout_text_format = _copy_text_format_without_effects(
            self._text_format)
        return (
            _safe_float(rect.width(), 6),
            _safe_float(rect.height(), 6),
            nodes_sig,
            text_sig,
            bool(render_html),
            bool(inline_html),
            int(self._h_align),
            _safe_float(self._h_margin, 6),
            _safe_float(self._v_margin, 6),
            _safe_float(self._letter_spacing, 6),
            bool(self._reverse),
            font.family() or "",
            _safe_float(font.pointSizeF(), 3),
            _safe_float(font.pixelSize(), 3),
            bool(font.bold()),
            bool(font.italic()),
            bool(font.underline()),
            bool(font.strikeOut()),
            bool(font.overline()),
            _safe_float(self._text_format.size(), 3),
            _safe_float(getattr(self._text_format, "lineHeight", lambda: 1.0)(), 3),
            cap,
            bool(text_format_allows_html(self._text_format)),
            _text_format_signature(layout_text_format),
        )

    def _store_spline_layout_cache(self, cache_key, payload, scale_factor):
        try:
            inv = 1.0 / float(scale_factor or 1.0)
        except Exception:
            inv = 1.0
        styles = []
        glyphs = []
        for g in payload.get("glyph_plan", []):
            try:
                src_font = QFont(g.get("src_font", QFont()))
                color = QColor(g.get("color", QColor()))
                style_index = None
                for index, (known_font, known_color) in enumerate(styles):
                    if known_font == src_font and known_color == color:
                        style_index = index
                        break
                if style_index is None:
                    style_index = len(styles)
                    styles.append((src_font, color))
                pos = g.get("pos", QPointF())
                glyphs.append((
                    g.get("ch", ""), style_index,
                    float(pos.x()) * inv, float(pos.y()) * inv,
                    float(g.get("angle", 0.0)),
                    float(g.get("advance", 0.0)) * inv,
                    float(g.get("s0", 0.0)) * inv,
                    float(g.get("s1", 0.0)) * inv,
                ))
            except Exception:
                record_suppressed_exception()
        self._layout_cache_key = cache_key
        self._layout_cache = (tuple(styles), tuple(glyphs))

    def _expand_cached_spline_glyph_plan(self, cache, scale_factor, render_ctx,
                                         fmt_size_unit, fmt_size_scale):
        glyph_plan = []
        try:
            sf = float(scale_factor or 1.0)
        except Exception:
            sf = 1.0
        try:
            styles, glyphs = cache
        except Exception:
            return glyph_plan

        runtime_styles = []
        for source_font, source_color in styles:
            src_font = QFont(source_font)
            df = _fixed_spline_font(
                src_font, sf, render_ctx,
                fmt_size_unit, fmt_size_scale, src_font.pointSizeF())
            runtime_styles.append((
                src_font, df, QtGui.QFontMetricsF(df), QColor(source_color)))

        for record in glyphs:
            style_data = None
            try:
                ch, style_index, x, y, angle, advance, s0, s1 = record
                style_data = runtime_styles[style_index]
            except Exception:
                record_suppressed_exception()
            if style_data is None:
                continue
            src_font, df, fm, color = style_data
            advance = float(advance) * sf
            pos = QPointF(float(x) * sf, float(y) * sf)
            gpath = QtGui.QPainterPath()
            gpath.addText(QPointF(-advance / 2.0, 0), df, ch)
            glyph_plan.append({
                "ch": ch, "src_font": src_font, "font": df, "color": color,
                "pos": pos, "angle": float(angle),
                "advance": advance,
                "s0": float(s0) * sf,
                "s1": float(s1) * sf,
                "fm": fm, "path": gpath,
            })
        return glyph_plan

    # -------------------------------------------------------------- rendering
    def draw(self, context):
        """
        Places glyphs one at a time along the spline path using exact
        QFontMetricsF advance-based positioning (NOT QgsTextRenderer per
        character, which adds its own internal glyph padding that doesn't
        match QFontMetricsF and causes uneven letter spacing).

        Effects (shadow, background, buffer) are rendered in FOUR GLOBAL
        PASSES across every glyph, followed by all glyph fills, so the text ink
        remains the topmost layer everywhere.
        """
        render_ctx = context.renderContext()
        painter = render_ctx.painter()
        if painter is None:
            return
        display_scale_factor = max(
            float(render_ctx.scaleFactor() or 1.0), 1.0e-9)
        composition_scale_factor = 16.0
        scale_factor = composition_scale_factor
        destination_ratio = (
            display_scale_factor / composition_scale_factor)
        unit_render_ctx = _CompositionUnitContext(
            render_ctx, composition_scale_factor / display_scale_factor)
        is_preview_render = _is_layout_preview_render(self)
        painter.save()
        try:
            painter.scale(destination_ratio, destination_ratio)
            painter.setRenderHint(AA_ANTIALIASING, True)
            painter.setRenderHint(AA_TEXT_ANTIALIASING, True)

            rect   = self.rect()
            w_px   = rect.width()  * scale_factor
            h_px   = rect.height() * scale_factor

            base_font, base_color = self._base_font_and_color(
                resolve_named_style=self._allow_html)
            fmt_size_unit = _format_size_unit(self._text_format)
            fmt_size_scale = _format_size_map_unit_scale(self._text_format)
            rfont  = _fixed_spline_font(
                base_font, scale_factor, unit_render_ctx,
                fmt_size_unit, fmt_size_scale, self._text_format.size())
            fm     = QtGui.QFontMetricsF(rfont)

            # Keep manual spline text effects from being clipped at the item
            # edge.  Since spline text is rendered glyph-by-glyph instead of
            # through QgsTextRenderer, we must explicitly include the same
            # component extents QGIS' native textbox includes: buffer width,
            # background X/Y, background stroke, and shadow offset/blur.
            effect_extra = 0.0
            try:
                buf = self._text_format.buffer()
                if buf.enabled() and not self._allow_html:
                    bsz = _setting_distance_to_painter_units(
                        unit_render_ctx, buf, "size", "sizeUnit",
                        "sizeMapUnitScale", scale_factor,
                    )
                    effect_extra = max(effect_extra, abs(bsz) * 2.0)
            except Exception:
                record_suppressed_exception()
            try:
                bg = self._text_format.background()
                if bg.enabled() and not self._allow_html:
                    bsx, bsy = _setting_size_to_painter_units(
                        unit_render_ctx, bg, "size", "sizeUnit",
                        "sizeMapUnitScale", scale_factor,
                    )
                    effect_extra = max(effect_extra, abs(bsx) * 2.0,
                                       abs(bsy) * 2.0)
                    sw = _setting_distance_to_painter_units(
                        unit_render_ctx, bg, "strokeWidth", "strokeWidthUnit",
                        "strokeWidthMapUnitScale", scale_factor,
                    )
                    effect_extra = max(effect_extra, abs(sw) * 2.0)
            except Exception:
                record_suppressed_exception()
            try:
                shd = self._text_format.shadow()
                if shd.enabled() and not self._allow_html:
                    d = _setting_distance_to_painter_units(
                        unit_render_ctx, shd, "offsetDistance",
                        "offsetUnit", "offsetMapUnitScale",
                        scale_factor,
                    )
                    b = _setting_distance_to_painter_units(
                        unit_render_ctx, shd, "blurRadius",
                        "blurRadiusUnit", "blurRadiusMapUnitScale",
                        scale_factor,
                    )
                    effect_extra = max(effect_extra, abs(d) + abs(b) * 3.0)
            except Exception:
                record_suppressed_exception()

            extra  = fm.height() * 0.8 + effect_extra
            painter.setClipRect(QRectF(-extra, -extra,
                                        w_px + 2*extra, h_px + 2*extra))

            nodes_px = [QPointF(n.x()*w_px, n.y()*h_px) for n in self._nodes]
            if self._reverse:
                nodes_px = list(reversed(nodes_px))

            path   = build_smooth_path(nodes_px)
            mapper = PathLengthMapper(path)

            if self.frameEnabled():
                pen = QPen(
                    self.frameStrokeColor(),
                    _frame_width_in_painter_units(self, scale_factor))
                try:
                    pen.setJoinStyle(self.frameJoinStyle())
                except Exception:
                    record_suppressed_exception()
                painter.setPen(pen)
                painter.drawPath(path)

            if mapper.total_length <= 0:
                if is_preview_render and self.isSelected():
                    self._draw_handles(painter, nodes_px, scale_factor)
                return

            cap = None
            try:
                cap = self._text_format.capitalization()
            except Exception:
                cap = None

            resolved       = evaluate_expressions(self._text, self)
            render_html    = self._allow_html
            inline_html    = (
                not render_html
                and text_format_allows_html(self._text_format)
            )
            cache_key = self._layout_signature(
                resolved, render_html, inline_html)
            # Preview glyph plans are deliberately not reused for exports.
            # Export render contexts can use a different DPI conversion than
            # the Layout Designer painter scale; rebuilding the plan makes
            # glyph advances and glyph sizes come from the same export font.
            cached = (
                self._layout_cache
                if (is_preview_render
                    and cache_key == self._layout_cache_key)
                else None
            )

            if cached is None and (render_html or inline_html):
                # Render as HTML and native Allow HTML formatting both parse
                # tags.  Only native Allow HTML keeps the QGIS text effects
                # active; Render as HTML is handled below as a document-like
                # mode which drops buffer/background/shadow/effects.
                segments = extract_segments(
                    resolved, True, base_font, base_color,
                    preserve_source_newlines=inline_html,
                    overlay_base_font=render_html)
            elif cached is None:
                # Keep the original stream intact here.  Capitalization must
                # be applied across the complete spline stream below, rather
                # than separately by Qt to every one-character glyph.
                segments = extract_segments(
                    resolved, False, base_font, base_color)

            hm_px = self._h_margin       * scale_factor
            vm_px = self._v_margin       * scale_factor
            ls_px = self._letter_spacing * scale_factor

            if cached is None:
                try:
                    forced_bold = bool(self._text_format.forcedBold())
                except Exception:
                    forced_bold = False
                try:
                    forced_italic = bool(self._text_format.forcedItalic())
                except Exception:
                    forced_italic = False
                char_stream = list(_inline_spline_char_stream(
                    segments, cap, forced_bold, forced_italic))
            else:
                char_stream = ()

            runtime_fonts = []

            def _runtime_font(font):
                for known_font, draw_font, metrics in runtime_fonts:
                    if known_font == font:
                        return draw_font, metrics
                draw_font = _fixed_spline_font(
                    font, scale_factor, unit_render_ctx,
                    fmt_size_unit, fmt_size_scale, font.pointSizeF())
                metrics = QtGui.QFontMetricsF(draw_font)
                runtime_fonts.append((QFont(font), draw_font, metrics))
                return draw_font, metrics

            # ── Pass 0: total advance (needed for center/right alignment) ──
            if cached is None and self._h_align != ALIGN_LEFT_:
                total_adv = 0.0
                for ch, font, _ in char_stream:
                    if ch in ("\n", "\u2028"):
                        continue
                    _df, metrics = _runtime_font(font)
                    adv = metrics.horizontalAdvance(ch)
                    if total_adv + adv > mapper.total_length - hm_px:
                        break
                    total_adv += adv + ls_px
                if self._h_align == ALIGN_CENTER_:
                    start = max(0.0, (mapper.total_length - total_adv) / 2.0)
                elif self._h_align == ALIGN_RIGHT_:
                    start = max(0.0, mapper.total_length - total_adv - hm_px)
                else:
                    start = hm_px
            else:
                start = hm_px

            _opacity = 1.0
            try:
                _opacity = float(self._text_format.opacity())
            except Exception:
                _opacity = 1.0
            if _opacity < 1.0:
                painter.setOpacity(_opacity)

            # ── Pass 1: compute the glyph plan (position/rotation only) ────
            # One single position-calculation pass, reused by every
            # subsequent rendering pass below -- guarantees every pass
            # places each glyph at EXACTLY the same spot.
            glyph_plan = []
            distance = start
            for ch, font, color in char_stream:
                if ch in ("\n", "\u2028"):
                    continue
                df, metrics = _runtime_font(font)
                advance = metrics.horizontalAdvance(ch)
                if distance + advance > mapper.total_length - hm_px:
                    break
                center = distance + advance / 2.0
                t      = mapper.percent_at_length(center)
                pos    = path.pointAtPercent(t)
                angle  = -path.angleAtPercent(t)
                gpath = QtGui.QPainterPath()
                gpath.addText(QPointF(-advance / 2.0, 0), df, ch)
                glyph_plan.append({
                    "ch": ch, "font": df, "src_font": font, "color": color,
                    "pos": pos, "angle": angle, "advance": advance,
                    "s0": distance, "s1": distance + advance,
                    "fm": metrics, "path": gpath,
                })
                distance += advance + ls_px

            if cached is not None:
                try:
                    glyph_plan = self._expand_cached_spline_glyph_plan(
                        cached, scale_factor, unit_render_ctx,
                        fmt_size_unit, fmt_size_scale)
                except Exception:
                    record_suppressed_exception()
            else:
                if glyph_plan and is_preview_render:
                    try:
                        self._store_spline_layout_cache(
                            cache_key, {
                                "glyph_plan": glyph_plan,
                            }, scale_factor)
                    except Exception:
                        record_suppressed_exception()

            if not glyph_plan:
                if is_preview_render and self.isSelected():
                    self._draw_handles(painter, nodes_px, scale_factor)
                return

            # Read each textFormat component once, then build world-space paths
            # for those components.  Native QgsTextRenderer applies effects to
            # "components" (background/shape, buffer, text) rather than only
            # to the glyph fill. These paths make the shadow and optional paint
            # effects follow the same component footprint.
            _txt_effect = (
                None if render_html else _paint_effect(self._text_format))

            _buf_en = False
            _buf_col = QColor(255, 255, 255)
            _buf_sz = 0.0
            _buf_effect = None

            _bg_en = False
            _bg_col = QColor(255, 255, 255)
            _bg_stroke_col = None
            _bg_stroke_w = 0.0
            _bg_sx = 0.0
            _bg_sy = 0.0
            _bg_off_x = 0.0
            _bg_off_y = 0.0
            _bg_radius_x = 0.0
            _bg_radius_y = 0.0
            _bg_is_fixed = False
            _bg_shape = "rectangle"
            _bg_effect = None

            _shd_en = False
            _shd_col = QColor(0, 0, 0, 180)
            _shd_dx = 1.0
            _shd_dy = 1.0
            _shd_op = 0.7
            _shd_blur = 0.0
            _shd_component = "text"

            try:
                buf = self._text_format.buffer()
                if buf.enabled() and not render_html:
                    _buf_en = True
                    _buf_sz = _setting_distance_to_painter_units(
                        unit_render_ctx, buf, "size", "sizeUnit",
                        "sizeMapUnitScale", scale_factor,
                    )
                    _buf_col = _color_with_opacity(
                        buf.color(), _setting_opacity(buf, 1.0))
                    _buf_effect = _paint_effect(buf)
            except Exception:
                record_suppressed_exception()

            try:
                bg = self._text_format.background()
                if bg.enabled() and not render_html:
                    _bg_en = True
                    _bg_col = _color_with_opacity(
                        bg.fillColor(), _setting_opacity(bg, 1.0))
                    try:
                        _bg_stroke_col = _color_with_opacity(
                            bg.strokeColor(), _setting_opacity(bg, 1.0))
                    except Exception:
                        _bg_stroke_col = None
                    _bg_stroke_w = _setting_distance_to_painter_units(
                        unit_render_ctx, bg, "strokeWidth", "strokeWidthUnit",
                        "strokeWidthMapUnitScale", scale_factor,
                    )
                    _bg_sx, _bg_sy = _setting_size_to_painter_units(
                        unit_render_ctx, bg, "size", "sizeUnit",
                        "sizeMapUnitScale", scale_factor,
                    )
                    _bg_off_x, _bg_off_y = _setting_size_to_painter_units(
                        unit_render_ctx, bg, "offset", "offsetUnit",
                        "offsetMapUnitScale", scale_factor,
                    )
                    _bg_radius_x, _bg_radius_y = (
                        _background_radii_to_painter_units(
                            unit_render_ctx, bg, scale_factor))
                    _bg_is_fixed = _background_is_fixed_size(bg)
                    _bg_shape = _background_shape(bg)
                    _bg_effect = _paint_effect(bg)
            except Exception:
                record_suppressed_exception()

            try:
                shd = self._text_format.shadow()
                if shd.enabled() and not render_html:
                    _shd_en = True
                    _shd_col = shd.color()
                    d = _setting_distance_to_painter_units(
                        unit_render_ctx, shd, "offsetDistance",
                        "offsetUnit", "offsetMapUnitScale",
                        scale_factor,
                    )
                    _shd_blur = _setting_distance_to_painter_units(
                        unit_render_ctx, shd, "blurRadius",
                        "blurRadiusUnit", "blurRadiusMapUnitScale",
                        scale_factor,
                    )
                    a = math.radians(shd.offsetAngle())
                    # QGIS angle is CLOCKWISE from NORTH; screen coords y-down.
                    # dx=d*sin(α), dy=-d*cos(α) gives the correct screen offset.
                    _shd_dx = d * math.sin(a)
                    _shd_dy = -d * math.cos(a)
                    try:
                        _shd_op = float(shd.opacity())
                    except Exception:
                        record_suppressed_exception()
                    _shd_component = _shadow_component(
                        shd, _bg_en, _buf_en)
            except Exception:
                record_suppressed_exception()

            if render_html:
                # Keep Render as HTML distinct from native Allow HTML
                # formatting: parse richer HTML, but do not apply the QGIS
                # font buffer/background/drop-shadow/draw-effects stack.
                _txt_effect = None
                _buf_en = False
                _bg_en = False
                _shd_en = False

            # Precompute the component paths in the spline item's painter
            # coordinate system.  Shadows and paint effects can now operate on
            # the actual background/buffer shapes instead of a hard-coded text
            # glyph footprint.
            geometry_key = (
                cache_key,
                round(float(scale_factor), 8),
                round(float(vm_px), 6),
            )
            geometry_cached = None
            try:
                if self._effect_geometry_cache.get("key") == geometry_key:
                    geometry_cached = self._effect_geometry_cache.get("value")
            except Exception:
                geometry_cached = None

            if geometry_cached is not None:
                _text_paths = geometry_cached
            else:
                _text_paths = [
                    _transformed_glyph_path(g, g["path"], vm_px)
                    for g in glyph_plan
                ]
                try:
                    self._effect_geometry_cache.clear()
                    self._effect_geometry_cache["key"] = geometry_key
                    self._effect_geometry_cache["value"] = tuple(_text_paths)
                except Exception:
                    record_suppressed_exception()

            # Text paint effects consume the same transformed paths. Keep the
            # glyph dictionaries compatible without recomputing transforms.
            for g, world_path in zip(glyph_plan, _text_paths):
                g["world_path"] = world_path

            effect_picture_cache = None
            effects_enabled = any((
                _paint_effect_enabled(_txt_effect),
                _paint_effect_enabled(_buf_effect) if _buf_en else False,
                _paint_effect_enabled(_bg_effect) if _bg_en else False,
            ))
            if effects_enabled and is_preview_render:
                effect_state = (
                    geometry_key,
                    self._text_format_revision,
                )
                try:
                    if self._paint_effect_picture_cache.get("state") != effect_state:
                        self._paint_effect_picture_cache.clear()
                        self._paint_effect_picture_cache["state"] = effect_state
                        self._paint_effect_picture_cache["pictures"] = {}
                    effect_picture_cache = (
                        self._paint_effect_picture_cache.get("pictures"))
                except Exception:
                    effect_picture_cache = None
            elif not effects_enabled:
                try:
                    self._paint_effect_picture_cache.clear()
                except Exception:
                    record_suppressed_exception()

            _bg_paths = []
            if _bg_en:
                continuous_bg = (
                    not _bg_is_fixed
                    and _bg_shape in ("rectangle", "rounded")
                    and len(glyph_plan) >= 2
                )
                if continuous_bg:
                    bg_path = _make_continuous_background_path(
                        path, mapper, glyph_plan,
                        _bg_sx, _bg_sy, _bg_off_x, _bg_off_y + vm_px,
                    )
                    if not bg_path.isEmpty():
                        _bg_paths.append(bg_path)
                if not _bg_paths:
                    for g in glyph_plan:
                        bg_local = _background_local_path(
                            g, _bg_sx, _bg_sy, _bg_is_fixed, _bg_shape,
                            _bg_off_x, _bg_off_y, _bg_radius_x, _bg_radius_y,
                        )
                        _bg_paths.append(_transformed_glyph_path(
                            g, bg_local, vm_px))

            # ── Pass 2: shadow, drawn under the configured component ─────────
            if _shd_en:
                shadow_paths = _text_paths
                shadow_stroke_width = 0.0
                if _shd_component == "background" and _bg_paths:
                    shadow_paths = _bg_paths
                elif _shd_component == "buffer" and _buf_en:
                    # Paint the same native stroke into the shadow source
                    # raster; no per-glyph halo geometry is required.
                    shadow_stroke_width = _buf_sz * 2.0

                try:
                    shadow_rgba = int(QColor(_shd_col).rgba())
                except Exception:
                    shadow_rgba = 0
                shadow_cache_key = (
                    geometry_key,
                    str(_shd_component),
                    shadow_rgba,
                    round(float(_shd_op), 6),
                    round(float(_shd_dx), 6),
                    round(float(_shd_dy), 6),
                    round(float(_shd_blur), 6),
                    round(float(shadow_stroke_width), 6),
                )
                _draw_soft_shadow_paths(
                    painter, shadow_paths,
                    _shd_dx, _shd_dy,
                    _shd_col, _shd_op, _shd_blur,
                    self._shadow_image_cache, shadow_cache_key,
                    shadow_stroke_width,
                )
            else:
                # Release a potentially large raster as soon as the effect is
                # disabled instead of retaining it for the item's lifetime.
                try:
                    self._shadow_image_cache.clear()
                except Exception:
                    record_suppressed_exception()

            # ── Pass 3: background/shape, including X/Y size and effects ─────
            if _bg_en and _bg_paths:
                _draw_with_optional_paint_effect(
                    render_ctx, painter, _bg_effect,
                    lambda p: _draw_paths(
                        p, _bg_paths, _bg_col,
                        _bg_stroke_col, _bg_stroke_w),
                    effect_picture_cache, "background",
                )

            # ── Pass 4: text buffer/halo, including units and effects ────────
            if _buf_en and _text_paths and _buf_sz > 0.0:
                _draw_with_optional_paint_effect(
                    render_ctx, painter, _buf_effect,
                    lambda p: _draw_buffer_strokes(
                        p, _text_paths, _buf_col, _buf_sz * 2.0),
                    effect_picture_cache, "buffer",
                )

            # ── Pass 5: actual glyph text, ALWAYS the topmost layer ──────────
            painter.save()
            try:
                if render_html:
                    try:
                        painter.setCompositionMode(
                            self._text_format.blendMode())
                    except Exception:
                        record_suppressed_exception()
                if _paint_effect_enabled(_txt_effect):
                    _draw_with_optional_paint_effect(
                        render_ctx, painter, _txt_effect,
                        lambda p: _draw_text_paths(p, glyph_plan),
                        effect_picture_cache, "text",
                    )
                else:
                    # Preserve Qt's hinted text rendering when no QGIS paint
                    # effect is requested; paths are used only when an effect
                    # needs a source path.
                    for g in glyph_plan:
                        painter.save()
                        painter.translate(g["pos"])
                        painter.rotate(g["angle"])
                        if vm_px:
                            painter.translate(0, vm_px)
                        painter.setFont(g["font"])
                        painter.setPen(QPen(g["color"]))
                        painter.drawText(
                            QPointF(-g["advance"] / 2.0, 0), g["ch"])
                        painter.restore()
            finally:
                painter.restore()

            if is_preview_render and self.isSelected():
                self._draw_handles(painter, nodes_px, scale_factor)
        finally:
            painter.restore()

    def _draw_handles(self, painter, nodes_px, scale_factor):
        painter.setPen(QPen(QColor(40,90,200), 0.25*scale_factor))
        painter.setBrush(QColor(255,255,255))
        r = 1.4 * scale_factor
        for pt in nodes_px:
            painter.drawEllipse(pt, r, r)


    # ---- QgsLayoutItem overrides (no rectangular background/frame) -----
    def drawBackground(self, context):
        """Always transparent — background effects come from Font group."""
        pass

    def drawFrame(self, context):
        """No frame border for spline text items."""
        pass
    # ------------------------------------------------------------ persistence
    def writePropertiesToElement(self, element, document, context):
        element.setAttribute("splineText",    self._text)
        element.setAttribute("splineHtml", "1" if self._allow_html else "0")
        element.setAttribute("splineHAlign",  str(self._h_align))
        element.setAttribute("splineHMargin", str(self._h_margin))
        element.setAttribute("splineVMargin", str(self._v_margin))
        element.setAttribute("splineLSpacing",str(self._letter_spacing))
        element.setAttribute("splineReversed","1" if self._reverse else "0")
        node_str = ";".join(f"{n.x():.6f},{n.y():.6f}" for n in self._nodes)
        element.setAttribute("splineNodes", node_str)
        _append_text_format_to_element(
            element, document, context, self._text_format, "splineTextFormat")
        return True

    def readPropertiesFromElement(self, element, document, context):
        self._text       = element.attribute("splineText", self._text)
        self._allow_html = element.attribute("splineHtml", "0") == "1"
        self._h_align    = int(element.attribute("splineHAlign",  "0") or "0")
        self._reverse    = element.attribute("splineReversed","0") == "1"
        for attr, field, default in [
            ("splineHMargin",  "_h_margin",       0.0),
            ("splineVMargin",  "_v_margin",        0.0),
            ("splineLSpacing", "_letter_spacing",  0.0),
        ]:
            v = element.attribute(attr, "")
            setattr(self, field, float(v) if v else default)

        fmt = _read_text_format_from_element(
            element, context, self._text_format, ("splineTextFormat",))
        if fmt is not None:
            self._text_format = fmt
        else:
            # Backward compat: read old font attributes
            fam  = element.attribute("splineFontFamily", "")
            sz   = element.attribute("splineFontSize",   "10.0")
            bold = element.attribute("splineFontBold",   "0") == "1"
            ital = element.attribute("splineFontItalic", "0") == "1"
            col  = element.attribute("splineColor", "#141414")
            f = QFont(fam) if fam else QFont(); f.setPointSizeF(float(sz) if sz else 10.0)
            f.setBold(bold); f.setItalic(ital)
            self._text_format = _make_default_text_format()
            self._text_format.setFont(f)
            self._text_format.setSize(float(sz) if sz else 10.0)
            self._text_format.setColor(QColor(col))
        self._text_format_revision += 1
        self._paint_effect_picture_cache.clear()

        node_str = element.attribute("splineNodes", "")
        if node_str:
            nodes = []
            for pair in node_str.split(";"):
                if not pair: continue
                xs, ys = pair.split(",")
                nodes.append(QPointF(float(xs), float(ys)))
            if len(nodes) >= 2:
                self._nodes = nodes
        return True

    def clone(self):
        from qgis.PyQt.QtXml import QDomDocument
        item = LayoutItemSplineText(self.layout())
        doc  = QDomDocument()
        elem = doc.createElement("clonedSplineText")
        self.writePropertiesToElement(elem, doc, QgsReadWriteContext())
        item.readPropertiesFromElement(elem, doc, QgsReadWriteContext())
        # QGIS copy/paste may use clone() directly.  Keep a direct deep copy of
        # the text format too, so buffer/background/shadow/effects survive even
        # if a binding-specific XML round trip skips part of QgsTextFormat.
        try:
            item._text_format = QgsTextFormat(self._text_format)
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
