"""Deterministic tests for the persistent leaderboard Database."""

from game.database import Database


def test_persist_across_reopen(tmp_path):
    db = Database(tmp_path / "scores.db")
    db.submit("alice", 42)
    db2 = Database(tmp_path / "scores.db")
    top = db2.top()
    assert len(top) == 1
    assert top[0]["name"] == "alice"
    assert top[0]["score"] == 42


def test_schema_created_on_first_open(tmp_path):
    db = Database(tmp_path / "scores.db")
    db.submit("carol", 7)
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scores'"
        ).fetchall()
    assert rows


def test_top_orders_desc(tmp_path):
    db = Database(tmp_path / "scores.db")
    for name, score in (("low", 1), ("mid", 50), ("high", 99)):
        db.submit(name, score)
    top = db.top(3)
    assert [t["score"] for t in top] == [99, 50, 1]
    assert top[0]["name"] == "high"


def test_top_limit(tmp_path):
    db = Database(tmp_path / "scores.db")
    for i in range(1, 11):
        db.submit(f"u{i}", i * 10)
    assert len(db.top(3)) == 3
    assert db.top(1)[0]["name"] == "u10"


def test_deaths_and_clear(tmp_path):
    db = Database(tmp_path / "scores.db")
    db.submit("a", 1)
    db.submit("b", 2)
    assert db.deaths() == 2
    db.clear()
    assert db.deaths() == 0
    assert db.top() == []


def test_trim_keeps_top_500(tmp_path):
    db = Database(tmp_path / "scores.db")
    for i in range(600):
        db.submit(f"p{i}", i)
    assert db.deaths() == 500
    assert db.top(1)[0]["score"] == 599
    assert db.top(500)[-1]["score"] == 100


def test_submit_ignores_low_scores(tmp_path):
    db = Database(tmp_path / "scores.db")
    db.submit("bob", 0)
    db.submit("bob", -5)
    assert db.deaths() == 0


def test_name_truncated_to_24(tmp_path):
    db = Database(tmp_path / "scores.db")
    db.submit("x" * 100, 10)
    assert db.top(1)[0]["name"] == "x" * 24