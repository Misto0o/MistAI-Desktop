# ============================================================
# ORB BEHAVIOR MODULE
# ============================================================
#
# Pure behavior for the floating orb (core/floating_orb.py applies it): where
# the orb sits while pointing at a click target, when and where it drifts while
# idle, whether a fullscreen app hides it, and where the task panels go.
# No Win32, no threads, no logging: every input is a number, a rect or a fake
# detector, so all of it is tested directly (tests/test_orb.py ORB BEHAVIOR).
# Physical pixels throughout; rects are (l, t, r, b), right/bottom exclusive;
# the orb is a square (x, y, side).
#
# SECTION INDEX
#   1. CLAMPING
#   2. RECT OVERLAP
#   3. ORB SQUARE AS A RECT
#   4. POINTER SETTINGS
#   5. POINTER FLIGHT SETTINGS
#   6. POINTER FLIGHT PLAN
#   7. POINTER ARRIVAL CHECK
#   8. POINT TO RECT
#   9. CLICK POINT OF A RECT
#   10. ORB CLEAR OF THE CLICK POINT
#   11. POINTER SPOT BESIDE A TARGET
#   12. POINTER SKIP DECISION
#   13. CAPPED FLIGHT WAIT
#   14. DOCK CLEAR OF OBSTACLES
#   15. IDLE DRIFT SETTINGS
#   16. IDLE DRIFT TARGET
#   17. IDLE DRIFT FACTS
#   18. IDLE DRIFT ALLOWED
#   19. IDLE DRIFT PLANNER
#   20. FULLSCREEN SETTINGS
#   21. FULLSCREEN FACTS
#   22. FULLSCREEN RECT TEST
#   23. FULLSCREEN TEST
#   24. FULLSCREEN ACTION TABLE
#   25. FULLSCREEN WATCH
#   26. PANEL PRESETS
#   27. PANEL STACK RECTS
#   28. PANEL PLACEMENT SCORING
#   29. PANEL ANCHOR CHOICE (ONCE PER TASK)
#   30. PANEL DRAG SETTINGS
#   31. PANEL DRAG DETECTION
# ============================================================

import math
import random
from collections import namedtuple


# ============================================================
# CLAMPING
# ============================================================

def _clamp(v, lo, hi):
    return max(lo, min(v, hi)) if hi >= lo else lo


# ============================================================
# RECT OVERLAP
# ============================================================

def _overlaps(a, b):
    """Do rects a and b share a pixel?"""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


# ============================================================
# ORB SQUARE AS A RECT
# ============================================================

def square_rect(x, y, side):
    return (x, y, x + side, y + side)


# ============================================================
# POINTER SETTINGS
# ============================================================

# Before a click the orb flies beside the target and pulses once; the click
# waits for that flight at most POINT_WAIT_S, never longer.
POINT_WAIT_S = 0.35
POINT_GAP = 10            # logical px between the target and the orb
POINT_PAD = 24            # logical px around a bare click point used as its rect
POINT_MARGIN = 12         # logical px the orb keeps from the click point itself
POINT_HOLD_S = 1.5        # the orb stays beside the target this long after landing
POINT_SIDES = ("right", "left", "below", "above")
# Pointer flights use the "point" spring (core/floating_orb.py spring_params:
# damping 0.9): close in ~0.2 s with under 3 px of overshoot, so the orb
# never swings over the click point on the way in.
POINT_MOTION = "point"
POINT_ARRIVE_PX = 12      # logical px from its spot: "arrived", the click may go


# ============================================================
# POINTER FLIGHT SETTINGS
# ============================================================

# ============================================================
# POINTER FLIGHT PLAN
# ============================================================

POINT_RESIZE_S = 0.05     # the quick shrink to the small size before a pointer flight


def pointer_plan(start, goal, flight_s=0.5, bow=0.15):
    """Flight segments for a pointer flight: shrink quickly about the
    centre (POINT_RESIZE_S, not the usual 0.12 s), then one spring move at
    the small size; a goal at the same size is the move alone. (The
    segment's seconds are estimates; the spring keeps its own time.)"""
    (x0, y0, s0), (x1, y1, s1) = start, goal
    if s0 == s1:
        return [(tuple(start), tuple(goal), flight_s, bow)]
    shrunk = (int(round(x0 + (s0 - s1) / 2)), int(round(y0 + (s0 - s1) / 2)), s1)
    return [(tuple(start), shrunk, POINT_RESIZE_S, 0.0), (shrunk, tuple(goal), flight_s, bow)]


