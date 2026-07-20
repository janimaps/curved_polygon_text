"""
text_engine.py

All the "feature parity with the native Add Label item" logic lives
here, shared by both custom layout items:

  1. evaluate_expressions()   -- runs [% ... %] QGIS expression tokens
                                  through QgsExpression.replaceExpressionText(),
                                  exactly like QgsLayoutItemLabel does.
  2. extract_segments()       -- parses "Allow HTML Formatting" rich text
                                  into a flat list of (text, font, color)
                                  segments using QTextDocument, so HTML
                                  tags such as <b>, <i>, <span style="color:..">
                                  are honoured rather than shown as raw
                                  markup.
  3. PathLengthMapper          -- arc-length parametrisation of a
                                  QPainterPath, used by the spline text
                                  item so glyph spacing stays visually
                                  even around curves.
  4. polygon_scanline_spans() -- horizontal ray/polygon intersection,
                                  used by the polygon text item to find
                                  how wide each line of text is allowed
                                  to be at a given height.
  5. point_segment_distance() -- shared geometry helper used when
                                  inserting a new node into the nearest
                                  curve/edge segment.

Keeping this logic in one module means both items render identically
and any future fix (e.g. better HTML parsing) benefits both at once.
"""
import math
from dataclasses import dataclass

from qgis.core import QgsExpression, QgsMessageLog, Qgis, QgsTextDocument

from .compat import QtGui, QFont, QColor, QTextDocument, QTextLayout, QTextCharFormat, QBrush, NO_BRUSH, QPainterPath
from .reliability import record_suppressed_exception

LOG_TAG = "Curved/Polygon Text"


# ============================================================ expressions
def evaluate_expressions(text, layout_item):
    """
    Resolves [% expression %] tokens in `text` using the layout item's
    own expression context (which already includes layout, page,
    project and atlas/coverage scopes) -- the same mechanism the native
    Add Label item uses for "Insert an Expression" dynamic text.

    Never raises: a malformed expression must not break rendering of
    the whole layout, so failures are logged and the original text is
    returned unmodified.
    """
    if not text:
        return ""
    # Static text is overwhelmingly the common case.  Avoid constructing a
    # full QGIS expression context unless the text can actually contain an
    # embedded layout expression.
    if "[%" not in text:
        return text
    try:
        context = layout_item.createExpressionContext()
        return QgsExpression.replaceExpressionText(text, context)
    except Exception as exc:  # noqa: BLE001 - rendering must never crash
        QgsMessageLog.logMessage(
            f"Expression evaluation failed, showing raw text instead: {exc}",
            LOG_TAG, Qgis.MessageLevel.Warning,
        )
        return text


# ============================================================ HTML parsing
@dataclass
class Segment:
    text: str          # plain run of characters, or the literal "\n" for a break
    font: QFont
    color: QColor


def _text_property(name):
    """Resolve QTextFormat property enums across Qt 5 and Qt 6."""
    owners = [getattr(QtGui, "QTextFormat", None)]
    scoped = getattr(getattr(QtGui, "QTextFormat", None), "Property", None)
    if scoped is not None:
        owners.append(scoped)
    for owner in owners:
        if owner is not None:
            value = getattr(owner, name, None)
            if value is not None:
                return value
    return None


def _inline_html_font(base_font, char_format):
    """Overlay only explicitly supplied inline-HTML font properties."""
    out = QFont(base_font)
    parsed = char_format.font()

    def has(*names):
        for name in names:
            prop = _text_property(name)
            if prop is not None:
                try:
                    if char_format.hasProperty(prop):
                        return True
                except Exception:
                    record_suppressed_exception()
        return False

    setters = (
        (("FontFamily", "FontFamilies"), "setFamily", "family"),
        (("FontPointSize",), "setPointSizeF", "pointSizeF"),
        (("FontWeight",), "setWeight", "weight"),
        (("FontItalic",), "setItalic", "italic"),
        (("FontUnderline", "TextUnderlineStyle"), "setUnderline", "underline"),
        (("FontStrikeOut",), "setStrikeOut", "strikeOut"),
        (("FontOverline",), "setOverline", "overline"),
        (("FontStretch",), "setStretch", "stretch"),
        (("FontCapitalization",), "setCapitalization", "capitalization"),
        (("FontKerning",), "setKerning", "kerning"),
        (("FontStyleName",), "setStyleName", "styleName"),
        (("FontWordSpacing",), "setWordSpacing", "wordSpacing"),
    )
    for names, setter_name, getter_name in setters:
        if not has(*names):
            continue
        try:
            getattr(out, setter_name)(getattr(parsed, getter_name)())
        except Exception:
            record_suppressed_exception()
    if has("FontLetterSpacing", "FontLetterSpacingType"):
        try:
            out.setLetterSpacing(
                parsed.letterSpacingType(), parsed.letterSpacing())
        except Exception:
            record_suppressed_exception()
    return out


