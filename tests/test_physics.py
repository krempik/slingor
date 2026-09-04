"""Physics unit tests: gravity, slingshots, collisions, pickups, respawns."""
import math
import random

from game.const import BONUS_KINDS, DT, SHIELD_DURATION, WORLD, WRECK_DECAY
from game.physics import HALF, Wreck, World


def _placed_player(world, pid="p1", x=1000.0, y=1000.0, vx=0.0, vy=0.0):
    p = world.add_player(pid, "tester")
    p.x, p.y, p.vx, p.vy = x, y, vx, vy
    p.invuln_until = 0.0
    p.shield_until = p.boost_until = p.magnet_until = 0.0
    # keep the fresh placement free of world pickups so stray spawn-steps
    # cannot derail collision / thrust tests
    for c in world.cores:
        if math.hypot(c.x - x, c.y - y) < 80:
            c.x, c.y = 3800.0, 3800.0
    for b in world.bonuses:
        if math.hypot(b.x - x, b.y - y) < 80:
            b.x, b.y = 3800.0, 3800.0
    return p


def test_gravity_pulls_toward_star():
    w = World(rng=random.Random(1))
    p = _placed_player(w)
    p.invuln_until = 0.0
    # place north of star: gravity should accelerate south (+y)
    p.x, p.y = HALF, HALF - 600
    w.step()
    assert p.vy > 0, "gravity must pull toward center"


def test_core_pickup_increments_score():
    w = World(rng=random.Random(2))
    p = _placed_player(w)
    p.invuln_until = 0.0
    c = w.cores[0]
    c.x, c.y = p.x + 10, p.y
    w.step()
    assert p.score > 0


def test_wall_bounce_keeps_in_world():
    w = World(rng=random.Random(3))
    p = _placed_player(w, x=-100, y=2000)
    p.vx = -5000
    p.invuln_until = 0.0
    for _ in range(40):
        w.step()
    assert 0 <= p.x <= WORLD


def test_crash_into_planet_kills_and_drops_wreck():
    w = World(rng=random.Random(4))
    planet = w.planets[0]
    p = _placed_player(w, x=planet.x, y=planet.y + planet.r + 12, vx=0, vy=-200)
    p.score = 5
    p.invuln_until = 0.0
    for _ in range(120):
        w.step()
        if not p.alive:
            break
    assert not p.alive
    assert any(e["t"] == "die" for e in w.events)
    assert any(ww.value == 5 for ww in w.wrecks)


def test_respawn_after_delay():
    w = World(rng=random.Random(5))
    p = _placed_player(w)
    p.alive = False
    p.respawn_at = 0.0
    w.step()
    assert p.alive
    assert p.invuln_until > w.t


def test_slingshot_boost_on_fast_exit():
    w = World(rng=random.Random(6))
    planet = w.planets[0]
    p = _placed_player(w, x=planet.x, y=planet.y - planet.r * 1.6, vx=0, vy=0)
    p.invuln_until = 0.0
    # diagonal motion: tangential + radial outward -> assist + boost on exit
    p.vx = 400.0
    p.vy = 140.0
    p.input = {"up": False, "down": False, "left": False, "right": True}
    speed0 = math.hypot(p.vx, p.vy)
    ran = 0
    while ran < 300:
        w.step()
        ran += 1
        if p.sling_body < 0 and ran > 2:
            break
        if not p.alive:
            break
    speed1 = math.hypot(p.vx, p.vy)
    assert ran < 300 or not p.alive, "bot never left the sling zone"
    assert speed1 >= speed0 * 1.1, "expected a slingshot speed-up"


def test_ship_collision_knocks_apart_and_breaks_combo():
    """Regression: contact must knock BOTH ships back and reset their sling
    chains — no instant ram-kills, so fights stay physical.
    """
    w = World(rng=random.Random(7))
    a = _placed_player(w, pid="a", x=1000, y=1000, vx=400, vy=0)
    b = _placed_player(w, pid="b", x=1030, y=1000, vx=-400, vy=0)
    a.invuln_until = 0.0
    b.invuln_until = 0.0
    a.sling_combo = 4
    b.sling_combo = 2
    evs = w.step()
    assert a.alive and b.alive, "contact must never kill either ship"
    assert a.sling_combo == 0 and b.sling_combo == 0, "combo must break on bump"
    assert any(e["t"] == "bump" for e in evs), "bump event must be emitted"


def test_gentle_collision_bounces_not_kills():
    w = World(rng=random.Random(8))
    a = _placed_player(w, pid="a", x=1000, y=1000, vx=60, vy=0)
    b = _placed_player(w, pid="b", x=1025, y=1000, vx=-60, vy=0)
    a.invuln_until = 0.0
    b.invuln_until = 0.0
    for _ in range(15):
        w.step()
    assert a.alive and b.alive


