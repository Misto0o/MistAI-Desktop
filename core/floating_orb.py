# ============================================================
# FLOATING ORB MODULE
# ============================================================
#
# The optional floating orb and everything it needs: pure layout geometry
# (docking, tours, panel anchors), spring motion and frame pacing, the orb
# window itself, and the task panels beside it.
#
# SECTION INDEX
#   1. ORB LAYOUT GEOMETRY
#   2. RESIZE ABOUT THE CENTER
#   3. STRAIGHT-LINE FLIGHT FRAMES
#   4. CLAMPING SQUARES TO THE WORK AREA
#   5. DOCKING SIDE CHOICE
#   6. DOCKING POSITION BESIDE A WINDOW
#   7. RECT OVERLAP AND PANEL SPACING
#   8. PANEL COLUMN LAYOUT
#   9. PANEL COLLISION TEST
#   10. PANEL ANCHORING
#   11. EASED SQUARE ALONG A FLIGHT
#   12. FLIGHT TIMINGS
#   13. FLIGHT PLANNING
#   14. WORK AREA COVERAGE
#   15. DOCKING INSIDE A MAXIMIZED WINDOW
#   16. TOUR REJECTION REASONS
#   17. SEARCH RESULT LINK CLASSIFICATION
#   18. PAGE TO SCREEN RECT MAPPING
#   19. PAGE MARGIN SPOT
#   20. SEARCH RESULT TOUR PLAN
#   21. ANSWER PANEL SPOT
#   22. ORB MOTION
#   23. SPRING SETTINGS
#   24. CRITICAL DAMPING SETTLE POINT
#   25. SPRING PARAMETERS PER DISTANCE
#   26. DAMPED SPRING FORMULA
#   27. RETARGETABLE SPRING FLIGHT
#   28. REFRESH RATE VALIDATION
#   29. FRAME TIMING STATISTICS
#   30. FRAME STATS LOG LINE
#   31. ORB WINDOW SETTINGS
#   32. ORB STATE FROM PYTHON FACTS
#   33. ORB PAGE BRIDGE
#   34. HOME PLACEMENT ON THE PRIMARY MONITOR
#   35. ROUND REGION BOX
#   36. ORB SIZE PER STATE
#   37. WIN32 CALLS
#   38. MOVING AND RESIZING THE WINDOW
#   39. FRAME PACING
#   40. TEST FRAME PACER
#   41. TIMER RESOLUTION BOOST STATE
#   42. TIMER RESOLUTION BOOST ON
#   43. TIMER RESOLUTION BOOST OFF
#   44. FLIGHT PLAYER
#   45. NO-ACTIVATE MOUSE ANSWER
#   46. NO-ACTIVATE WINDOW SUBCLASS
#   47. TASKBAR HIDING
#   48. WIN32 FAILURE LOGGING
#   49. ORB WINDOW HARDENING
#   50. CREATE-TIME CORNER POSITION
#   51. ORB CONTROLLER
#   52. TASK PANELS
#   53. TASK PANEL SETTINGS
#   54. STEP LABELS
#   55. TOOL CATEGORY LOOKUP
#   56. PANEL MODEL
#   57. PANEL PAGE BRIDGE
#   58. PANEL WINDOW HARDENING
#   59. PANEL ROUNDED CORNERS
#   60. PANEL WINDOWS
# ============================================================

"""
core/floating_orb.py — the optional floating orb (Settings > "Floating orb", off
by default): a small frameless always-on-top window that shows only the orb,
so Mist's state stays visible while the main window is minimized for a task.

Safety: the window is excluded from screen capture where Windows supports it
(SetWindowDisplayAffinity WDA_EXCLUDEFROMCAPTURE, Windows 10 2004+; older
builds get WDA_MONITOR, a black box) and from the OCR search (the own-window
mask, by handle). It never activates (WS_EX_NOACTIVATE) and stays out of the
taskbar and Alt-Tab (WS_EX_TOOLWINDOW). Its title is empty, so title lookups
(window_list, window_activate) never return it; it lives in MistAI's own
process, so Open apps and process lookups already skip it (is_self_process).
Input actions refuse to type into it (Api._ensure_input_target). Its page
talks to Python only through OrbBridge (orb_state, talk), never the full Api.
A WM_MOUSEACTIVATE subclass answers MA_NOACTIVATE so a click never activates
it. A control thread (tick, 4 Hz) resizes it about its center per state
(config ORB_SIZE_BIG/MEDIUM/SMALL), flies it beside the task's window when
"Orb follows the task" is on (core/orb_layout.py), and drives the task
panels (core/orb_panels.py).

The window is opaque and cut to a circle with a window region rather than
using pywebview's transparent mode, which is unreliable with WebView2.

Placement and the region are done in PHYSICAL pixels once the window is shown
(pywebview makes the process DPI-aware, and it scales create-time x/y by the
display scale itself): the primary monitor's work area (above the taskbar),
its scale from GetDpiForMonitor, a square of ORB_SIZE * scale placed
ORB_MARGIN * scale from the right and bottom edges, clamped inside the area.
Logs carry numbers and Win32 error codes only.
"""

import ctypes
import sys
import threading
import time
import math

from . import live_state as events
from .config import (
    orb_background,
    orb_debug_capture,
    orb_frame_stats,
    orb_motion,
    orb_sizes,
    panel_anchor,
    panel_blur_enabled,
)


# ============================================================
# ORB LAYOUT GEOMETRY
# ============================================================

# core/orb_layout.py — pure geometry for the floating orb and its panels.
# Everything here is physical pixels; rects are (l, t, r, b), the orb is a
# square (x, y, side). No Win32, no threads: core/floating_orb.py and
# core/orb_panels.py apply the results.

# ============================================================
# RESIZE ABOUT THE CENTER
# ============================================================

def resize_about_center(x, y, side, new_side):
    """(x, y, new_side) keeping the square's center where it was."""
    cx, cy = x + side / 2.0, y + side / 2.0
    new_side = max(1, int(round(new_side)))
    return int(round(cx - new_side / 2.0)), int(round(cy - new_side / 2.0)), new_side


# ============================================================
# STRAIGHT-LINE FLIGHT FRAMES
# ============================================================

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


# ============================================================
# CLAMPING SQUARES TO THE WORK AREA
# ============================================================

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


# ============================================================
# DOCKING SIDE CHOICE
# ============================================================

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


# ============================================================
# DOCKING POSITION BESIDE A WINDOW
# ============================================================

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


# ============================================================
# RECT OVERLAP AND PANEL SPACING
# ============================================================

def overlaps(a, b):
    """Do rects a and b (l, t, r, b; right/bottom exclusive) share a pixel?"""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


PANEL_INSET = 16      # logical px from the work area's edges
PANEL_GAP = 12        # logical px between stacked panels
PANEL_ORDER = ("top-right", "top-left", "top-center")


# ============================================================
# PANEL COLUMN LAYOUT
# ============================================================

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


# ============================================================
# PANEL COLLISION TEST
# ============================================================

def _hits(spots, sizes, rect):
    return rect is not None and any(overlaps((x, y, x + w, y + h), rect)
                                    for (x, y), (w, h) in zip(spots, sizes))


# ============================================================
# PANEL ANCHORING
# ============================================================

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


# ============================================================
# EASED SQUARE ALONG A FLIGHT
# ============================================================

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


# ============================================================
# FLIGHT TIMINGS
# ============================================================

RESIZE_S = 0.12
FLIGHT_S = 0.5
NUDGE_S = 0.2


# ============================================================
# FLIGHT PLANNING
# ============================================================

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


# ============================================================
# WORK AREA COVERAGE
# ============================================================

def coverage(rect, work):
    """Fraction of ``work`` covered by ``rect`` (0..1)."""
    l, t, r, b = _clip([int(v) for v in rect], [int(v) for v in work])
    area = max(1, (work[2] - work[0]) * (work[3] - work[1]))
    return max(0, r - l) * max(0, b - t) / area


# ============================================================
# DOCKING INSIDE A MAXIMIZED WINDOW
# ============================================================

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


# ============================================================
# TOUR REJECTION REASONS
# ============================================================

TOUR_REJECTIONS = ("hidden", "ad", "above-fold", "below-fold", "outside-window", "limit")
HIDDEN, AD = 1, 2          # flag bits link_boxes sets per candidate


# ============================================================
# SEARCH RESULT LINK CLASSIFICATION
# ============================================================

def classify_page_rects(info, window_rect, limit=4):
    """(physical screen rects or None, counts) for the candidate rects in
    ``info`` (from BrowserSession.link_boxes: CSS px viewport rects
    [l, t, r, b, flags] plus the window's screenX/Y, outer and inner sizes and
    devicePixelRatio, which in Firefox already includes the Windows display
    scale). Content origin = screenX + side border ((outerWidth - innerWidth)
    / 2), screenY + toolbar height (outerHeight - innerHeight).

    In order, the first ``limit`` candidates that are visible, not ads and
    have their centre inside the viewport are accepted (a partly visible one
    is clipped to the viewport, not dropped). Rejections are counted by label
    (TOUR_REJECTIONS). Any accepted rect that maps outside ``window_rect``
    means the mapping can't be trusted: no rects. counts: found, measured,
    accepted, rejected and one count per label. Never raises."""
    counts = {"found": 0, "measured": 0, "accepted": 0, "rejected": 0}
    counts.update({k: 0 for k in TOUR_REJECTIONS})
    try:
        cands = list(info["rects"])
        counts["measured"] = len(cands)
        counts["found"] = max(len(cands), int(info.get("found", len(cands)) or 0))
        dpr = float(info["dpr"]) or 1.0
        ox = float(info["screenX"]) + (float(info["outerWidth"]) - float(info["innerWidth"])) / 2
        oy = float(info["screenY"]) + float(info["outerHeight"]) - float(info["innerHeight"])
        inner_w, inner_h = float(info["innerWidth"]), float(info["innerHeight"])
        wl, wt, wr, wb = window_rect
        out = []
        for cand in cands:
            left, top, right, bottom = (float(v) for v in cand[:4])
            flags = int(cand[4]) if len(cand) > 4 else 0
            why = None
            if flags & HIDDEN or right - left < 2 or bottom - top < 2:
                why = "hidden"
            elif flags & AD:
                why = "ad"
            elif (top + bottom) / 2 < 0:
                why = "above-fold"
            elif (top + bottom) / 2 >= inner_h or not 0 <= (left + right) / 2 < inner_w:
                why = "below-fold"
            elif len(out) >= limit:
                why = "limit"
            if why is None:
                top, bottom = max(0.0, top), min(inner_h, bottom)
                left, right = max(0.0, left), min(inner_w, right)
                r = (round((ox + left) * dpr), round((oy + top) * dpr),
                     round((ox + right) * dpr), round((oy + bottom) * dpr))
                if not (wl <= r[0] and r[2] <= wr and wt <= r[1] and r[3] <= wb):
                    why = "outside-window"
                else:
                    out.append(r)
            if why is not None:
                counts[why] += 1
                counts["rejected"] += 1
        if counts["outside-window"]:
            counts["rejected"] += len(out)
            out = []
        counts["accepted"] = len(out)
        return (out or None), counts
    except Exception:
        return None, counts


