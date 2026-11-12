"""
core/orb_layout.py — pure geometry for the floating orb and its panels.
Everything here is physical pixels; rects are (l, t, r, b), the orb is a
square (x, y, side). No Win32, no threads: core/floating_orb.py and
core/orb_panels.py apply the results.
"""


def resize_about_center(x, y, side, new_side):
    """(x, y, new_side) keeping the square's center where it was."""
    cx, cy = x + side / 2.0, y + side / 2.0
    new_side = max(1, int(round(new_side)))
    return int(round(cx - new_side / 2.0)), int(round(cy - new_side / 2.0)), new_side


def frames(start, end, n=10, bow=0.0):
    """``n`` squares (x, y, side) from ``start`` to ``end`` (both included as
    the last frame; the first frame is one step in). With ``bow`` > 0 the
    path is a quadratic arc whose control point sits ``bow`` x the distance
    ABOVE the midpoint (an upward bow, the screen's y grows down); 0 is a
    straight line. Sides and positions ease in and out (smoothstep)."""
    n = max(1, int(n))
    (x0, y0, s0), (x1, y1, s1) = start, end
    dist = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0 - bow * dist
    out = []
    for i in range(1, n + 1):
        t = i / n
        t = t * t * (3 - 2 * t)
        u = 1 - t
        x = u * u * x0 + 2 * u * t * cx + t * t * x1
        y = u * u * y0 + 2 * u * t * cy + t * t * y1
        out.append((int(round(x)), int(round(y)), max(1, int(round(s0 + (s1 - s0) * t)))))
    return out


def clamp_square(x, y, side, work):
    """Move the square fully inside ``work`` (it is shrunk only if it cannot fit)."""
    l, t, r, b = (int(v) for v in work)
    side = max(1, min(int(side), max(1, r - l), max(1, b - t)))
    return min(max(int(x), l), r - side), min(max(int(y), t), b - side), side


def _clip(rect, work):
    l, t, r, b = rect
    wl, wt, wr, wb = work
    return max(l, wl), max(t, wt), min(r, wr), min(b, wb)


def _clamp(v, lo, hi):
    return max(lo, min(v, hi)) if hi >= lo else lo


def dock_side(target_rect, work, side, gap=0):
    """Which side of the target the orb docks on: "right", "left", "above",
    "below" (the first with room for ``side`` + 2*``gap``, in that order), or
    "edge" when none fits."""
    wl, wt, wr, wb = (int(v) for v in work)
    l, t, r, b = _clip([int(v) for v in target_rect], (wl, wt, wr, wb))
    need = side + 2 * gap
    room = {"right": wr - r, "left": l - wl, "above": t - wt, "below": wb - b}
    for name in ("right", "left", "above", "below"):
        if room[name] >= need:
            return name
    return "edge"


def dock_position(target_rect, work, side, gap=8):
    """(x, y) for the docked orb (a square of ``side``): beside the target's
    top on its right, else its left, else above or below near its right
    end; never over the target when a side has room, always fully inside
    ``work``. When no side has room (the target fills the screen): the work
    area's top-right corner, against the screen edge."""
    wl, wt, wr, wb = (int(v) for v in work)
    side = max(1, min(int(side), wr - wl, wb - wt))
    l, t, r, b = _clip([int(v) for v in target_rect], (wl, wt, wr, wb))
    where = dock_side(target_rect, work, side, gap)
    y_beside = _clamp(t + gap, wt, wb - side)
    x_near_right = _clamp(r - side - gap, wl, wr - side)
    if where == "right":
        x, y = r + gap, y_beside
    elif where == "left":
        x, y = l - gap - side, y_beside
    elif where == "above":
        x, y = x_near_right, t - gap - side
    elif where == "below":
        x, y = x_near_right, b + gap
    else:
        x, y = wr - side - gap, wt + gap
    return _clamp(x, wl, wr - side), _clamp(y, wt, wb - side)


def overlaps(a, b):
    """Do rects a and b (l, t, r, b; right/bottom exclusive) share a pixel?"""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def panel_stack(orb, sizes, work, target_rect=None, gap=8):
    """(x, y) for each panel (sizes: [(w, h), ...], top to bottom) stacked
    beside the orb square ``orb`` (x, y, side) on the side AWAY from the
    target (right of the orb when the orb is right of the target's center,
    else left); with no target, toward the middle of the work area. A side
    without room flips; the stack starts level with the orb's top and stays
    inside ``work``."""
    wl, wt, wr, wb = (int(v) for v in work)
    ox, oy, side = orb
    ocx = ox + side / 2.0
    if target_rect is not None:
        right = ocx >= (target_rect[0] + target_rect[2]) / 2.0
    else:
        right = ocx < (wl + wr) / 2.0
    total = sum(h for _w, h in sizes) + gap * max(0, len(sizes) - 1)
    y = _clamp(oy, wt, wb - total)
    out = []
    for w, h in sizes:
        x = ox + side + gap if right else ox - gap - w
        if (right and x + w > wr) or (not right and x < wl):
            x = ox - gap - w if right else ox + side + gap
        out.append((_clamp(x, wl, wr - w), _clamp(y, wt, wb - h)))
        y += h + gap
    return out
