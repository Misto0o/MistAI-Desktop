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


PANEL_INSET = 16      # logical px from the work area's edges
PANEL_GAP = 12        # logical px between stacked panels
PANEL_ORDER = ("top-right", "top-left", "top-center")


def _panel_column(sizes, work, anchor, inset, gap, y0=None):
    wl, wt, wr, wb = (int(v) for v in work)
    y = wt + inset if y0 is None else y0
    out = []
    for w, h in sizes:
        if anchor == "top-left":
            x = wl + inset
        elif anchor == "top-center":
            x = (wl + wr) // 2 - w // 2
        else:
            x = wr - inset - w
        out.append((_clamp(x, wl, wr - w), _clamp(y, wt, wb - h)))
        y += h + gap
    return out


def _hits(spots, sizes, rect):
    return rect is not None and any(overlaps((x, y, x + w, y + h), rect)
                                    for (x, y), (w, h) in zip(spots, sizes))


def anchored_panels(sizes, work, anchor="top-right", scale=1.0, target_rect=None,
                    maximized=False, orb=None):
    """((x, y) per panel, anchor used): panels stacked downward from the top
    of ``work`` (the target's monitor), inset PANEL_INSET and PANEL_GAP apart
    (x ``scale``), at ``anchor``. Independent of the orb, except that they
    never cover it (the stack moves below the orb, else to another anchor).
    A non-maximized target covering the stack moves it to the first anchor of
    PANEL_ORDER that is clear of it; a maximized one keeps ``anchor``."""
    inset, gap = round(PANEL_INSET * scale), round(PANEL_GAP * scale)
    order = [anchor] + [a for a in PANEL_ORDER if a != anchor]
    choice = anchor
    if target_rect is not None and not maximized:
        choice = next((a for a in order
                       if not _hits(_panel_column(sizes, work, a, inset, gap), sizes,
                                    target_rect)), anchor)
    orb_rect = (orb[0], orb[1], orb[0] + orb[2], orb[1] + orb[2]) if orb else None
    spots = _panel_column(sizes, work, choice, inset, gap)
    if not _hits(spots, sizes, orb_rect):
        return spots, choice
    below = _panel_column(sizes, work, choice, inset, gap, y0=orb_rect[3] + gap)
    if not _hits(below, sizes, orb_rect):
        return below, choice
    for a in order:
        spots = _panel_column(sizes, work, a, inset, gap)
        if not _hits(spots, sizes, orb_rect):
            return spots, a
    return spots, choice


# -- time-based flights (fix: smooth movement) ------------------------------------------

def square_at(start, end, t, bow=0.0):
    """The square at progress ``t`` (0..1, eased with smoothstep) on the
    path from ``start`` to ``end``: a quadratic arc bowed upward by ``bow``
    x the distance (0 = straight); the side is interpolated."""
    t = min(1.0, max(0.0, float(t)))
    e = t * t * (3 - 2 * t)
    (x0, y0, s0), (x1, y1, s1) = start, end
    dist = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0 - bow * dist
    u = 1 - e
    x = u * u * x0 + 2 * u * e * cx + e * e * x1
    y = u * u * y0 + 2 * u * e * cy + e * e * y1
    return int(round(x)), int(round(y)), max(1, int(round(s0 + (s1 - s0) * e)))


RESIZE_S = 0.12
FLIGHT_S = 0.5
NUDGE_S = 0.2


def flight_plan(start, goal):
    """Segments [(from, to, seconds, bow), ...] from ``start`` to ``goal``.
    A short move or a pure resize is one straight segment. A flight keeps
    the size fixed while it moves (so the window is only moved, never
    resized, mid-flight): shrinking happens before take-off, growing after
    landing, each as a short resize about the center."""
    (x0, y0, s0), (x1, y1, s1) = start, goal
    far = abs((x1 + s1 / 2) - (x0 + s0 / 2)) + abs((y1 + s1 / 2) - (y0 + s0 / 2)) > s0
    if not far:
        return [(start, goal, NUDGE_S, 0.0)]
    if s1 < s0:                                   # shrink first, then fly small
        shrunk = resize_about_center(x0, y0, s0, s1)
        segs = [(start, shrunk, RESIZE_S, 0.0)] if shrunk != start else []
        return segs + [(shrunk, goal, FLIGHT_S, 0.3)]
    # fly at the current size to where the goal's center is, then grow
    landing = resize_about_center(x1, y1, s1, s0)
    segs = [(start, landing, FLIGHT_S, 0.3)]
    return segs + ([(landing, goal, RESIZE_S, 0.0)] if landing != goal else [])


# -- maximized targets (fix: follow the element, not the screen corner) -------------------

def coverage(rect, work):
    """Fraction of ``work`` covered by ``rect`` (0..1)."""
    l, t, r, b = _clip([int(v) for v in rect], [int(v) for v in work])
    area = max(1, (work[2] - work[0]) * (work[3] - work[1]))
    return max(0, r - l) * max(0, b - t) / area