# ============================================================
# PAGE TO SCREEN RECT MAPPING
# ============================================================

def map_page_rects(info, window_rect, limit=4):
    """The accepted screen rects of classify_page_rects, or None."""
    return classify_page_rects(info, window_rect, limit)[0]


# ============================================================
# PAGE MARGIN SPOT
# ============================================================

def margin_spot(element, area, side, gap=8):
    """(x, y) for a small orb beside ``element`` inside ``area`` (the page):
    in the right margin when it has room, else the left, vertically centred
    on the element; kept inside ``area``."""
    el, et, er, eb = element
    al, at, ar, ab = area
    x = er + gap if ar - er >= side + 2 * gap else el - gap - side
    y = (et + eb) // 2 - side // 2
    return _clamp(x, al, ar - side), _clamp(y, at, ab - side)


# ============================================================
# SEARCH RESULT TOUR PLAN
# ============================================================

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


# ============================================================
# ANSWER PANEL SPOT
# ============================================================

def answer_spot(panel, side, work, gap=12):
    """(x, y) for the orb beside the answer panel: to its left, vertically
    centred, else to its right when the left has no room; inside ``work``."""
    pl, pt, pr, pb = panel
    wl, wt, wr, wb = (int(v) for v in work)
    x = pl - gap - side if pl - gap - side >= wl else pr + gap
    y = (pt + pb) // 2 - side // 2
    return _clamp(x, wl, wr - side), _clamp(y, wt, wb - side)


# ============================================================
# ORB MOTION
# ============================================================

# core/orb_motion.py — pure motion for the floating orb's flights: a 2-D damped
# spring with a closed-form solution, so a flight's position is a function of
# the time since take-off only (a dropped frame skips ahead, it never bends the
# path), plus the frame-timing statistics. No Win32, no threads, no logging:
# core/floating_orb.Tween samples it once per frame.
#
# Motion (config.orb_motion(), MISTAI_ORB_MOTION):
#   lively  damping ratio 0.58 (short hops) .. 0.68 (long flights): a small
#           overshoot that settles (soft-capped near OVERSHOOT_PX); omega is the larger of a snappy minimum and
#           the value that brings the decay envelope under SETTLE_PX by
#           LIVELY_SETTLE_S, so a long flight is never slow; flights longer than
#           ANTICIPATION_MIN_PX first lean back ANTICIPATION_PX for
#           ANTICIPATION_S.
#   calm    critically damped (no overshoot), settled by CALM_SETTLE_S.
# Both bow upward with a perpendicular offset that is zero (and flat) at both
# ends. A flight can be retargeted mid-way: the new spring starts from the
# rendered position and velocity at that moment, so there is no jump.

# ============================================================
# SPRING SETTINGS
# ============================================================

SETTLE_PX = 0.5            # closer than this ...
SETTLE_SPEED = 30.0        # ... and slower than this (px/s): snap and finish
MAX_FLIGHT_S = 1.5         # hard cap per (re)target, then snap
LIVELY_SETTLE_S = 0.52     # + the anticipation: everything settled by ~0.6 s
CALM_SETTLE_S = 0.6
LIVELY_MIN_OMEGA = 18.0    # rad/s: short hops stay snappy
CALM_MIN_OMEGA = 12.0
MAX_OMEGA = 60.0
ANTICIPATION_S = 0.06
ANTICIPATION_PX = 8.0
ANTICIPATION_MIN_PX = 200.0
ARC_CAP_PX = 200.0
OVERSHOOT_PX = 28.0        # overshoot past the target is soft-capped near this (tanh)
_H = 1e-5                  # s, for the numeric velocity of the rendered path


# ============================================================
# CRITICAL DAMPING SETTLE POINT
# ============================================================

def _critical_x(amplitude):
    """x = omega * t at which a critically damped spring released from rest
    at ``amplitude`` is within SETTLE_PX: (1 + x) e^-x = SETTLE_PX / amplitude."""
    goal = SETTLE_PX / amplitude
    if goal >= 1.0:
        return 0.0
    x = max(1.0, math.log(1.0 / goal))
    for _ in range(40):                      # Newton on g(x) = ln(1+x) - x - ln(goal)
        g = math.log1p(x) - x - math.log(goal)
        dg = 1.0 / (1.0 + x) - 1.0
        step = g / dg
        x -= step
        if abs(step) < 1e-9:
            break
    return max(0.0, x)


# ============================================================
# SPRING PARAMETERS PER DISTANCE
# ============================================================

def spring_params(distance, motion="lively"):
    """(damping ratio, natural frequency rad/s) for a flight of ``distance`` px."""
    d = max(1.0, float(distance))
    if motion == "calm":
        return 1.0, min(MAX_OMEGA, max(CALM_MIN_OMEGA, _critical_x(d) / CALM_SETTLE_S))
    share = min(1.0, max(0.0, (math.log10(d) - 2.0) / 1.2))
    zeta = 0.58 + 0.10 * share
    amplitude = d / math.sqrt(1.0 - zeta * zeta)          # the envelope from rest
    need = math.log(max(1.0, amplitude / SETTLE_PX)) / (zeta * LIVELY_SETTLE_S)
    return zeta, min(MAX_OMEGA, max(LIVELY_MIN_OMEGA, need))


# ============================================================
# DAMPED SPRING FORMULA
# ============================================================

def spring(e0, v0, zeta, omega, t):
    """(offset, velocity) at ``t`` of a damped spring released at offset
    ``e0`` with velocity ``v0`` (closed form; zeta <= 1)."""
    if t <= 0:
        return e0, v0
    if zeta >= 1.0:
        k = math.exp(-omega * t)
        b = v0 + omega * e0
        return (e0 + b * t) * k, (v0 - omega * b * t) * k
    wd = omega * math.sqrt(1.0 - zeta * zeta)
    k = math.exp(-zeta * omega * t)
    c, s = math.cos(wd * t), math.sin(wd * t)
    e = k * (e0 * c + (v0 + zeta * omega * e0) / wd * s)
    v = k * (v0 * c - (omega * omega * e0 + zeta * omega * v0) / wd * s)
    return e, v


# ============================================================
# RETARGETABLE SPRING FLIGHT
# ============================================================

class Flight:
    """A retargetable spring flight of a point (the orb's top-left corner;
    the size never changes in flight). ``at(t)`` -> (x, y, done)."""

    def __init__(self, start, target, t0, motion="lively", bow=0.0):
        self.motion = motion if motion in ("lively", "calm") else "lively"
        self.bow = max(0.0, float(bow))
        self._begin((float(start[0]), float(start[1])), (0.0, 0.0), target, t0, fresh=True)

    def _begin(self, pos, vel, target, t0, fresh):
        self.t0, self.p0, self.v0 = float(t0), pos, vel
        self.target = (float(target[0]), float(target[1]))
        dx, dy = self.target[0] - pos[0], self.target[1] - pos[1]
        dist = math.hypot(dx, dy)
        self.dist = dist
        self.dir = (dx / dist, dy / dist) if dist > 0 else (0.0, 0.0)
        self.zeta, self.omega = spring_params(dist, self.motion)
        self.lead = (ANTICIPATION_S if fresh and self.motion == "lively"
                     and dist > ANTICIPATION_MIN_PX else 0.0)
        arc = 0.0
        nx, ny = 0.0, 0.0
        if dist > 0:
            arc = min(ARC_CAP_PX, self.bow / 2.0 * dist) * abs(dx) / dist   # none when vertical
            nx, ny = dy / dist, -dx / dist                                 # perpendicular ...
            if ny > 0:
                nx, ny = -nx, -ny                                          # ... pointing up
        self.arc = (nx * arc, ny * arc)
        self.settled = False

    def _raw(self, t):
        """(x, y, spring vx, spring vy) at ``t``, before settling."""
        s = max(0.0, t - self.t0)
        (px, py), (dx, dy) = self.p0, self.dir
        if s < self.lead:                   # lean back, then hand over at rest
            back = -ANTICIPATION_PX * math.sin(math.pi * s / self.lead) ** 2
            return px + dx * back, py + dy * back, 0.0, 0.0
        s -= self.lead
        tx, ty = self.target
        ex, vx = spring(px - tx, self.v0[0], self.zeta, self.omega, s)
        ey, vy = spring(py - ty, self.v0[1], self.zeta, self.omega, s)
        x, y = tx + ex, ty + ey
        past = ex * dx + ey * dy                  # beyond the target along the flight
        if past > 0:
            cut = past - OVERSHOOT_PX * math.tanh(past / OVERSHOOT_PX)
            x, y = x - dx * cut, y - dy * cut
        if self.dist > 0 and self.arc != (0.0, 0.0):
            p = ((x - px) * dx + (y - py) * dy) / self.dist
            f = math.sin(math.pi * min(1.0, max(0.0, p))) ** 2
            x, y = x + self.arc[0] * f, y + self.arc[1] * f
        return x, y, vx, vy

    def position(self, t):
        x, y, _vx, _vy = self._raw(t)
        return x, y

    def velocity(self, t):
        """The rendered path's velocity (px/s) at ``t`` (0 before take-off)."""
        if self.settled:
            return 0.0, 0.0
        if t - _H < self.t0:
            a, b = self._raw(t), self._raw(t + _H)
            return (b[0] - a[0]) / _H, (b[1] - a[1]) / _H
        a, b = self._raw(t - _H), self._raw(t + _H)
        return (b[0] - a[0]) / (2 * _H), (b[1] - a[1]) / (2 * _H)

    def at(self, t):
        """(x, y, done): floats in flight, exactly the target once settled."""
        if self.settled:
            return self.target[0], self.target[1], True
        x, y, vx, vy = self._raw(t)
        s = t - self.t0
        near = math.hypot(x - self.target[0], y - self.target[1]) < SETTLE_PX
        if (s >= self.lead and near and math.hypot(vx, vy) < SETTLE_SPEED) or s >= MAX_FLIGHT_S:
            self.settled = True
            return self.target[0], self.target[1], True
        return x, y, False

    def retarget(self, t, target):
        """Fly on to ``target`` from where the orb is at ``t``, with the
        velocity it has there (no restart, no jump)."""
        if self.settled:
            pos, vel = self.target, (0.0, 0.0)
        else:
            pos, vel = self.position(t), self.velocity(t)
        self._begin(pos, vel, target, t, fresh=False)


# -- frame timing ------------------------------------------------------------------------


# ============================================================
# REFRESH RATE VALIDATION
# ============================================================

def valid_refresh(hz, default=60):
    """A usable refresh rate (Hz): 24..500, else ``default`` (0 and 1 mean
    "hardware default" to EnumDisplaySettings)."""
    try:
        hz = int(round(float(hz)))
    except (TypeError, ValueError):
        return default
    return hz if 24 <= hz <= 500 else default


# ============================================================
# FRAME TIMING STATISTICS
# ============================================================

