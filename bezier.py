"""Shared cubic-Bezier geometry helpers for spline and polygon text items.

The editor stores one optional incoming and outgoing control point per anchor.
Control points use the same normalised item coordinate system as anchors.  A
segment with no outgoing handle on its start anchor and no incoming handle on
its end anchor is rendered as a straight line, preserving all v1.0.1 polygon
geometry exactly.
"""
import json
import math

from .compat import QPointF, QPainterPath


def _pt(x, y=None):
    if y is None:
        return QPointF(float(x.x()), float(x.y()))
    return QPointF(float(x), float(y))


def _lerp(a, b, t):
    return QPointF(a.x() + (b.x() - a.x()) * t,
                   a.y() + (b.y() - a.y()) * t)


def _dist(a, b):
    return math.hypot(a.x() - b.x(), a.y() - b.y())


def empty_handles(count):
    return [{"in": None, "out": None, "mode": "corner"}
            for _ in range(max(0, int(count)))]


def clone_handles(handles, count=None):
    source = list(handles or [])
    if count is None:
        count = len(source)
    out = []
    for i in range(count):
        rec = source[i] if i < len(source) and isinstance(source[i], dict) else {}
        out.append({
            "in": _pt(rec["in"]) if rec.get("in") is not None else None,
            "out": _pt(rec["out"]) if rec.get("out") is not None else None,
            "mode": rec.get("mode", "corner") if rec.get("mode", "corner") in
                    ("corner", "smooth", "symmetric") else "corner",
        })
    return out




def straight_handles(points, closed=False):
    """Return control handles which reproduce straight segments exactly.

    Closed paths receive an incoming and outgoing handle at every anchor.
    Open paths intentionally omit the unused incoming handle on the first
    anchor and unused outgoing handle on the last anchor.
    """
    pts = list(points)
    n = len(pts)
    hs = empty_handles(n)
    if n < 2:
        return hs
    for i, anchor in enumerate(pts):
        prev_i = i - 1 if i > 0 else (n - 1 if closed else None)
        next_i = i + 1 if i + 1 < n else (0 if closed else None)
        if prev_i is not None:
            prev = pts[prev_i]
            hs[i]["in"] = QPointF(
                anchor.x() + (prev.x() - anchor.x()) / 3.0,
                anchor.y() + (prev.y() - anchor.y()) / 3.0)
        if next_i is not None:
            nxt = pts[next_i]
            hs[i]["out"] = QPointF(
                anchor.x() + (nxt.x() - anchor.x()) / 3.0,
                anchor.y() + (nxt.y() - anchor.y()) / 3.0)
    return hs


def ensure_closed_handles(points, handles):
    """Fill missing closed-path controls with exact straight-line handles."""
    pts = list(points)
    n = len(pts)
    hs = clone_handles(handles, n)
    if n < 2:
        return hs
    defaults = straight_handles(pts, closed=True)
    for i in range(n):
        if hs[i].get("in") is None:
            hs[i]["in"] = defaults[i]["in"]
        if hs[i].get("out") is None:
            hs[i]["out"] = defaults[i]["out"]
    return hs


def bounded_polygon_handles(points, max_local_fraction=0.28):
    """Return straight-preserving handles with a local length cap.

    Polygon Text uses these defaults so the editable handles stay close to
    each anchor even for very long or highly irregular edges.  The controls
    remain collinear with their corresponding polygon edges, therefore the
    initial cubic segments are still exact straight lines.  The cap is based
    on the shorter of the two edges incident to each anchor, which keeps
    control arms proportional to local geometry rather than the overall
    polygon extent.
    """
    pts = list(points)
    n = len(pts)
    hs = empty_handles(n)
    if n < 2:
        return hs

    fraction = min(max(float(max_local_fraction), 0.05), 0.45)
    for i, anchor in enumerate(pts):
        prev_i = (i - 1) % n
        next_i = (i + 1) % n
        prev = pts[prev_i]
        nxt = pts[next_i]
        prev_len = _dist(anchor, prev)
        next_len = _dist(anchor, nxt)
        local_cap = min(prev_len, next_len) * fraction

        if prev_len > 1.0e-12:
            in_len = min(prev_len / 3.0, local_cap)
            hs[i]["in"] = QPointF(
                anchor.x() + (prev.x() - anchor.x()) / prev_len * in_len,
                anchor.y() + (prev.y() - anchor.y()) / prev_len * in_len)
        if next_len > 1.0e-12:
            out_len = min(next_len / 3.0, local_cap)
            hs[i]["out"] = QPointF(
                anchor.x() + (nxt.x() - anchor.x()) / next_len * out_len,
                anchor.y() + (nxt.y() - anchor.y()) / next_len * out_len)
    return hs


