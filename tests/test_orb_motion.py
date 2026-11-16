"""Smooth, lively orb motion: the spring flight (core/orb_motion.py), the frame
loop and pacing in core/floating_orb.Tween, frame stats, and the search
tour's candidate counting. Fake clocks and a fake Win32 only: nothing moves
on screen."""

import math
import sys
import threading
from types import SimpleNamespace

import pytest

from core import config
from core import floating_orb as fo
from core import orb_layout as ol
from core import orb_motion as om

DISTANCES = (12, 60, 180, 250, 600, 1200, 2600)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("MISTAI_ORB_MOTION", "MISTAI_ORB_FRAME_STATS", "MISTAI_ORB_SIZES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(fo.Tween, "_logged_animate", True)


def _fly(flight, dt=1 / 240, limit=3.0):
    """Sample a Flight every ``dt`` until it settles: (samples, settle time)."""
    t, out = flight.t0, []
    while t - flight.t0 < limit:
        x, y, done = flight.at(t)
        out.append((t, x, y))
        if done:
            return out, t - flight.t0
        t += dt
    raise AssertionError("never settled")


# -- the spring ----------------------------------------------------------------------------

@pytest.mark.parametrize("motion, bound", [("lively", 0.6), ("calm", 0.65)])
@pytest.mark.parametrize("dist", DISTANCES)
def test_the_spring_settles_exactly_within_the_bound(motion, bound, dist):
    f = om.Flight((100, 500), (100 + dist, 400), 0.0, motion, bow=0.3)
    samples, settle = _fly(f)
    assert settle <= bound
    assert samples[-1][1:] == (100.0 + dist, 400.0)             # snapped exactly


@pytest.mark.parametrize("dist", (100, 600, 2600))
def test_lively_overshoots_a_little_and_calm_does_not(dist):
    lively, _ = _fly(om.Flight((0, 0), (dist, 0), 0.0, "lively"))
    calm, _ = _fly(om.Flight((0, 0), (dist, 0), 0.0, "calm"))
    over = max(x for _t, x, _y in lively) - dist
    assert 1.0 < over <= om.OVERSHOOT_PX + 0.5 and over < 0.12 * dist
    assert max(x for _t, x, _y in calm) <= dist + 1e-6
    assert min(x for _t, x, _y in calm) >= -1e-6                   # no anticipation either


def test_long_lively_flights_lean_back_first_and_short_ones_do_not():
    long_, _ = _fly(om.Flight((0, 0), (800, 0), 0.0, "lively"))
    back = [x for t, x, _y in long_ if t <= om.ANTICIPATION_S]
    assert -om.ANTICIPATION_PX - 0.01 <= min(back) < -om.ANTICIPATION_PX * 0.9
    short, _ = _fly(om.Flight((0, 0), (150, 0), 0.0, "lively"))
    assert min(x for _t, x, _y in short) >= 0


def test_most_of_a_long_flight_is_done_fast():
    for motion, by in (("lively", 0.3), ("calm", 0.4)):
        f = om.Flight((0, 0), (1500, 0), 0.0, motion)
        assert f.position(by)[0] >= 0.9 * 1500


def test_the_arc_bows_upward_and_vanishes_at_both_ends():
    f = om.Flight((0, 800), (1000, 800), 0.0, "calm", bow=0.3)
    mids = [f.position(t)[1] for t in (0.1, 0.15, 0.2)]
    assert min(mids) < 800 - 40                                    # up (screen y grows down)
    assert f.position(0.0) == (0.0, 800.0)
    vertical = om.Flight((0, 0), (0, 900), 0.0, "calm", bow=0.3)
    assert all(abs(vertical.position(t)[0]) < 1e-9 for t in (0.05, 0.1, 0.2))


def test_retargeting_keeps_position_and_velocity():
    for motion in ("lively", "calm"):
        f = om.Flight((0, 0), (900, 300), 0.0, motion, bow=0.3)
        t = 0.14
        pos, vel = f.position(t), f.velocity(t)
        f.retarget(t, (200, 900))
        assert f.position(t) == pytest.approx(pos, abs=1e-6)
        assert f.velocity(t + 2e-5) == pytest.approx(vel, rel=0.02, abs=20)
        samples, settle = _fly(f)
        assert samples[-1][1:] == (200.0, 900.0) and settle <= om.MAX_FLIGHT_S


def test_a_retarget_has_no_jump_between_frames():
    f = om.Flight((0, 0), (1200, 0), 0.0, "lively", bow=0.3)
    dt, t, last, steps = 1 / 60, 0.0, None, []
    while t < 1.5:
        if abs(t - 0.2) < dt / 2:
            f.retarget(t, (1200, 700))
        x, y, done = f.at(t)
        if last is not None:
            steps.append(math.hypot(x - last[0], y - last[1]))
        last = (x, y)
        if done:
            break
        t += dt
    assert (x, y) == (1200.0, 700.0)
    around = steps[int(0.2 / dt) - 2:int(0.2 / dt) + 3]
    assert max(around) < 2.5 * max(steps[int(0.2 / dt) - 4:int(0.2 / dt) - 1])


def test_the_path_depends_on_time_not_on_the_frame_count():
    smooth = om.Flight((10, 700), (1400, 120), 5.0, "lively", bow=0.3)
    choppy = om.Flight((10, 700), (1400, 120), 5.0, "lively", bow=0.3)
    times = [5.0, 5.016, 5.1, 5.117, 5.3, 5.31, 5.45]          # dropped frames between
    for t in times:
        assert choppy.at(t)[:2] == pytest.approx(smooth.position(t), abs=1e-9)
    assert _fly(smooth)[0][-1][1:] == _fly(choppy)[0][-1][1:] == (1400.0, 120.0)


def test_spring_params_follow_the_motion_setting():
    zeta, omega = om.spring_params(600, "lively")
    assert 0.55 <= zeta <= 0.7 and om.LIVELY_MIN_OMEGA <= omega <= om.MAX_OMEGA
    assert om.spring_params(600, "calm")[0] == 1.0
    assert om.spring_params(2600, "lively")[1] >= om.spring_params(60, "lively")[1]


def test_the_motion_and_stats_flags_are_read_at_call_time(monkeypatch):
    assert config.orb_motion() == "lively"
    monkeypatch.setenv("MISTAI_ORB_MOTION", "Calm")
    assert config.orb_motion() == "calm"
    monkeypatch.setenv("MISTAI_ORB_MOTION", "bouncy")
    assert config.orb_motion() == "lively"
    assert config.orb_frame_stats() is False
    monkeypatch.setenv("MISTAI_ORB_FRAME_STATS", "1")
    assert config.orb_frame_stats() is True
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert config.orb_frame_stats() is False                     # ignored in a built app


# -- frame stats ----------------------------------------------------------------------------

def test_frame_stat_math():
    times = [0.0, 0.016, 0.033, 0.050, 0.100, 0.116]           # one 50 ms gap
    s = om.frame_stats(times, 60)
    assert s["frames"] == 6 and s["duration_ms"] == 116.0
    assert s["mean_ms"] == pytest.approx(23.2) and s["max_ms"] == 50.0
    assert s["p95_ms"] == 50.0 and s["slow"] == 1 and s["refresh"] == 60
    s144 = om.frame_stats(times, 144)                          # 1.5 x 6.9 ms = 10.4 ms
    assert s144["slow"] == 5 and s144["refresh"] == 144
    assert om.frame_stats([1.0], 60)["frames"] == 1 and om.frame_stats([], 60)["mean_ms"] == 0
    line = om.stats_line(s, "dwm", "lively")
    assert line.startswith("[FloatingOrb] frame stats frames 6 duration 116 ms interval mean 23.2")
    assert line.endswith("slow 1 refresh 60 Hz pacer dwm motion lively")


@pytest.mark.parametrize("raw, hz", [(144, 144), (59.94, 60), (0, 60), (1, 60), (None, 60),
                                     ("x", 60), (1000, 60), (240, 240)])
def test_the_refresh_rate_falls_back_to_60(raw, hz):
    assert om.valid_refresh(raw) == hz


def test_win32_refresh_rate_falls_back_when_the_read_fails(monkeypatch):
    import win32api

    monkeypatch.setattr(win32api, "MonitorFromWindow",
                        lambda *a: (_ for _ in ()).throw(OSError("no")))
    assert fo.Win32().refresh_rate(1) == 60
    monkeypatch.setattr(win32api, "MonitorFromWindow", lambda *a: 5)
    monkeypatch.setattr(win32api, "GetMonitorInfo", lambda m: {"Device": "D1"})
    monkeypatch.setattr(win32api, "EnumDisplaySettings",
                        lambda dev, mode: SimpleNamespace(DisplayFrequency=1))
    assert fo.Win32().refresh_rate(1) == 60
    monkeypatch.setattr(win32api, "EnumDisplaySettings",
                        lambda dev, mode: SimpleNamespace(DisplayFrequency=165))
    assert fo.Win32().refresh_rate(1) == 165


# -- the frame loop (fake clock, fake Win32) ------------------------------------------------

class _Clock:
    """Returns scripted times, then keeps stepping by ``step``."""

    def __init__(self, script=(), step=1 / 60, start=0.0):
        self.script, self.step, self.t = list(script), step, start

    def __call__(self):
        if self.script:
            self.t = self.script.pop(0)
        else:
            self.t += self.step
        return self.t


class _Pacer:
    kind = "fake"

    def __init__(self):
        self.waits = self.closed = 0

    def wait(self):
        self.waits += 1

    def close(self):
        self.closed += 1


class _MoveWin:
    def __init__(self, animations=True, hz=60):
        self.calls, self.animations, self.hz = [], animations, hz
        self.pacer = _Pacer()
        self.prio = []

    def move_only(self, hwnd, x, y):
        self.calls.append(("move_only", (x, y)))
        return True

    def move(self, hwnd, x, y, w, h):
        self.calls.append(("move", (x, y, w)))
        return True

    def create_ellipse(self, box):
        self.calls.append(("ellipse", box))
        return 1

    def set_region(self, hwnd, region):
        return True

    def delete_object(self, h):
        pass

    def animations_enabled(self):
        return self.animations

    def refresh_rate(self, hwnd):
        if self.hz is None:
            raise OSError("no")
        return self.hz

    def make_pacer(self, hz):
        self.pacer.hz = hz
        return self.pacer

    def boost_thread(self):
        self.prio.append("up")
        return 0

    def restore_thread(self, prev):
        self.prio.append(("restore", prev))


def _play(win, segments, clock, side=None):
    tw = fo.Tween(win, 7, clock=clock)
    tw.side = side if side is not None else segments[0][0][2]
    seen, done = [], []
    tw.play(segments, on_frame=lambda sq: seen.append((clock.t, sq)),
            done=lambda: done.append(1)).join(5)
    return tw, seen, done


def test_a_move_frame_is_one_setwindowpos_and_nothing_else():
    win = _MoveWin()
    tw, seen, done = _play(win, [((1700, 900, 35), (300, 200, 35), 0.5, 0.3)], _Clock())
    assert done == [1] and seen[-1][1] == (300, 200, 35)
    assert {c[0] for c in win.calls} == {"move_only"}            # no resize, no region
    assert win.pacer.waits >= 20 and win.pacer.closed == 1
    assert win.prio == ["up", ("restore", 0)]


def test_tween_frames_follow_the_clock_even_with_dropped_frames():
    script = [0.0, 0.016, 0.033, 0.21, 0.226, 0.4]               # 0.033 -> 0.21: dropped
    win = _MoveWin()
    tw, seen, done = _play(win, [((0, 600, 35), (1300, 100, 35), 0.5, 0.3)], _Clock(script))
    ref = om.Flight((0, 600), (1300, 100), 0.0, "lively", 0.3)
    for t, sq in seen:
        if t <= 0.4:
            x, y = ref.position(t)
            assert sq == (int(round(x)), int(round(y)), 35)
    assert seen[-1][1] == (1300, 100, 35)


def test_resizes_stay_short_timed_steps_before_take_off_and_after_landing():
    win = _MoveWin()
    plan = ol.flight_plan((1700, 900, 82), (300, 200, 28))
    tw, seen, done = _play(win, plan, _Clock())
    kinds = [c[0] for c in win.calls]
    first_move = kinds.index("move_only")
    assert "move" not in kinds[first_move:]                      # never resized in flight
    assert all(k in ("move", "ellipse") for k in kinds[:first_move])
    assert seen[-1][1] == (300, 200, 28)


def test_animations_off_jumps_to_each_segment_end(capsys):
    win = _MoveWin(animations=False)
    plan = ol.flight_plan((1700, 900, 82), (300, 200, 28))
    tw, seen, done = _play(win, plan, _Clock())
    assert [sq for _t, sq in seen] == [seg[1] for seg in plan]
    assert done == [1] and win.pacer.waits == 0
    assert capsys.readouterr().out.count("animation effects are off") == 1
    _play(win, plan, _Clock())
    assert "animation effects are off" not in capsys.readouterr().out   # once, not per flight


def test_retarget_redirects_the_move_in_flight_and_resizes_after_landing():
    gate, go = threading.Event(), threading.Event()

    class Clock(_Clock):
        def __call__(self):
            t = super().__call__()
            if t > 0.15 and not gate.is_set():
                gate.set()
                go.wait(2)
            return t

    win, clock = _MoveWin(), Clock()
    tw = fo.Tween(win, 7, clock=clock)
    tw.side = 35
    seen = []
    th = tw.play([((0, 0, 35), (1000, 0, 35), 0.5, 0.0)], on_frame=seen.append)
    assert gate.wait(2) and tw.in_flight
    assert tw.retarget((600, 600, 28)) is True and tw.goal == (600, 600, 28)
    go.set()
    th.join(5)
    assert seen[-1] == (600, 600, 28)
    moves = [sq for sq in seen if sq[2] == 35]
    steps = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(moves, moves[1:])]
    ref = om.Flight((0, 0), (1000, 0), 0.0, "lively")             # the same flight, no retarget
    ref_pts = [ref.position(i / 60) for i in range(40)]
    ref_max = max(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(ref_pts, ref_pts[1:]))
    assert max(steps) <= ref_max * 1.1 + 2                        # no jump at the retarget
    assert [sq[2] for sq in seen].index(28) > len(moves) - 1      # resized only after landing
    assert tw.retarget((1, 1, 35)) is False                       # nothing flying now