def test_sandwich_collision_no_double_death():
    """Regression: a slow ship sandwiched between two fast ones must be pushed
    away and survive — collision is handled once per unordered pair.
    """
    w = World(rng=random.Random(13))
    v = _placed_player(w, pid="v", x=1000, y=1000)
    a = _placed_player(w, pid="a", x=985, y=1000, vx=400, vy=0)
    c = _placed_player(w, pid="c", x=1015, y=1000, vx=-400, vy=0)
    bumps = 0
    for _ in range(10):
        evs = w.step()
        bumps += sum(1 for e in evs if e["t"] == "die")
    assert v.alive, "sandwich must knock the victim away, not kill it"
    assert bumps == 0, "no die event may come from ship-v-ship contact"


def test_cores_respawn_after_pickup():
    w = World(rng=random.Random(9))
    for c in w.cores:
        c.taken = True
        c.respawn_at = 0.0
    for _ in range(360):
        w.step()
    assert any(not c.taken for c in w.cores)


def test_two_wrecks_both_collected():
    w = World(rng=random.Random(11))
    p = _placed_player(w)
    p.invuln_until = 0.0
    # two overlapping wrecks: iterating while removing must not skip the 2nd
    w.wrecks.append(Wreck(x=p.x + 8, y=p.y, value=3))
    w.wrecks.append(Wreck(x=p.x + 10, y=p.y, value=5))
    w.step()
    assert not w.wrecks, "both wrecks must be collected in one pass"
    assert sum(e["t"] == "wreck" for e in w.events) == 2
    assert p.score >= 8


def test_shield_bonus_pickup_grants_duration():
    w = World(rng=random.Random(18))
    p = _placed_player(w)
    p.invuln_until = 0.0
    bonus = next(b for b in w.bonuses if b.kind == "shield")
    bonus.taken = False
    bonus.respawn_at = 0.0
    bonus.x, bonus.y = p.x + 12, p.y
    w.step()
    assert bonus.taken
    assert p.shield_until > w.t
    assert any(e["t"] == "bonus" and e["kind"] == "shield" for e in w.events)
    assert p.score >= 2


def test_shield_bounces_player_safe_from_planet():
    w = World(rng=random.Random(19))
    planet = w.planets[0]
    p = _placed_player(w, x=planet.x, y=planet.y)
    p.invuln_until = 0.0
    p.shield_until = w.t + SHIELD_DURATION
    evs = w.step()
    assert p.alive, "shield must absorb the crash"
    assert not p.shield_until  # consumed
    assert any(e["t"] == "shield" for e in evs)
    # exhausted shield: gravity drags the ship back into the body -> lethal
    for _ in range(240):
        if not p.alive:
            break
        w.step()
    assert not p.alive, "shield-less ship in the gravity well must die"


def test_bonuses_respawn_after_pickup():
    w = World(rng=random.Random(17))
    for b in w.bonuses:
        b.taken = True
        b.respawn_at = 0.0
    assert len(w.bonuses) > 0
    assert all(b.kind in BONUS_KINDS for b in w.bonuses)
    for _ in range(360):
        w.step()
    assert any(not b.taken for b in w.bonuses)


def test_magnet_drags_core_toward_ship():
    w = World(rng=random.Random(21))
    p = _placed_player(w)
    c = w.cores[0]
    c.x, c.y = p.x + 120, p.y
    p.magnet_until = 1000.0
    p.invuln_until = 1000.0  # invuln blocks pickup, not the magnet
    d0 = math.hypot(c.x - p.x, c.y - p.y)
    for _ in range(240):
        w.step()
    d1 = math.hypot(c.x - p.x, c.y - p.y)
    assert d1 < d0 * 0.5


def test_boost_increases_thrust_acceleration():
    w1 = World(rng=random.Random(15))
    w2 = World(rng=random.Random(15))
    p1 = _placed_player(w1, pid="a")
    p2 = _placed_player(w2, pid="b")
    for p in (p1, p2):
        p.invuln_until = 0.0
        p.input = {"right": True, "up": False, "down": False, "left": False}
    p1.boost_until = 1000.0
    for _ in range(160):
        w1.step()
        w2.step()
    s1 = math.hypot(p1.vx, p1.vy)
    s2 = math.hypot(p2.vx, p2.vy)
    assert s1 > s2 * 1.3


def test_wreck_decays_if_never_collected():
    w = World(rng=random.Random(23))
    p = _placed_player(w, x=3000, y=3000)
    p.invuln_until = 1e9  # never dies, never collects: no stray wrecks
    w.wrecks.append(Wreck(x=500.0, y=500.0, value=7, born=w.t))
    steps = int(WRECK_DECAY / DT) + 30
    for _ in range(steps):
        w.step()
    assert not w.wrecks, "uncollected wreck must despawn after the decay window"


