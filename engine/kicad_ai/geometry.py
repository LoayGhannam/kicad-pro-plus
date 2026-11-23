"""Coordinate helpers. Library coords are Y-up; schematic coords are Y-down (mm)."""
import math

GRID = 1.27


def snap(v, g=GRID):
    return round(round(v / g) * g, 4)


def rnd(v):
    return round(v + 0.0, 4)


def xform(px, py, rot=0, mirror=None):
    """Library point -> offset in schematic coords for a symbol placed with rot/mirror.
    Matches KiCad's convention (verified against kicad-cli netlists)."""
    y = -py
    x = px
    if mirror == "x":
        y = -y
    elif mirror == "y":
        x = -x
    r = math.radians(rot)
    c, s = round(math.cos(r)), round(math.sin(r))
    return rnd(x * c + y * s), rnd(-x * s + y * c)


def xdir(angle_deg, rot=0, mirror=None):
    """Direction a lib pin points *into the body* -> outward unit vector in sch coords."""
    a = math.radians(angle_deg)
    dx, dy = round(math.cos(a)), round(math.sin(a))
    ix, iy = xform(dx, dy, rot, mirror)
    return (-ix, -iy)


def bbox_xform(b, rot=0, mirror=None):
    pts = [xform(b[0], b[1], rot, mirror), xform(b[2], b[3], rot, mirror),
           xform(b[0], b[3], rot, mirror), xform(b[2], b[1], rot, mirror)]
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_move(b, x, y):
    return (rnd(b[0] + x), rnd(b[1] + y), rnd(b[2] + x), rnd(b[3] + y))


def bbox_inflate(b, d):
    return (b[0] - d, b[1] - d, b[2] + d, b[3] + d)


def bbox_overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def seg_hits_box(p, q, b):
    """Axis-aligned segment vs open box."""
    x0, y0, x1, y1 = b
    if p[0] == q[0]:
        x = p[0]
        if not (x0 < x < x1):
            return False
        lo, hi = sorted((p[1], q[1]))
        return lo < y1 and hi > y0
    if p[1] == q[1]:
        y = p[1]
        if not (y0 < y < y1):
            return False
        lo, hi = sorted((p[0], q[0]))
        return lo < x1 and hi > x0
    raise ValueError("non-orthogonal segment")


def on_segment(pt, p, q, strict=True):
    """Is pt on segment pq (strictly inside if strict)."""
    x, y = pt
    if p[0] == q[0] == x:
        lo, hi = sorted((p[1], q[1]))
        return lo < y < hi if strict else lo <= y <= hi
    if p[1] == q[1] == y:
        lo, hi = sorted((p[0], q[0]))
        return lo < x < hi if strict else lo <= x <= hi
    return False


def collinear_overlap(a, b):
    (p, q), (r, s) = a, b
    if p[0] == q[0] == r[0] == s[0]:
        lo1, hi1 = sorted((p[1], q[1])); lo2, hi2 = sorted((r[1], s[1]))
        return min(hi1, hi2) - max(lo1, lo2) > 1e-6
    if p[1] == q[1] == r[1] == s[1]:
        lo1, hi1 = sorted((p[0], q[0])); lo2, hi2 = sorted((r[0], s[0]))
        return min(hi1, hi2) - max(lo1, lo2) > 1e-6
    return False


def seg_cross(a, b):
    """Perpendicular segments crossing (returns point or None)."""
    (p, q), (r, s) = a, b
    if p[0] == q[0] and r[1] == s[1]:
        v, h = (p, q), (r, s)
    elif p[1] == q[1] and r[0] == s[0]:
        v, h = (r, s), (p, q)
    else:
        return None
    x = v[0][0]; y = h[0][1]
    if min(h[0][0], h[1][0]) <= x <= max(h[0][0], h[1][0]) and min(v[0][1], v[1][1]) <= y <= max(v[0][1], v[1][1]):
        return (x, y)
    return None