def test_frame_stats_are_logged_once_per_flight_only_when_asked(monkeypatch, capsys):
    win = _MoveWin(hz=None)                                       # refresh read fails: 60
    _play(win, [((0, 0, 35), (500, 0, 35), 0.5, 0.0)], _Clock())
    assert "frame stats" not in capsys.readouterr().out
    monkeypatch.setenv("MISTAI_ORB_FRAME_STATS", "1")
    tw, seen, done = _play(win, [((0, 0, 35), (500, 0, 35), 0.5, 0.0)], _Clock())
    out = [ln for ln in capsys.readouterr().out.splitlines() if "frame stats" in ln]
    assert len(out) == 1 and "refresh 60 Hz pacer fake motion lively" in out[0]
    assert tw.last_stats["frames"] >= 20 and tw.last_stats["slow"] == 0
    assert win.pacer.hz == 60


def test_calm_motion_is_used_when_set(monkeypatch):
    monkeypatch.setenv("MISTAI_ORB_MOTION", "calm")
    win = _MoveWin()
    tw, seen, done = _play(win, [((0, 0, 35), (900, 0, 35), 0.5, 0.0)], _Clock(step=1 / 240))
    assert max(sq[0] for _t, sq in seen) == 900 and min(sq[0] for _t, sq in seen) >= 0


