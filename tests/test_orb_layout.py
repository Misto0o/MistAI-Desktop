"""core/orb_layout.py: pure geometry for the floating orb and its panels
(physical px; rects are (l, t, r, b), the orb a square (x, y, side))."""

import pytest

from core import orb_layout as ol


def test_resize_about_center_rounds_to_whole_pixels():
    assert ol.resize_about_center(100, 100, 110, 94) == (108, 108, 94)
    assert ol.resize_about_center(100, 100, 110, 0) == (154, 154, 1)


def test_frames_end_exactly_on_the_goal_and_bow_upward():
    path = ol.frames((0, 500, 110), (1000, 500, 36), n=12, bow=0.25)
    assert len(path) == 12 and path[-1] == (1000, 500, 36)
    mid = path[5]
    assert mid[1] < 500 - 100                         # well above the straight line
    assert all(p[1] <= 500 for p in path)
    sides = [p[2] for p in path]
    assert sides == sorted(sides, reverse=True)       # shrinks monotonically


def test_frames_without_bow_are_a_straight_line():
    path = ol.frames((0, 0, 10), (100, 100, 10), n=4)
    assert all(abs(x - y) <= 1 for x, y, _ in path)


@pytest.mark.parametrize("sq, want", [
    ((-50, -50, 40), (0, 0, 40)), ((1900, 1060, 40), (1880, 1040, 40)),
    ((10, 10, 5000), (10, 0, 1080)),
])
def test_clamp_square_keeps_it_inside(sq, want):
    assert ol.clamp_square(*sq, (0, 0, 1920, 1080)) == want


# -- docking (Stage 2) --------------------------------------------------------------------

W = (0, 0, 1920, 1040)


@pytest.mark.parametrize("target, side, where, pos", [
    ((100, 100, 1000, 800), 45, "right", (1008, 108)),
    ((900, 100, 1900, 800), 45, "left", (847, 108)),
    ((0, 300, 1920, 1000), 45, "above", (1867, 247)),
    ((0, 0, 1920, 700), 45, "below", (1867, 708)),
])
def test_dock_prefers_right_then_left_then_above_then_below(target, side, where, pos):
    assert ol.dock_side(target, W, side, 8) == where
    x, y = ol.dock_position(target, W, side)
    assert (x, y) == pos
    assert not ol.overlaps((x, y, x + side, y + side), target)
    assert W[0] <= x and W[1] <= y and x + side <= W[2] and y + side <= W[3]


def test_a_full_screen_target_falls_back_to_the_screen_edge():
    maximized = (-8, -8, 1928, 1048)                 # real maximized rects overhang
    assert ol.dock_side(maximized, W, 45, 8) == "edge"
    x, y = ol.dock_position(maximized, W, 45)
    assert (x, y) == (1867, 8)
    assert x + 45 <= W[2] and y >= W[1]


def test_a_tiny_work_area_still_keeps_the_orb_inside():
    x, y = ol.dock_position((0, 0, 10, 10), (0, 0, 30, 30), 45)
    assert (x, y) == (0, 0)                          # side clamps to 30, fills the area
    x, y = ol.dock_position((0, 0, 20, 60), (0, 0, 90, 60), 45)
    assert ol.dock_side((0, 0, 20, 60), (0, 0, 90, 60), 45, 8) == "right"
    assert (x, y) == (28, 8) and x + 45 <= 90


@pytest.mark.parametrize("work, target, want", [
    ((-1920, 0, 0, 1040), (-1800, 100, -900, 800), (-892, 108)),        # monitor on the left
    ((1920, -200, 3840, 880), (2000, -100, 3000, 600), (3008, -92)),     # right, shifted up
    ((1920, -200, 3840, 880), (1920, -200, 3840, 880), (3787, -192)),    # full screen there
])
def test_multi_monitor_offsets(work, target, want):
    x, y = ol.dock_position(target, work, 45)
    assert (x, y) == want
    assert work[0] <= x and work[1] <= y and x + 45 <= work[2] and y + 45 <= work[3]


def test_a_target_hanging_off_the_monitor_is_clipped_first():
    # A window half off the right edge: the right side has no room on this monitor.
    assert ol.dock_side((1500, 100, 2500, 800), W, 45, 8) == "left"


def test_never_over_the_target_when_any_side_fits_randomized():
    import random
    rnd = random.Random(1234)
    for _ in range(2000):
        wl, wt = rnd.randint(-3000, 3000), rnd.randint(-1500, 1500)
        work = (wl, wt, wl + rnd.randint(200, 3840), wt + rnd.randint(200, 2160))
        l = rnd.randint(work[0] - 50, work[2] - 10)
        t = rnd.randint(work[1] - 50, work[3] - 10)
        target = (l, t, l + rnd.randint(10, 4000), t + rnd.randint(10, 2500))
        side = rnd.choice([36, 45, 54])
        x, y = ol.dock_position(target, work, side)
        assert work[0] <= x and work[1] <= y and x + side <= work[2] and y + side <= work[3]
        if ol.dock_side(target, work, side, 8) != "edge":
            assert not ol.overlaps((x, y, x + side, y + side), target), (work, target)