def sanitise_open_handles(handles, count):
    """Remove control points which cannot affect an open cubic path.

    An open path never uses the first anchor's incoming control point or the
    last anchor's outgoing control point. Keeping either around only bloats
    the editable item's scene bounds and exposes misleading handles.
    """
    hs = clone_handles(handles, count)
    if count > 0:
        hs[0]["in"] = None
        hs[-1]["out"] = None
    return hs

def catmull_rom_handles(points):
    """Return handles matching the legacy v1.0.1 Catmull-Rom spline exactly."""
    points = list(points)
    n = len(points)
    handles = empty_handles(n)
    if n < 3:
        return handles
    padded = [points[0]] + points + [points[-1]]
    for i in range(1, len(padded) - 2):
        p0, p1, p2, p3 = padded[i - 1], padded[i], padded[i + 1], padded[i + 2]
        c1 = QPointF(p1.x() + (p2.x() - p0.x()) / 6.0,
                     p1.y() + (p2.y() - p0.y()) / 6.0)
        c2 = QPointF(p2.x() - (p3.x() - p1.x()) / 6.0,
                     p2.y() - (p3.y() - p1.y()) / 6.0)
        start = i - 1
        end = i
        handles[start]["out"] = c1
        handles[end]["in"] = c2
        handles[start]["mode"] = "smooth"
        handles[end]["mode"] = "smooth"
    return handles


def build_bezier_path(points, handles=None, closed=False):
    points = list(points)
    path = QPainterPath()
    if not points:
        return path
    handles = clone_handles(handles, len(points))
    path.moveTo(points[0])
    segment_count = len(points) if closed else len(points) - 1
    for i in range(max(0, segment_count)):
        j = (i + 1) % len(points)
        a, b = points[i], points[j]
        c1 = handles[i].get("out")
        c2 = handles[j].get("in")
        if c1 is None and c2 is None:
            path.lineTo(b)
        else:
            path.cubicTo(c1 if c1 is not None else a,
                         c2 if c2 is not None else b,
                         b)
    if closed:
        path.closeSubpath()
    return path


def _cubic_point(p0, p1, p2, p3, t):
    mt = 1.0 - t
    a = mt * mt * mt
    b = 3.0 * mt * mt * t
    c = 3.0 * mt * t * t
    d = t * t * t
    return QPointF(a * p0.x() + b * p1.x() + c * p2.x() + d * p3.x(),
                   a * p0.y() + b * p1.y() + c * p2.y() + d * p3.y())


def _point_line_distance(p, a, b):
    dx = b.x() - a.x()
    dy = b.y() - a.y()
    denom = dx * dx + dy * dy
    if denom <= 1e-18:
        return _dist(p, a)
    t = ((p.x() - a.x()) * dx + (p.y() - a.y()) * dy) / denom
    proj = QPointF(a.x() + t * dx, a.y() + t * dy)
    return _dist(p, proj)


def _flatten_cubic(p0, p1, p2, p3, tolerance, out, depth=0):
    flatness = max(_point_line_distance(p1, p0, p3),
                   _point_line_distance(p2, p0, p3))
    if flatness <= tolerance or depth >= 12:
        out.append(_pt(p3))
        return
    q0 = _lerp(p0, p1, 0.5)
    q1 = _lerp(p1, p2, 0.5)
    q2 = _lerp(p2, p3, 0.5)
    r0 = _lerp(q0, q1, 0.5)
    r1 = _lerp(q1, q2, 0.5)
    s = _lerp(r0, r1, 0.5)
    _flatten_cubic(p0, q0, r0, s, tolerance, out, depth + 1)
    _flatten_cubic(s, r1, q2, p3, tolerance, out, depth + 1)


