"""
compat.py

Cross-binding / cross-Qt-major-version compatibility helpers.

QGIS 3.44.x ships with PyQt5 (Qt5, "flat" enums, e.g. ``Qt.AlignCenter``).
QGIS 4.0.x ships with PyQt6 (Qt6, "scoped" enums, e.g.
``Qt.AlignmentFlag.AlignCenter``). QGIS itself re-exports whichever
binding it was built against through ``qgis.PyQt`` -- that is the
officially recommended import path for plugins and is guaranteed to
exist inside any running QGIS process, so we use it as our primary
source of Qt classes. On top of that we add a thin layer to resolve the
enum-scoping difference between Qt5 and Qt6, since that is the change
that actually breaks plugin code across the 3.x -> 4.x jump (see the
QGIS "pyqgis4-checker" migration tool, which exists specifically to
rewrite flat PyQt5 enum access into scoped PyQt6 form).

Every other module in this plugin imports Qt symbols from here instead
of importing PyQt5/PyQt6 directly, so this is the *only* file that
needs to know which binding is actually in use.
"""

# --- Step 1: use the Qt binding supplied by QGIS ----------------------
from qgis.PyQt import QtCore, QtGui, QtWidgets

try:
    from qgis.PyQt import QtXml
except ImportError:  # pragma: no cover - QtXml is optional in some builds
    QtXml = None

_BINDING_SOURCE = "qgis.PyQt"

Qt = QtCore.Qt

# Frequently used classes, re-exported so callers never import a binding
# module directly.
QPointF = QtCore.QPointF
QRectF = QtCore.QRectF
QSizeF = QtCore.QSizeF
QObject = QtCore.QObject

QPainter = QtGui.QPainter
QPainterPath = QtGui.QPainterPath
QColor = QtGui.QColor
QFont = QtGui.QFont
QFontMetricsF = QtGui.QFontMetricsF
QPolygonF = QtGui.QPolygonF
QPen = QtGui.QPen
QBrush = QtGui.QBrush
QIcon = QtGui.QIcon
QPixmap = QtGui.QPixmap
QTextDocument = QtGui.QTextDocument
QTextLayout = QtGui.QTextLayout
QTextOption = QtGui.QTextOption
QTextCharFormat = QtGui.QTextCharFormat


def _resolve_enum(flat_owner, flat_name, scoped_owner, scoped_enum_name, scoped_name):
    """
    Resolve an enum value that may be flat (Qt5: ``Qt.AlignLeft``) or
    scoped (Qt6: ``Qt.AlignmentFlag.AlignLeft``).

    Tries the flat attribute first; if that doesn't exist (or, in some
    PyQt6 builds, resolves to the enum *class* itself rather than a
    value) it falls back to walking the scoped path.
    """
    val = getattr(flat_owner, flat_name, None)
    if val is not None and not isinstance(val, type):
        return val
    enum_cls = getattr(scoped_owner, scoped_enum_name)
    return getattr(enum_cls, scoped_name)


IS_QT6 = _BINDING_SOURCE in ("PyQt6", "PySide6") or not hasattr(Qt, "AlignCenter")

# ---- Alignment ---------------------------------------------------------
ALIGN_LEFT = _resolve_enum(Qt, "AlignLeft", Qt, "AlignmentFlag", "AlignLeft")
ALIGN_RIGHT = _resolve_enum(Qt, "AlignRight", Qt, "AlignmentFlag", "AlignRight")
ALIGN_HCENTER = _resolve_enum(Qt, "AlignHCenter", Qt, "AlignmentFlag", "AlignHCenter")
ALIGN_VCENTER = _resolve_enum(Qt, "AlignVCenter", Qt, "AlignmentFlag", "AlignVCenter")
ALIGN_TOP = _resolve_enum(Qt, "AlignTop", Qt, "AlignmentFlag", "AlignTop")
ALIGN_BOTTOM = _resolve_enum(Qt, "AlignBottom", Qt, "AlignmentFlag", "AlignBottom")

# ---- Painter render hints ------------------------------------------------
AA_ANTIALIASING = _resolve_enum(QPainter, "Antialiasing", QPainter, "RenderHint", "Antialiasing")
AA_TEXT_ANTIALIASING = _resolve_enum(QPainter, "TextAntialiasing", QPainter, "RenderHint", "TextAntialiasing")
AA_SMOOTH_PIXMAP = _resolve_enum(QPainter, "SmoothPixmapTransform", QPainter, "RenderHint", "SmoothPixmapTransform")

# ---- Mouse buttons ---------------------------------------------------------
LEFT_BUTTON = _resolve_enum(Qt, "LeftButton", Qt, "MouseButton", "LeftButton")
RIGHT_BUTTON = _resolve_enum(Qt, "RightButton", Qt, "MouseButton", "RightButton")
MIDDLE_BUTTON = _resolve_enum(Qt, "MiddleButton", Qt, "MouseButton", "MiddleButton")

# ---- Pen / brush ------------------------------------------------------------
SOLID_LINE = _resolve_enum(Qt, "SolidLine", Qt, "PenStyle", "SolidLine")
DASH_LINE = _resolve_enum(Qt, "DashLine", Qt, "PenStyle", "DashLine")
NO_PEN = _resolve_enum(Qt, "NoPen", Qt, "PenStyle", "NoPen")
NO_BRUSH = _resolve_enum(Qt, "NoBrush", Qt, "BrushStyle", "NoBrush")
SOLID_PATTERN = _resolve_enum(Qt, "SolidPattern", Qt, "BrushStyle", "SolidPattern")

# ---- Cursors -----------------------------------------------------------------
CURSOR_CROSS = _resolve_enum(Qt, "CrossCursor", Qt, "CursorShape", "CrossCursor")
CURSOR_ARROW = _resolve_enum(Qt, "ArrowCursor", Qt, "CursorShape", "ArrowCursor")
CURSOR_CLOSED_HAND = _resolve_enum(
    Qt, "ClosedHandCursor", Qt, "CursorShape", "ClosedHandCursor")

# ---- Keyboard ------------------------------------------------------------
KEY_RETURN = _resolve_enum(Qt, "Key_Return", Qt, "Key", "Key_Return")
KEY_ENTER = _resolve_enum(Qt, "Key_Enter", Qt, "Key", "Key_Enter")
KEY_ESCAPE = _resolve_enum(Qt, "Key_Escape", Qt, "Key", "Key_Escape")

# ---- Text option word wrap ----------------------------------------------------
WORD_WRAP = _resolve_enum(QTextOption, "WordWrap", QTextOption, "WrapMode", "WordWrap")