# ============================================================
# POINTER ARRIVAL CHECK
# ============================================================

def arrived(square, goal, px):
    """Is the orb within ``px`` of its pointer spot (same size)? The click
    goes then; the spring's last settling pixels need not hold it up."""
    if not square or not goal or square[2] != goal[2]:
        return False
    return math.hypot(square[0] - goal[0], square[1] - goal[1]) <= px


# ============================================================
# POINT TO RECT
# ============================================================

def point_rect(target, pad):
    """A rect for ``target``: (x, y) becomes a box of ``pad`` around the
    point; a 4-tuple is returned as ints. None for anything else."""
    try:
        vals = [int(round(float(v))) for v in target]
    except Exception:
        return None
    if len(vals) == 2:
        x, y = vals
        return (x - pad, y - pad, x + pad, y + pad)
    if len(vals) == 4 and vals[2] > vals[0] and vals[3] > vals[1]:
        return tuple(vals)
    return None


# ============================================================
# CLICK POINT OF A RECT
# ============================================================

def click_point(rect):
    """The point a click on ``rect`` lands on: its centre."""
    return ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)


# ============================================================
# ORB CLEAR OF THE CLICK POINT
# ============================================================

def clear_of_point(x, y, side, point, margin=0):
    """Is the click point outside the orb's square grown by ``margin``?"""
    px, py = point
    return not (x - margin <= px < x + side + margin and y - margin <= py < y + side + margin)


# ============================================================
# POINTER SPOT BESIDE A TARGET
# ============================================================

def _beside(where, rect, side, gap):
    l, t, r, b = rect
    cx, cy = (l + r) // 2 - side // 2, (t + b) // 2 - side // 2
    return {"right": (r + gap, cy), "left": (l - gap - side, cy),
            "below": (cx, b + gap), "above": (cx, t - gap - side)}[where]


def pointer_spot(target, work, side, gap=POINT_GAP, margin=POINT_MARGIN, avoid=()):
    """(x, y, where) for the orb (a square of ``side``) BESIDE ``target`` (the
    control's rect), never on the click point (its centre): the first side of
    POINT_SIDES (right, left, below, above) whose spot lies fully inside
    ``work`` and clear of the target and of ``avoid`` (the task panels);
    then the same sides clamped into ``work`` that keep the click point
    ``margin`` outside the orb; then the work area's corner farthest from the
    click point. None when even that covers the point (a work area hardly
    bigger than the orb): the caller then does not point."""
    wl, wt, wr, wb = (int(v) for v in work)
    side = int(side)
    if side < 1 or wr - wl < side or wb - wt < side:
        return None
    point = click_point(target)
    spots = [(w,) + _beside(w, target, side, gap) for w in POINT_SIDES]

    def ok(x, y, strict):
        sq = square_rect(x, y, side)
        if not clear_of_point(x, y, side, point, margin):
            return False
        if strict and (_overlaps(sq, target) or any(_overlaps(sq, a) for a in avoid)):
            return False
        return True

    for where, x, y in spots:                      # fits as is, clear of everything
        if wl <= x and x + side <= wr and wt <= y and y + side <= wb and ok(x, y, True):
            return x, y, where
    for strict in (True, False):                   # clamped into the work area
        for where, x, y in spots:
            cx, cy = _clamp(x, wl, wr - side), _clamp(y, wt, wb - side)
            if ok(cx, cy, strict):
                return cx, cy, where
    corners = [(wl, wt), (wr - side, wt), (wl, wb - side), (wr - side, wb - side)]
    corners.sort(key=lambda c: -math.hypot(c[0] + side / 2 - point[0], c[1] + side / 2 - point[1]))
    x, y = corners[0]
    if clear_of_point(x, y, side, point, 0):
        return x, y, "corner"
    return None


# ============================================================
# POINTER SKIP DECISION
# ============================================================

def pointer_skip_reason(shown, flight_on, pointer_on, animations=True):
    """Why the orb does NOT point before a click ("" = it points): the orb is
    hidden, flight (the Floating orb switch) is off, or the pointer setting
    (orb_pointer / MISTAI_ORB_POINTER) is off. Each of these skips at once,
    so the click is not delayed. Windows animation effects off still points
    (the orb jumps there, no wait)."""
    if not shown:
        return "hidden"
    if not flight_on:
        return "flight-off"
    if not pointer_on:
        return "pointer-off"
    return ""