def flatten_bezier(points, handles=None, closed=False, tolerance=0.20):
    """Flatten cubic geometry into a dense polyline for polygon wrapping."""
    points = list(points)
    if not points:
        return []
    handles = clone_handles(handles, len(points))
    out = [_pt(points[0])]
    segment_count = len(points) if closed else len(points) - 1
    for i in range(max(0, segment_count)):
        j = (i + 1) % len(points)
        p0, p3 = points[i], points[j]
        p1 = handles[i].get("out") or p0
        p2 = handles[j].get("in") or p3
        if handles[i].get("out") is None and handles[j].get("in") is None:
            out.append(_pt(p3))
        else:
            _flatten_cubic(p0, p1, p2, p3, max(0.01, float(tolerance)), out)
    if closed and len(out) > 1 and _dist(out[0], out[-1]) < 1e-9:
        out.pop()
    return out


def segment_points(points, handles, index, closed=False):
    n = len(points)
    if n < 2:
        return None
    max_segments = n if closed else n - 1
    if index < 0 or index >= max_segments:
        return None
    j = (index + 1) % n
    hs = clone_handles(handles, n)
    p0, p3 = points[index], points[j]
    return (p0, hs[index].get("out") or p0,
            hs[j].get("in") or p3, p3)


def nearest_segment(point, points, handles=None, closed=False, samples=40):
    """Return (segment_index, t, nearest_point, distance)."""
    points = list(points)
    handles = clone_handles(handles, len(points))
    segment_count = len(points) if closed else len(points) - 1
    best = None
    for i in range(max(0, segment_count)):
        seg = segment_points(points, handles, i, closed)
        if seg is None:
            continue
        p0, p1, p2, p3 = seg
        steps = max(8, int(samples))
        for k in range(steps + 1):
            t = k / steps
            q = _cubic_point(p0, p1, p2, p3, t)
            d = _dist(point, q)
            if best is None or d < best[3]:
                best = (i, t, q, d)
    if best is None:
        return (-1, 0.0, _pt(point), float("inf"))
    # Refine around the best sampled t with a few ternary-search rounds.
    i, t0, _q, _d = best
    lo = max(0.0, t0 - 1.0 / max(8, int(samples)))
    hi = min(1.0, t0 + 1.0 / max(8, int(samples)))
    p0, p1, p2, p3 = segment_points(points, handles, i, closed)
    for _ in range(10):
        t1 = lo + (hi - lo) / 3.0
        t2 = hi - (hi - lo) / 3.0
        d1 = _dist(point, _cubic_point(p0, p1, p2, p3, t1))
        d2 = _dist(point, _cubic_point(p0, p1, p2, p3, t2))
        if d1 <= d2:
            hi = t2
        else:
            lo = t1
    t = (lo + hi) * 0.5
    q = _cubic_point(p0, p1, p2, p3, t)
    return (i, t, q, _dist(point, q))



