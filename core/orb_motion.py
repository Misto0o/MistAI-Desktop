"""
core/orb_motion.py — pure motion for the floating orb's flights: a 2-D damped
spring with a closed-form solution, so a flight's position is a function of
the time since take-off only (a dropped frame skips ahead, it never bends the
path), plus the frame-timing statistics. No Win32, no threads, no logging:
core/floating_orb.Tween samples it once per frame.

Motion (config.orb_motion(), MISTAI_ORB_MOTION):
  lively  damping ratio 0.58 (short hops) .. 0.68 (long flights): a small
          overshoot that settles (soft-capped near OVERSHOOT_PX); omega is the larger of a snappy minimum and
          the value that brings the decay envelope under SETTLE_PX by
          LIVELY_SETTLE_S, so a long flight is never slow; flights longer than
          ANTICIPATION_MIN_PX first lean back ANTICIPATION_PX for
          ANTICIPATION_S.
  calm    critically damped (no overshoot), settled by CALM_SETTLE_S.
Both bow upward with a perpendicular offset that is zero (and flat) at both
ends. A flight can be retargeted mid-way: the new spring starts from the
rendered position and velocity at that moment, so there is no jump.
"""

import math

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

def valid_refresh(hz, default=60):
    """A usable refresh rate (Hz): 24..500, else ``default`` (0 and 1 mean
    "hardware default" to EnumDisplaySettings)."""
    try:
        hz = int(round(float(hz)))
    except (TypeError, ValueError):
        return default
    return hz if 24 <= hz <= 500 else default


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


def stats_line(stats, pacer, motion):
    """The one log line (numbers and fixed labels only)."""
    return (f"[FloatingOrb] frame stats frames {stats['frames']} duration "
            f"{stats['duration_ms']:.0f} ms interval mean {stats['mean_ms']:.1f} p95 "
            f"{stats['p95_ms']:.1f} max {stats['max_ms']:.1f} ms slow {stats['slow']} refresh "
            f"{stats['refresh']} Hz pacer {pacer} motion {motion}")
