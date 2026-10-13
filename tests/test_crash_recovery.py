"""Kill disposable workers at real SQLite write/commit boundaries.

These are process-crash tests, not physical power-cut or SD-controller tests.
No test opens a configured production database or signals an existing process.
"""
import json
import os
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from venator_vault.core import Game
from venator_vault.runtime import Runtime

NOW = 2_000_000_000
ROOT = Path(__file__).resolve().parents[1]


def snapshot(path):
    with closing(sqlite3.connect(path)) as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {table: db.execute('SELECT * FROM "'+table.replace('"', '""')+'" ORDER BY rowid').fetchall()
                for table in tables}


def verify_sqlite(path):
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def terminate_worker(payload, tmp_path, mechanism):
    payload = {**payload, "mechanism": mechanism, "marker": str(tmp_path / "crash-marker.json")}
    path = tmp_path / "crash-input.json"
    path.write_text(json.dumps(payload))
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--crash-worker", str(path)],
                            env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
                            capture_output=True, text=True, timeout=15, check=False)
    expected = -signal.SIGKILL if mechanism == "sigkill" else 73
    assert result.returncode == expected, result.stdout + result.stderr
    marker = json.loads(Path(payload["marker"]).read_text())
    assert marker["reached_fault"]
    # The killed worker leaves its WAL in place; reopening must recover it.
    assert Path(payload["database"]+"-wal").is_file()
    return marker


@pytest.fixture
def game_case(tmp_path):
    cards = [{"id": key, "name": key.title(), "aliases": [], "rarity": rarity,
              "category": "canon", "arena": "ground", "faction": "Republic", "role": "striker",
              "attack": 100, "health": 400, "speed": 70, "weight": 1, "release": None}
             for key, rarity in (("alpha", "common"), ("beta", "common"), ("result", "uncommon"))]
    catalog = tmp_path / "cards.json"
    catalog.write_text(json.dumps({"schema_version": 1, "cards": cards}))
    database = tmp_path / "game.sqlite3"
    return Game(database, catalog), {"database": str(database), "catalog": str(catalog)}


def seed(game, user, card="alpha", count=1):
    with game._tx() as db:
        return [game._mint(db, "guild", user, card, NOW, "crash fixture")["id"] for _ in range(count)]


@pytest.mark.parametrize("mechanism", ["sigkill", "exit"])
@pytest.mark.parametrize("boundary", ["partial", "committed"])
@pytest.mark.parametrize("operation", ["capture", "trade", "craft"])
def test_killed_game_operation_rolls_back_or_preserves_whole_commit(game_case, tmp_path, mechanism, boundary, operation):
    game, payload = game_case
    payload.update(operation=operation, boundary=boundary)
    if operation == "capture":
        drop = game.create_drop("guild", "channel", "alpha", now=NOW)
        payload["drop"] = drop["id"]
    elif operation == "trade":
        payload["a"] = seed(game, "a")[0]
        payload["b"] = seed(game, "b", "beta")[0]
        trade = game.trade_create("guild", "a", "b", now=NOW)
        game.trade_offer(trade["id"], "a", [payload["a"]], now=NOW)
        trade = game.trade_offer(trade["id"], "b", [payload["b"]], now=NOW)
        game.trade_accept(trade["id"], "a", trade["version"], now=NOW)
        payload.update(trade=trade["id"], version=trade["version"])
    else:
        payload["ingredients"] = seed(game, "a", count=3)
        with game._tx() as db:
            game._credit(db, "guild", "a", 200, "fixture", "seed", NOW)
    before = snapshot(payload["database"])
    marker = terminate_worker(payload, tmp_path, mechanism)
    assert marker["in_transaction"] is (boundary == "partial")
    assert marker["wal_bytes"] > 32
    verify_sqlite(payload["database"])
    reopened = Game(payload["database"], payload["catalog"])
    if boundary == "partial":
        assert snapshot(payload["database"]) == before
    elif operation == "capture":
        drop = reopened.get_drop(payload["drop"])
        assert len(drop["claims"]) == 1 and drop["remaining"] == 1
        assert reopened.profile("guild", "a")["credits"] == 25
        assert reopened.profile("guild", "a")["xp"] == 10
        assert reopened.claim(payload["drop"], "a", "Alpha", now=NOW+1)["replayed"]
        assert reopened.profile("guild", "a")["credits"] == 25
    elif operation == "trade":
        assert reopened.info("guild", "b", payload["a"])["locked_by"] is None
        assert reopened.info("guild", "a", payload["b"])["locked_by"] is None
        assert reopened.get_trade(payload["trade"])["status"] == "completed"
    else:
        assert reopened.profile("guild", "a")["credits"] == 50
        inventory = reopened.inventory("guild", "a")
        assert len(inventory) == 1 and inventory[0]["card_id"] == "result"
        with closing(sqlite3.connect(payload["database"])) as db:
            assert db.execute("SELECT COUNT(*) FROM instances WHERE consumed_at IS NOT NULL").fetchone()[0] == 3
            assert db.execute("SELECT COUNT(*) FROM ledger WHERE reason='craft'").fetchone()[0] == 1
    health = reopened.health()
    assert (health["schema_version"], health["synchronous"], health["foreign_keys"], health["foreign_key_violations"]) == (7, 2, True, 0)