# ============================================================
# CAPPED FLIGHT WAIT
# ============================================================

def capped_wait(wait, cap=POINT_WAIT_S, clock=None):
    """Wait for the pointer flight with ``wait(timeout) -> bool`` (an
    Event.wait), at most ``cap`` seconds. (landed, seconds waited)."""
    import time

    clock = clock or time.perf_counter
    cap = max(0.0, min(float(cap), POINT_WAIT_S * 2))
    t0 = clock()
    try:
        landed = bool(wait(cap))
    except Exception:
        landed = False
    return landed, max(0.0, clock() - t0)


# ============================================================
# DOCK CLEAR OF OBSTACLES
# ============================================================

def dock_clear_of(pos, side, work, obstacles, gap=8):
    """(x, y): ``pos`` for a square of ``side`` when it overlaps none of
    ``obstacles`` (the task panels); else the first spot beside an
    overlapping obstacle (below, above, left, right of it) that is inside
    ``work`` and clear of all of them; else ``pos`` unchanged."""
    x, y = pos
    obstacles = [tuple(o) for o in obstacles or () if o]
    if not any(_overlaps(square_rect(x, y, side), o) for o in obstacles):
        return pos
    wl, wt, wr, wb = (int(v) for v in work)
    for o in obstacles:
        if not _overlaps(square_rect(x, y, side), o):
            continue
        for cx, cy in ((x, o[3] + gap), (x, o[1] - gap - side),
                       (o[0] - gap - side, y), (o[2] + gap, y)):
            cx, cy = _clamp(cx, wl, wr - side), _clamp(cy, wt, wb - side)
            if not any(_overlaps(square_rect(cx, cy, side), p) for p in obstacles):
                return cx, cy
    return pos


# ============================================================
# IDLE DRIFT SETTINGS
# ============================================================

DRIFT_DELAY_S = (20.0, 60.0)     # idle this long (random) before a drift
DRIFT_DIST = (40, 100)           # logical px from home
DRIFT_PAUSE_S = (3.0, 6.0)       # rest at the drift spot, then return
DRIFT_FLIGHT_S = 1.2             # estimate of one calm drift flight
HOVER_PX = 150                   # logical px: a cursor this near the orb stops a drift


# ============================================================
# IDLE DRIFT TARGET
# ============================================================

def drift_target(home, work, rng=random, scale=1.0, dist=DRIFT_DIST):
    """(x, y, side): ``home`` moved a random ``dist`` (logical px x
    ``scale``) in a random direction, inside ``work`` (the work area: the
    taskbar is outside it). Tries a few directions for one that really moves
    at least the minimum distance after clamping; else home itself."""
    hx, hy, side = home
    wl, wt, wr, wb = (int(v) for v in work)
    lo, hi = dist[0] * scale, dist[1] * scale
    for _ in range(12):
        ang = rng.uniform(0.0, 2 * math.pi)
        d = rng.uniform(lo, hi)
        x = _clamp(int(round(hx + d * math.cos(ang))), wl, wr - side)
        y = _clamp(int(round(hy + d * math.sin(ang))), wt, wb - side)
        if math.hypot(x - hx, y - hy) >= lo - 1:
            return x, y, side
    return tuple(home)


# ============================================================
# IDLE DRIFT FACTS
# ============================================================

# enabled: the setting; idle: orb state "idle" (no task, nothing lingering);
# busy: a task runs; panels: a task panel is shown; fullscreen: see
# FULLSCREEN DECISION; grabbed: the user holds the orb; animations: Windows
# animation effects on (off = reduced motion: no drift).
DriftFacts = namedtuple("DriftFacts", "enabled idle busy panels fullscreen grabbed animations")


# ============================================================
# IDLE DRIFT ALLOWED
# ============================================================

def drift_allowed(f):
    return bool(f.enabled and f.idle and not f.busy and not f.panels
                and not f.fullscreen and not f.grabbed and f.animations)


# ============================================================
# IDLE DRIFT PLANNER
# ============================================================