def extract_segments(text, allow_html, base_font, base_color,
                     preserve_source_newlines=False,
                     overlay_base_font=False, block_spacing_out=None):
    """
    Turns `text` into a flat list of Segment objects carrying per-run
    font/colour formatting.

    When allow_html is False this mirrors the native label item's
    plain-text mode: the whole string uses base_font/base_color, with
    "\\n" segments marking line breaks.

    When allow_html is True, a QTextDocument is used to parse the HTML
    (bold/italic/underline tags, inline `style="color:.."`/`font-*`
    attributes, <span>, <br>, multiple <p>/<div> blocks, etc.) and the
    formatting Qt resolved for each fragment of text is carried over
    into the resulting Segment list.
    """
    segments = []
    if block_spacing_out is not None:
        block_spacing_out.clear()
        block_spacing_out.update({
            "leading": 0.0, "trailing": 0.0, "breaks": {}})
    if not text:
        return segments

    if not allow_html:
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line:
                segments.append(Segment(line, QFont(base_font), QColor(base_color)))
            if i != len(lines) - 1:
                segments.append(Segment("\n", QFont(base_font), QColor(base_color)))
        return segments

    if preserve_source_newlines:
        # QGIS' native "Allow HTML Formatting" is an inline/simple-HTML mode.
        # Raw editor newlines still represent user-authored line breaks, but
        # QTextDocument's HTML parser normally collapses them as whitespace.
        # Convert only this mode's source newlines to explicit HTML breaks;
        # plugin-level Render as HTML retains its previous parsing unchanged.
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\n", "<br/>")

    doc = QTextDocument()
    doc.setDefaultFont(base_font)
    doc.setHtml(text)

    block = doc.begin()
    first_block = True
    previous_bottom_margin = 0.0
    plain_offset = 0
    while block.isValid():
        try:
            block_format = block.blockFormat()
            top_margin = max(0.0, float(block_format.topMargin()))
            bottom_margin = max(0.0, float(block_format.bottomMargin()))
        except Exception:
            top_margin = 0.0
            bottom_margin = 0.0
        if not first_block:
            if block_spacing_out is not None:
                block_spacing_out["breaks"][plain_offset] = max(
                    previous_bottom_margin, top_margin)
            segments.append(Segment("\n", QFont(base_font), QColor(base_color)))
            plain_offset += 1
        elif block_spacing_out is not None:
            block_spacing_out["leading"] = top_margin
        first_block = False

        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid():
                frag_text = frag.text()
                fmt = frag.charFormat()
                font = fmt.font()
                if preserve_source_newlines or overlay_base_font:
                    # Native "Allow HTML formatting" is an inline overlay on
                    # the configured QgsTextFormat. Render-as-HTML callers may
                    # also request the same base-font overlay while retaining
                    # rich block parsing. Resolve only explicitly supplied HTML
                    # font attributes over the complete base QFont so family,
                    # named style, size and formatting are not replaced by
                    # QTextDocument defaults.
                    font = _inline_html_font(base_font, fmt)
                if not font.family():
                    font.setFamily(base_font.family())
                if font.pointSizeF() <= 0:
                    font.setPointSizeF(base_font.pointSizeF())
                brush = fmt.foreground()
                color = brush.color() if brush.style() != NO_BRUSH else QColor(base_color)

                # Qt represents a <br> *inside* a paragraph as U+2028
                # (LINE SEPARATOR) within the fragment text, rather than
                # as a new block -- split on it so spline/polygon
                # rendering both treat it as a forced break.
                parts = frag_text.split("\u2028")
                for idx, chunk in enumerate(parts):
                    if chunk:
                        segments.append(Segment(chunk, font, color))
                        plain_offset += len(chunk)
                    if idx != len(parts) - 1:
                        segments.append(Segment("\n", font, color))
                        plain_offset += 1
            it += 1
        previous_bottom_margin = bottom_margin
        block = block.next()

    if block_spacing_out is not None:
        block_spacing_out["trailing"] = previous_bottom_margin

    return segments