def frame_stats(times, refresh_hz=60):
    """Numbers for one flight from its per-frame clock readings (seconds):
    frames, duration_ms, mean_ms / p95_ms / max_ms of the intervals, slow
    (intervals above 1.5 x the refresh interval) and the refresh used."""
    hz = valid_refresh(refresh_hz)
    times = [float(t) for t in times]
    gaps = sorted(b - a for a, b in zip(times, times[1:]))
    out = {"frames": len(times), "duration_ms": 0.0, "mean_ms": 0.0, "p95_ms": 0.0,
           "max_ms": 0.0, "slow": 0, "refresh": hz}
    if not gaps:
        return out
    limit = 1.5 / hz
    rank = max(0, math.ceil(0.95 * len(gaps)) - 1)
    out.update(duration_ms=round((times[-1] - times[0]) * 1000, 1),
               mean_ms=round(sum(gaps) / len(gaps) * 1000, 1),
               p95_ms=round(gaps[rank] * 1000, 1), max_ms=round(gaps[-1] * 1000, 1),
               slow=sum(1 for g in gaps if g > limit))
    return out


# ============================================================
# FRAME STATS LOG LINE
# ============================================================

def stats_line(stats, pacer, motion):
    """The one log line (numbers and fixed labels only)."""
    return (f"[FloatingOrb] frame stats frames {stats['frames']} duration "
            f"{stats['duration_ms']:.0f} ms interval mean {stats['mean_ms']:.1f} p95 "
            f"{stats['p95_ms']:.1f} max {stats['max_ms']:.1f} ms slow {stats['slow']} refresh "
            f"{stats['refresh']} Hz pacer {pacer} motion {motion}")


# ============================================================
# ORB WINDOW SETTINGS
# ============================================================

ORB_MARGIN = 16           # logical px from the work area's right and bottom edges
MIN_SIZE = (1, 1)         # pywebview's default min_size (200, 100) clamps every resize
MAXIMIZED = 0.9           # a target covering this much of its work area is "maximized"
ELEMENT_PAD = 24          # logical px around a click point used as its element rect
# Windows the orb never follows: the taskbar/desktop shell and Task Manager
# (MistAI's own windows, the orb and its panels are excluded by handle/PID).
SHELL_CLASSES = frozenset({"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW",
                           "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland",
                           "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow"})
IGNORED_PROCESSES = ("taskmgr",)
# MistAI's dedicated browser is Playwright's Firefox, run from its own folder.
PLAYWRIGHT_PATH_HINTS = ("ms-playwright", ".local-browsers")
# Organic result title links: DuckDuckGo's normal page (current and older
# markup), its HTML endpoint, Google. Ads are flagged in the page and skipped.
RESULT_LINKS = ('a[data-testid="result-title-a"], article[data-testid="result"] h2 a, '
                'li[data-layout="organic"] h2 a, a.result__a, .result__title a, '
                '#search a:has(h3), #rso a:has(h3)')
TOUR_STOPS = 4
TOUR_CANDIDATES = 12      # measured per read; the first TOUR_STOPS usable ones are kept
TOUR_RETRY_S = 0.4        # fewer than TOUR_STOPS found: the page may still be rendering
TOUR_DWELL_S = 0.7
SPEECH_START_S = 1.0      # the voice may start a moment after the answer arrives
WORDS_PER_S = 2.5         # speech-length estimate when the TTS has no speaking flag
SPEECH_CAP_S = 20.0

WDA_MONITOR = 0x1
WDA_EXCLUDEFROMCAPTURE = 0x11
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_APPWINDOW = 0x00040000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020     # with LAYERED: clicks pass through
LWA_ALPHA = 0x2
MONITOR_DEFAULTTONEAREST = 2
MONITOR_DEFAULTTOPRIMARY = 1
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOOWNERZORDER = 0x0200
SWP_ASYNCWINDOWPOS = 0x4000
# One flight frame: position only. ASYNCWINDOWPOS because the window belongs
# to the WinForms UI thread: a synchronous cross-thread SetWindowPos would
# block the animation thread whenever that thread is busy (each post is an
# absolute position, so a late one is harmless). No SWP_NOCOPYBITS: it would
# make WebView2 repaint on every move.
SWP_FRAME = SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_ASYNCWINDOWPOS
SPI_GETCLIENTAREAANIMATION = 0x1042
ENUM_CURRENT_SETTINGS = -1
THREAD_PRIORITY_ABOVE_NORMAL = 1
CREATE_WAITABLE_TIMER_HIGH_RESOLUTION = 0x2
TIMER_ALL_ACCESS = 0x1F0003
FLIGHT_SWITCH_INTERVAL = 0.001     # s, sys.setswitchinterval during a flight


# ============================================================
# ORB STATE FROM PYTHON FACTS
# ============================================================

def orb_state(api):
    """The orb's state from Python's own facts, with the page's priority
    (ui/html.py orbState): the push-to-talk mic, then Mist's voice, then a
    running task, else idle."""
    try:
        if api._ptt_wants_mic():
            return "listening"
    except Exception:
        pass
    try:
        if getattr(getattr(api, "tts", None), "speaking", False):
            return "speaking"
    except Exception:
        pass
    try:
        if api._agent_lock.locked():
            return "thinking"
    except Exception:
        pass
    return "idle"


# ============================================================
# ORB PAGE BRIDGE
# ============================================================

class OrbBridge:
    """The orb page's js_api: the read-only state and talk(), nothing else,
    so the floating window cannot reach the rest of the Api."""

    _TALK = ("start", "stop", "toggle")

    def __init__(self, api):
        self._api = api

    def orb_state(self):
        return {"state": orb_state(self._api)}

    def talk(self, kind):
        """Push-to-talk from the orb: "start" (a hold began; only when not
        already listening), "stop" (the hold ended; sends, only while
        listening), "toggle" (a tap: start, or send a latched listen). The
        same Api.push_to_talk as the Talk button, with its refusals."""
        if kind not in self._TALK:
            return {"success": False}
        try:
            listening = self._api._ptt_wants_mic()
            if (kind == "start" and listening) or (kind == "stop" and not listening):
                return {"success": True}
            r = self._api.push_to_talk() or {}
            return {"success": bool(r.get("success")), "message": str(r.get("message", ""))}
        except Exception as e:
            print(f"[FloatingOrb] talk failed ({type(e).__name__})")
            return {"success": False}


# -- geometry (pure) ------------------------------------------------------------


# ============================================================
# HOME PLACEMENT ON THE PRIMARY MONITOR
# ============================================================

def place(work, scale, size=None, margin=ORB_MARGIN):
    """(x, y, side) in physical px: a square of ``size * scale`` with
    ``margin * scale`` to the right and bottom edges of ``work`` (l, t, r, b),
    shrunk and clamped so it always lies fully inside the work area."""
    l, t, r, b = (int(v) for v in work)
    scale = scale if scale and scale > 0 else 1.0
    size = orb_sizes()[0] if size is None else size
    avail_w, avail_h = max(1, r - l), max(1, b - t)
    side = max(1, min(int(round(size * scale)), avail_w, avail_h))
    gap = max(0, min(int(round(margin * scale)), avail_w - side, avail_h - side))
    return max(l, r - gap - side), max(t, b - gap - side), side


# ============================================================
# ROUND REGION BOX
# ============================================================

def ellipse_box(width, height):
    """CreateEllipticRgn's box for a window of this size: relative to the
    window's top-left, right/bottom exclusive (hence the +1)."""
    return 0, 0, int(width) + 1, int(height) + 1


# ============================================================
# ORB SIZE PER STATE
# ============================================================

def size_for(state, docked=False):
    """Logical side for a state: small while docked beside the task's window,
    medium while thinking or speaking, big when idle or listening."""
    big, medium, small = orb_sizes()
    if docked:
        return small
    return medium if state in ("thinking", "speaking") else big


# -- Win32 (ctypes; tests pass a fake with the same methods) --------------------


# ============================================================
# WIN32 CALLS
# ============================================================