@pytest.mark.parametrize("mechanism", ["sigkill", "exit"])
@pytest.mark.parametrize("boundary", ["partial", "committed"])
@pytest.mark.parametrize("operation", ["runtime_advance", "runtime_bind"])
def test_killed_runtime_transaction_recovers_committed_schedule_and_binding(tmp_path, mechanism, boundary, operation):
    path = tmp_path / "runtime.sqlite3"
    runtime = Runtime(path)
    runtime.configure("guild", "channel", 900, "owner", now=NOW)
    before = snapshot(path)
    marker = terminate_worker({"database": str(path), "operation": operation, "boundary": boundary}, tmp_path, mechanism)
    assert marker["in_transaction"] is (boundary == "partial")
    verify_sqlite(path)
    reopened = Runtime(path)
    if boundary == "partial":
        assert snapshot(path) == before
    elif operation == "runtime_advance":
        saved = reopened.get("guild")
        assert saved["cycle_started_at"] == NOW+9000
        assert NOW+9720 <= saved["next_drop"] <= NOW+10200
        assert saved["base_next_drop"] == saved["next_drop"]
        assert saved["minimum_drop_at"] == NOW+9480
        assert Runtime(path).get("guild") == saved
    else:
        assert reopened.binding("trade", "trade-id")["message_id"] == "message"
    with reopened.connection() as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


@pytest.mark.parametrize("mechanism", ["sigkill", "exit"])
@pytest.mark.parametrize("kind", ["game", "runtime"])
def test_killed_initial_schema_creation_is_atomic(tmp_path, game_case, mechanism, kind):
    _game, source = game_case
    path = tmp_path / "empty.sqlite3"
    payload = {**source, "database": str(path), "operation": "init_"+kind, "boundary": "fresh"}
    marker = terminate_worker(payload, tmp_path, mechanism)
    assert marker["in_transaction"]
    verify_sqlite(path)
    assert snapshot(path) == {}
    if kind == "game":
        assert Game(path, source["catalog"]).health()["schema_version"] == 7
    else:
        assert Runtime(path).get("guild") is None