def test_wreck_decay_timer_starts_at_drop():
    w = World(rng=random.Random(24))
    p = _placed_player(w, x=3000, y=3000)
    p.invuln_until = 1e9  # never dies, never collects: no stray wrecks
    w.wrecks.append(Wreck(x=500.0, y=500.0, value=5, born=w.t))
    for _ in range(int(WRECK_DECAY / DT / 2)):
        w.step()
    assert len(w.wrecks) == 1, "wreck must survive the first half of its window"


def test_sling_chain_compounds_combo_x():
    """Regression: chained slings inside the window score x2, x3, ... x5."""
    w = World(rng=random.Random(25))
    planet = w.planets[0]
    p = w.add_player("p1", "tester")
    p.invuln_until = 0.0

    def force_sling():
        # teleport the ship into the sling zone with a fast tangential exit
        p.x = planet.x
        p.y = planet.y - planet.r * 1.5
        p.vx = planet.r * 6.0
        p.vy = 0.0
        for _ in range(200):
            evs = w.step()
            if not p.alive:
                return 0
            for e in evs:
                if e["t"] == "sling":
                    return e["combo"]
        return 0

    assert force_sling() == 1
    assert force_sling() == 2
    assert force_sling() == 3
    assert force_sling() == 4


def test_spawn_avoids_fresh_wrecks():
    w = World(rng=random.Random(26))
    # a wreck right in the middle of the arena; the next spawn must not land on it
    w.wrecks.append(Wreck(x=HALF, y=HALF, value=9, born=w.t))
    p = w.add_player("p1", "tester")
    assert math.hypot(p.x - HALF, p.y - HALF) > 120.0


def test_core_pickup_tracks_stats_and_value():
    w = World(rng=random.Random(30))
    p = _placed_player(w)
    p.invuln_until = 0.0
    c = w.cores[0]
    c.value = 5
    c.taken = False
    c.x, c.y = p.x + 10, p.y
    evs = w.step()
    pickup = next((e for e in evs if e["t"] == "pickup"), None)
    assert pickup is not None and pickup["value"] == 5
    assert p.stats["cores"] == 5, "core stat must track the value, not just a count"


def test_dist_accumulates_with_motion():
    w = World(rng=random.Random(31))
    p = _placed_player(w, x=1000, y=1000, vx=800, vy=0)
    before = p.stats["dist"]
    for _ in range(60):
        w.step()
    assert p.alive
    assert p.stats["dist"] > before + 100.0, "moving ships must accrue distance"


def test_wreck_keeps_owner_and_loot_reaches_grabber():
    """Regression: a wreck must remember who dropped it so the killfeed can
    attribute theft, and the grabber must see value + owner in the event.
    """
    w = World(rng=random.Random(32))
    po = _placed_player(w, pid="owner", x=3000, y=3000)
    po.invuln_until = 1e9  # keeps the drop origin stable
    w.wrecks.append(Wreck(x=po.x + 5, y=po.y, value=11, born=w.t,
                          owner=po.id, owner_name=po.name))
    p = _placed_player(w, pid="thief", x=po.x, y=po.y + 12)
    p.invuln_until = 0.0
    evs = w.step()
    wreck = next((e for e in evs if e["t"] == "wreck"), None)
    assert wreck is not None
    assert wreck["id"] == "thief"
    assert wreck["owner"] == "owner" and wreck["owner_name"] == "tester"
    assert wreck["value"] == 11
    assert p.stats["wreck"] == 11


def test_die_event_carries_stats_and_arena_leader():
    w = World(rng=random.Random(33))
    victim = _placed_player(w, pid="v", x=2500, y=2500)
    leader = _placed_player(w, pid="l", x=600, y=600)
    leader.invuln_until = 1e9
    leader.score = 42
    victim.invuln_until = 0.0
    victim.score = 7
    victim.stats["cores"] = 9
    victim.stats["bestCombo"] = 3
    planet = w.planets[0]
    victim.x, victim.y = planet.x, planet.y  # faceplant into the gravity well
    die = None
    for _ in range(120):
        evs = w.step()
        die = next((e for e in evs if e["t"] == "die" and e["id"] == "v"), None)
        if die:
            break
    assert die is not None, "victim must die in the planet"
    assert die["score"] == 7, "carried score must be reported"
    assert die["stats"]["cores"] == 9 and die["stats"]["bestCombo"] == 3
    assert die["stats"]["deaths"] == 1
    assert die["top"]["name"] == "tester" and die["top"]["score"] == 42
    assert victim.stats["deaths"] == 1


def test_death_drops_wreck_with_owner_attribution():
    w = World(rng=random.Random(34))
    p = _placed_player(w, pid="d", x=2500, y=2500)
    p.invuln_until = 0.0
    p.score = 5
    planet = w.planets[0]
    p.x, p.y = planet.x, planet.y
    for _ in range(120):
        evs = w.step()
        if any(e["t"] == "die" and e["id"] == "d" for e in evs):
            break
    assert w.wrecks and w.wrecks[0].owner == "d", "wreck must track its dropper"