class Win32:
    """The few Win32 calls the orb needs, with last-error capture. pywin32's
    win32gui has no CreateEllipticRgn, so the region calls go through ctypes."""

    def __init__(self):
        from ctypes import wintypes

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self.user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
        self.user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                             ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                             wintypes.UINT]
        self.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        self.gdi32.CreateEllipticRgn.argtypes = [ctypes.c_int] * 4
        self.gdi32.CreateEllipticRgn.restype = wintypes.HANDLE
        self.gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
        self.gdi32.CreateRoundRectRgn.restype = wintypes.HANDLE
        self.gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        self._RECT = wintypes.RECT

    def last_error(self):
        return ctypes.get_last_error()

    def set_affinity(self, hwnd, mode):
        return bool(self.user32.SetWindowDisplayAffinity(hwnd, mode))

    def add_exstyle(self, hwnd, bits):
        style = self.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        self.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | bits)

    def set_click_through(self, hwnd, on):
        """WS_EX_LAYERED|WS_EX_TRANSPARENT on (fully opaque) or both off."""
        style = self.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        bits = WS_EX_LAYERED | WS_EX_TRANSPARENT
        if on:
            self.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | bits)
            self.user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
        else:
            self.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style & ~bits)

    def show(self, hwnd, on):
        """ShowWindow: SW_SHOWNOACTIVATE (4) or SW_HIDE (0); never activates."""
        return bool(self.user32.ShowWindow(hwnd, 4 if on else 0))

    def dwm_set(self, hwnd, attribute, value):
        """DwmSetWindowAttribute with an int value; the HRESULT (0 = ok)."""
        v = ctypes.c_int(int(value))
        return ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(v),
                                                          ctypes.sizeof(v))

    def create_round_rect(self, box):
        return self.gdi32.CreateRoundRectRgn(*box)

    def exstyle(self, hwnd):
        return self.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)

    def set_exstyle(self, hwnd, add=0, remove=0):
        style = self.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        self.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, (style | add) & ~remove)

    def move_only(self, hwnd, x, y):
        """Position only: no resize, no z-order change, no activation, and
        posted to the window's own thread (SWP_FRAME)."""
        return bool(self.user32.SetWindowPos(hwnd, None, x, y, 0, 0, SWP_FRAME))

    def animations_enabled(self):
        """SPI_GETCLIENTAREAANIMATION (Settings > Accessibility > Animation
        effects); True when it can't be read."""
        from ctypes import wintypes

        v = wintypes.BOOL(1)
        if not self.user32.SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(v), 0):
            return True
        return bool(v.value)

    def refresh_rate(self, hwnd):
        """The refresh rate (Hz) of the monitor the window is on, else 60."""
        try:
            import win32api

            mon = win32api.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
            device = win32api.GetMonitorInfo(mon)["Device"]
            mode = win32api.EnumDisplaySettings(device, ENUM_CURRENT_SETTINGS)
            return valid_refresh(mode.DisplayFrequency)
        except Exception:
            return 60

    def make_pacer(self, hz):
        return FramePacer(hz)

    def _kernel32(self):
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCurrentThread.restype = wintypes.HANDLE
        k.GetThreadPriority.argtypes = [wintypes.HANDLE]
        k.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        return k

    def boost_thread(self):
        """Raise the calling thread to above-normal; its old priority, or None."""
        k = self._kernel32()
        h = k.GetCurrentThread()
        prev = k.GetThreadPriority(h)
        return prev if k.SetThreadPriority(h, THREAD_PRIORITY_ABOVE_NORMAL) else None

    def restore_thread(self, prev):
        k = self._kernel32()
        k.SetThreadPriority(k.GetCurrentThread(), int(prev))

    def foreground(self):
        return self.user32.GetForegroundWindow()

    def is_minimized(self, hwnd):
        return bool(self.user32.IsIconic(hwnd))

    def window_info(self, hwnd):
        """(pid, process name, window class) of a window; never raises."""
        try:
            import psutil
            import win32gui
            import win32process

            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            return pid, psutil.Process(pid).name(), win32gui.GetClassName(hwnd)
        except Exception:
            return 0, "", ""

    def browser_window(self):
        """The visible top-level window of MistAI's Playwright Firefox, or None."""
        try:
            import psutil
            import win32gui
            import win32process

            found = []

            def visit(hwnd, _):
                if found or not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd):
                    return True
                try:
                    exe = psutil.Process(win32process.GetWindowThreadProcessId(hwnd)[1]).exe()
                except Exception:
                    return True
                if any(h in exe.lower() for h in PLAYWRIGHT_PATH_HINTS):
                    found.append(hwnd)
                return True

            win32gui.EnumWindows(visit, None)
            return found[0] if found else None
        except Exception:
            return None

    def monitor_for_rect(self, rect):
        """(work area, scale) of the monitor nearest ``rect`` (l, t, r, b)."""
        import win32api

        mon = win32api.MonitorFromRect(tuple(int(v) for v in rect), MONITOR_DEFAULTTONEAREST)
        return tuple(win32api.GetMonitorInfo(mon)["Work"]), self.monitor_scale(mon)

    def primary_work_area(self):
        """(monitor handle, (l, t, r, b)): the primary monitor's work area,
        i.e. without the taskbar, in this process's DPI context."""
        import win32api

        mon = win32api.MonitorFromPoint((0, 0), MONITOR_DEFAULTTOPRIMARY)
        return mon, tuple(win32api.GetMonitorInfo(mon)["Work"])

    def monitor_scale(self, monitor, hwnd=None):
        """Effective DPI / 96 of ``monitor`` (GetDpiForMonitor), else of the
        window (GetDpiForWindow), else 1.0. A DPI-unaware process gets 96."""
        try:
            x, y = ctypes.c_uint(), ctypes.c_uint()
            if ctypes.windll.shcore.GetDpiForMonitor(int(monitor), 0, ctypes.byref(x),
                                                     ctypes.byref(y)) == 0 and x.value:
                return x.value / 96.0
        except Exception:
            pass
        try:
            dpi = self.user32.GetDpiForWindow(hwnd) if hwnd else 0
            if dpi:
                return dpi / 96.0
        except Exception:
            pass
        return 1.0

    def window_rect(self, hwnd):
        rect = self._RECT()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom

    def move(self, hwnd, x, y, w, h):
        return bool(self.user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE))

    def create_ellipse(self, box):
        return self.gdi32.CreateEllipticRgn(*box)

    def set_region(self, hwnd, region):
        return bool(self.user32.SetWindowRgn(hwnd, region, True))

    def delete_object(self, handle):
        self.gdi32.DeleteObject(handle)


# ============================================================
# MOVING AND RESIZING THE WINDOW
# ============================================================

def apply_square(win, hwnd, x, y, side, last_side=None):
    """Put the window on the square. Same side as ``last_side``: a plain
    move (SetWindowPos SWP_NOSIZE), no region work. A new side: resize, then
    cut a circle for that size. True when applied; never raises or logs."""
    try:
        if last_side is not None and side == last_side:
            return bool(win.move_only(hwnd, x, y))
        ok = win.move(hwnd, x, y, side, side)
        region = win.create_ellipse(ellipse_box(side, side))
        if not region:
            return False
        if not win.set_region(hwnd, region):
            win.delete_object(region)
            return False
        return bool(ok)
    except Exception:
        return False


# ============================================================
# FRAME PACING
# ============================================================

class FramePacer:
    """Waits one display frame per wait(): DwmFlush() (returns after the next
    DWM composition, i.e. vsync), else a high-resolution waitable timer
    (CreateWaitableTimerExW, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION) on
    deadlines one refresh apart, else timeBeginPeriod(1) and time.sleep.
    DwmFlush falls back for the rest of the flight when it fails or returns
    in under 30% of a frame three times running (nothing to compose: it would
    spin). ``kind`` is "dwm", "timer" or "sleep"; close() frees the timer and
    ends the timer period. Never raises."""

    FAST_SHARE = 0.3
    FAST_LIMIT = 3

    def __init__(self, hz, clock=time.perf_counter):
        self.interval = 1.0 / valid_refresh(hz)
        self._clock = clock
        self._fast = 0
        self._timer = None
        self._period = False
        self._k = None
        self._next = clock() + self.interval
        self.kind = "dwm"
        try:
            self._dwm = ctypes.WinDLL("dwmapi").DwmFlush
            self._dwm.restype = ctypes.c_long
        except Exception:
            self._dwm = None
            self._to_timer()

    def _to_timer(self):
        from ctypes import wintypes

        self.kind = "timer"
        try:
            k = ctypes.WinDLL("kernel32")
            k.CreateWaitableTimerExW.restype = wintypes.HANDLE
            k.CreateWaitableTimerExW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                                 wintypes.DWORD, wintypes.DWORD]
            k.SetWaitableTimer.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong),
                                           ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
                                           wintypes.BOOL]
            k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            self._k = k
            self._timer = k.CreateWaitableTimerExW(None, None,
                                                   CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,
                                                   TIMER_ALL_ACCESS) or None
        except Exception:
            self._timer = None
        if self._timer is None:
            self.kind = "sleep"
            try:
                ctypes.WinDLL("winmm").timeBeginPeriod(1)
                self._period = True
            except Exception:
                pass
        self._next = self._clock() + self.interval

    def wait(self):
        try:
            if self.kind == "dwm":
                t = self._clock()
                hr = self._dwm()
                if hr != 0:
                    self._to_timer()
                elif self._clock() - t < self.FAST_SHARE * self.interval:
                    self._fast += 1
                    if self._fast < self.FAST_LIMIT:
                        return
                    self._to_timer()
                else:
                    self._fast = 0
                    return
            now = self._clock()
            self._next += self.interval
            if self._next <= now:                    # a late frame: skip ahead, no catch-up
                self._next = now + self.interval
            delay = self._next - now
            if self._timer is not None:
                due = ctypes.c_longlong(-max(1, int(delay * 1e7)))     # relative, 100 ns units
                if self._k.SetWaitableTimer(self._timer, ctypes.byref(due), 0, None, None, False):
                    self._k.WaitForSingleObject(self._timer, int(delay * 1000) + 50)
                    return
            time.sleep(delay)
        except Exception:
            time.sleep(self.interval)

    def close(self):
        try:
            if self._timer is not None:
                self._k.CloseHandle(self._timer)
                self._timer = None
            if self._period:
                ctypes.WinDLL("winmm").timeEndPeriod(1)
                self._period = False
        except Exception:
            pass


# ============================================================
# TEST FRAME PACER
# ============================================================

class _EventPacer:
    """For a Win32 stand-in without make_pacer (tests): Event.wait(FRAME_S)."""

    kind = "sleep"

    def __init__(self, stop, seconds):
        self._stop, self._s = stop, seconds

    def wait(self):
        if self._s > 0:
            self._stop.wait(self._s)

    def close(self):
        pass


# ============================================================
# TIMER RESOLUTION BOOST STATE
# ============================================================

_BOOST_LOCK = threading.Lock()
_BOOST = {"n": 0, "interval": None}


# ============================================================
# TIMER RESOLUTION BOOST ON
# ============================================================

def _boost_enter(win):
    """Above-normal priority for this thread and a 1 ms GIL switch interval
    for the flight (ref-counted, so overlapping flights restore it once).
    The thread's old priority, or None."""
    prev = None
    try:
        prev = win.boost_thread() if hasattr(win, "boost_thread") else None
    except Exception:
        prev = None
    with _BOOST_LOCK:
        if _BOOST["n"] == 0:
            _BOOST["interval"] = sys.getswitchinterval()
            sys.setswitchinterval(FLIGHT_SWITCH_INTERVAL)
        _BOOST["n"] += 1
    return prev


# ============================================================
# TIMER RESOLUTION BOOST OFF
# ============================================================

def _boost_exit(win, prev):
    try:
        if prev is not None and hasattr(win, "restore_thread"):
            win.restore_thread(prev)
    except Exception:
        pass
    with _BOOST_LOCK:
        _BOOST["n"] = max(0, _BOOST["n"] - 1)
        if _BOOST["n"] == 0 and _BOOST["interval"] is not None:
            sys.setswitchinterval(_BOOST["interval"])
            _BOOST["interval"] = None


# ============================================================
# FLIGHT PLAYER
# ============================================================