def extract_qgis_html_segments(text, text_format, base_font, base_color):
    """Parse native Allow HTML formatting through QGIS itself.

    QgsTextDocument.fromTextAndFormat() is the parser used by
    QgsTextRenderer/Add Label. Flattening that parsed document gives the
    polygon compositor QGIS-resolved runs for measurement while ensuring that
    the supported tag and CSS subset follows the installed QGIS version.
    """
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    try:
        document = QgsTextDocument.fromTextAndFormat(
            normalized.split("\n"), text_format)
    except Exception:
        return extract_segments(
            normalized, True, base_font, base_color,
            preserve_source_newlines=True, overlay_base_font=True)

    def _enum_override(value, inherited):
        try:
            name = value.name.lower()
        except Exception:
            name = str(value).lower()
        if "settrue" in name:
            return True
        if "setfalse" in name:
            return False
        return inherited

    def _set_weight(font, weight):
        try:
            weight = int(weight)
        except Exception:
            return
        if weight < 0:
            return
        try:
            scoped = getattr(QFont, "Weight", None)
            font.setWeight(scoped(weight) if scoped is not None else weight)
        except Exception:
            try:
                font.setWeight(weight)
            except Exception:
                record_suppressed_exception()

    segments = []
    try:
        block_count = int(document.size())
    except Exception:
        block_count = 0

    for block_index in range(block_count):
        if block_index:
            segments.append(Segment(
                "\n", QFont(base_font), QColor(base_color)))
        fragment_count = 0
        try:
            block = document.at(block_index)
            fragment_count = int(block.size())
        except Exception:
            record_suppressed_exception()

        for fragment_index in range(fragment_count):
            fragment_text = None
            character_format = None
            try:
                fragment = block.at(fragment_index)
                fragment_text = fragment.text()
                character_format = fragment.characterFormat()
            except Exception:
                record_suppressed_exception()
            if not fragment_text or character_format is None:
                continue

            font = QFont(base_font)
            try:
                family = character_format.family()
                if family:
                    font.setFamily(family)
            except Exception:
                record_suppressed_exception()
            try:
                point_size = float(character_format.fontPointSize())
                percentage_size = float(
                    character_format.fontPercentageSize())
                if point_size > 0:
                    font.setPointSizeF(point_size)
                elif percentage_size > 0 and font.pointSizeF() > 0:
                    font.setPointSizeF(
                        font.pointSizeF() * percentage_size)
            except Exception:
                record_suppressed_exception()
            try:
                _set_weight(font, character_format.fontWeight())
            except Exception:
                record_suppressed_exception()
            for getter, setter, inherited in (
                    ("italic", "setItalic", font.italic()),
                    ("underline", "setUnderline", font.underline()),
                    ("strikeOut", "setStrikeOut", font.strikeOut()),
                    ("overline", "setOverline", font.overline())):
                try:
                    value = getattr(character_format, getter)()
                    getattr(font, setter)(_enum_override(value, inherited))
                except Exception:
                    record_suppressed_exception()
            try:
                spacing = float(character_format.wordSpacing())
                if math.isfinite(spacing):
                    dpi = 96.0
                    try:
                        app = getattr(QtGui, "QGuiApplication", None)
                        screen = app.primaryScreen() if app is not None else None
                        if screen is not None:
                            dpi = float(screen.logicalDotsPerInchX())
                    except Exception:
                        record_suppressed_exception()
                    font.setWordSpacing(spacing * dpi / 72.0)
            except Exception:
                record_suppressed_exception()

            color = QColor(base_color)
            try:
                override_color = character_format.textColor()
                if override_color.isValid():
                    color = QColor(override_color)
            except Exception:
                record_suppressed_exception()
            segments.append(Segment(fragment_text, font, color))

    return segments


def segments_to_char_stream(segments):
    """Expands Segments into a flat (char, font, color) iterator -- used
    by the spline item, which places one glyph at a time along a path."""
    for seg in segments:
        if seg.text == "\n":
            yield ("\n", seg.font, seg.color)
            continue
        for ch in seg.text:
            yield (ch, seg.font, seg.color)


