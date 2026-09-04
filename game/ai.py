"""Bot pilots: survive and score.

Bots drive the exact same Player.input dict as humans, so the simulation
stays authoritative and identical for everyone.  Decisions are deterministic
given a seeded RNG (per-bot personality + wander noise).

Priority stack, highest first:
  1. avoid crashing into a body (radial or tangential escape)
  2. chase a valuable wreck / bonus / core target
  3. aggressive lunge at a nearby enemy when fast
  4. orbit a planet tangentially (styled, feeds the slingshot)

All target helpers return a *relative* direction (dx, dy, d) from the bot,
so `_set_input` can steer with them without coordinate-free bugs.
"""
from __future__ import annotations

import math
import random

from .const import PLAYER_RADIUS, SKIN_HUES, SKINS
from .physics import World

# horizon (s) for collision avoidance: time-to-contact below this is an alarm
_AVOID_HORIZON = 2.6
# distance multiplier around a body that counts as "inside the danger shell"
_DANGER_SHELL = 1.9
# per-body clearance margin the bot wants to keep
_PAD = 46.0
# target acquisition radii
WRECK_RANGE = 1150.0
BONUS_RANGE = 760.0
CORE_RANGE = 860.0
RAM_RANGE = 520.0
ORBIT_IN = 2.3  # planet radii: begin steering toward orbit band
ORBIT_OUT = 4.6  # planet radii: end of orbit band