class Tween:
    """Plays flight segments [(from, to, seconds, bow), ...] on a worker
    thread, one frame per display refresh (FramePacer). A segment that keeps
    the size is a spring move (core/orb_motion.Flight, MISTAI_ORB_MOTION read
    per play): one SetWindowPos per frame and nothing else; its ``seconds`` is
    only the caller's estimate. A segment that changes the size is a short
    timed resize step (smoothstep), the region recut per frame. Positions come
    from the clock, never from a frame count. retarget() redirects a spring
    move in flight without a jump; a new play() (or cancel()) stops the one
    before. With Windows animations off every segment ends at once.
    MISTAI_ORB_FRAME_STATS=1 logs one numbers-only line per play."""

    FRAME_S = 1 / 60          # only for a Win32 stand-in without make_pacer
    JOIN_S = 0.05
    _logged_animate = True    # log "animations off" once per change, not per flight

    def __init__(self, win, hwnd, clock=time.perf_counter):
        self._win, self._hwnd, self._clock = win, hwnd, clock
        self._stop = None
        self._thread = None
        self._leg = None          # the spring Flight being flown, else None
        self._pending = None      # (landing, tail segments) from retarget()
        self.side = None          # the side last applied (physical px)
        self.goal = None          # where the current play ends
        self.last_stats = None

    def _ask(self, name, default):
        fn = getattr(self._win, name, None)
        if fn is None:
            return default
        try:
            return fn(self._hwnd) if name == "refresh_rate" else fn()
        except Exception:
            return default

    def play(self, segments, on_frame=None, done=None):
        self.cancel()
        old = self._thread
        if old is not None and old.is_alive() and old is not threading.current_thread():
            old.join(self.JOIN_S)                    # never two threads moving the window
        segments = [tuple(s) for s in segments]
        stop = self._stop = threading.Event()
        self._leg, self._pending = None, None
        self.goal = tuple(segments[-1][1]) if segments else None
        opts = {"motion": orb_motion(), "stats": orb_frame_stats(),
                "animate": bool(self._ask("animations_enabled", True)),
                "hz": valid_refresh(self._ask("refresh_rate", 60))}
        if opts["animate"] != Tween._logged_animate:
            Tween._logged_animate = opts["animate"]
            if not opts["animate"]:
                print("[FloatingOrb] Windows animation effects are off: the orb jumps")
        t = self._thread = threading.Thread(target=self._run,
                                            args=(stop, segments, on_frame, done, opts),
                                            daemon=True, name="OrbTween")
        t.start()
        return t

    def _apply(self, sq, on_frame, counts):
        counts[0] += 1
        if not apply_square(self._win, self._hwnd, *sq, last_side=self.side):
            counts[1] += 1
        self.side = sq[2]
        if on_frame:
            on_frame(sq)

    def _run(self, stop, segments, on_frame, done, opts):
        counts = [0, 0]                              # frames applied, failed
        times = []
        pacer = None
        if not opts["animate"]:                      # Windows animations off: jump
            for _start, end, _s, _bow in segments:
                if stop.is_set():
                    return
                self._apply(tuple(end), on_frame, counts)
        else:
            pacer = (self._win.make_pacer(opts["hz"]) if hasattr(self._win, "make_pacer")
                     else _EventPacer(stop, self.FRAME_S))
            prev = _boost_enter(self._win)
            try:
                self._fly(stop, segments, on_frame, opts, counts, times, pacer)
            finally:
                _boost_exit(self._win, prev)
                pacer.close()
                self._leg, self._pending = None, None
        if counts[1]:
            print(f"[FloatingOrb] tween: {counts[1]} of {counts[0]} frames failed")
        if opts["stats"] and times:
            self.last_stats = frame_stats(times, opts["hz"])
            print(stats_line(self.last_stats, pacer.kind if pacer else "none", opts["motion"]))
        if done and not stop.is_set():
            done()

    def _fly(self, stop, segments, on_frame, opts, counts, times, pacer):
        """The frame loop: one clock read per frame, no logging, no locks."""
        queue = list(segments)
        while queue and not stop.is_set():
            start, end, seconds, bow = queue.pop(0)
            last = tuple(start)                      # already there: no frame for it
            now = t0 = self._clock()
            side = end[2]
            leg = None
            if start[2] == side:                     # a move: the spring
                leg = self._leg = Flight(start[:2], end[:2], now, opts["motion"], bow)
            while not stop.is_set():
                if opts["stats"]:
                    times.append(now)
                if leg is not None:
                    pend = self._pending
                    if pend is not None:
                        self._pending = None
                        leg.retarget(now, pend[0][:2])
                        queue = list(pend[1])
                    x, y, finished = leg.at(now)
                    sq = (int(round(x)), int(round(y)), side)
                else:                                # a resize step
                    t = 1.0 if seconds <= 0 else (now - t0) / seconds
                    sq = square_at(start, end, t, bow)
                    finished = t >= 1.0
                if sq != last:
                    self._apply(sq, on_frame, counts)
                    last = sq
                if finished:
                    break
                pacer.wait()
                now = self._clock()
            self._leg, self._pending = None, None

    @property
    def in_flight(self):
        """A spring move is flying (retarget() can redirect it)."""
        return self._leg is not None and self.running

    def retarget(self, goal):
        """Redirect the spring move in flight to ``goal`` (x, y, side),
        keeping its position and velocity: it lands at the current size on
        the goal's centre, then resizes there. False when no move is flying."""
        if not self.in_flight or self.side is None:
            return False
        goal = tuple(int(v) for v in goal)
        side = self.side
        land = goal if goal[2] == side else resize_about_center(goal[0], goal[1], goal[2], side)
        tail = [] if land == goal else [(land, goal, RESIZE_S, 0.0)]
        self.goal = goal
        self._pending = (land, tail)
        return True

    def cancel(self):
        if self._stop is not None:
            self._stop.set()

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()


# ============================================================
# NO-ACTIVATE MOUSE ANSWER
# ============================================================

WM_MOUSEACTIVATE = 0x0021
MA_NOACTIVATE = 3


def mouse_activate_answer(msg):
    """The subclass's answer: MA_NOACTIVATE to WM_MOUSEACTIVATE, else None
    (pass the message on)."""
    return MA_NOACTIVATE if msg == WM_MOUSEACTIVATE else None


_SUBCLASS_PROCS = []      # keeps the ctypes callbacks alive


# ============================================================
# NO-ACTIVATE WINDOW SUBCLASS
# ============================================================

def guard_mouse_activate(hwnd, run_on_ui=None):
    """Subclass the window (comctl32 SetWindowSubclass) so a click answers
    WM_MOUSEACTIVATE with MA_NOACTIVATE: a click never activates the orb,
    on top of WS_EX_NOACTIVATE. SetWindowSubclass works only on the
    window's own thread, so a failed direct call is retried through
    ``run_on_ui`` (pywebview's WinForms Invoke). True when installed."""
    try:
        from ctypes import wintypes

        comctl = ctypes.WinDLL("comctl32")
        lresult = ctypes.c_ssize_t
        proto = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                   wintypes.LPARAM, ctypes.c_size_t, ctypes.c_size_t)
        comctl.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                           wintypes.LPARAM]
        comctl.DefSubclassProc.restype = lresult
        comctl.SetWindowSubclass.argtypes = [wintypes.HWND, proto, ctypes.c_size_t,
                                             ctypes.c_size_t]

        def proc(h, msg, wp, lp, _uid, _data):
            answer = mouse_activate_answer(msg)
            return answer if answer is not None else comctl.DefSubclassProc(h, msg, wp, lp)

        cb = proto(proc)
        _SUBCLASS_PROCS.append(cb)
        done = {"ok": bool(comctl.SetWindowSubclass(hwnd, cb, 0x4D49, 0))}
        if not done["ok"] and run_on_ui is not None:
            run_on_ui(lambda: done.update(ok=bool(comctl.SetWindowSubclass(hwnd, cb, 0x4D49, 0))))
        return done["ok"]
    except Exception:
        return False


# ============================================================
# TASKBAR HIDING
# ============================================================

def hide_from_taskbar(hwnd, win, tag):
    """WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE on, WS_EX_APPWINDOW off, then hide
    and re-show without activating: the taskbar and Alt-Tab only re-read a
    window's styles when it is shown, so bits set on a visible WinForms form
    (ShowInTaskbar is True by default) leave its button in place. Returns
    True when the read-back has both bits and no APPWINDOW."""
    try:
        win.set_exstyle(hwnd, add=WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, remove=WS_EX_APPWINDOW)
        win.show(hwnd, False)
        win.show(hwnd, True)
        style = win.exstyle(hwnd)
        ok = bool(style & WS_EX_TOOLWINDOW and style & WS_EX_NOACTIVATE
                  and not style & WS_EX_APPWINDOW)
        print(f"[{tag}] taskbar: {'hidden' if ok else 'still listed'}")
        return ok
    except Exception as e:
        print(f"[{tag}] SetWindowLongW error ({type(e).__name__})")
        return False


# ============================================================
# WIN32 FAILURE LOGGING
# ============================================================

def _fail(call, win):
    """Log a failed Win32 call by name and GetLastError() code only."""
    try:
        code = int(win.last_error())
    except Exception:
        code = -1
    print(f"[FloatingOrb] {call} failed (error {code})")


# ============================================================
# ORB WINDOW HARDENING
# ============================================================

def harden(hwnd, win=None):
    """Exclude ``hwnd`` from capture, make it non-activating and taskbar-free,
    place it inside the primary work area and cut it to a circle. Returns
    {"capture": "excluded" | "black" | "none", "noactivate": bool,
    "round": bool, "position": (x, y, w, h, scale) | None}; never raises."""
    out = {"capture": "none", "noactivate": False, "round": False, "position": None}
    try:
        win = win or Win32()
    except Exception as e:
        print(f"[FloatingOrb] Win32 unavailable ({type(e).__name__})")
        return out
    try:
        if orb_debug_capture():
            out["capture"] = "debug-visible"
            print("[FloatingOrb] capture exclusion off (MISTAI_ORB_DEBUG_CAPTURE)")
        elif win.set_affinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
            out["capture"] = "excluded"
        elif win.set_affinity(hwnd, WDA_MONITOR):
            out["capture"] = "black"
        else:
            _fail("SetWindowDisplayAffinity", win)
    except Exception as e:
        print(f"[FloatingOrb] SetWindowDisplayAffinity error ({type(e).__name__})")
    out["noactivate"] = hide_from_taskbar(hwnd, win, "FloatingOrb")
    scale = 1.0
    side = None
    try:
        monitor, work = win.primary_work_area()
        scale = win.monitor_scale(monitor, hwnd) or 1.0
        x, y, side = place(work, scale)
        if not win.move(hwnd, x, y, side, side):
            _fail("SetWindowPos", win)
    except Exception as e:
        print(f"[FloatingOrb] placement error ({type(e).__name__})")
    try:
        rect = win.window_rect(hwnd)       # the ACTUAL size after the move
        if rect is None:
            _fail("GetWindowRect", win)
            return out
        l, t, r, b = rect
        out["position"] = (l, t, r - l, b - t, round(scale, 2))
        req = f"{side},{side}" if side else "none"
        print(f"[FloatingOrb] at {l},{t} requested {req} actual {r - l},{b - t} "
              f"monitor-scale {scale:.2f}")
        if side and (r - l, b - t) != (side, side):
            print("[FloatingOrb] actual size differs from the requested size")
        region = win.create_ellipse(ellipse_box(r - l, b - t))
        if not region:
            _fail("CreateEllipticRgn", win)
        elif win.set_region(hwnd, region):
            out["round"] = True             # the system owns the region now
        else:
            _fail("SetWindowRgn", win)
            win.delete_object(region)
    except Exception as e:
        print(f"[FloatingOrb] region error ({type(e).__name__})")
    return out


# ============================================================
# CREATE-TIME CORNER POSITION
# ============================================================

def _corner_position(size=None, win=None):
    """Create-time (x, y) in LOGICAL px near the work area's bottom-right
    corner. pywebview multiplies create-time x/y by the display scale, so the
    physical spot from place() is divided by it; in a DPI-unaware context the
    scale reads 1 and the work area is already logical. (None, None) on any
    error. harden() places the window exactly once it is shown."""
    try:
        win = win or Win32()
        monitor, work = win.primary_work_area()
        scale = win.monitor_scale(monitor) or 1.0
        x, y, _side = place(work, scale, size)
        return int(round(x / scale)), int(round(y / scale))
    except Exception:
        return None, None


# ============================================================
# ORB CONTROLLER
# ============================================================