def segments_to_plain_and_formats(segments, scale_factor=None,
                                  render_ctx=None, size_unit=None,
                                  size_map_unit_scale=None):
    """
    Flattens Segments into (plain_text, format_ranges) where
    format_ranges is a list of QTextLayout.FormatRange -- used by the
    polygon item, which hands the whole string to QTextLayout for
    proper word-wrapping and only needs formatting *ranges*, not
    per-character lookups.

    If scale_factor is given, every per-run font is converted via
    render_font() first, so HTML-formatted runs (e.g. a <span> with its
    own font-size) stay in lockstep with the rest of the zoom-aware
    rendering the same way the base font is (see both items' draw()).
    """
    plain_parts = []
    formats = []
    offset = 0
    for seg in segments:
        if seg.text == "\n":
            plain_parts.append("\n")
            offset += 1
            continue
        plain_parts.append(seg.text)
        char_fmt = QTextCharFormat()
        font = (render_font(seg.font, scale_factor, render_ctx,
                            size_unit, size_map_unit_scale)
                if scale_factor else seg.font)
        char_fmt.setFont(font)
        char_fmt.setForeground(QBrush(seg.color))
        rng = QTextLayout.FormatRange()
        rng.start = offset
        rng.length = len(seg.text)
        rng.format = char_fmt
        formats.append(rng)
        offset += len(seg.text)
    return "".join(plain_parts), formats


def text_format_allows_html(text_format):
    """Return QgsTextFormat's native 'Allow HTML formatting' flag.

    This is intentionally separate from the plugin's "Render as HTML"
    checkbox.  Native Allow HTML formatting is for simple inline tags while
    still keeping QGIS text effects (buffer, shadow, background, opacity).
    """
    if text_format is None:
        return False
    for name in ("allowHtmlFormatting", "allowsHtmlFormatting",
                 "htmlFormattingAllowed"):
        getter = getattr(text_format, name, None)
        if getter is not None:
            try:
                return bool(getter())
            except TypeError:
                try:
                    return bool(getter)
                except Exception:
                    record_suppressed_exception()
            except Exception:
                record_suppressed_exception()
    return False


def render_html_base_font(font):
    """Keep only font family and size for plugin Render-as-HTML mode."""
    try:
        out = QFont(font.family())
    except Exception:
        out = QFont()
    try:
        if font.pointSizeF() > 0:
            out.setPointSizeF(font.pointSizeF())
        elif font.pixelSize() > 0:
            out.setPixelSize(font.pixelSize())
    except Exception:
        record_suppressed_exception()
    return out


def _color_css(color, base_color=None):
    try:
        if base_color is not None and color == base_color:
            return None
    except Exception:
        record_suppressed_exception()
    try:
        return color.name()
    except Exception:
        return None


def _font_css(font, base_font=None, base_color=None, color=None):
    """Return compact Qt-rich-text CSS for a Segment's resolved style."""
    css = []

    def _base_call(attr, default=None):
        if base_font is None:
            return default
        try:
            return getattr(base_font, attr)()
        except Exception:
            return default

    try:
        family = font.family()
        base_family = _base_call("family", "")
        if family and family != base_family:
            # Qt rich text accepts CSS font-family with quoted family names.
            family = family.replace("\\", "\\\\").replace("'", "\\'")
            css.append(f"font-family:'{family}'")
    except Exception:
        record_suppressed_exception()

    try:
        pt = float(font.pointSizeF())
    except Exception:
        pt = -1.0
    try:
        base_pt = float(base_font.pointSizeF()) if base_font is not None else -1.0
    except Exception:
        base_pt = -1.0
    if pt > 0 and (base_pt <= 0 or abs(pt - base_pt) > 0.01):
        css.append(f"font-size:{pt:.3f}pt")

    try:
        bold = bool(font.bold())
        base_bold = bool(_base_call("bold", False))
        if bold != base_bold:
            css.append("font-weight:bold" if bold else "font-weight:normal")
    except Exception:
        record_suppressed_exception()

    try:
        italic = bool(font.italic())
        base_italic = bool(_base_call("italic", False))
        if italic != base_italic:
            css.append("font-style:italic" if italic else "font-style:normal")
    except Exception:
        record_suppressed_exception()

    decorations = []
    try:
        underline = bool(font.underline())
        base_underline = bool(_base_call("underline", False))
        if underline != base_underline and underline:
            decorations.append("underline")
    except Exception:
        record_suppressed_exception()
    try:
        strike = bool(font.strikeOut())
        base_strike = bool(_base_call("strikeOut", False))
        if strike != base_strike and strike:
            decorations.append("line-through")
    except Exception:
        record_suppressed_exception()
    try:
        overline = bool(font.overline())
        base_overline = bool(_base_call("overline", False))
        if overline != base_overline and overline:
            decorations.append("overline")
    except Exception:
        record_suppressed_exception()
    if decorations:
        css.append("text-decoration:" + " ".join(decorations))

    col = _color_css(color, base_color) if color is not None else None
    if col:
        css.append(f"color:{col}")

    return ";".join(css)