def test_the_flight_boost_is_restored(monkeypatch):
    before = sys.getswitchinterval()
    prev = fo._boost_enter(_MoveWin())
    inner = fo._boost_enter(_MoveWin())
    assert sys.getswitchinterval() == pytest.approx(fo.FLIGHT_SWITCH_INTERVAL)
    fo._boost_exit(_MoveWin(), inner)
    assert sys.getswitchinterval() == pytest.approx(fo.FLIGHT_SWITCH_INTERVAL)
    fo._boost_exit(_MoveWin(), prev)
    assert sys.getswitchinterval() == pytest.approx(before)


def test_a_new_play_waits_for_the_old_thread():
    joined = []
    tw = fo.Tween(_MoveWin(), 7, clock=_Clock())
    tw._thread = SimpleNamespace(is_alive=lambda: True, join=lambda s: joined.append(s))
    tw.play([]).join(2)
    assert joined == [fo.Tween.JOIN_S]


def test_the_pacer_falls_back_from_dwmflush():
    class Now:
        t = 0.0

        def __call__(self):
            return self.t

    p = fo.FramePacer(60, clock=Now())
    p._dwm = lambda: 0                                            # returns at once: no vsync
    try:
        for _ in range(fo.FramePacer.FAST_LIMIT - 1):
            p.wait()
            assert p.kind == "dwm"
        p.wait()
        assert p.kind in ("timer", "sleep")
    finally:
        p.close()
    q = fo.FramePacer(60, clock=Now())
    q._dwm = lambda: -2147467259                                  # E_FAIL
    try:
        q.wait()
        assert q.kind in ("timer", "sleep")
    finally:
        q.close()


