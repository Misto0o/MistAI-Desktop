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


# -- panel stack (Stage 3) -------------------------------------------------------------------

SIZES = [(240, 44), (260, 196), (300, 150)]


def test_panels_stack_away_from_the_target_without_overlapping_it():
    target, orb = (100, 100, 1000, 800), (1008, 108, 45)
    spots = ol.panel_stack(orb, SIZES, W, target)
    assert [x for x, _ in spots] == [1061, 1061, 1061]
    assert [y for _, y in spots] == [108, 160, 364]
    for (x, y), (w, h) in zip(spots, SIZES):
        assert not ol.overlaps((x, y, x + w, y + h), target)
    left = ol.panel_stack((847, 108, 45), SIZES, W, (900, 100, 1900, 800))
    assert all(x + w <= 839 for (x, _), (w, _h) in zip(left, SIZES))


def test_panels_flip_when_their_side_has_no_room_and_stay_inside():
    spots = ol.panel_stack((1867, 8, 45), SIZES, W, (0, 300, 1700, 1000))
    for (x, y), (w, h) in zip(spots, SIZES):
        assert x + w <= 1867 - 8 + 1 and W[0] <= x and y + h <= W[3]
    low = ol.panel_stack((1762, 900, 138), SIZES, W)          # home, bottom right
    assert all(x + w <= 1762 for (x, _), (w, _h) in zip(low, SIZES))
    assert low[-1][1] + SIZES[-1][1] <= W[3]                  # moved up to fit


def test_panels_on_a_second_monitor_stay_on_it():
    work = (-1920, 0, 0, 1040)
    spots = ol.panel_stack((-60, 20, 45), SIZES, work, (-1900, 0, -100, 1000))
    for (x, y), (w, h) in zip(spots, SIZES):
        assert work[0] <= x and x + w <= work[2] and work[1] <= y and y + h <= work[3]