def segments_slice_to_html(segments, start, length,
                           base_font=None, base_color=None):
    """Serialize a slice of resolved Segments back to safe inline HTML.

    Polygon text uses QTextLayout to decide line breaks from the parsed rich
    text.  Once it knows the plain-text start/length of a line, this helper
    rebuilds only that line as simple HTML so QgsTextRenderer can draw it with
    the native buffer/background/shadow/effect stack still active.
    """
    try:
        start = max(0, int(start))
        length = max(0, int(length))
    except Exception:
        return ""
    if not segments or length <= 0:
        return ""

    end = start + length
    pos = 0
    parts = []
    for seg in segments:
        text = seg.text or ""
        seg_len = len(text)
        seg_start = pos
        seg_end = pos + seg_len
        pos = seg_end
        if seg_len <= 0 or seg_end <= start or seg_start >= end:
            continue

        local_start = max(0, start - seg_start)
        local_end = min(seg_len, end - seg_start)
        chunk = text[local_start:local_end]
        if not chunk:
            continue

        # A line slice should normally not contain newlines.  If one slips
        # through, keep the HTML valid without asking QgsTextRenderer to draw
        # an extra line outside the polygon span.
        if chunk == "\n":
            continue

        escaped = _html.escape(chunk, quote=False)
        css = _font_css(seg.font, base_font, base_color, seg.color)
        if css:
            parts.append(f'<span style="{css}">{escaped}</span>')
        else:
            parts.append(escaped)

    return "".join(parts)




MM_PER_POINT = 25.4 / 72.0  # 1 typographic point = 1/72 inch = 0.352778 mm


def render_font(font, scale_factor, render_ctx=None, size_unit=None,
                size_map_unit_scale=None, size_value=None):
    """
    Returns a copy of `font` with an explicit *pixel* size.

    When a QgsRenderContext and a text-format size unit are available,
    QGIS' own convertToPainterUnits() is used first.  That keeps the
    custom spline/polygon measurement path in step with QgsTextRenderer
    and the native layout label item.  The point-to-mm calculation is
    retained as a safe fallback for older bindings or unit APIs which
    are not exposed to Python.
    """
    if size_value is None:
        size_value = font.pointSizeF()
    try:
        size_value = float(size_value)
    except Exception:
        size_value = 0.0

    def _prefer_no_hinting(out_font):
        try:
            hint_pref = getattr(QFont, "HintingPreference", None)
            pref = None
            if hint_pref is not None:
                pref = getattr(hint_pref, "PreferNoHinting", None)
            if pref is None:
                pref = getattr(QFont, "PreferNoHinting", None)
            if pref is not None and hasattr(out_font, "setHintingPreference"):
                out_font.setHintingPreference(pref)
            elif pref is not None and hasattr(out_font, "setStyleStrategy"):
                try:
                    out_font.setStyleStrategy(pref)
                except Exception:
                    record_suppressed_exception()
        except Exception:
            record_suppressed_exception()
        return out_font

    def _font_with_fractional_pixel_size(size_px):
        """Create a measurement font without integer pixel-size rounding.

        QFont.setPixelSize accepts integers only.  Rounding at every layout
        zoom changes the font/polygon ratio (most noticeably below 100%).
        A fractional point size, converted using the GUI screen DPI, gives
        QFontMetricsF the requested fractional pixel height instead.
        """
        out = QFont(font)
        dpi = 96.0
        try:
            app = getattr(QtGui, "QGuiApplication", None)
            screen = app.primaryScreen() if app is not None else None
            dpi = float(screen.logicalDotsPerInchY()) if screen is not None else 96.0
            if dpi <= 0:
                dpi = 96.0
            out.setPointSizeF(max(0.01, float(size_px) * 72.0 / dpi))
        except Exception:
            out.setPixelSize(max(1, int(round(float(size_px)))))

        # QFont stores word spacing, and AbsoluteSpacing letter spacing, in
        # device pixels.  Merely changing the point size leaves those values
        # at screen scale while the rest of the font is converted to layout/
        # export painter units.  Scale them by the same font-size ratio so
        # QTextLayout measures exactly the spacing QgsTextRenderer will draw.
        try:
            source_px = float(font.pixelSize())
            if source_px <= 0.0:
                source_px = float(font.pointSizeF()) * dpi / 72.0
            spacing_ratio = float(size_px) / max(source_px, 0.01)
            out.setWordSpacing(float(font.wordSpacing()) * spacing_ratio)
            spacing_type = font.letterSpacingType()
            absolute = getattr(QFont, "AbsoluteSpacing", None)
            if absolute is None:
                scoped = getattr(QFont, "SpacingType", None)
                absolute = getattr(scoped, "AbsoluteSpacing", None)
            if absolute is not None and spacing_type == absolute:
                out.setLetterSpacing(
                    spacing_type,
                    float(font.letterSpacing()) * spacing_ratio)
        except Exception:
            record_suppressed_exception()
        return _prefer_no_hinting(out)

    if render_ctx is not None and size_value > 0 and size_unit is not None:
        converter = getattr(render_ctx, "convertToPainterUnits", None)
        if converter is not None:
            try:
                if size_map_unit_scale is not None:
                    size_px = float(converter(
                        size_value, size_unit, size_map_unit_scale))
                else:
                    size_px = float(converter(size_value, size_unit))
                if size_px > 0:
                    return _font_with_fractional_pixel_size(size_px)
            except TypeError:
                try:
                    size_px = float(converter(size_value, size_unit))
                    if size_px > 0:
                        return _font_with_fractional_pixel_size(size_px)
                except Exception:
                    record_suppressed_exception()
            except Exception:
                record_suppressed_exception()

    size_pt = size_value if size_value > 0 else font.pointSizeF()
    if size_pt <= 0:
        size_pt = 10.0
    size_px = size_pt * MM_PER_POINT * (scale_factor or 1.0)
    return _font_with_fractional_pixel_size(size_px)