class DriftPlanner:
    """When to drift and when to come back. update(now, facts, cursor_near)
    returns None (nothing to do), "go" (fly to a drift spot), "home" (fly
    back) or "stop" (the user grabbed the orb: stay where it is). Resting:
    after a random DRIFT_DELAY_S of allowed idle time it goes, unless the
    cursor is within HOVER_PX (``cursor_near()``, called only then), which
    restarts the wait. Away: it returns after a random DRIFT_PAUSE_S, or at
    once when anything stops being allowed or the cursor comes near.
    ``cursor_near`` is only called when due or while away, so resting costs
    nothing per tick."""

    def __init__(self, rng=None, delay=DRIFT_DELAY_S, pause=DRIFT_PAUSE_S):
        self._rng = rng or random.Random()
        self._delay_range, self._pause_range = delay, pause
        self.phase = "rest"
        self.idle_since = None
        self.delay = None
        self.away_until = 0.0

    def reset(self):
        self.phase = "rest"
        self.idle_since = None
        self.delay = None

    def update(self, now, facts, cursor_near=lambda: False):
        allowed = drift_allowed(facts)
        if self.phase == "away":
            if facts.grabbed:
                self.reset()
                return "stop"
            if not allowed or now >= self.away_until or cursor_near():
                self.reset()
                return "home"
            return None
        if not allowed:
            self.idle_since = None
            return None
        if self.idle_since is None:
            self.idle_since = now
            self.delay = self._rng.uniform(*self._delay_range)
            return None
        if now - self.idle_since < self.delay:
            return None
        if cursor_near():
            self.idle_since = now                   # the user is near: wait again
            self.delay = self._rng.uniform(*self._delay_range)
            return None
        self.phase = "away"
        self.away_until = now + DRIFT_FLIGHT_S + self._rng.uniform(*self._pause_range)
        return "go"


# ============================================================
# FULLSCREEN SETTINGS
# ============================================================

# SHQueryUserNotificationState results that mean a fullscreen app or a
# presentation: QUNS_BUSY (a fullscreen app), QUNS_RUNNING_D3D_FULL_SCREEN,
# QUNS_PRESENTATION_MODE.
QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4
FULLSCREEN_STATES = (QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE)
FS_POLL_S = 1.0          # checked from the orb's tick at most this often
FS_CONFIRM = 2           # readings in a row before the decision changes (no flapping)
FS_TOLERANCE = 2         # px a window may miss the monitor edge and still cover it


# ============================================================
# FULLSCREEN FACTS
# ============================================================

# covers: the foreground window (not ours, not the shell) covers its whole
# monitor rect (not just the work area); notification: the
# SHQueryUserNotificationState result or None when unknown.
FullscreenFacts = namedtuple("FullscreenFacts", "covers notification")


# ============================================================
# FULLSCREEN RECT TEST
# ============================================================

def covers_monitor(rect, monitor, tol=FS_TOLERANCE):
    """Does ``rect`` cover the whole ``monitor`` rect (the full screen, the
    taskbar's strip included)? A maximized window covers only the work area,
    so it is not fullscreen."""
    if not rect or not monitor:
        return False
    l, t, r, b = rect
    ml, mt, mr, mb = monitor
    return l <= ml + tol and t <= mt + tol and r >= mr - tol and b >= mb - tol


# ============================================================
# FULLSCREEN TEST
# ============================================================

def is_fullscreen(facts):
    return bool(facts and (facts.covers or facts.notification in FULLSCREEN_STATES))


# ============================================================
# FULLSCREEN ACTION TABLE
# ============================================================

def fullscreen_action(fullscreen, busy):
    """The decision table: fullscreen and no task -> "hide" (the orb window
    is hidden, never activated; shown again without activation when it
    ends); fullscreen during a task -> "stay" (visible, no drift);
    otherwise "show"."""
    if fullscreen and not busy:
        return "hide"
    return "stay" if fullscreen else "show"


# ============================================================
# FULLSCREEN WATCH
# ============================================================

class FullscreenWatch:
    """Polls ``detector() -> FullscreenFacts`` at most every FS_POLL_S and
    changes ``active`` only after FS_CONFIRM readings agree. A detector that
    raises counts as not fullscreen."""

    def __init__(self, detector, interval=FS_POLL_S, confirm=FS_CONFIRM):
        self._detector = detector
        self._interval, self._confirm = interval, confirm
        self._last = None
        self._run = 0
        self._seen = False
        self.active = False

    def poll(self, now):
        if self._last is not None and now - self._last < self._interval:
            return self.active
        self._last = now
        try:
            seen = is_fullscreen(self._detector())
        except Exception:
            seen = False
        self._run = self._run + 1 if seen == self._seen else 1
        self._seen = seen
        if seen != self.active and self._run >= self._confirm:
            self.active = seen
        return self.active


