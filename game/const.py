"""Shared tuning constants for the world simulation.

Single source of truth for gameplay tuning. The server sim consumes these
directly; the client only mirrors a small subset for HUD rendering.
"""

# ------------------------------------------------------------ simulation
DT = 1.0 / 30.0  # fixed server tick (30 Hz)

# ------------------------------------------------------------- arena
WORLD = 4200.0
HALF = WORLD / 2.0

# ------------------------------------------------------------- player
PLAYER_RADIUS = 9.0
BASE_MAX_SPEED = 470.0
SLING_MAX_SPEED = 1280.0
SOFT_CAP_K = 2.1
THRUST_ACCEL = 290.0
DRAG = 0.05

# Skins: white-listed ids (client renders the hull shape); any unknown id
# falls back to the first entry on both sides.
SKINS = ("probe", "vortex", "hex", "blade")
SKIN_HUES = {"probe": 180, "vortex": 300, "hex": 25, "blade": 150}

# ------------------------------------------------------------- pickups
CORE_RADIUS = 6.0
CORE_RESPAWN = 2.5

# ------------------------------------------------------------- bonuses
BONUS_KINDS = ("shield", "boost", "magnet")
BONUS_COUNT = 9
BONUS_RADIUS = 12.0
BONUS_RESPAWN = 14.0
BONUS_SCORE = 2
SHIELD_DURATION = 5.0
BOOST_DURATION = 5.0
MAGNET_DURATION = 6.0
BOOST_THRUST_MULT = 2.0
MAGNET_RADIUS = 260.0
MAGNET_PULL = 420.0

# ------------------------------------------------------------- gravity
BODY_K_STAR = 560.0
BODY_K_PLANET = 150.0
BODY_K_ASTEROID = 0.0
GRAV_HARD_CAP = 900.0

# ----------------------------------------------------------- slingshot
SLING_ZONE = 2.05
SLING_TANG_ACCEL = 72.0
SLING_EVENT_SPEED = 330.0
SLING_MIN_TIME = 0.16
SLING_BOOST = 1.35
# Chained slings inside the combo window multiply the score pickup.
SLING_COMBO_WINDOW = 6.0  # seconds each sling has to chain on top of the last
SLING_COMBO_MAX = 5  # first is x1, then x2 ... capped at x5 per sling

# ------------------------------------------------------------ lifecycle
RESPAWN_DELAY = 2.2
INVULN = 3.0
WRECK_DECAY = 26.0  # uncollected wreck containers vanish after this many seconds

# ---------------------------------------------------------------- bots
MAX_BOTS = 10