def apply_node_mode(points, handles, index, mode, closed=False):
    """Apply a Bezier anchor mode and, where appropriate, reshape now.

    ``corner`` only changes future handle coupling. ``smooth`` immediately
    makes the two valid handles collinear while preserving their individual
    lengths. ``symmetric`` additionally makes both valid handle lengths equal.
    Open-path endpoints naturally have only one valid handle and therefore
    only record the requested mode.
    """
    pts = [_pt(p) for p in points]
    hs = clone_handles(handles, len(pts))
    if mode not in ("corner", "smooth", "symmetric"):
        return hs, False
    if not (0 <= index < len(pts)):
        return hs, False

    rec = hs[index]
    rec["mode"] = mode
    if mode == "corner":
        return hs, True

    n = len(pts)
    anchor = pts[index]
    in_valid = closed or index > 0
    out_valid = closed or index < n - 1

    if closed:
        # Closed polygons are cyclic: node 0 is not an open-path endpoint.
        # Build the node tangent from its actual cyclic neighbours so the mode
        # is deterministic and independent of which node happens to be stored
        # first in the XML/list.  This prevents the first node from collapsing
        # or acquiring start/end-specific behaviour.
        prev_i = (index - 1) % n
        next_i = (index + 1) % n
        prev = pts[prev_i]
        nxt = pts[next_i]
        vin_x = anchor.x() - prev.x()
        vin_y = anchor.y() - prev.y()
        vout_x = nxt.x() - anchor.x()
        vout_y = nxt.y() - anchor.y()
        prev_len = math.hypot(vin_x, vin_y)
        next_len = math.hypot(vout_x, vout_y)

        # Existing handle lengths are retained where they are meaningful.
        # If a handle has collapsed, derive a stable local length from the
        # corresponding adjacent anchor segment instead of using zero.
        old_in = rec.get("in")
        old_out = rec.get("out")
        lin = (math.hypot(anchor.x() - old_in.x(), anchor.y() - old_in.y())
               if old_in is not None else 0.0)
        lout = (math.hypot(old_out.x() - anchor.x(), old_out.y() - anchor.y())
                if old_out is not None else 0.0)
        if lin <= 1.0e-12:
            lin = prev_len / 3.0 if prev_len > 1.0e-12 else 0.0
        if lout <= 1.0e-12:
            lout = next_len / 3.0 if next_len > 1.0e-12 else 0.0

        # A smooth closed-path node owns one local tangent axis. Derive it
        # from the unit incoming and outgoing path directions so the result
        # follows the local corner consistently at every node, including the
        # first and last stored nodes. The calculation is cyclic and does not
        # depend on which anchor happened to be stored at index 0.
        dx = 0.0
        dy = 0.0
        if prev_len > 1.0e-12:
            dx += vin_x / prev_len
            dy += vin_y / prev_len
        if next_len > 1.0e-12:
            dx += vout_x / next_len
            dy += vout_y / next_len

        tangent_len = math.hypot(dx, dy)
        if tangent_len <= 1.0e-12:
            # A 180-degree reversal has no unique averaged tangent. Use the
            # outgoing edge, then the incoming edge, as a deterministic local
            # fallback without consulting any other node's handles.
            if next_len > 1.0e-12:
                dx, dy = vout_x / next_len, vout_y / next_len
                tangent_len = 1.0
            elif prev_len > 1.0e-12:
                dx, dy = vin_x / prev_len, vin_y / prev_len
                tangent_len = 1.0
            else:
                return hs, True
        else:
            dx /= tangent_len
            dy /= tangent_len

        if mode == "symmetric":
            lengths = [length for length in (lin, lout) if length > 1.0e-12]
            shared = sum(lengths) / len(lengths) if lengths else 0.0
            lin = lout = shared

        rec["in"] = QPointF(anchor.x() - dx * lin, anchor.y() - dy * lin)
        rec["out"] = QPointF(anchor.x() + dx * lout, anchor.y() + dy * lout)
        return hs, True

    # Open paths retain endpoint semantics: first/last anchors have only one
    # valid control handle and therefore cannot be coupled to an opposite one.
    if in_valid and rec.get("in") is None:
        prev_i = index - 1
        prev = pts[prev_i]
        rec["in"] = _lerp(anchor, prev, 1.0 / 3.0)
    if out_valid and rec.get("out") is None:
        next_i = index + 1
        nxt = pts[next_i]
        rec["out"] = _lerp(anchor, nxt, 1.0 / 3.0)

    hin = rec.get("in") if in_valid else None
    hout = rec.get("out") if out_valid else None
    if hin is None or hout is None:
        return hs, True

    vin_x = anchor.x() - hin.x()
    vin_y = anchor.y() - hin.y()
    vout_x = hout.x() - anchor.x()
    vout_y = hout.y() - anchor.y()
    lin = math.hypot(vin_x, vin_y)
    lout = math.hypot(vout_x, vout_y)
    dirs = []
    if lin > 1.0e-12:
        dirs.append((vin_x / lin, vin_y / lin))
    if lout > 1.0e-12:
        dirs.append((vout_x / lout, vout_y / lout))
    if not dirs:
        return hs, True
    dx = sum(v[0] for v in dirs)
    dy = sum(v[1] for v in dirs)
    dlen = math.hypot(dx, dy)
    if dlen <= 1.0e-12:
        dx, dy = dirs[-1]
        dlen = 1.0
    dx /= dlen
    dy /= dlen
    if mode == "symmetric":
        lengths = [length for length in (lin, lout) if length > 1.0e-12]
        shared = sum(lengths) / len(lengths) if lengths else 0.0
        lin = lout = shared
    rec["in"] = QPointF(anchor.x() - dx * lin, anchor.y() - dy * lin)
    rec["out"] = QPointF(anchor.x() + dx * lout, anchor.y() + dy * lout)
    return hs, True