def test_the_win32_move_flags():
    assert fo.SWP_FRAME == (fo.SWP_NOSIZE | fo.SWP_NOZORDER | fo.SWP_NOACTIVATE
                            | fo.SWP_NOOWNERZORDER | fo.SWP_ASYNCWINDOWPOS)
    assert not fo.SWP_FRAME & 0x0100                              # no SWP_NOCOPYBITS


# -- the orb in flight ----------------------------------------------------------------------

def _flying_orb(goal_dest):
    from test_floating_orb import _flight_orb

    orb, played = _flight_orb()
    calls = []
    orb._tween = SimpleNamespace(running=True, in_flight=True, goal=goal_dest,
                                 retarget=lambda g: calls.append(g) or True,
                                 play=lambda *a, **k: played.append(a))
    return orb, played, calls


def test_a_goal_that_moves_in_flight_retargets_instead_of_waiting():
    orb, played, calls = _flying_orb((1008, 108, 35))             # already heading there
    orb.tick()
    assert calls == [] and played == []
    orb._win.target = (300, 300, 1200, 900)                       # the window moved
    orb.tick()
    assert calls and calls[-1] == (1208, 308, 35) and played == []


def test_no_retarget_during_a_tour_or_a_resize_step():
    orb, played, calls = _flying_orb((5, 5, 45))
    orb._tour = {"stops": [], "i": 0, "next_at": 0.0}
    orb.tick()
    orb._tour = None
    orb._tween.in_flight = False
    orb.tick()
    assert calls == []


