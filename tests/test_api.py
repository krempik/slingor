"""Integration tests: REST endpoints + WebSocket join/state/input roundtrip.

Each test gets a fresh Database on a temp file and an empty rooms dict, so
tests never touch the real dev leaderboard or leak rooms between runs.
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

import main
from game.database import Database


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "db", Database(tmp_path / "scores.db"))
    main.rooms.clear()
    with TestClient(main.app) as c:
        yield c


def test_version_endpoint(client):
    r = client.get("/api/version")
    assert r.status_code == 200
    assert r.json()["version"]


def test_stats_endpoint(client):
    r = client.get("/api/stats")
    assert r.status_code == 200
    data = r.json()
    assert "online" in data and "deaths" in data
    assert "uptime" in data and data["uptime"] >= 0


def test_leaderboard_endpoint(client):
    r = client.get("/api/leaderboard")
    assert r.status_code == 200
    assert isinstance(r.json()["top"], list)


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "SLINGOR" in r.text or "slingor" in r.text.lower()


def test_ws_join_and_state(client):
    with client.websocket_connect("/ws?name=vlad&skin=vortex") as ws:
        hello = ws.receive_json()
        assert hello["t"] == "hello"
        assert hello["world"]["w"] == main.WORLD
        assert len(hello["bodies"]) >= 7
        assert len(hello["cores"]) > 50
        assert len(hello["bonuses"]) > 0
        me = next((p for p in hello.get("players", []) if p[0] == hello["id"]), None)
        assert me is not None
        assert me[10] == "vortex", "chosen skin must be echoed in the roster"
        ws.send_json({"t": "input", "keys": {"up": True, "left": False}})
        for _ in range(40):
            msg = ws.receive_json()
            if msg["t"] == "st":
                ids = {p[0] for p in msg["p"]}
                assert hello["id"] in ids
                assert "bo" in msg, "state must carry the bonuses field"
                assert "w" in msg, "state must carry the wrecks field"
                break
        else:
            raise AssertionError("no state message received")


def test_two_clients_see_each_other(client):
    with client.websocket_connect("/ws?name=alice") as a, \
         client.websocket_connect("/ws?name=bob") as b:
        a.receive_json()
        b.receive_json()
        seen_b = None
        for _ in range(60):
            msg = a.receive_json()
            if msg["t"] == "st":
                names = {p[6] for p in msg["p"]}
                if "bob" in names:
                    seen_b = names
                    break
        assert seen_b, "alice never saw bob"


def test_bots_fill_arena(client):
    with client.websocket_connect("/ws?name=botsee") as ws:
        ws.receive_json()  # hello
        for _ in range(120):
            msg = ws.receive_json()
            if msg["t"] == "st":
                bots = [p for p in msg["p"] if p[6].startswith("BOT-")]
                assert len(bots) >= 4
                break
        else:
            raise AssertionError("no bots visible")


def test_unknown_skin_falls_back_to_probe(client):
    with client.websocket_connect("/ws?name=x&skin=galaxy") as ws:
        hello = ws.receive_json()
        me = next(p for p in hello["players"] if p[0] == hello["id"])
        assert me[10] == "probe"


def test_long_name_truncated_on_server(client):
    with client.websocket_connect("/ws?name=" + ("x" * 80)) as ws:
        hello = ws.receive_json()
        me = next(p for p in hello["players"] if p[0] == hello["id"])
        assert len(me[6]) <= 24


def test_rooms_endpoint_reports_rooms(client):
    with client.websocket_connect("/ws?name=roomie") as ws:
        ws.receive_json()  # hello
        r = client.get("/api/rooms")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 1
        assert data["online"] >= 1
        assert any(x["humans"] >= 1 for x in data["rooms"])


def test_message_flood_gets_kicked(client, monkeypatch):
    monkeypatch.setattr(main, "MSG_RATE_WINDOW", 0.1)
    monkeypatch.setattr(main, "MAX_MSG_RATE", 10.0)  # cap = 1 msg / window
    with client.websocket_connect("/ws?name=flooder") as ws:
        ws.receive_json()  # hello
        ws.send_text(json.dumps({"t": "ping"}))
        # the second message already blows the 1-msg window cap -> server closes
        for _ in range(8):
            try:
                ws.send_text(json.dumps({"t": "ping"}))
                ws.receive_json()
            except Exception:
                return
        raise AssertionError("flooder was never kicked")


def test_oversized_message_is_ignored(client):
    with client.websocket_connect("/ws?name=bigmsg") as ws:
        hello = ws.receive_json()
        blob = json.dumps({"t": "input", "keys": {"up": True, "left": False}})
        # a frame well above MAX_MSG_BYTES must be dropped, not crash the loop
        padding = "x" * 5000
        ws.send_text('{"t":"input","keys":{"up":true},"trash":"' + padding + '"}')
        ws.send_text(blob)
        got_state = False
        for _ in range(40):
            msg = ws.receive_json()
            if msg["t"] == "st" and hello["id"] in {p[0] for p in msg["p"]}:
                got_state = True
                break
        assert got_state, "server must keep serving after an oversized message"


def _drain_until_pong(ws):
    while True:
        msg = ws.receive_json()
        if msg.get("t") == "pong":
            return


def test_analog_input_vector_is_applied(client):
    """The ws reader must accept the analog `d:[dx,dy]` frame (and reject
    malformed ones) without crashing the room."""

    def player(pid):
        room = next(r for r in main.rooms.values() if pid in r.world.players)
        return room.world.players[pid]

    with client.websocket_connect("/ws?name=analog") as ws:
        hello = ws.receive_json()
        pid = hello["id"]
        ws.send_text(json.dumps({"t": "input", "d": [0.5, -0.2]}))
        ws.send_text(json.dumps({"t": "ping"}))
        _drain_until_pong(ws)
        assert player(pid).input == {"dx": 0.5, "dy": -0.2}
        # malformed vector is ignored, the loop keeps running
        ws.send_text(json.dumps({"t": "input", "d": ["bad", 1]}))
        ws.send_text(json.dumps({"t": "ping"}))
        _drain_until_pong(ws)
        assert player(pid).input == {"dx": 0.5, "dy": -0.2}
        # zeros disarm thrust cleanly (not the boolean fallback path)
        time.sleep(0.06)  # let the server's INPUT_MIN_INTERVAL window pass
        ws.send_text(json.dumps({"t": "input", "d": [0, 0]}))
        ws.send_text(json.dumps({"t": "ping"}))
        _drain_until_pong(ws)
        assert player(pid).input == {"dx": 0.0, "dy": 0.0}