import re as _re
import html as _html


def strip_html(text):
    """Strip HTML tags, returning plain text for measurement purposes."""
    return _re.sub(r'<[^>]+>', '', text)


def apply_capitalization(text, cap):
    """Apply a QgsTextFormat capitalization enum to text (for measurement).
    cap is the integer value of Qgis.Capitalization / QgsStringUtils.Capitalization:
    0=MixedCase, 1=AllUppercase, 2=AllLowercase, 3=ForceFirstLetter, 4=SmallCaps,
    5=AllSmallCaps, 6=TitleCase."""
    if not text or cap is None:
        return text
    try:
        v = int(cap)
    except (TypeError, ValueError):
        try:
            v = cap.value
        except Exception:
            return text
    if v == 1 or v == 5:   return text.upper()   # AllUppercase / AllSmallCaps
    if v == 2:              return text.lower()   # AllLowercase
    if v in (3, 6):         return text.title()   # ForceFirstLetter / TitleCase
    return text


def resolve_qgs_halign(align_int):
    """Convert plugin alignment int (0=L, 1=C, 2=R, 3=Justify) to
    QgsTextRenderer.HAlignment, handling both PyQt5 flat and PyQt6 scoped enums.
    Returns None if QgsTextRenderer is unavailable."""
    try:
        from qgis.core import QgsTextRenderer as _TR
        try:                    # PyQt6 scoped
            HA = _TR.HAlignment
            return {0: HA.AlignLeft, 1: HA.AlignCenter,
                    2: HA.AlignRight, 3: HA.AlignJustify}.get(align_int, HA.AlignLeft)
        except AttributeError:  # PyQt5 flat
            just = getattr(_TR, 'AlignJustify', _TR.AlignLeft)
            return {0: _TR.AlignLeft, 1: _TR.AlignCenter,
                    2: _TR.AlignRight, 3: just}.get(align_int, _TR.AlignLeft)
    except Exception:
        return None