class BotBrain:
    def __init__(self, world: World, rng: random.Random | None = None):
        self.world = world
        self.rng = rng or random.Random()
        # pid -> int orbit slot (which planet this bot prefers)
        self.orbit: dict[str, int] = {}
        # pid -> float 0..1: how eager this bot is to ram
        self._aggro: dict[str, float] = {}
        # (pid, body_idx) -> tangential turn sense, fixed per encounter
        self._turn: dict[tuple[str, int], int] = {}

    def decide(self, pid: str) -> None:
        world = self.world
        p = world.players[pid]
        if not p.alive:
            p.input.clear()
            return
        if pid not in self._aggro:
            self._aggro[pid] = self.rng.uniform(0.15, 0.75)
            if pid not in self.orbit:
                self.orbit[pid] = self.rng.randrange(0, len(world.planets))
        if not p.skin or p.skin not in SKINS:
            p.skin = self.rng.choice(SKINS)
        p.color = SKIN_HUES.get(p.skin, p.color)

        sp = math.hypot(p.vx, p.vy)

        # 1) don't fly into planets / the star
        ev = self._avoid(p)
        if ev is not None:
            self._set_input(p, ev[0], ev[1], mag=1.0)
            return

        # 2) combat: harass a closing enemy (bump breaks their chain) when fast
        if self._aggro[pid] > 0.42 and sp > 350.0:
            ram = self._ram_target(p)
            if ram is not None:
                nx, ny = ram[0] / ram[2], ram[1] / ram[2]
                self._steer(p, nx, ny, mag=self._aggro[pid])
                return

        # 3) treasure: wrecks > bonuses > cores
        target = self._best_wreck(p) or self._best_bonus(p) or self._best_core(p)
        if target is not None:
            nx, ny = target[0] / target[2], target[1] / target[2]
            self._steer(p, nx, ny, mag=1.0)
            return

        # 4) style: orbit a planet with smooth band control (feeds the sling)
        planet = world.planets[self.orbit.get(pid, 0) % len(world.planets)]
        dx = p.x - planet.x
        dy = p.y - planet.y
        d = math.hypot(dx, dy) or 1.0
        sense = self._turn_sense(pid, self.orbit.get(pid, 0), planet)
        tx, ty = -dy / d * sense, dx / d * sense
        target_r = planet.r * (ORBIT_IN + ORBIT_OUT) * 0.5
        # proportional radial correction, capped so escape overrules it softly
        corr = max(-0.7, min(0.7, (d - target_r) * 0.0035))
        ux, uy = tx + (-dx / d) * corr, ty + (-dy / d) * corr
        n = math.hypot(ux, uy) or 1.0
        self._steer(p, ux / n, uy / n, mag=min(1.0, n))

    # ------------------------------------------------------------ avoidance
    def _avoid(self, p):
        """Return (nx, ny) thrust if a body is about to hit us, else None."""
        sp = math.hypot(p.vx, p.vy)
        for i, b in enumerate(self.world.bodies):
            dx = p.x - b.x
            dy = p.y - b.y
            d = math.hypot(dx, dy) or 1.0
            radius = b.r + PLAYER_RADIUS + _PAD
            nx, ny = dx / d, dy / d
            if d < radius * _DANGER_SHELL:
                # already inside the shell: thrust away-hard, blended with a
                # tangential escape so we don't stall in the gravity well
                tx, ty = -dy / d, dx / d
                sense = self._turn_sense(p.id, i, b)
                ex, ey = -nx + tx * sense * 0.9, -ny + ty * sense * 0.9
                n = math.hypot(ex, ey) or 1.0
                return ex / n, ey / n
            if sp < 40.0:
                continue
            radial = (p.vx * dx + p.vy * dy) / d  # > 0 = moving away
            if radial >= -40.0:
                continue  # not meaningfully closing
            ttc = (d - radius) / -radial
            if ttc < _AVOID_HORIZON:
                # keep the current orbit sense for a smooth, non-flappy dodge
                tx, ty = -dy / d, dx / d
                sense = self._turn_sense(p.id, i, b)
                return tx * sense, ty * sense
        return None

    # ------------------------------------------------------- player dodging
    def _steer(self, p, nx: float, ny: float, mag: float) -> None:
        """Steer toward (nx, ny) at `mag`, deflected away from nearby players."""
        lx, ly, w = self._player_push(p)
        if w > 0:
            ax = nx * (1 - w) + lx * w
            ay = ny * (1 - w) + ly * w
            n = math.hypot(ax, ay)
            if n:
                nx, ny = ax / n, ay / n
        self._set_input(p, nx, ny, mag=mag)

    def _player_push(self, p):
        """Soft avoid of a too-close opponent: (lx, ly, w) with w in 0..1."""
        best = None
        best_d = 170.0 * 170.0
        for q in self.world.players.values():
            if q is p or not q.alive:
                continue
            dx = p.x - q.x
            dy = p.y - q.y
            d2 = dx * dx + dy * dy
            if d2 < best_d:
                best_d = d2
                best = (dx, dy, math.sqrt(d2))
        if best is None or best[2] > 170.0:
            return 0.0, 0.0, 0.0
        dx, dy, d = best
        nx, ny = dx / d, dy / d
        closing = -(p.vx * nx + p.vy * ny)  # > 0 = closing on q
        w = (1.0 - d / 170.0) * (1.4 if closing > 30.0 else 1.0)
        return nx, ny, min(1.0, w)

    def _turn_sense(self, pid: str, body_idx: int, _body) -> int:
        key = (pid, body_idx)
        sense = self._turn.get(key)
        if sense is None:
            sense = self.rng.choice((-1, 1))
            self._turn[key] = sense
        return sense

    # ------------------------------------------------------------- targeting
    def _best_wreck(self, p):
        """Nearest wreck as a relative direction (dx, dy, d), else None."""
        best = None
        best_d = WRECK_RANGE * WRECK_RANGE
        for w in self.world.wrecks:
            dx = w.x - p.x
            dy = w.y - p.y
            d2 = dx * dx + dy * dy
            if d2 < best_d:
                best_d = d2
                best = (dx, dy, math.sqrt(d2))
        return best

    def _best_bonus(self, p):
        best = None
        best_d = BONUS_RANGE * BONUS_RANGE
        for b in self.world.bonuses:
            if b.taken:
                continue
            dx = b.x - p.x
            dy = b.y - p.y
            d2 = dx * dx + dy * dy
            if d2 < best_d:
                best_d = d2
                best = (dx, dy, math.sqrt(d2))
        return best

    def _nearest_core_target(self, p, radius):
        """Truly the nearest core inside `radius`, as (dx, dy, d)."""
        best = None
        best_d = radius * radius
        for c in self.world.cores:
            if c.taken:
                continue
            dx = c.x - p.x
            dy = c.y - p.y
            d2 = dx * dx + dy * dy
            if d2 < best_d:
                best_d = d2
                best = (dx, dy, math.sqrt(d2))
        return best

    def _best_core(self, p):
        return self._nearest_core_target(p, CORE_RANGE)

    def _ram_target(self, p):
        """Predicted intercept of a human we can catch. Returns (dx, dy, d)."""
        best = None
        best_d = RAM_RANGE * RAM_RANGE
        for q in self.world.players.values():
            if q is p or not q.alive or q.is_bot:
                continue
            # aim slightly ahead of their current motion
            qx = q.x + q.vx * 0.35
            qy = q.y + q.vy * 0.35
            dx = qx - p.x
            dy = qy - p.y
            d2 = dx * dx + dy * dy
            if d2 < best_d:
                best_d = d2
                best = (dx, dy, math.sqrt(d2))
        return best

    # ---------------------------------------------------------------- output
    def _set_input(self, p, nx: float, ny: float, mag: float = 1.0,
                   wander: float = 0.0) -> None:
        """Write an analog thrust vector: direction (nx, ny) scaled by `mag`
        (both in 0..1), plus a little personality noise. Clamped to unit."""
        jx = self.rng.uniform(-wander, wander)
        jy = self.rng.uniform(-wander, wander)
        dx = nx * mag + jx
        dy = ny * mag + jy
        n = math.hypot(dx, dy) or 1.0
        if n > 1.0:
            dx /= n
            dy /= n
        p.input = {"dx": round(dx, 4), "dy": round(dy, 4)}