class FloatingOrb:
    """Creates the orb window on first show; hide() keeps it for the next show."""

    def __init__(self, api, webview=None, html=None):
        self._api = api
        self._webview = webview
        self._html = html
        self.window = None
        self.hwnd = None
        self.hardening = None
        self.visible = False
        self.square = None        # (x, y, side) physical px, as last applied
        self.home = None          # the big square at home
        self.scale = 1.0
        self._win = None
        self._tween = None
        self._loop_stop = threading.Event()
        self.dock = None          # side docked on ("right", ...) or None
        self._logged_dock = None
        self.click_through = False
        self._cur_target = None   # this tick's target dict or None
        self._logged_target = None
        self.panels = None        # core.orb_panels.OrbPanels once used
        self.aborted = False      # the task failed, paused or was stopped
        self._was_busy = False
        self._busy_since = 0.0
        self._kick = threading.Event()     # tick now (a step just ran)
        self._fast_until = 0.0             # poll faster after a step
        self._acted = False                # the task has run an action
        self._last_action = ""             # its most recent action id
        self._pre_task_fg = None           # the window in front when it started
        self._dwell_until = 0.0            # hold a dock until then (minimum dwell)
        self._tour = None                  # {"stops": [...], "i", "next_at"} while touring
        self._tour_gen = 0                 # bumped by every step: stale tours are dropped
        self._answer = None                # {"at", "words"} from the final AgentStep
        self._clock = time.monotonic
        self._sleep = time.sleep           # the tour's one re-read (tests: no-op)

    TICK_S = 0.25
    DWELL_S = 1.5             # a docked arrival stays put this long
    FAST_TICK_S = 0.1
    LAUNCH_POLL_S = 2.0

    def _flight_on(self):
        try:
            return bool(self._api.orb_option("orb_flight"))
        except Exception:
            return False

    def _busy(self):
        try:
            return self._api._agent_lock.locked()
        except Exception:
            return False

    def _element_rect(self, window_rect, scale):
        """The rect the current task's last click touched (a UIA control's
        rect, else a box around the click point), if it was made during this
        task and lies in ``window_rect``; else None."""
        best = None
        for owner in (getattr(self._api, "click_handler", None),
                      getattr(self._api, "uia_nav", None)):
            got = getattr(owner, "last_element_rect", None)
            if got and (best is None or got[1] > best[1]):
                best = got
        if not best or best[1] < self._busy_since:
            return None
        rect = tuple(int(v) for v in best[0])
        if len(rect) == 2:                           # a click point
            pad = round(ELEMENT_PAD * scale)
            rect = (rect[0] - pad, rect[1] - pad, rect[0] + pad, rect[1] + pad)
        wl, wt, wr, wb = window_rect
        cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        return rect if wl <= cx <= wr and wt <= cy <= wb else None

    def _ignored(self, hwnd):
        """MistAI's own windows (main window, orb, panels, its PID), the
        taskbar/desktop shell and Task Manager are never a target."""
        if not hwnd or hwnd in self.handles():
            return True
        try:
            if hwnd == self._api._own_window_handle():
                return True
        except Exception:
            pass
        pid, name, cls = self._win.window_info(hwnd)
        if cls in SHELL_CLASSES:
            return True
        from .config import is_self_process, process_matches

        try:
            if is_self_process(name, pid):
                return True
        except Exception:
            pass
        return any(process_matches(p, name) for p in IGNORED_PROCESSES)

    def _target(self):
        """(target dict or None, reason label). Only after the task's first
        action. browser_* steps: MistAI's own browser window ("browser").
        Otherwise the window in front ("foreground"), unless it is one of
        ours, the shell or Task Manager ("no-window"), or it is the same
        window that was in front before the task and not the app the step
        opened or switched to ("unrelated-window": the user's own window).
        {"rect", "work", "scale", "inside", "element"}; a window covering
        MAXIMIZED of its work area is docked inside, beside the element the
        step touched. Quiet: it runs up to ten times a second."""
        if not self._acted:
            return None, "no-action-yet"
        why = "foreground"
        hwnd = None
        if self._last_action.startswith("browser_"):
            hwnd = self._win.browser_window()
            why = "browser"
        if not hwnd:
            why = "foreground"
            hwnd = self._win.foreground()
            if self._ignored(hwnd):
                return None, "no-window"
            app = getattr(self._api, "_last_opened_app", None)
            touched = False
            if app:
                try:
                    touched = bool(self._api._window_belongs_to(hwnd, app))
                except Exception:
                    touched = False
            if hwnd == self._pre_task_fg and not touched:
                return None, "unrelated-window"
        rect = self._win.window_rect(hwnd)
        if not rect:
            return None, "no-rect"
        work, scale = self._win.monitor_for_rect(rect)
        scale = scale or 1.0
        target = {"rect": tuple(rect), "work": tuple(work), "scale": scale,
                  "inside": coverage(rect, work) >= MAXIMIZED, "element": None}
        if not target["inside"]:
            return target, why
        target["element"] = self._element_rect(rect, scale)
        return target, ("maximized-element" if target["element"] else "maximized-edge")

    def _log_target(self, target, why):
        key = (why, target and target["rect"], target and target["element"])
        if key != self._logged_target:
            self._logged_target = key
            if target:
                l, t, r, b = target["rect"]
                el = target["element"]
                el_txt = f" element {el[0]},{el[1]},{el[2]},{el[3]}" if el else ""
                print(f"[FloatingOrb] target {why} rect {l},{t},{r},{b}{el_txt}")
            else:
                print(f"[FloatingOrb] target none ({why})")

    def _here_scale(self):
        try:
            x, y, s = self.square
            return self._win.monitor_for_rect((x, y, x + s, y + s))[1] or self.scale
        except Exception:
            return self.scale

    def _goal(self, state, from_square=None):
        """(x, y, side) the orb should have now. Without flight: at home,
        resized about its center for the state. With flight (Settings "Orb
        follows the task"): docked small beside the task's window while one
        is known and in front, medium where it is while working or speaking,
        else big at home. ``from_square``: judge from there instead of the
        current square (a flight's destination, so a goal re-checked in
        flight doesn't wander with the orb)."""
        x, y, side = from_square or self.square
        self.dock = None
        if not self._flight_on():
            return resize_about_center(x, y, side, round(size_for(state) * self.scale))
        if self.aborted or self._stop_requested():
            self._answer = None
            return self.home                         # failed, paused or stopped: home now
        if self._answer_speaking():
            panel = self.panels.rects.get("answer") if self.panels is not None else None
            medium = round(orb_sizes()[1] * self._here_scale())
            if panel:                                # beside the answer panel, speaking
                self.dock = "answer"
                work, _scale = self._win.monitor_for_rect(panel)
                return answer_spot(panel, medium, work) + (medium,)
            return resize_about_center(x, y, side, medium)
        busy = self._busy()
        target = self._cur_target
        _big, medium, small_l = orb_sizes()
        if target:
            rect, work, scale = target["rect"], target["work"], target["scale"]
            small = max(1, round(small_l * scale))
            if target["inside"]:
                self.dock = "element" if target["element"] else "inner-edge"
                pos = dock_inside(rect, work, small, target["element"],
                                  near=(x + side / 2, y + side / 2))
            else:
                self.dock = dock_side(rect, work, small, 8)
                pos = dock_position(rect, work, small)
            return pos + (small,)
        if busy or state == "speaking":
            # No target yet (or it left the front): stay where it is.
            return resize_about_center(x, y, side, round(medium * self._here_scale()))
        return self.home

    def _answer_speaking(self):
        """Is the final answer being spoken? The TTS's own flag when it has
        one (plus a moment before the voice starts), else an estimate from
        the word count, capped. Clears the answer once it is over."""
        a = self._answer
        if a is None:
            return False
        elapsed = self._clock() - a["at"]
        tts = getattr(self._api, "tts", None)
        if tts is not None and hasattr(tts, "speaking"):
            on = bool(tts.speaking) or elapsed < SPEECH_START_S
        else:
            on = elapsed < min(SPEECH_CAP_S, max(SPEECH_START_S, a["words"] / WORDS_PER_S))
        if not on:
            self._answer = None
        return on

    def _stop_requested(self):
        try:
            return self._api._stop_requested.is_set()
        except Exception:
            return False

    def _on_bus(self, ev):
        """Bus subscriber (fast): a step ran -> tick now (and poll faster
        after a launch); a task failed or paused, or the network sentence ->
        go home."""
        try:
            name, data = ev.name, ev.data or {}
            if name in (events.ACTION_EXECUTED, events.ACTION_FAILED):
                # After every step: re-check now, then every 0.1 s for a while
                # (the foreground can change a moment later, e.g. a launch).
                self._acted = True
                self._last_action = str(data.get("action", ""))
                self._fast_until = time.monotonic() + self.LAUNCH_POLL_S
            if name == events.AGENT_STEP:
                self._cancel_tour()                  # the next real action or "done"
                if data.get("action") == "done" and data.get("speech"):
                    # Only the length is kept (for the estimate), never the text.
                    self._answer = {"at": self._clock(),
                                    "words": len(str(data["speech"]).split())}
            if (name == events.ACTION_EXECUTED and data.get("action") == "browser_search"
                    and self._flight_on()):
                gen = self._tour_gen
                threading.Thread(target=self._prepare_tour, args=(gen,), daemon=True,
                                 name="OrbTour").start()
            if name in (events.TASK_FAILED, events.TASK_PAUSED) or (
                    name == events.AGENT_STEP and data.get("action") == "failed"):
                self.aborted = True
            self._kick.set()
        except Exception:
            pass

    # -- the search tour ---------------------------------------------------------
    # Choreography only: while the agent waits on its next model call after a
    # browser_search, the orb hops down the first results. It does NOT mean the
    # results were read: it adds no step, tick or event, never delays an action,
    # and the next AgentStep (a real action or "done") cancels it.

    def _cancel_tour(self):
        self._tour_gen += 1
        if self._tour is not None:
            self._tour = None
            try:
                if not getattr(self._tween, "in_flight", False):
                    self._tween.cancel()             # in flight: tick() retargets it instead
            except Exception:
                pass

    def _read_tour_rects(self):
        """(screen rects or None, counts or None, browser window rect)."""
        try:
            info = self._api.browser.link_boxes(RESULT_LINKS, TOUR_CANDIDATES)
            hwnd = self._win.browser_window()
            window = self._win.window_rect(hwnd) if hwnd else None
            if not (info and window):
                return None, None, window
            rects, counts = classify_page_rects(info, window, TOUR_STOPS)
            return rects, counts, window
        except Exception:
            return None, None, None

    def _prepare_tour(self, gen):
        """Worker thread: read the result link boxes and build the stops; no
        usable rect skips the tour (the normal docking stays). Fewer results
        than stops found: the page may still be rendering, so it reads once
        more after TOUR_RETRY_S. Logs counts and fixed labels only."""
        rects, counts, window = self._read_tour_rects()
        if counts and counts["found"] < TOUR_STOPS and gen == self._tour_gen:
            self._sleep(TOUR_RETRY_S)
            if gen == self._tour_gen:
                again = self._read_tour_rects()
                if again[1] and again[1]["found"] >= counts["found"]:
                    rects, counts, window = again
        if counts:
            why = ", ".join(f"{k} {counts[k]}" for k in TOUR_REJECTIONS if counts.get(k))
            print(f"[FloatingOrb] tour candidates found {counts['found']} measured "
                  f"{counts['measured']} accepted {counts['accepted']} rejected "
                  f"{counts['rejected']}" + (f" ({why})" if why else ""))
        if gen != self._tour_gen:
            return                                   # a step arrived meanwhile
        if not rects:
            print("[FloatingOrb] tour skipped (no-rects)")
            return
        work, scale = self._win.monitor_for_rect(window)
        _big, medium, small = orb_sizes()
        small, medium = round(small * (scale or 1.0)), round(medium * (scale or 1.0))
        area = (max(window[0], work[0]), max(window[1], work[1]),
                min(window[2], work[2]), min(window[3], work[3]))
        stops = [margin_spot(r, area, small) + (small,) for r in rects]
        self._tour = {"stops": stops, "i": 0, "next_at": 0.0, "medium": medium}
        print(f"[FloatingOrb] tour stops {len(stops)}")
        self._kick.set()

    def _tour_step(self):
        """Play the next hop when the last one has dwelt; True while touring."""
        tour = self._tour
        if tour is None:
            return False
        if self._clock() < tour["next_at"]:
            return True
        if tour["i"] >= len(tour["stops"]):
            self._tour = None
            return False
        stop = tour["stops"][tour["i"]]
        plan = tour_plan(self.square, stop, tour["medium"])
        tour["i"] += 1
        tour["next_at"] = self._clock() + sum(s[2] for s in plan) + TOUR_DWELL_S   # estimate
        self.dock = "tour"
        self._tween.play(plan, on_frame=self._set_square,
                         done=lambda: self._hop_landed(tour))
        return True

    def _hop_landed(self, tour):
        """Tween thread: a hop has landed; dwell from now."""
        if self._tour is tour:
            tour["next_at"] = self._clock() + TOUR_DWELL_S

    def _landed(self):
        """Tween thread: a flight has landed; a dock dwells from now (finish,
        then dwell)."""
        if self.dock and self.dock != "tour" and not (self.aborted or self._stop_requested()):
            self._dwell_until = self._clock() + self.DWELL_S

    def _follow_in_flight(self):
        """In flight and the goal moved (a new dock, the tour cancelled):
        retarget the spring, keeping its position and velocity, instead of
        waiting for it to land. The goal is judged from the destination."""
        tw = self._tween
        if self._tour is not None or not getattr(tw, "in_flight", False):
            return
        dest = getattr(tw, "goal", None)
        if not dest:
            return
        goal = self._goal(orb_state(self._api), from_square=dest)
        if max(abs(a - b) for a, b in zip(goal, dest)) <= 2:
            return
        if tw.retarget(goal) and self.dock != self._logged_dock:
            self._logged_dock = self.dock
            print(f"[FloatingOrb] dock {self.dock or 'none'} size {goal[2]} (retarget)")

    def _sync_panels(self, busy):
        """Task panels (Settings "Task panels"): created on first use, placed
        beside the orb away from the target, hidden when the option is off."""
        try:
            on = bool(self._api.orb_option("orb_panels"))
        except Exception:
            on = False
        if not on:
            if self.panels is not None:
                self.panels.hide_all()
            return
        if self.panels is None:

            self.panels = OrbPanels(self._api, self._win)
        if self._cur_target:                          # the target's monitor
            t = self._cur_target
            rect, work, scale, maximized = t["rect"], t["work"], t["scale"], t["inside"]
        else:                                         # else the primary monitor
            rect, maximized = None, False
            monitor, work = self._win.primary_work_area()
            scale = self._win.monitor_scale(monitor)
        speaking = bool(getattr(getattr(self._api, "tts", None), "speaking", False))
        self.panels.update(self.square, rect, work, scale or self.scale, busy,
                           maximized=maximized, ended=self.aborted or self._stop_requested(),
                           speaking=speaking)

    def _sync_click_through(self):
        """Click-through while flight is on and the agent acts, so the orb can
        never swallow one of its clicks; restored afterwards."""
        want = self._flight_on() and self._busy()
        if want != self.click_through:
            try:
                self._win.set_click_through(self.hwnd, want)
                self.click_through = want
                print(f"[FloatingOrb] click-through {'on' if want else 'off'}")
            except Exception as e:
                print(f"[FloatingOrb] click-through error ({type(e).__name__})")

    def _adopt_drag(self):
        """The user dragged the orb: take the real rect as the square, and as
        the new home while the orb is at home."""
        try:
            rect = self._win.window_rect(self.hwnd)
        except Exception:
            rect = None
        if not rect:
            return
        l, t, r, b = rect
        real = (l, t, r - l)
        if real != self.square:
            at_home = self.square == self.home
            self.square = real
            if at_home and not self._busy():
                self.home = real

    def tick(self):
        """One controller step: tween towards the goal for the current state.
        Never raises."""
        try:
            if not (self.visible and self.square and self._tween):
                return
            busy = self._busy()
            if busy and not self._was_busy:          # a new task
                self.aborted = False
                self._answer = None
                self._busy_since = time.monotonic()
                self._acted, self._last_action = False, ""
                try:
                    self._pre_task_fg = self._win.foreground()
                except Exception:
                    self._pre_task_fg = None
            self._was_busy = busy
            self._sync_click_through()
            try:
                self._cur_target, why = self._target() if busy else (None, "idle")
            except Exception:
                self._cur_target, why = None, "error"
            if busy and self._flight_on():
                self._log_target(self._cur_target, why)
            self._sync_panels(busy)
            if self._tween.running:
                self._follow_in_flight()
                return
            if busy and self._tour_step():
                return                               # touring the results
            if self._clock() < self._dwell_until and not (self.aborted
                                                          or self._stop_requested()):
                return                               # minimum dwell at a dock
            self._adopt_drag()
            goal = self._goal(orb_state(self._api))
            if self.dock != self._logged_dock:
                self._logged_dock = self.dock
                print(f"[FloatingOrb] dock {self.dock or 'none'} size {goal[2]}")
            if max(abs(a - b) for a, b in zip(goal, self.square)) <= 2:
                return
            plan = flight_plan(self.square, goal)
            if self.dock:                            # a flight to a dock: finish, then dwell
                # (an estimate now; _landed() restarts the dwell at the landing)
                self._dwell_until = self._clock() + sum(s[2] for s in plan) + self.DWELL_S
            self._tween.play(plan, on_frame=self._set_square, done=self._landed)
        except Exception as e:
            print(f"[FloatingOrb] tick error ({type(e).__name__})")

    def _set_square(self, sq):
        self.square = sq

    def _run(self):
        while not self._loop_stop.is_set():
            fast = time.monotonic() < self._fast_until
            self._kick.wait(self.FAST_TICK_S if fast else self.TICK_S)
            self._kick.clear()
            self.tick()

    def _module(self):
        if self._webview is None:
            import webview

            self._webview = webview
        return self._webview

    def show(self):
        """Show the orb (create it the first time). Returns True on success."""
        try:
            if self.window is None:
                html = self._html
                if html is None:
                    from ui.html import ORB_HTML as html
                x, y = _corner_position()
                self.window = self._module().create_window(
                    "", html=html, js_api=OrbBridge(self._api),
                    width=orb_sizes()[0], height=orb_sizes()[0], x=x, y=y,
                    min_size=MIN_SIZE,
                    resizable=False, frameless=True, easy_drag=True, shadow=False,
                    focus=False, on_top=True, background_color=orb_background(),
                )
                self.window.events.shown += self._on_shown
            else:
                self.window.show()
            self.visible = True
            return True
        except Exception as e:
            print(f"[FloatingOrb] could not show the orb ({type(e).__name__})")
            self.visible = False
            return False

    def hide(self):
        if self.window is not None:
            try:
                self.window.hide()
            except Exception:
                pass
        self.visible = False
        if self.panels is not None:
            self.panels.hide_all()

    def _on_shown(self):
        try:
            self.hwnd = int(self.window.native.Handle.ToInt32()) or None
        except Exception:
            self.hwnd = None
        if self.hwnd:
            self.hardening = harden(self.hwnd)
            print(f"[FloatingOrb] shown | capture: {self.hardening['capture']} | "
                  f"no-activate: {self.hardening['noactivate']} | round: {self.hardening['round']}")
            self._guard_clicks()
            pos = self.hardening.get("position")
            if pos and self._tween is None:
                x, y, w, h, scale = pos
                self.square = self.home = (x, y, w)
                self.scale = scale or 1.0
                try:
                    self._win = Win32()
                except Exception:
                    return
                self._tween = Tween(self._win, self.hwnd)
                self._tween.side = w
                try:
                    for name in (events.ACTION_EXECUTED, events.ACTION_FAILED,
                                 events.TASK_FAILED,
                                 events.TASK_PAUSED, events.AGENT_STEP):
                        self._api.bus.subscribe(name, self._on_bus)
                except Exception:
                    pass
                threading.Thread(target=self._run, daemon=True, name="OrbControl").start()

    def _guard_clicks(self):
        """Verify WS_EX_NOACTIVATE took and install the WM_MOUSEACTIVATE
        guard; logs labels only."""
        verified = False
        try:
            verified = bool(Win32().exstyle(self.hwnd) & WS_EX_NOACTIVATE)
        except Exception:
            pass

        def run_on_ui(fn):
            from System import Action  # pythonnet, as pywebview uses on Windows

            self.window.native.Invoke(Action(fn))

        guarded = guard_mouse_activate(self.hwnd, run_on_ui)
        print(f"[FloatingOrb] no-activate verified: {verified} | mouse-activate guard: "
              f"{'on' if guarded else 'off'}")

    def handles(self):
        """The orb's and its panels' window handles while shown (for the
        own-window OCR mask and the input-target refusal)."""
        if not (self.visible and self.hwnd):
            return []
        return [self.hwnd] + (self.panels.handles() if self.panels is not None else [])