# -- panels anchored to the top of the target's monitor --------------------------------------

PANELS = [(240, 176), (280, 140)]                      # steps above answer
WORK = (0, 0, 1920, 1040)


def _rects(spots, sizes=PANELS):
    return [(x, y, x + w, y + h) for (x, y), (w, h) in zip(spots, sizes)]


@pytest.mark.parametrize("anchor, xs", [
    ("top-right", [1664, 1624]), ("top-left", [16, 16]), ("top-center", [840, 820]),
])
def test_each_anchor_stacks_down_from_the_top_with_inset_and_gap(anchor, xs):
    spots, used = ol.anchored_panels(PANELS, WORK, anchor)
    assert used == anchor and [x for x, _ in spots] == xs
    assert [y for _, y in spots] == [16, 16 + 176 + 12]


@pytest.mark.parametrize("scale, inset, gap", [(1.0, 16, 12), (1.25, 20, 15), (1.5, 24, 18)])
def test_inset_and_gap_scale_with_dpi(scale, inset, gap):
    sizes = [(round(240 * scale), round(176 * scale)), (round(280 * scale), round(140 * scale))]
    spots, _ = ol.anchored_panels(sizes, WORK, "top-right", scale)
    assert spots[0] == (1920 - inset - sizes[0][0], inset)
    assert spots[1][1] == inset + sizes[0][1] + gap


@pytest.mark.parametrize("orb", [(100, 900, 82), (1700, 900, 82), (900, 500, 28)])
def test_the_position_does_not_depend_on_the_orb(orb):
    assert ol.anchored_panels(PANELS, WORK, "top-right", orb=orb)[0] == \
        ol.anchored_panels(PANELS, WORK, "top-right")[0]


def test_panels_never_cover_the_orb():
    orb = (1700, 20, 82)                                # docked in the top-right corner
    spots, used = ol.anchored_panels(PANELS, WORK, "top-right", orb=orb)
    assert not any(ol.overlaps(r, (1700, 20, 1782, 102)) for r in _rects(spots))
    assert used == "top-right" and spots[0][1] == 102 + 12   # moved below the orb


def test_a_target_covering_the_corner_moves_the_panels_to_the_next_anchor():
    target = (1000, 0, 1920, 800)                       # covers the top-right
    spots, used = ol.anchored_panels(PANELS, WORK, "top-right", target_rect=target)
    assert used == "top-left"
    assert not any(ol.overlaps(r, target) for r in _rects(spots))
    both = (0, 0, 700, 800), (1200, 0, 1920, 800)       # left and right covered: centre
    for t in both:
        assert ol.anchored_panels(PANELS, WORK, "top-left", target_rect=t)[1] in (
            "top-left", "top-right", "top-center")
    wide = (0, 0, 1920, 600)                            # covers every anchor
    assert ol.anchored_panels(PANELS, WORK, "top-center", target_rect=wide)[1] == "top-center"


def test_a_maximized_target_keeps_the_configured_anchor():
    spots, used = ol.anchored_panels(PANELS, WORK, "top-left", target_rect=(-8, -8, 1928, 1048),
                                     maximized=True)
    assert used == "top-left" and spots[0] == (16, 16)


@pytest.mark.parametrize("work", [(-1920, 0, 0, 1040), (1920, -200, 3840, 880),
                                  (0, 0, 2560, 1400)])
def test_multi_monitor_offsets(work):
    spots, _ = ol.anchored_panels(PANELS, work, "top-right")
    assert spots[0] == (work[2] - 16 - 240, work[1] + 16)
    for x, y, r, b in _rects(spots):
        assert work[0] <= x and r <= work[2] and work[1] <= y and b <= work[3]


def test_a_small_work_area_keeps_every_panel_inside():
    work = (0, 0, 300, 250)
    spots, _ = ol.anchored_panels(PANELS, work, "top-right")
    for x, y, r, b in _rects(spots):
        assert 0 <= x and r <= 300 and 0 <= y and b <= 250


def test_the_anchor_setting_is_read_at_call_time_and_validated(monkeypatch):
    from core.config import panel_anchor
    monkeypatch.delenv("MISTAI_PANEL_ANCHOR", raising=False)
    assert panel_anchor() == "top-right"
    for raw, want in (("top-left", "top-left"), (" TOP-CENTER ", "top-center"),
                      ("bottom-left", "top-right"), ("", "top-right")):
        monkeypatch.setenv("MISTAI_PANEL_ANCHOR", raw)
        assert panel_anchor() == want