def dock_inside(window_rect, work, side, element=None, near=None, gap=8):
    """(x, y) for a small orb INSIDE a (maximized) window. With ``element``
    (the rect the current step touches): beside it on the emptier side
    (right/left by the room inside the window, then below/above), not over
    it when a side fits. Without one: at the MIDDLE of the window's inner
    edge nearest ``near`` (the orb's current center), never in a corner.
    Always inside both the window and the work area."""
    area = _clip([int(v) for v in window_rect], [int(v) for v in work])
    al, at, ar, ab = area
    if ar - al < 1 or ab - at < 1:
        al, at, ar, ab = (int(v) for v in work)
    side = max(1, min(int(side), ar - al, ab - at))
    if element is not None:
        el, et, er, eb = _clip([int(v) for v in element], (al, at, ar, ab))
        need = side + 2 * gap
        room = {"right": ar - er, "left": el - al, "below": ab - eb, "above": et - at}
        y_mid = _clamp((et + eb) // 2 - side // 2, at, ab - side)
        x_mid = _clamp((el + er) // 2 - side // 2, al, ar - side)
        horiz = max(("right", "left"), key=lambda k: room[k])
        vert = max(("below", "above"), key=lambda k: room[k])
        for where in (horiz, vert):
            if room[where] >= need:
                x, y = {"right": (er + gap, y_mid), "left": (el - gap - side, y_mid),
                        "below": (x_mid, eb + gap), "above": (x_mid, et - gap - side)}[where]
                return _clamp(x, al, ar - side), _clamp(y, at, ab - side)
        x, y = er + gap, y_mid                     # no room anywhere: as close as fits
        return _clamp(x, al, ar - side), _clamp(y, at, ab - side)
    cx, cy = near if near is not None else ((al + ar) / 2.0, (at + ab) / 2.0)
    dists = {"left": cx - al, "right": ar - cx, "top": cy - at, "bottom": ab - cy}
    edge = min(dists, key=dists.get)
    x = _clamp(int((al + ar) / 2 - side / 2), al + gap, ar - side - gap)
    y = _clamp(int((at + ab) / 2 - side / 2), at + gap, ab - side - gap)
    if edge == "left":
        x = al + gap
    elif edge == "right":
        x = ar - side - gap
    elif edge == "top":
        y = at + gap
    else:
        y = ab - side - gap
    return _clamp(x, al, ar - side), _clamp(y, at, ab - side)


# -- browser page rects and the search tour ---------------------------------------------------

def map_page_rects(info, window_rect):
    """Physical screen rects for the viewport rects in ``info`` (from
    BrowserSession.link_boxes: CSS px plus the window's screenX/Y, outer and
    inner sizes and devicePixelRatio, which in Firefox already includes the
    Windows display scale). Content origin = screenX + side border
    ((outerWidth - innerWidth) / 2), screenY + toolbar height (outerHeight -
    innerHeight). Rects below the fold are dropped; None when a remaining
    rect is empty or not inside ``window_rect`` (the mapping can't be trusted)."""
    try:
        dpr = float(info["dpr"]) or 1.0
        ox = float(info["screenX"]) + (float(info["outerWidth"]) - float(info["innerWidth"])) / 2
        oy = float(info["screenY"]) + float(info["outerHeight"]) - float(info["innerHeight"])
        inner_h = float(info["innerHeight"])
        out = []
        for left, top, right, bottom in info["rects"]:
            if bottom > inner_h or top < 0:
                continue                                  # not on screen now
            r = (round((ox + left) * dpr), round((oy + top) * dpr),
                 round((ox + right) * dpr), round((oy + bottom) * dpr))
            wl, wt, wr, wb = window_rect
            if r[2] - r[0] < 2 or r[3] - r[1] < 2 or not (
                    wl <= r[0] and r[2] <= wr and wt <= r[1] and r[3] <= wb):
                return None
            out.append(r)
        return out or None
    except Exception:
        return None


def margin_spot(element, area, side, gap=8):
    """(x, y) for a small orb beside ``element`` inside ``area`` (the page):
    in the right margin when it has room, else the left, vertically centred
    on the element; kept inside ``area``."""
    el, et, er, eb = element
    al, at, ar, ab = area
    x = er + gap if ar - er >= side + 2 * gap else el - gap - side
    y = (et + eb) // 2 - side // 2
    return _clamp(x, al, ar - side), _clamp(y, at, ab - side)


def tour_plan(start, goal, medium, hop_s=0.35):
    """Segments for one tour hop: grow to ``medium`` in place, fly (a slight
    bow) to ``goal``'s center, shrink to ``goal``'s side there."""
    x0, y0, s0 = start
    x1, y1, s1 = goal
    big = resize_about_center(x0, y0, s0, medium)
    land = resize_about_center(x1, y1, s1, medium)
    segs = [] if big == start else [(start, big, RESIZE_S, 0.0)]
    segs.append((big, land, hop_s, 0.15))
    return segs + ([(land, goal, RESIZE_S, 0.0)] if land != goal else [])


def answer_spot(panel, side, work, gap=12):
    """(x, y) for the orb beside the answer panel: to its left, vertically
    centred, else to its right when the left has no room; inside ``work``."""
    pl, pt, pr, pb = panel
    wl, wt, wr, wb = (int(v) for v in work)
    x = pl - gap - side if pl - gap - side >= wl else pr + gap
    y = (pt + pb) // 2 - side // 2
    return _clamp(x, wl, wr - side), _clamp(y, wt, wb - side)
