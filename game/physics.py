"""Authoritative world simulation: gravity orbits, slingshots, collisions, pickups.

Pure-python, deterministic, no async — designed to be stepped by the server
tick loop and unit-tested directly with a seeded RNG and fixed dt.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .const import (
    BASE_MAX_SPEED,
    BODY_K_ASTEROID,
    BODY_K_PLANET,
    BODY_K_STAR,
    BONUS_COUNT,
    BONUS_KINDS,
    BONUS_RADIUS,
    BONUS_RESPAWN,
    BONUS_SCORE,
    BOOST_DURATION,
    BOOST_THRUST_MULT,
    CORE_RESPAWN,
    DRAG,
    DT,
    GRAV_HARD_CAP,
    HALF,
    INVULN,
    MAGNET_DURATION,
    MAGNET_PULL,
    MAGNET_RADIUS,
    PLAYER_RADIUS,
    RESPAWN_DELAY,
    SHIELD_DURATION,
    SKIN_HUES,
    SKINS,
    SLING_BOOST,
    SLING_COMBO_MAX,
    SLING_COMBO_WINDOW,
    SLING_EVENT_SPEED,
    SLING_MAX_SPEED,
    SLING_MIN_TIME,
    SLING_TANG_ACCEL,
    SLING_ZONE,
    SOFT_CAP_K,
    THRUST_ACCEL,
    WORLD,
    WRECK_DECAY,
)

_NEVER = -1e9  # "not since the start of time" marker for buff-last-set timers


@dataclass(slots=True)
class Body:
    x: float
    y: float
    r: float
    k: float
    name: str
    hue: int
    kind: str = "planet"


@dataclass(slots=True)
class Player:
    id: str
    name: str
    x: float = 0.0
    y: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    score: int = 0
    kills: int = 0
    alive: bool = False
    respawn_at: float = 0.0
    invuln_until: float = 0.0
    color: int = 180
    skin: str = "probe"
    shield_until: float = 0.0
    boost_until: float = 0.0
    magnet_until: float = 0.0
    input: dict = field(default_factory=dict)
    sling_body: int = -1
    sling_since: float = 0.0
    sling_combo: int = 0
    last_sling_at: float = _NEVER
    is_bot: bool = False
    # per-session round stats, sent with the die event for the end-of-run panel
    stats: dict = field(default_factory=lambda: {
        "cores": 0, "wreck": 0, "kills": 0, "bestCombo": 0, "dist": 0.0, "deaths": 0,
    })


@dataclass(slots=True)
class Core:
    x: float
    y: float
    value: int = 1
    taken: bool = False
    respawn_at: float = 0.0


@dataclass(slots=True)
class Bonus:
    x: float
    y: float
    kind: str = "shield"
    taken: bool = False
    respawn_at: float = 0.0


@dataclass(slots=True)
class Wreck:
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0
    value: int = 0
    born: float = 0.0  # world-time the wreck was dropped; used for decay
    owner: str = ""      # player id that dropped the container (killfeed)
    owner_name: str = ""  # display name that dropped it


class World:
    """Owns all entities and steps the simulation forward."""

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.Random()
        self.t: float = 0.0
        self.bodies, self.planets = make_system()
        self.cores: list[Core] = make_cores(self.bodies, self.rng)
        self.bonuses: list[Bonus] = make_bonuses(self.bodies, self.rng)
        self.wrecks: list[Wreck] = []
        self.players: dict[str, Player] = {}
        self.events: list[dict] = []

    # ------------------------------------------------------------- entities
    def add_player(self, pid: str, name: str, is_bot: bool = False,
                   skin: str = "probe") -> Player:
        if skin not in SKINS:
            skin = "probe"
        p = Player(id=pid, name=name, color=SKIN_HUES.get(skin, self.rng.randrange(0, 360)),
                   skin=skin, is_bot=is_bot)
        self.players[pid] = p
        self.spawn(p)
        return p

    def remove_player(self, pid: str) -> None:
        self.players.pop(pid, None)

    def spawn(self, p: Player) -> None:
        point = self._free_spot()
        p.x, p.y = point
        p.vx = p.vy = 0.0
        p.alive = True
        p.invuln_until = self.t + INVULN
        p.sling_body = -1
        p.sling_combo = 0
        p.last_sling_at = _NEVER
        self.events.append({"t": "spawn", "id": p.id, "x": p.x, "y": p.y})

    def _free_spot(self) -> tuple[float, float]:
        """A spot far from bodies, spawn points and pickups (best effort)."""
        for _ in range(60):
            x = self.rng.uniform(300, WORLD - 300)
            y = self.rng.uniform(300, WORLD - 300)
            if not self._spot_clear(x, y):
                continue
            return x, y
        return HALF, HALF

    def _spot_clear(self, x: float, y: float) -> bool:
        def crowded(px: float, py: float, radius: float) -> bool:
            return (x - px) ** 2 + (y - py) ** 2 < radius ** 2

        if any(crowded(b.x, b.y, b.r + 300.0) for b in self.bodies):
            return False
        for p in self.players.values():
            if p.alive and crowded(p.x, p.y, 160.0):
                return False
        if any(crowded(c.x, c.y, 150.0) for c in self.cores):
            return False
        if any(crowded(b.x, b.y, 150.0) for b in self.bonuses if not b.taken):
            return False
        if any(crowded(w.x, w.y, 150.0) for w in self.wrecks):
            return False
        return True

    # ------------------------------------------------------------------ tick
    def step(self, dt: float = DT) -> list[dict]:
        dt = min(max(dt, 0.0), 0.25)
        self.t += dt
        self.events.clear()

        for p in self.players.values():
            if not p.alive:
                if self.t >= p.respawn_at:
                    self.spawn(p)
                continue
            self._step_player(p, dt)

        self._step_wrecks(dt)
        self._step_core_respawns()
        self._step_bonus_respawns()
        return self.events

    def _step_player(self, p: Player, dt: float) -> None:
        ax = ay = 0.0
        # gravity from all bodies
        for b in self.bodies:
            dx = b.x - p.x
            dy = b.y - p.y
            d = math.hypot(dx, dy)
            if d < b.r + 4:
                d = b.r + 4
            a = min(b.k * (b.r * b.r) / (d * d), GRAV_HARD_CAP)
            ax += a * dx / d
            ay += a * dy / d

        # slingshot tangential assist (only around planets, on the way out)
        near_idx = -1
        for i, b in enumerate(self.planets):
            dx = b.x - p.x
            dy = b.y - p.y
            d = math.hypot(dx, dy)
            if d < b.r * SLING_ZONE and d > 20:
                radial = (dx * p.vx + dy * p.vy) / d
                if radial > 40:
                    tx, ty = -dy / d, dx / d
                    s = 1.0 if (p.vx * tx + p.vy * ty) >= 0 else -1.0
                    ax += SLING_TANG_ACCEL * s * tx
                    ay += SLING_TANG_ACCEL * s * ty
                near_idx = i

        # thrust input: analog (dx, dy) preferred; legacy booleans fall back
        inp = p.input
        boost = BOOST_THRUST_MULT if self.t < p.boost_until else 1.0
        dx = inp.get("dx")
        dy = inp.get("dy")
        if dx is not None and dy is not None:
            tx = min(1.0, max(-1.0, dx))
            ty = min(1.0, max(-1.0, dy))
        else:
            tx = (1.0 if inp.get("right") else 0.0) - (1.0 if inp.get("left") else 0.0)
            ty = (1.0 if inp.get("down") else 0.0) - (1.0 if inp.get("up") else 0.0)
            n = math.hypot(tx, ty)
            if n:
                tx /= n
                ty /= n
        if tx or ty:
            ax += THRUST_ACCEL * boost * tx
            ay += THRUST_ACCEL * boost * ty

        # slingshot zone tracking
        self._track_sling(p, near_idx)

        # integrate + drag + soft speed cap
        f = math.exp(-DRAG * dt)
        p.vx = (p.vx + ax * dt) * f
        p.vy = (p.vy + ay * dt) * f
        sp = math.hypot(p.vx, p.vy)
        if sp > BASE_MAX_SPEED:
            cap_f = math.exp(-SOFT_CAP_K * (sp - BASE_MAX_SPEED) / BASE_MAX_SPEED * dt)
            p.vx *= cap_f
            p.vy *= cap_f
        p.x += p.vx * dt
        p.y += p.vy * dt

        # soft walls
        m = PLAYER_RADIUS + 6
        if p.x < m:
            p.x = m
            p.vx = abs(p.vx) * 0.4
        elif p.x > WORLD - m:
            p.x = WORLD - m
            p.vx = -abs(p.vx) * 0.4
        if p.y < m:
            p.y = m
            p.vy = abs(p.vy) * 0.4
        elif p.y > WORLD - m:
            p.y = WORLD - m
            p.vy = -abs(p.vy) * 0.4

        # session distance travelled (round-stats panel)
        p.stats["dist"] += math.hypot(p.vx, p.vy) * dt

        # magnet: actively drag nearby cores toward the ship
        if self.t < p.magnet_until:
            for c in self.cores:
                if c.taken:
                    continue
                mx = p.x - c.x
                my = p.y - c.y
                d2 = mx * mx + my * my
                if d2 < MAGNET_RADIUS * MAGNET_RADIUS and d2 > 1.0:
                    d = math.sqrt(d2)
                    pull = MAGNET_PULL * dt
                    c.x += mx / d * pull
                    c.y += my / d * pull

        if p.invuln_until > self.t:
            return

        # collect cores / wrecks
        for c in self.cores:
            if c.taken:
                continue
            if (p.x - c.x) ** 2 + (p.y - c.y) ** 2 < (PLAYER_RADIUS + c.value * 4 + 2) ** 2:
                c.taken = True
                c.respawn_at = self.t + CORE_RESPAWN + self.rng.uniform(0, 1.0)
                p.score += c.value
                p.stats["cores"] += c.value
                self.events.append({"t": "pickup", "id": p.id, "value": c.value})
        for w in list(self.wrecks):
            if (p.x - w.x) ** 2 + (p.y - w.y) ** 2 < (PLAYER_RADIUS + max(8, w.value * 0.5) + 2) ** 2:
                p.score += w.value
                p.stats["wreck"] += w.value
                self.wrecks.remove(w)
                self.events.append({"t": "wreck", "id": p.id, "value": w.value,
                                    "owner": w.owner, "owner_name": w.owner_name})

        # collect bonuses / powerups
        for b in self.bonuses:
            if b.taken:
                continue
            if (p.x - b.x) ** 2 + (p.y - b.y) ** 2 < (PLAYER_RADIUS + BONUS_RADIUS + 4) ** 2:
                b.taken = True
                b.respawn_at = self.t + BONUS_RESPAWN
                p.score += BONUS_SCORE
                if b.kind == "shield":
                    p.shield_until = self.t + SHIELD_DURATION
                elif b.kind == "boost":
                    p.boost_until = self.t + BOOST_DURATION
                else:
                    p.magnet_until = self.t + MAGNET_DURATION
                self.events.append({"t": "bonus", "id": p.id, "kind": b.kind})

        # crash into a body; a shield bounces you out instead of killing you
        for b in self.bodies:
            if (p.x - b.x) ** 2 + (p.y - b.y) ** 2 < (b.r - 2 + PLAYER_RADIUS) ** 2:
                if self.t < p.shield_until:
                    dx = p.x - b.x
                    dy = p.y - b.y
                    d = math.hypot(dx, dy) or 1.0
                    out = b.r + PLAYER_RADIUS + 3
                    p.x = b.x + dx / d * out
                    p.y = b.y + dy / d * out
                    radial = (p.vx * dx + p.vy * dy) / d
                    if radial < 0:
                        p.vx -= radial * dx / d * 1.6
                        p.vy -= radial * dy / d * 1.6
                    p.shield_until = 0.0
                    self.events.append({"t": "shield", "id": p.id, "x": p.x, "y": p.y})
                    continue
                self._kill(p, None, "crash")
                return

        # player v player: any contact knocks both ships apart and breaks
        # their sling chains — fights stay bouncy, no instant ram-kills
        for q in self.players.values():
            if q.id <= p.id or not q.alive:
                continue
            dx = p.x - q.x
            dy = p.y - q.y
            d = math.hypot(dx, dy)
            if d >= PLAYER_RADIUS * 2 + 2 or d < 1e-6:
                continue
            self._knockback(p, q)
            p.sling_combo = 0
            p.last_sling_at = _NEVER
            q.sling_combo = 0
            q.last_sling_at = _NEVER
            self.events.append({"t": "bump", "a": p.id, "b": q.id,
                                "x": round((p.x + q.x) * 0.5, 1),
                                "y": round((p.y + q.y) * 0.5, 1)})

    def _track_sling(self, p: Player, near_idx: int) -> None:
        if near_idx >= 0:
            if p.sling_body < 0:
                p.sling_body = near_idx
                p.sling_since = self.t
            return
        if p.sling_body < 0:
            return
        if self.t - p.sling_since >= SLING_MIN_TIME:
            sp = math.hypot(p.vx, p.vy)
            if sp > SLING_EVENT_SPEED:
                f = SLING_BOOST
                p.vx *= f
                p.vy *= f
                cap = math.hypot(p.vx, p.vy)
                if cap > SLING_MAX_SPEED:
                    p.vx *= SLING_MAX_SPEED / cap
                    p.vy *= SLING_MAX_SPEED / cap
                # chained slings compound inside the window (x1, x2, ... x5)
                if self.t - p.last_sling_at <= SLING_COMBO_WINDOW:
                    p.sling_combo = min(p.sling_combo + 1, SLING_COMBO_MAX)
                else:
                    p.sling_combo = 1
                p.last_sling_at = self.t
                p.score += p.sling_combo
                p.stats["bestCombo"] = max(p.stats["bestCombo"], p.sling_combo)
                self.events.append({"t": "sling", "id": p.id, "x": p.x, "y": p.y,
                                    "sp": round(sp), "combo": p.sling_combo})
        p.sling_body = -1

    def _knockback(self, a: Player, b: Player) -> None:
        """Shove both ships apart along their contact normal.

        Header-on contact keeps their charge pointing toward each other, so the
        rebound flip carries both clear — a bounce, never a ram-kill.
        """
        dx = a.x - b.x
        dy = a.y - b.y
        d = math.hypot(dx, dy) or 1.0
        nx, ny = dx / d, dy / d
        ra = a.vx * nx + a.vy * ny
        rb = b.vx * nx + b.vy * ny
        KICK = 1.4
        a.vx -= (ra * KICK - rb) * nx
        a.vy -= (ra * KICK - rb) * ny
        b.vx += (ra - rb * KICK) * nx
        b.vy += (ra - rb * KICK) * ny
        a.x += nx * 4
        a.y += ny * 4
        b.x -= nx * 4
        b.y -= ny * 4

    def _kill(self, p: Player, killer: Player | None, cause: str) -> None:
        p.alive = False
        p.respawn_at = self.t + RESPAWN_DELAY
        p.stats["deaths"] += 1
        carried = p.score
        if carried > 0:
            self.wrecks.append(Wreck(x=p.x, y=p.y, vx=p.vx, vy=p.vy,
                                     value=carried, born=self.t,
                                     owner=p.id, owner_name=p.name))
        p.score = 0
        p.sling_combo = 0
        p.last_sling_at = _NEVER
        leader = max(self.players.values(), key=lambda q: q.score)
        self.events.append({"t": "die", "id": p.id, "x": p.x, "y": p.y,
                            "score": carried, "stats": dict(p.stats),
                            "top": {"name": leader.name, "score": leader.score}})
        if killer is not None:
            killer.score += 2
            killer.kills += 1
            killer.stats["kills"] += 1
            self.events.append({"t": "kill", "killer": killer.id, "victim": p.id,
                                "x": p.x, "y": p.y})

    def _step_wrecks(self, dt: float) -> None:
        f = math.exp(-0.5 * dt)
        alive: list[Wreck] = []
        for w in self.wrecks:
            if self.t - w.born >= WRECK_DECAY:
                continue  # decayed container vanishes
            w.vx *= f
            w.vy *= f
            w.x += w.vx * dt
            w.y += w.vy * dt
            alive.append(w)
        self.wrecks = alive

    def _step_core_respawns(self) -> None:
        if self.t < 5.0:
            return
        for c in self.cores:
            if c.taken and self.t >= c.respawn_at:
                x, y = self._rand_core_pos()
                c.x, c.y = x, y
                c.taken = False
                c.respawn_at = 0.0

    def _step_bonus_respawns(self) -> None:
        if self.t < 5.0:
            return
        for b in self.bonuses:
            if b.taken and self.t >= b.respawn_at:
                x, y = self._rand_core_pos(clear_objects=True)
                b.x, b.y = x, y
                b.taken = False
                b.respawn_at = 0.0

    def _rand_core_pos(self, clear_objects: bool = False) -> tuple[float, float]:
        # ring belts around the star — but never inside a body (or another
        # live pickup, when that matters)
        for _ in range(60):
            x, y = _ring_core(self.rng)
            if not _clear_of_bodies(x, y, self.bodies):
                continue
            if clear_objects and self._overlaps_pickup(x, y):
                continue
            return x, y
        # fallback: anywhere clear of bodies
        for _ in range(150):
            x = self.rng.uniform(80, WORLD - 80)
            y = self.rng.uniform(80, WORLD - 80)
            if not _clear_of_bodies(x, y, self.bodies):
                continue
            if clear_objects and self._overlaps_pickup(x, y):
                continue
            return x, y
        return HALF - 500.0, HALF + 500.0

    def _overlaps_pickup(self, x: float, y: float) -> bool:
        """True when a live bonus already sits at (x, y) — stops stacking."""
        for b in self.bonuses:
            if b.taken:
                continue
            if (x - b.x) ** 2 + (y - b.y) ** 2 < 120 ** 2:
                return True

        for c in self.cores:
            if c.taken:
                continue
            if (x - c.x) ** 2 + (y - c.y) ** 2 < 120 ** 2:
                return True
        return False


# ------------------------------------------------------------- world gen
_ring_radii = (700.0, 1100.0, 1500.0)


def _ring_core(rng: random.Random) -> tuple[float, float]:
    ring = rng.choice(_ring_radii)
    ang = rng.uniform(0, math.tau)
    rr = ring + rng.uniform(-80, 80)
    x = max(60, min(WORLD - 60, HALF + math.cos(ang) * rr))
    y = max(60, min(WORLD - 60, HALF + math.sin(ang) * rr))
    return x, y


def _clear_of_bodies(x: float, y: float, bodies: list[Body], pad: float = 20.0) -> bool:
    for b in bodies:
        dx = x - b.x
        dy = y - b.y
        if dx * dx + dy * dy < (b.r + pad) ** 2:
            return False
    return True


def make_system() -> tuple[list[Body], list[Body]]:
    bodies = [
        Body(x=HALF, y=HALF, r=290.0, k=BODY_K_STAR, name="PR-7", hue=40, kind="star"),
        Body(x=HALF + 1250, y=HALF, r=120.0, k=BODY_K_PLANET, name="A-1", hue=180),
        Body(x=HALF - 1250, y=HALF + 320, r=105.0, k=BODY_K_PLANET, name="B-2", hue=300),
        Body(x=HALF - 850, y=HALF - 900, r=95.0, k=BODY_K_PLANET, name="C-3", hue=25),
        Body(x=HALF + 1150, y=HALF - 600, r=85.0, k=BODY_K_PLANET, name="D-4", hue=340),
        Body(x=HALF - 1500, y=HALF + 1200, r=75.0, k=BODY_K_PLANET, name="E-5", hue=150),
        Body(x=HALF + 500, y=HALF + 1250, r=70.0, k=BODY_K_PLANET, name="F-6", hue=260),
    ]
    asteroids = [
        Body(x=HALF + 620, y=HALF - 420, r=26.0, k=BODY_K_ASTEROID, name="Ast", hue=0, kind="asteroid"),
        Body(x=HALF - 700, y=HALF + 500, r=32.0, k=BODY_K_ASTEROID, name="Ast", hue=0, kind="asteroid"),
        Body(x=HALF + 980, y=HALF + 700, r=22.0, k=BODY_K_ASTEROID, name="Ast", hue=0, kind="asteroid"),
        Body(x=HALF + 40, y=HALF - 980, r=28.0, k=BODY_K_ASTEROID, name="Ast", hue=0, kind="asteroid"),
        Body(x=HALF - 40, y=HALF + 980, r=24.0, k=BODY_K_ASTEROID, name="Ast", hue=0, kind="asteroid"),
        Body(x=HALF - 1200, y=HALF - 300, r=30.0, k=BODY_K_ASTEROID, name="Ast", hue=0, kind="asteroid"),
    ]
    return bodies + asteroids, [b for b in bodies if b.kind == "planet"]


def make_cores(bodies: list[Body], rng: random.Random) -> list[Core]:
    cores: list[Core] = []
    # ring belts around the star: accept only positions clear of bodies
    for ring, count in ((700, 34), (1100, 40), (1500, 46)):
        placed = 0
        tried = 0
        while placed < count and tried < count * 40:
            tried += 1
            x, y = _ring_core(rng)
            if not _clear_of_bodies(x, y, bodies):
                continue
            cores.append(Core(x=x, y=y))
            placed += 1
        while placed < count:  # safety net (never reached in practice)
            x = rng.uniform(80, WORLD - 80)
            y = rng.uniform(80, WORLD - 80)
            if _clear_of_bodies(x, y, bodies):
                cores.append(Core(x=x, y=y))
                placed += 1
    # sparse scatter, also clear of bodies
    placed = 0
    while placed < 42:
        x = rng.uniform(300, WORLD - 300)
        y = rng.uniform(300, WORLD - 300)
        if _clear_of_bodies(x, y, bodies):
            cores.append(Core(x=x, y=y, value=3 if rng.random() < 0.12 else 1))
            placed += 1
    return cores


def make_bonuses(bodies: list[Body], rng: random.Random) -> list[Bonus]:
    kinds = iter(BONUS_KINDS)
    bonuses: list[Bonus] = []
    placed = 0
    while placed < BONUS_COUNT:
        x, y = _ring_core(rng)
        if not _clear_of_bodies(x, y, bodies, pad=60.0):
            continue
        try:
            kind = next(kinds)
        except StopIteration:
            kinds = iter(BONUS_KINDS)
            kind = next(kinds)
        bonuses.append(Bonus(x=x, y=y, kind=kind))
        placed += 1
    return bonuses