# ============================================================ curve geometry
def build_smooth_path(points):
    """
    Builds a smooth QPainterPath through `points` using a uniform
    Catmull-Rom -> cubic Bezier conversion (tangent at each interior
    point = (P[i+1] - P[i-1]) / 6). With 0 or 1 points an (almost)
    empty path is returned; with exactly 2 points a straight line is
    used; 3+ points produce a smooth curve that passes through every
    node, which is what lets the spline text item bend visibly as soon
    as a third node is added.
    """
    path = QPainterPath()
    n = len(points)
    if n == 0:
        return path
    path.moveTo(points[0])
    if n == 1:
        return path
    if n == 2:
        path.lineTo(points[1])
        return path

    padded = [points[0]] + list(points) + [points[-1]]
    for i in range(1, len(padded) - 2):
        p0, p1, p2, p3 = padded[i - 1], padded[i], padded[i + 1], padded[i + 2]
        c1x = p1.x() + (p2.x() - p0.x()) / 6.0
        c1y = p1.y() + (p2.y() - p0.y()) / 6.0
        c2x = p2.x() - (p3.x() - p1.x()) / 6.0
        c2y = p2.y() - (p3.y() - p1.y()) / 6.0
        path.cubicTo(c1x, c1y, c2x, c2y, p2.x(), p2.y())
    return path


class PathLengthMapper:
    """
    Maps "distance travelled along a QPainterPath" to the path's 0..1
    'percent' parameter, using a sampled polyline approximation.

    QPainterPath.pointAtPercent()/angleAtPercent() take a percent value
    that the Qt documentation explicitly warns is *not* proportional to
    arc length for curved (Bezier) sub-paths. Left uncorrected, that
    causes glyphs to bunch together on tight bends and spread apart on
    flat sections. We build a lookup table of
    (cumulative arc length -> percent) by densely sampling the path
    once, then binary-search + linearly interpolate it for every glyph
    placement, which keeps character spacing visually even.
    """

    def __init__(self, path, samples=600):
        self._cum = [0.0]
        self._percents = [0.0]
        self.total_length = 0.0
        if path.elementCount() < 2:
            return
        prev = path.pointAtPercent(0.0)
        for i in range(1, samples + 1):
            t = i / samples
            pt = path.pointAtPercent(t)
            d = math.hypot(pt.x() - prev.x(), pt.y() - prev.y())
            self._cum.append(self._cum[-1] + d)
            self._percents.append(t)
            prev = pt
        self.total_length = self._cum[-1]

    def percent_at_length(self, length):
        if self.total_length <= 0 or length <= 0:
            return 0.0
        if length >= self.total_length:
            return 1.0
        lo, hi = 0, len(self._cum) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self._cum[mid] < length:
                lo = mid + 1
            else:
                hi = mid
        i = max(lo, 1)
        d0, d1 = self._cum[i - 1], self._cum[i]
        p0, p1 = self._percents[i - 1], self._percents[i]
        if d1 <= d0:
            return p1
        frac = (length - d0) / (d1 - d0)
        return p0 + frac * (p1 - p0)


# ============================================================ polygon geometry
def polygon_scanline_spans(points, y):
    """
    Even-odd horizontal ray intersection of a polygon with the
    horizontal line y=const. Returns a (possibly empty) list of
    (x_left, x_right) spans -- the segments of that scanline which lie
    inside the polygon. `points` is a sequence of objects with x()/y().

    This is the standard scan-line polygon-fill algorithm; it is what
    lets the polygon text item discover "how wide can this line of
    text be" at every vertical position, including for concave shapes,
    banners with notches, etc.
    """
    n = len(points)
    xs = []
    for i in range(n):
        p1 = points[i]
        p2 = points[(i + 1) % n]
        y1, y2 = p1.y(), p2.y()
        if y1 == y2:
            continue
        if min(y1, y2) <= y < max(y1, y2):
            t = (y - y1) / (y2 - y1)
            xs.append(p1.x() + t * (p2.x() - p1.x()))
    xs.sort()
    spans = []
    for i in range(0, len(xs) - 1, 2):
        spans.append((xs[i], xs[i + 1]))
    return spans


def widest_span(spans):
    """
    Picks the widest available span on a scanline. For simple convex
    or mildly concave shapes (banners, shields, trapezoids, blobs) this
    is exactly the span you want. For multi-lobed / self-intersecting
    polygons it deliberately keeps things simple by only ever filling
    one column per line rather than wrapping text independently into
    each lobe -- a reasonable, predictable limitation for a layout text
    tool.
    """
    if not spans:
        return None
    return max(spans, key=lambda s: s[1] - s[0])


def point_segment_distance(p, a, b):
    """Shortest distance from point p to the segment a-b (used to find
    which curve/polygon edge a newly inserted node should snap into)."""
    ax, ay, bx, by, px, py = a.x(), a.y(), b.x(), b.y(), p.x(), p.y()
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)