def convert_segment_to_curve(points, handles, index, closed=False):
    hs = clone_handles(handles, len(points))
    n = len(points)
    if n < 2 or index < 0 or index >= n or (not closed and index >= n - 1):
        return hs
    j = (index + 1) % n
    a, b = points[index], points[j]
    if hs[index].get("out") is None:
        hs[index]["out"] = _lerp(a, b, 1.0 / 3.0)
    if hs[j].get("in") is None:
        hs[j]["in"] = _lerp(a, b, 2.0 / 3.0)
    return hs


def convert_segment_to_straight(handles, index, count, closed=False):
    hs = clone_handles(handles, count)
    if (count < 2 or index < 0 or index >= count
            or (not closed and index >= count - 1)):
        return hs
    j = (index + 1) % count
    hs[index]["out"] = None
    hs[j]["in"] = None
    return hs


def split_segment(points, handles, index, t, closed=False):
    """Insert an anchor by exact de Casteljau subdivision."""
    pts = [_pt(p) for p in points]
    hs = clone_handles(handles, len(pts))
    seg = segment_points(pts, hs, index, closed)
    if seg is None:
        return pts, hs, -1
    p0, p1, p2, p3 = seg
    t = min(max(float(t), 0.001), 0.999)
    was_curve = hs[index].get("out") is not None or hs[(index + 1) % len(pts)].get("in") is not None
    q0 = _lerp(p0, p1, t)
    q1 = _lerp(p1, p2, t)
    q2 = _lerp(p2, p3, t)
    r0 = _lerp(q0, q1, t)
    r1 = _lerp(q1, q2, t)
    s = _lerp(r0, r1, t)
    j = (index + 1) % len(pts)
    new_rec = {"in": r0 if was_curve else None,
               "out": r1 if was_curve else None,
               "mode": "smooth" if was_curve else "corner"}
    if was_curve:
        hs[index]["out"] = q0
        hs[j]["in"] = q2
    if closed and j == 0:
        pts.append(s)
        hs.append(new_rec)
        new_index = len(pts) - 1
    else:
        pts.insert(j, s)
        hs.insert(j, new_rec)
        new_index = j
    return pts, hs, new_index


def reverse_geometry(points, handles):
    pts = [_pt(p) for p in reversed(points)]
    old = clone_handles(handles, len(points))
    hs = []
    for rec in reversed(old):
        hs.append({"in": _pt(rec["out"]) if rec.get("out") is not None else None,
                   "out": _pt(rec["in"]) if rec.get("in") is not None else None,
                   "mode": rec.get("mode", "corner")})
    return pts, hs


def serialise_handles(handles):
    payload = []
    for rec in clone_handles(handles):
        payload.append({
            "i": [rec["in"].x(), rec["in"].y()] if rec.get("in") is not None else None,
            "o": [rec["out"].x(), rec["out"].y()] if rec.get("out") is not None else None,
            "m": rec.get("mode", "corner"),
        })
    return json.dumps(payload, separators=(",", ":"))


def deserialise_handles(text, count):
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list):
        return None
    hs = empty_handles(count)
    for i, entry in enumerate(payload[:count]):
        if not isinstance(entry, dict):
            continue
        for key, source_key in (("in", "i"), ("out", "o")):
            value = entry.get(source_key)
            if isinstance(value, (list, tuple)) and len(value) == 2:
                try:
                    hs[i][key] = QPointF(float(value[0]), float(value[1]))
                except (TypeError, ValueError):
                    hs[i][key] = None
        mode = entry.get("m", "corner")
        hs[i]["mode"] = mode if mode in ("corner", "smooth", "symmetric") else "corner"
    return hs


def handle_scene_records(item, points, handles):
    """Return (node_index, kind, scene_point) for all non-null handles."""
    rect = item.rect()
    hs = clone_handles(handles, len(points))
    records = []
    for i, rec in enumerate(hs):
        for kind in ("in", "out"):
            p = rec.get(kind)
            if p is None:
                continue
            local = QPointF(p.x() * rect.width(), p.y() * rect.height())
            records.append((i, kind, item.mapToScene(local)))
    return records