def test_a_dock_dwell_starts_at_the_landing():
    orb, played, _calls = _flying_orb(None)
    now = [100.0]
    orb._clock = lambda: now[0]
    orb.dock = "right"
    now[0] = 103.0
    orb._landed()
    assert orb._dwell_until == pytest.approx(103.0 + orb.DWELL_S)
    orb.dock, orb._dwell_until = None, 0.0
    orb._landed()
    assert orb._dwell_until == 0.0                                # not a dock: no dwell


def test_a_cancelled_tour_in_flight_is_retargeted_not_stopped():
    orb, played, calls = _flying_orb((5, 5, 45))
    cancelled = []
    orb._tween.cancel = lambda: cancelled.append(1)
    orb._tour = {"stops": [], "i": 0, "next_at": 0.0}
    orb._cancel_tour()
    assert orb._tour is None and cancelled == []
    orb.tick()
    assert calls                                                  # flies on to the dock


# -- the search tour's candidates ------------------------------------------------------------

def _page(rects, found=None, inner_h=800):
    return dict(screenX=0, screenY=0, outerWidth=1216, innerWidth=1200, outerHeight=900,
                innerHeight=inner_h, dpr=1.25, rects=rects,
                found=len(rects) if found is None else found)


WINDOW = (-10, -10, 1930, 1300)