# ============================================================
# PANEL PRESETS
# ============================================================

PANEL_PRESETS = ("top-right", "top-left", "top-center", "bottom-right", "bottom-left")


# ============================================================
# PANEL STACK RECTS
# ============================================================
# Where a stack of panels would sit at one anchor, NOT clamped, so a stack
# that does not fit the work area shows up as rects outside it.

def panel_stack(sizes, work, anchor, inset, gap):
    """[(l, t, r, b)] per panel of ``sizes`` [(w, h)] stacked at ``anchor``
    of ``work``: top anchors stack down from the top inset, bottom anchors
    end at the bottom inset; left/right inset, or centered for top-center."""
    wl, wt, wr, wb = (int(v) for v in work)
    total = sum(h for _w, h in sizes) + gap * max(0, len(sizes) - 1)
    y = (wb - inset - total) if anchor.startswith("bottom") else wt + inset
    out = []
    for w, h in sizes:
        if anchor in ("top-left", "bottom-left"):
            x = wl + inset
        elif anchor == "top-center":
            x = (wl + wr) // 2 - w // 2
        else:
            x = wr - inset - w
        out.append((x, y, x + w, y + h))
        y += h + gap
    return out


# ============================================================
# PANEL PLACEMENT SCORING
# ============================================================

def _inside(rect, work):
    return rect[0] >= work[0] and rect[1] >= work[1] and rect[2] <= work[2] and rect[3] <= work[3]


def _area(a, b):
    """Overlap area of rects a and b (0 when apart)."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0, w) * max(0, h)


def _stack_overlap(rects, obstacles):
    return sum(_area(r, o) for r in rects for o in obstacles if o)


# ============================================================
# PANEL ANCHOR CHOICE (ONCE PER TASK)
# ============================================================
# At the start of each task the panel stack gets one anchor: every preset
# whose stack lies inside the target monitor's work area and touches none of
# the obstacles (the orb, the click target / element rect, the task's
# window) is a candidate; one is picked at random (``rng``, injectable), not
# the previous task's anchor when another candidate exists. With no
# candidate: the least-overlapping preset, scored first by overlap with the
# "hard" obstacles (orb, click target), then by overlap with the window, then
# by the part outside the work area; ties are again picked at random
# (avoiding the previous anchor). Returns (anchor, reason) with reason
# "random" | "only" | "fallback".

def choose_panel_anchor(stacks, work, hard=(), window=None, last=None, rng=None):
    """``stacks``: {anchor: [rects]} (panel_stack per preset, in preset
    order); ``hard``: rects that must stay uncovered (orb, element);
    ``window``: the task's window rect or None; ``last``: the previous task's
    anchor; ``rng``: a random.Random (a new one when None)."""
    rng = rng or random.Random()
    work = tuple(int(v) for v in work)
    hard = [tuple(h) for h in hard or () if h]
    soft = [tuple(window)] if window else []
    order = [a for a in PANEL_PRESETS if a in stacks] + [a for a in stacks if a not in PANEL_PRESETS]
    if not order:
        return PANEL_PRESETS[0], "fallback"

    def pick(cands):
        if len(cands) > 1 and last in cands:
            cands = [a for a in cands if a != last]
        return cands[0] if len(cands) == 1 else rng.choice(cands)

    clear = [a for a in order
             if all(_inside(r, work) for r in stacks[a])
             and _stack_overlap(stacks[a], hard + soft) == 0]
    if clear:
        return pick(clear), ("only" if len(clear) == 1 else "random")

    def outside(rects):
        return sum((r[2] - r[0]) * (r[3] - r[1]) - _area(r, work) for r in rects)

    score = {a: (_stack_overlap(stacks[a], hard), _stack_overlap(stacks[a], soft),
                 outside(stacks[a])) for a in order}
    best = min(score.values())
    return pick([a for a in order if score[a] == best]), "fallback"


# ============================================================
# PANEL DRAG SETTINGS
# ============================================================

DRAG_SETTLE_S = 0.6       # a dragged panel is adopted (this task only) once still this long


# ============================================================
# PANEL DRAG DETECTION
# ============================================================

def moved_by_user(applied, actual, tol=2):
    """Did the panel window move away from where MistAI last put it?"""
    if not applied or not actual:
        return False
    return max(abs(a - b) for a, b in zip(applied[:2], actual[:2])) > tol