@pytest.mark.parametrize("mechanism", ["sigkill", "exit"])
@pytest.mark.parametrize("kind", ["game", "runtime"])
def test_killed_legacy_migration_reverts_columns_then_retries_cleanly(tmp_path, game_case, mechanism, kind):
    game, source = game_case
    if kind == "game":
        path = Path(source["database"])
        copy_id = seed(game, "a")[0]
        with closing(sqlite3.connect(path)) as db:
            db.execute("DROP INDEX published_drop_time")
            db.execute("DROP TRIGGER published_capture")
            for name in ("delivery_pending", "delivery_deadline", "capture_duration"):
                db.execute(f"ALTER TABLE drops DROP COLUMN {name}")
            db.execute("PRAGMA user_version=2")
            db.commit()
        migrated_table, missing_column = "drops", "delivery_pending"
    else:
        path = tmp_path / "legacy-runtime.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE guild_config(guild_id TEXT PRIMARY KEY,channel_id TEXT NOT NULL,
                interval_seconds INTEGER NOT NULL,paused INTEGER NOT NULL DEFAULT 0,next_drop REAL NOT NULL,
                configured_by TEXT NOT NULL,updated_at REAL NOT NULL)""")
            db.execute("INSERT INTO guild_config VALUES('guild','channel',900,0,2000000900,'owner',2000000000)")
            db.commit()
        migrated_table, missing_column = "guild_config", "next_raid"
    before = snapshot(path)
    marker = terminate_worker({**source, "database": str(path), "operation": "init_"+kind,
                               "boundary": "migration"}, tmp_path, mechanism)
    assert marker["in_transaction"]
    verify_sqlite(path)
    assert snapshot(path) == before
    with closing(sqlite3.connect(path)) as db:
        assert missing_column not in {row[1] for row in db.execute(f"PRAGMA table_info({migrated_table})")}
    if kind == "game":
        recovered = Game(path, source["catalog"])
        assert recovered.health()["schema_version"] == 7
        assert recovered.info("guild", "a", copy_id)["card_id"] == "alpha"
    else:
        assert Runtime(path).get("guild")["next_drop"] == NOW+900


@pytest.mark.parametrize("kind", ["game", "runtime"])
@pytest.mark.parametrize("damage", ["foreign_key", "constraint", "unreadable"])
def test_initialization_refuses_damaged_database_without_mutating_it(tmp_path, game_case, kind, damage):
    _game, source = game_case
    path = Path(source["database"]) if kind == "game" else tmp_path / "damaged-runtime.sqlite3"
    if kind == "runtime":
        Runtime(path).configure("guild", "channel", 900, "owner", now=NOW)
    with closing(sqlite3.connect(path)) as db:
        # A rollback-journal fixture makes any attempted migration/WAL conversion
        # visible in the byte comparison, independently of logical row checks.
        db.execute("PRAGMA journal_mode=DELETE")
        if kind == "game":
            db.execute("PRAGMA user_version=2")
            db.execute("DELETE FROM supply WHERE card_id='alpha'")
        else:
            # This partial legacy schema is otherwise recoverable. Refusal must
            # precede the CREATEs that would restore its missing tables.
            db.execute("DROP TABLE message_bindings")
            db.execute("DROP TABLE audit")
        if damage == "foreign_key":
            if kind == "game":
                db.execute("INSERT INTO attempts VALUES('missing-drop','user',0)")
            else:
                db.execute("CREATE TABLE dependent(guild_id TEXT REFERENCES guild_config(guild_id))")
                db.execute("INSERT INTO dependent VALUES('missing-guild')")
        elif damage == "constraint":
            db.execute("PRAGMA ignore_check_constraints=ON")
            if kind == "game":
                db.execute("UPDATE supply SET minted=-1 WHERE card_id='beta'")
            else:
                db.execute("UPDATE guild_config SET interval_seconds=1")
        db.commit()
    if damage == "unreadable":
        contents = path.read_bytes()
        path.write_bytes(b"corrupt database" + contents[16:])
    before = path.read_bytes()
    error = sqlite3.DatabaseError if damage == "unreadable" else ValueError
    with pytest.raises(error):
        Game(path, source["catalog"]) if kind == "game" else Runtime(path)
    assert path.read_bytes() == before
    assert not path.with_name(path.name + "-wal").exists()


def test_runtime_health_reports_real_connection_and_schema_state(tmp_path):
    health = Runtime(tmp_path / "runtime.sqlite3").health()
    assert health == {
        "schema_version": 2, "journal_mode": "wal", "synchronous": 2,
        "foreign_keys": True, "foreign_key_violations": 0, "integrity": "ok",
        "tables": ["admin_events", "audit", "guild_config", "message_bindings", "spawn_activity"],
    }


def crash_worker(payload):
    """Instrument only this newly spawned process; never change production code."""
    armed = payload["operation"].startswith("init_")
    base_connect = sqlite3.connect
    current_statement = ""

    def crash(connection):
        marker = {"reached_fault": True, "in_transaction": connection.in_transaction,
                  "statement": current_statement,
                  "wal_bytes": Path(payload["database"]+"-wal").stat().st_size}
        with Path(payload["marker"]).open("w") as handle:
            json.dump(marker, handle)
            handle.flush()
            os.fsync(handle.fileno())
        if payload["mechanism"] == "sigkill":
            os.kill(os.getpid(), signal.SIGKILL)
        os._exit(73)

    class InterruptedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            nonlocal current_statement
            result = super().execute(sql, parameters)
            normalized = " ".join(sql.split())
            current_statement = normalized
            prefixes = {
                "capture": "INSERT INTO claims", "trade": "UPDATE instances SET user_id=",
                "craft": "UPDATE instances SET consumed_at=", "runtime_advance": "UPDATE guild_config SET next_drop=",
                "runtime_bind": "INSERT OR REPLACE INTO message_bindings",
                "init_runtime": "CREATE TABLE IF NOT EXISTS guild_config" if payload["boundary"] == "fresh"
                                else "ALTER TABLE guild_config ADD COLUMN next_raid",
                "init_game": "ALTER TABLE drops ADD COLUMN delivery_pending",
            }
            if armed and payload["boundary"] != "committed" and normalized.startswith(prefixes[payload["operation"]]):
                crash(self)
            return result

        def commit(self):
            super().commit()
            if armed and payload["boundary"] == "committed":
                crash(self)

    def connect(*args, **kwargs):
        connection = base_connect(*args, **kwargs, factory=InterruptedConnection)
        # Force uncommitted dirty pages into the WAL before killing the child.
        connection.execute("PRAGMA cache_size=1")
        if armed and payload["operation"] == "init_game" and payload["boundary"] == "fresh":
            def trace(sql):
                nonlocal current_statement
                current_statement = " ".join(sql.split())
                if current_statement.startswith("CREATE TABLE IF NOT EXISTS supply"):
                    crash(connection)
            connection.set_trace_callback(trace)
        return connection

    sqlite3.connect = connect
    if "runtime" in payload["operation"]:
        runtime = Runtime(Path(payload["database"]))
        armed = True
        if payload["operation"] == "runtime_advance":
            runtime.advance("guild", now=NOW+9000)
        elif payload["operation"] == "runtime_bind":
            runtime.bind("trade", "trade-id", "channel", "message")
    else:
        game = Game(payload["database"], payload["catalog"])
        armed = True
        if payload["operation"] == "capture":
            game.claim(payload["drop"], "a", "Alpha", now=NOW+1)
        elif payload["operation"] == "trade":
            game.trade_accept(payload["trade"], "b", payload["version"], now=NOW+1)
        elif payload["operation"] == "craft":
            game.craft("guild", "a", payload["ingredients"], "result", now=NOW+1)
    raise AssertionError("The intended crash boundary was not reached")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--crash-worker":
        raise SystemExit("Only the disposable crash-test worker may execute this file directly")
    crash_worker(json.loads(Path(sys.argv[2]).read_text()))