def test_candidates_are_counted_accepted_and_rejected_by_label():
    rects = [[20, -40, 600, -10, 0],          # above the fold
             [20, 100, 600, 124, 2],          # an ad
             [20, 150, 600, 174, 0],          # ok 1
             [20, 200, 600, 200, 0],          # zero height: hidden
             [20, 250, 600, 274, 1],          # hidden by style
             [20, 300, 600, 324, 0],          # ok 2
             [20, 400, 600, 424, 0],          # ok 3
             [20, 780, 600, 810, 0],          # ok 4: partly visible, clipped
             [20, 500, 600, 524, 0],          # limit
             [20, 900, 600, 924, 0]]          # below the fold
    got, counts = ol.classify_page_rects(_page(rects, found=14), WINDOW, 4)
    assert len(got) == 4 and counts["accepted"] == 4
    assert counts["found"] == 14 and counts["measured"] == 10 and counts["rejected"] == 6
    assert {k: counts[k] for k in ol.TOUR_REJECTIONS} == {
        "hidden": 2, "ad": 1, "above-fold": 1, "below-fold": 1, "outside-window": 0, "limit": 1}
    assert got[3][3] == round((100 + 800) * 1.25)                 # clipped to the viewport


def test_a_rect_outside_the_window_voids_the_tour():
    got, counts = ol.classify_page_rects(_page([[20, 100, 600, 124, 0]]), (0, 0, 300, 300))
    assert got is None and counts["outside-window"] == 1 and counts["accepted"] == 0


def test_bad_info_counts_nothing_and_never_raises():
    got, counts = ol.classify_page_rects({"dpr": "x"}, WINDOW)
    assert got is None and counts["found"] == 0 and counts["accepted"] == 0


def _tour_orb(*infos):
    from test_orb_fixes import _tour_orb as make

    orb, played, now = make(infos[0])
    seq = list(infos)
    orb._api.browser.link_boxes = lambda sel, n: seq.pop(0) if len(seq) > 1 else seq[0]
    slept = []
    orb._sleep = slept.append
    return orb, slept


FOUR = [[20, 100 + 90 * i, 600, 124 + 90 * i, 0] for i in range(4)]


def test_the_tour_logs_the_counts_and_keeps_up_to_four(capsys):
    orb, slept = _tour_orb(_page(FOUR + [[20, 700, 600, 724, 2]]))
    orb._prepare_tour(orb._tour_gen)
    out = capsys.readouterr().out
    assert len(orb._tour["stops"]) == 4 and slept == []
    assert ("[FloatingOrb] tour candidates found 5 measured 5 accepted 4 rejected 1 (ad 1)"
            in out)
    assert "tour stops 4" in out


def test_a_page_still_rendering_is_read_once_more(capsys):
    orb, slept = _tour_orb(_page(FOUR[:1]), _page(FOUR))
    orb._prepare_tour(orb._tour_gen)
    assert slept == [fo.TOUR_RETRY_S] and len(orb._tour["stops"]) == 4
    assert "found 4 measured 4 accepted 4" in capsys.readouterr().out


def test_nothing_usable_still_skips_the_tour(capsys):
    orb, slept = _tour_orb(_page([[20, 900, 600, 924, 0]] * 2))
    orb._prepare_tour(orb._tour_gen)
    out = capsys.readouterr().out
    assert orb._tour is None and "tour skipped (no-rects)" in out
    assert "accepted 0 rejected 2 (below-fold 2)" in out


def test_the_link_box_script_reads_no_text():
    js = fo.Win32 and __import__("core.browser_session", fromlist=["x"]).BrowserSession._LINK_BOXES_JS
    for word in ("innerText", "textContent", "innerHTML", "title"):
        assert word not in js
    assert "found:" in js and "f |= 2" in js