# ============================================================
# TASK PANELS
# ============================================================

# core/orb_panels.py — task panels beside the floating orb (Settings "Task
# panels" / MISTAI_ORB_PANELS, default off; needs the floating orb shown).
#
# Three small frameless windows built like the orb (core/floating_orb.py):
# excluded from capture, never activated (WS_EX_NOACTIVATE), untitled, in
# MistAI's own process (so Open apps and process lookups skip them), listed in
# the orb's handles() (so the OCR mask blacks them out and input is refused
# while one is in front). Each page reads its content through its own
# PanelBridge: panel_state() (read-only) and dismiss() (hides that panel only).
#
# - steps: each step as a human label from the tool's category (label_for:
#   "Clicking", "Typing", "Reading the screen", ...), ticked as each one
#   completes (the last one is the current step); answer: the final speech.
#   They close ANSWER_S after the answer, and at once when the task fails, is
#   paused or stopped, or the network sentence is spoken.
#
# Content comes from the event bus (events.AGENT_STEP, ACTION_EXECUTED/FAILED)
# and is never logged; logs carry labels and numbers only.

# ============================================================
# TASK PANEL SETTINGS
# ============================================================

KINDS = ("steps", "answer")
SIZES = {"steps": (240, 176), "answer": (280, 140)}   # logical px
ANSWER_S = 8.0
MAX_STEPS = 8
CORNER = 12                  # logical px
PANEL_BACKGROUND = "#161c26"  # = the page's panel colour, so nothing else shows

