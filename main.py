"""SLINGOR.IO — authoritative server.

FastAPI + WebSocket rooms. Each room runs its own fixed-step simulation loop
and fans state out to its clients at ~16Hz with immediate event messages.

Design notes
------------
- The sim (`game.physics.World`) is pure and dt-driven; the event loop only
  steps it. Never put wall-clock/random semantics inside `step`.
- A websocket that fails to send is forced closed and cleaned up in exactly
  one place: the `finally` of `ws_endpoint` → `Room.remove_human`.
- Idle rooms (no humans for a grace period) are reaped on the next join
  attempt, so `rooms` does not grow without bound.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import random
import time
import weakref
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from game import __version__
from game.ai import BotBrain
from game.const import BONUS_KINDS, DT, MAX_BOTS, WORLD, WRECK_DECAY
from game.database import Database
from game.physics import World

log = logging.getLogger("slingor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
# overridable so tests can point the server at a scratch database
DB_PATH = Path(os.environ.get("SLINGOR_DB") or (BASE_DIR / "slingor.db"))

MAX_HUMANS = 24
STATE_EVERY = 2  # broadcast full state every N ticks (16.6 Hz @ 30tps)

# clients ping every 3s while alive; silence beyond this frees their slot
WS_RECV_TIMEOUT = 15.0
# server-driven close code used when a send fails mid-stream
_DFLT_CODE = 1011
# minimum spacing between accepted input messages (input-flood guard)
INPUT_MIN_INTERVAL = 0.04
# reject oversized websocket frames before json.loads
MAX_MSG_BYTES = 4096
# sustained message-rate anti-abuse: more than MAX_MSG_RATE * MSG_RATE_WINDOW
# frames inside the window earns an automated kick (bots' injective inputs go
# through the sim, so any ws client flooding past this is not playing)
MAX_MSG_RATE = 40.0  # messages per second, sustained
MSG_RATE_WINDOW = 3.0  # seconds
# keep an empty room's bots running this long after the last human left
ROOM_IDLE_GRACE = 60.0

HEADERS = {"Cache-Control": "no-store"}
STARTED = time.time()


class Room:
    def __init__(self, key: str, db: Database):
        self.key = key
        self.db = db
        # One RNG per room, explicitly seeded, so no global/shared RNG state
        # leaks between concurrent rooms (see AGENTS.md rule #6).
        self._rng = random.Random(int.from_bytes(os.urandom(8), "big"))
        self.world = World(self._rng)
        self.brain = BotBrain(self.world, self._rng)
        self.humans: dict[str, WebSocket] = {}
        self.bot_ids: list[str] = []
        self.dead = False
        self.task: asyncio.Task | None = None
        self._tick = 0
        self._seq = 0
        self._pending_sends: set[asyncio.Task] = set()
        # sockets already logged as broken (WeakSet → no unbounded growth)
        self._warned: weakref.WeakSet[WebSocket] = weakref.WeakSet()
        self._last_input: dict[str, float] = {}  # pid -> monotonic ts
        self._msg_ts: dict[str, deque] = {}  # pid -> deque of msg timestamps
        self._empty_since: float | None = None  # monotonic ts of first empty state
        self._created = time.monotonic()

    # ------------------------------------------------------------ lifecycle
    def add_human(self, ws: WebSocket, name: str, skin: str = "probe") -> str:
        self._empty_since = None
        pid = f"h{self._seq}"
        self._seq += 1
        self.humans[pid] = ws
        self.world.add_player(pid, name or f"anon{pid}", is_bot=False, skin=skin)
        self._refill_bots()
        return pid

    def remove_human(self, pid: str) -> None:
        if pid not in self.humans:
            return  # idempotent: the single disconnect path may race with <self>
        self.humans.pop(pid, None)
        self.world.remove_player(pid)
        self._last_input.pop(pid, None)
        self._msg_ts.pop(pid, None)
        self._refill_bots()
        if not self.humans:
            self._empty_since = time.monotonic()

    def _refill_bots(self) -> None:
        while len(self.humans) + len(self.bot_ids) < MAX_BOTS:
            bid = f"b{self._seq}"
            self._seq += 1
            self.bot_ids.append(bid)
            self.world.add_player(bid, f"BOT-{self._seq}", is_bot=True)
            self.brain.orbit[bid] = self._seq % 6

    # -------------------------------------------------------------- network
    @staticmethod
    def _buff_left(world: World, p, attr: str) -> int:
        left = getattr(p, f"{attr}_until") - world.t
        return max(0, math.ceil(left))

    def _roster(self) -> list[list[Any]]:
        world = self.world
        return [
            [p.id, round(p.x, 1), round(p.y, 1), round(p.vx, 1), round(p.vy, 1),
             p.score, p.name, p.color, int(p.alive), p.kills, p.skin,
             self._buff_left(world, p, "shield"),
             self._buff_left(world, p, "boost"),
             self._buff_left(world, p, "magnet"),
             p.sling_combo]
            for p in world.players.values()
        ]

    def _bonus_state(self) -> list[list[Any]]:
        return [[round(b.x, 1), round(b.y, 1), BONUS_KINDS.index(b.kind)]
                for b in self.world.bonuses if not b.taken]

    def send_sync(self, ws: WebSocket, msg: dict) -> None:
        task = asyncio.create_task(self._send(ws, msg))
        self._pending_sends.add(task)
        task.add_done_callback(self._pending_sends.discard)

    async def _send(self, ws: WebSocket, msg: dict) -> None:
        try:
            await ws.send_text(json.dumps(msg, ensure_ascii=False, separators=(",", ":")))
        except Exception as exc:
            # single point where a dead socket surfaces: log once, then force
            # the pending receive to abort so the ws_endpoint finally runs
            # remove_human exactly once.
            if ws not in self._warned:
                self._warned.add(ws)
                log.warning("send to ws %s failed (%s); closing", id(ws), exc)
            await self._close_ws(ws)

    async def _close_ws(self, ws: WebSocket) -> None:
        try:
            await ws.close(code=_DFLT_CODE)
        except Exception as exc:
            # the recv loop will eventually raise either way; log so a broken
            # connection is traceable instead of swallowing it silently.
            log.debug("ws.close for %s failed: %s", id(ws), exc)

    async def hello(self, ws: WebSocket, pid: str) -> None:
        world = self.world
        bodies: list[list[Any]] = [
            [b.x, b.y, b.r, b.name, b.hue, b.kind] for b in world.bodies
        ]
        cores = [[round(c.x, 1), round(c.y, 1), c.value] for c in world.cores]
        player = world.players.get(pid)
        await self._send(ws, {
            "t": "hello",
            "id": pid,
            "world": {"w": WORLD, "h": WORLD},
            "bodies": bodies,
            "cores": cores,
            "bonuses": self._bonus_state(),
            "players": self._roster(),
            "a": time.time(),
            "you": {"name": player.name if player else "", "skin": player.skin if player else "probe"},
            "version": __version__,
        })

    def broadcast(self, msg: dict) -> None:
        for pid, ws in list(self.humans.items()):
            if pid not in self.world.players:
                continue
            self.send_sync(ws, msg)

    def state_msg(self) -> dict:
        world = self.world
        cores = [[round(c.x, 1), round(c.y, 1), c.value] for c in world.cores if not c.taken]
        wrecks = [[round(w.x, 1), round(w.y, 1), w.value,
                   max(0.0, round(w.born + WRECK_DECAY - world.t, 1))]
                  for w in world.wrecks]
        return {
            "t": "st",
            "p": self._roster(),
            "c": cores,
            "w": wrecks,
            "bo": self._bonus_state(),
            "a": time.time(),
        }

    # ---------------------------------------------------------------- loop
    def kill(self) -> None:
        """Stop this room exactly once. The single source of truth for dead."""
        if self.dead:
            return
        self.dead = True
        if self.task and not self.task.done():
            self.task.cancel()

    async def run(self) -> None:
        try:
            while not self.dead:
                t0 = time.monotonic()
                self._tick += 1
                events = self.world.step(DT)
                self._after_tick(events)
                if events:
                    self.broadcast({"t": "evt", "ev": events, "a": time.time()})
                if self._tick % STATE_EVERY == 0:
                    self.broadcast(self.state_msg())
                elapsed = time.monotonic() - t0
                await asyncio.sleep(max(0.0, DT - elapsed))
        except asyncio.CancelledError:
            log.debug("room %s loop cancelled", self.key)
        finally:
            self.dead = True

    def _after_tick(self, events: list[dict]) -> None:
        for e in events:
            if e["t"] == "die":
                player = self.world.players.get(e["id"])
                if player is not None and not player.is_bot:
                    self.db.submit(player.name, e.get("score", 0))
        # bot steering
        for bid in self.bot_ids:
            if bid in self.world.players:
                self.brain.decide(bid)


def _run_room_task(room: Room) -> asyncio.Task:
    async def runner() -> None:
        try:
            await room.run()
        except Exception:
            room.dead = True
            log.exception("room %s crashed", room.key)

    return asyncio.create_task(runner())


async def _stop_room(room: Room) -> None:
    if room.task and not room.task.done():
        room.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await room.task


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await asyncio.gather(*(_stop_room(r) for r in rooms.values()), return_exceptions=True)


app = FastAPI(title="SLINGOR", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
db = Database(DB_PATH)
rooms: dict[str, Room] = {}


@app.middleware("http")
async def no_store_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store"
    return response


def reap_rooms() -> None:
    """Drop rooms whose bots have been alone for the idle grace period."""
    now = time.monotonic()
    for key, room in list(rooms.items()):
        if room.humans:
            room._empty_since = None
            continue
        if room._empty_since is None:
            room._empty_since = now
        elif now - room._empty_since >= ROOM_IDLE_GRACE:
            room.kill()
            rooms.pop(key, None)
            log.info("room %s reaped after idle grace", key)


def get_room(key: str) -> Room:
    reap_rooms()
    room = rooms.get(key)
    if room is None or room.dead:
        room = Room(key, db)
        rooms[key] = room
        room.task = _run_room_task(room)
    return room


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers=HEADERS)


@app.get("/api/version")
async def api_version():
    return {"version": __version__}


@app.get("/api/stats")
async def api_stats():
    online = sum(len(r.humans) for r in rooms.values())
    bots = sum(len(r.bot_ids) for r in rooms.values())
    cores = len(rooms[list(rooms)[0]].world.cores) if rooms else 0
    return {"version": __version__, "online": online, "bots": bots,
            "rooms": len(rooms), "deaths": db.deaths(), "cores": cores,
            "uptime": round(time.time() - STARTED, 1)}


@app.get("/api/rooms")
async def api_rooms():
    out = []
    for key, room in rooms.items():
        out.append({
            "key": key,
            "humans": len(room.humans),
            "bots": len(room.bot_ids),
            "dead": room.dead,
            "age": round(time.monotonic() - room._created, 1),
        })
    return {"rooms": out, "online": sum(r["humans"] for r in out), "total": len(out)}


@app.get("/api/leaderboard")
async def api_leaderboard():
    return {"top": db.top(10)}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, name: str = "", room: str = "r1", skin: str = "probe"):
    await websocket.accept()
    key = f"r{room.strip()}" if room and room.strip() else "r1"
    r = get_room(key)
    if len(r.humans) >= MAX_HUMANS:
        await r._send(websocket, {"t": "full"})
        await websocket.close()
        return
    pid = r.add_human(websocket, name.strip()[:24], skin=skin.strip()[:16])
    await r.hello(websocket, pid)
    try:
        while True:
            raw = await asyncio.wait_for(
                websocket.receive_text(), timeout=WS_RECV_TIMEOUT
            )
            # anti-abuse: sliding-window message-rate kick (counts every frame,
            # including malformed ones, so junk floods get reaped too)
            now = time.monotonic()
            ts = r._msg_ts.setdefault(pid, deque())
            ts.append(now)
            while ts[0] < now - MSG_RATE_WINDOW:
                ts.popleft()
            if len(ts) > MAX_MSG_RATE * MSG_RATE_WINDOW:
                await r._send(websocket, {"t": "kick", "reason": "msg_rate"})
                await websocket.close(code=1008)
                return
            if len(raw) > MAX_MSG_BYTES:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            mtype = data.get("t")
            if mtype == "input":
                keys = data.get("keys")
                d = data.get("d")
                if isinstance(d, list) and len(d) == 2 and all(
                        isinstance(v, (int, float)) and math.isfinite(v) for v in d):
                    player = r.world.players.get(pid)
                    if player is None:
                        continue
                    now = time.monotonic()
                    if now - r._last_input.get(pid, 0.0) < INPUT_MIN_INTERVAL:
                        continue  # input flood guard
                    r._last_input[pid] = now
                    player.input = {"dx": min(1.0, max(-1.0, d[0])),
                                    "dy": min(1.0, max(-1.0, d[1]))}
                elif isinstance(keys, dict):
                    player = r.world.players.get(pid)
                    if player is None:
                        continue
                    now = time.monotonic()
                    if now - r._last_input.get(pid, 0.0) < INPUT_MIN_INTERVAL:
                        continue  # input flood guard
                    r._last_input[pid] = now
                    player.input = {k: bool(keys.get(k, False))
                                    for k in ("up", "down", "left", "right")}
            elif mtype == "ping":
                await r._send(websocket, {"t": "pong", "a": time.time()})
    except WebSocketDisconnect:
        pass
    except asyncio.TimeoutError:
        # silent/broken client: release the slot instead of holding it forever
        await r._close_ws(websocket)
    finally:
        r.remove_human(pid)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8033, log_level="info")


if __name__ == "__main__":
    main()