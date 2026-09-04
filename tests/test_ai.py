"""BotBrain tests: deterministic decisions, avoidance, wreck chasing."""
import math
import random

from game.ai import BotBrain
from game.physics import Wreck, World


def test_brain_deterministic_for_seeded_rng():
    """Identical seeds -> identical input dicts (no wall-clock secrets)."""
    w1 = World(rng=random.Random(42))
    w2 = World(rng=random.Random(42))
    p1 = w1.add_player("b1", "BOT-1", is_bot=True)
    p2 = w2.add_player("b1", "BOT-1", is_bot=True)
    p1.x, p1.y = p2.x, p2.y = 1000.0, 1000.0
    b1 = BotBrain(w1, rng=random.Random(7))
    b2 = BotBrain(w2, rng=random.Random(7))
    b1.decide("b1")
    b2.decide("b1")
    assert p1.input == p2.input
    assert p1.skin == p2.skin


def test_brain_chases_valuable_wreck():
    w = World(rng=random.Random(43))
    p = w.add_player("b1", "BOT-1", is_bot=True)
    p.x, p.y = 1000.0, 1000.0
    w.wrecks.append(Wreck(x=1110.0, y=1000.0, value=9))
    brain = BotBrain(w, rng=random.Random(3))
    brain.decide("b1")
    # analog thrust must point +x (toward the wreck), not away
    assert p.input["dx"] > 0.5
    assert abs(p.input["dy"]) < 0.2


def test_brain_avoids_direct_body_collision():
    w = World(rng=random.Random(44))
    p = w.add_player("b1", "BOT-1", is_bot=True)
    planet = w.planets[0]
    # flying straight at the planet center
    p.x = planet.x
    p.y = planet.y - planet.r - 400.0
    p.vx = 0.0
    p.vy = 500.0
    brain = BotBrain(w, rng=random.Random(5))
    brain.decide("b1")
    dx, dy = p.input["dx"], p.input["dy"]
    # not a dead-straight dive into the body, and some thruster is engaged
    assert not (dy > 0.9 and abs(dx) < 0.1)
    assert math.hypot(dx, dy) > 0.5


def test_brain_cleared_when_dead():
    w = World(rng=random.Random(45))
    p = w.add_player("b1", "BOT-1", is_bot=True)
    p.x, p.y = 1000.0, 1000.0
    p.alive = False
    brain = BotBrain(w, rng=random.Random(6))
    brain.decide("b1")
    assert p.input == {}

    # and a dead bot never makes it back to full survival speed (sanity check
    # that decide() never writes input for corpses)
    p.alive = False
    for _ in range(10):
        brain.decide("b1")
    assert p.input == {}


def test_brain_survives_ten_seconds_near_planet():
    """Bots must not plough into the planet they orbit within 10 sim seconds."""
    w = World(rng=random.Random(46))
    p = w.add_player("b1", "BOT-1", is_bot=True)
    planet = w.planets[0]
    p.x = planet.x
    p.y = planet.y - planet.r * 2.6
    p.invuln_until = 0.0
    brain = BotBrain(w, rng=random.Random(9))
    for _ in range(300):
        brain.decide("b1")
        evs = w.step()
        if any(e["t"] == "die" and e["id"] == "b1" for e in evs):
            break
    assert p.alive, "bot crashed into its own orbit planet"
    assert math.hypot(p.vx, p.vy) > 0.0


def test_brain_steers_up_toward_wreck_north():
    """Regression: wreck targets must be RELATIVE vectors, not absolute coords.

    The old code returned absolute world positions, so a wreck anywhere north
    of the bot pushed it DOWN (all abs coords are positive). A bot fleeing a
    wreck northward must thrust up instead.
    """
    w = World(rng=random.Random(47))
    p = w.add_player("b1", "BOT-1", is_bot=True)
    p.x, p.y = 1000.0, 1000.0
    w.wrecks.append(Wreck(x=1000.0, y=900.0, value=8))  # due north
    brain = BotBrain(w, rng=random.Random(4))
    brain.decide("b1")
    # analog: thrust up (negative dy toward the wreck)
    assert p.input["dy"] < 0, "must thrust up toward the northern wreck"
    assert abs(p.input["dx"]) < 0.2


def test_brain_steers_left_toward_wreck_west():
    """Regression (mirror): a wreck due west must produce a left thrust."""
    w = World(rng=random.Random(48))
    p = w.add_player("b1", "BOT-1", is_bot=True)
    p.x, p.y = 1000.0, 1000.0
    w.wrecks.append(Wreck(x=850.0, y=1000.0, value=8))  # due west
    brain = BotBrain(w, rng=random.Random(4))
    brain.decide("b1")
    assert p.input["dx"] < 0
    assert abs(p.input["dy"]) < 0.2


def test_brain_deflects_away_from_close_player():
    """Regression: a treasure chase must not plough straight through a player
    who is standing between the bot and its loot — _steer blends a soft push."""
    w = World(rng=random.Random(51))
    p = w.add_player("b1", "BOT-1", is_bot=True)
    q = w.add_player("b2", "BOT-2", is_bot=True)
    p.x, p.y = 1000.0, 1000.0
    q.x, q.y = 1008.0, 1000.0  # close, slightly east
    w.wrecks.append(Wreck(x=1200.0, y=1200.0, value=9))  # east-down target
    brain = BotBrain(w, rng=random.Random(13))
    brain.decide("b1")
    # naive chase would thrust east-down; the close player east must fling it west
    assert p.input["dx"] < -0.3
    assert p.input["dy"] < 0.5


def test_brain_analog_magnitude_scales_thrust():
    """Regression: _set_input must emit the magnitude (0..1), not just a flag."""
    w = World(rng=random.Random(52))
    p = w.add_player("b1", "BOT-1", is_bot=True)
    p.x, p.y = 1000.0, 1000.0
    w.wrecks.append(Wreck(x=1200.0, y=1000.0, value=9))
    brain = BotBrain(w, rng=random.Random(13))
    brain.decide("b1")  # full-speed treasure chase -> unit magnitude
    assert math.hypot(p.input["dx"], p.input["dy"]) > 0.9
    # a partial steer keeps its reduced magnitude, waxing and waning smooth
    p.input.clear()
    brain._set_input(p, 1.0, 0.0, mag=0.35)
    assert abs(p.input["dx"] - 0.35) < 0.05
    assert abs(p.input["dy"]) < 0.05


def test_nearest_core_returns_actually_nearest():
    """Regression: _nearest_core_target used to return the FIRST core in range,
    not the closest. Park every other core out of range, leave one far and one
    near, and assert the near one wins by distance.
    """
    w = World(rng=random.Random(49))
    p = w.add_player("b1", "BOT-1", is_bot=True)
    p.x, p.y = 1000.0, 1000.0
    for c in w.cores:
        c.taken = False
        c.x = 0.0
        c.y = 5000.0  # far beyond CORE_RANGE
    far = w.cores[0]
    far.x, far.y = p.x + 800.0, p.y  # in range (CORE_RANGE=860) but far
    near = w.cores[1]
    near.x, near.y = p.x + 200.0, p.y
    brain = BotBrain(w, rng=random.Random(7))
    picked = brain._best_core(p)
    assert picked is not None
    dx, dy, d = picked
    assert abs(d - 200.0) < 1.0, "must pick the near core, got d=%s" % d
    assert dx > 0 and abs(dy) < 1.0