# Tool categories (core/tools.py SPECS) -> labels, first match wins.
CATEGORY_LABELS = (
    ("perception", "Reading the screen"), ("click", "Clicking"), ("type", "Typing"),
    ("browser", "Using the browser"), ("app", "Opening an app"),
    ("window", "Arranging windows"), ("system", "Running a command"),
    ("file", "Working with files"), ("messaging", "Sending a message"),
    ("navigation", "Scrolling"), ("memory", "Remembering"), ("info", "Looking it up"),
)

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_TRANSIENTWINDOW = 3   # acrylic


# ============================================================
# STEP LABELS
# ============================================================

def label_for(action, categories=()):
    """A human label from the tool's categories ("click" -> "Clicking");
    "Working" when none is known. Never the action id or its parameter."""
    for cat, label in CATEGORY_LABELS:
        if cat in tuple(categories or ()):
            return label
    return "Working"


# ============================================================
# TOOL CATEGORY LOOKUP
# ============================================================

def tool_categories(api, action):
    try:
        return tuple(api.tools.get(action).categories or ())
    except Exception:
        return ()


# ============================================================
# PANEL MODEL
# ============================================================

class PanelModel:
    """What the panels show, fed by the bus (subscribers must be fast)."""

    def __init__(self, clock=time.monotonic, categories=None):
        self._lock = threading.Lock()
        self._clock = clock
        self._categories = categories or (lambda action: ())
        self.reset()

    def reset(self):
        with self._lock:
            self.steps = []           # [{"label", "action", "state": run|ok|fail}]
            self.answer = ""
            self.answer_at = None
            self.spoken_until = None  # when Mist last spoke the answer
            self.closed = set()
            self.ended = False        # failed, paused, stopped: close all now

    def on_event(self, ev):
        try:
            data = ev.data or {}
            with self._lock:
                if ev.name == events.AGENT_STEP:
                    action = str(data.get("action", ""))
                    if action == "failed":
                        self.ended = True
                    elif action == "done":
                        self.answer = str(data.get("speech", ""))
                        self.answer_at = self._clock()
                    else:
                        self.steps.append({"label": label_for(action, self._categories(action)),
                                           "action": action, "state": "run"})
                        del self.steps[:-MAX_STEPS]
                elif ev.name in (events.TASK_FAILED, events.TASK_PAUSED):
                    self.ended = True
                elif ev.name in (events.ACTION_EXECUTED, events.ACTION_FAILED):
                    action = str(data.get("action", ""))
                    for step in reversed(self.steps):
                        if step["state"] == "run" and step["action"] == action:
                            step["state"] = "ok" if ev.name == events.ACTION_EXECUTED else "fail"
                            break
        except Exception:
            pass

    def mark_speaking(self, speaking):
        """Panels close ANSWER_S after the answer's speech ENDS."""
        if speaking and self.answer_at is not None:
            self.spoken_until = self._clock()

    def expired(self):
        if self.answer_at is None:
            return False
        last = max(self.answer_at, self.spoken_until or self.answer_at)
        return self._clock() - last > ANSWER_S

    def visible(self, kind, busy):
        """Should this panel be on screen now?"""
        if self.ended or kind in self.closed or self.expired():
            return False
        if kind == "answer":
            return bool(self.answer)
        return bool(self.steps) and (busy or self.answer_at is not None)

    def state(self, kind):
        with self._lock:
            if kind == "steps":
                return {"kind": kind, "steps": [{"label": s["label"], "state": s["state"]}
                                                for s in self.steps]}
            return {"kind": kind, "text": self.answer}

    def close(self, kind):
        with self._lock:
            self.closed.add(kind)


# ============================================================
# PANEL PAGE BRIDGE
# ============================================================

class PanelBridge:
    """A panel page's js_api: its own content (read-only) and its own close."""

    def __init__(self, panels, kind):
        self._panels, self._kind = panels, kind

    def panel_state(self):
        st = self._panels.model.state(self._kind)
        st["side"] = self._panels.side
        return st

    def dismiss(self):
        self._panels.model.close(self._kind)
        return {"ok": True}


# ============================================================
# PANEL WINDOW HARDENING
# ============================================================

def harden_panel(hwnd, win, w, h, scale, blur):
    """Capture exclusion, no activation, rounded corners. Returns "blur" when
    the DWM backdrop took (Windows 11), else "plain" (a rounded region)."""


    try:
        if not orb_debug_capture() and not win.set_affinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
            win.set_affinity(hwnd, WDA_MONITOR)
    except Exception as e:
        print(f"[OrbPanels] hardening error ({type(e).__name__})")
    hide_from_taskbar(hwnd, win, "OrbPanels")
    if blur:
        try:
            if (win.dwm_set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_TRANSIENTWINDOW) == 0
                    and win.dwm_set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND) == 0):
                return "blur"
        except Exception:
            pass
    harden_corners(hwnd, win, w, h, scale)
    return "plain"


# ============================================================
# PANEL ROUNDED CORNERS
# ============================================================

def harden_corners(hwnd, win, w, h, scale):
    """Cut rounded corners for a window of this actual size."""
    try:
        r = max(2, round(CORNER * 2 * scale))
        region = win.create_round_rect((0, 0, int(w) + 1, int(h) + 1, r, r))
        if region and not win.set_region(hwnd, region):
            win.delete_object(region)
    except Exception as e:
        print(f"[OrbPanels] region error ({type(e).__name__})")


# ============================================================
# PANEL WINDOWS
# ============================================================

class OrbPanels:
    """The three panel windows. update() is called from the orb's control
    loop with the orb square, the target rect (or None) and the work area."""

    def __init__(self, api, win, webview=None, html=None, clock=time.monotonic):
        self._api, self._win = api, win
        self._webview, self._html = webview, html
        self.model = PanelModel(clock, lambda action: tool_categories(api, action))
        self.windows = {}             # kind -> pywebview window
        self.hwnds = {}               # kind -> hwnd once shown
        self.shown = {}               # kind -> bool
        self.side = "top-right"       # the anchor in use (the page slides in from above)
        self.rects = {}               # kind -> (l, t, r, b) while shown
        self.style = None
        self.sizes = {}               # kind -> actual (w, h) the corners were cut for
        self._was_busy = False
        self._unsubs = []
        try:
            bus = api.bus
            for name in (events.AGENT_STEP, events.ACTION_EXECUTED, events.ACTION_FAILED,
                         events.TASK_FAILED, events.TASK_PAUSED):
                self._unsubs.append(bus.subscribe(name, self.model.on_event))
        except Exception:
            pass

    def handles(self):
        return [h for k, h in self.hwnds.items() if h and self.shown.get(k)]

    def _module(self):
        if self._webview is None:
            import webview

            self._webview = webview
        return self._webview

    def _create(self, kind, x, y, w, h, scale):
        html = self._html
        if html is None:
            from ui.html import PANEL_HTML as html
        window = self._module().create_window(
            "", html=html, js_api=PanelBridge(self, kind),
            width=round(w / scale), height=round(h / scale),
            x=round(x / scale), y=round(y / scale),
            min_size=MIN_SIZE,
            resizable=False, frameless=True, easy_drag=False, shadow=False,
            focus=False, on_top=True, background_color=PANEL_BACKGROUND,
        )
        self.windows[kind] = window

        def shown():
            try:
                hwnd = int(window.native.Handle.ToInt32()) or None
            except Exception:
                hwnd = None
            if not hwnd:
                return
            self.hwnds[kind] = hwnd
            self._win.move(hwnd, x, y, w, h)
            aw, ah = self._actual(hwnd, w, h, kind)
            style = harden_panel(hwnd, self._win, aw, ah, scale, panel_blur_enabled())
            self.sizes[kind] = (aw, ah)
            if self.style is None:
                self.style = style
                print(f"[OrbPanels] panel style: {style}")

        window.events.shown += shown

    def _actual(self, hwnd, w, h, kind):
        """The window's real (w, h) after a move; logged next to the request."""
        try:
            l, t, r, b = self._win.window_rect(hwnd)
            aw, ah = r - l, b - t
        except Exception:
            return w, h
        if (aw, ah) != (w, h) or kind not in self.sizes:
            print(f"[OrbPanels] {kind} requested {w},{h} actual {aw},{ah}")
        return aw, ah

    def update(self, orb, target_rect, work, scale, busy, maximized=False, ended=False,
               speaking=False):
        """Show, place or hide the panels for this tick: anchored to the top of
        ``work`` (the target's monitor, else the primary), never over the orb.
        Never raises."""
        try:
            if busy and not self._was_busy:
                self.model.reset()
            self._was_busy = busy
            if ended:
                self.model.ended = True
            self.model.mark_speaking(speaking)
            if self.model.expired() and not busy:
                self.model.reset()
            kinds = [k for k in KINDS if self.model.visible(k, busy)]
            sizes = [tuple(round(v * scale) for v in SIZES[k]) for k in kinds]
            spots, self.side = anchored_panels(sizes, work, panel_anchor(), scale,
                                               target_rect, maximized, orb)
            for kind, (x, y), (w, h) in zip(kinds, spots, sizes):
                self.rects[kind] = (x, y, x + w, y + h)
                hwnd = self.hwnds.get(kind)
                if kind not in self.windows:
                    self._create(kind, x, y, w, h, scale)
                elif hwnd:
                    if self.sizes.get(kind) == (w, h):
                        self._win.move_only(hwnd, x, y)
                    else:                     # new size (another monitor): recut corners
                        self._win.move(hwnd, x, y, w, h)
                        aw, ah = self._actual(hwnd, w, h, kind)
                        harden_corners(hwnd, self._win, aw, ah, scale)
                        self.sizes[kind] = (aw, ah)
                    if not self.shown.get(kind):
                        self._win.show(hwnd, True)
                if not self.shown.get(kind):
                    print(f"[OrbPanels] show {kind}")
                self.shown[kind] = True
            for kind in KINDS:
                if kind not in kinds and self.shown.get(kind):
                    if self.hwnds.get(kind):
                        self._win.show(self.hwnds[kind], False)
                    self.shown[kind] = False
                    self.rects.pop(kind, None)
                    print(f"[OrbPanels] hide {kind}")
        except Exception as e:
            print(f"[OrbPanels] update error ({type(e).__name__})")

    def hide_all(self):
        for kind in KINDS:
            if self.shown.get(kind) and self.hwnds.get(kind):
                try:
                    self._win.show(self.hwnds[kind], False)
                except Exception:
                    pass
            self.shown[kind] = False
