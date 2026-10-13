import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from venator_vault.runtime import ADMIN_EVENT_DURATION, SCHEMA_VERSION, Runtime


def rows(runtime, table):
    with runtime.connection() as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]


def test_event_hour_survives_restart_and_expires_without_catch_up(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    runtime = Runtime(path)
    event = runtime.start_event("guild", "manager", "request", now=100)
    assert event["starts_at"] == 100 and event["ends_at"] == 3700
    assert event["ends_at"] - event["starts_at"] == ADMIN_EVENT_DURATION == 3600
    assert event["active"] is True
    assert runtime.event("other", now=101) is None
    assert runtime.event("guild", now=99) is None
    assert Runtime(path).event("guild", now=100) == event
    assert Runtime(path).event("guild", now=3699.99) == event
    assert Runtime(path).event("guild", now=3700) is None
    assert Runtime(path).event("guild", now=100000) is None
    assert len(rows(runtime, "admin_events")) == 1
    audit = rows(runtime, "audit")
    assert len(audit) == 1
    assert audit[0]["actor_id"] == "manager" and audit[0]["occurred_at"] == 100
    assert json.loads(audit[0]["detail"])["ends_at"] == 3700


def test_current_and_old_interaction_replays_never_extend_or_restart(tmp_path):
    runtime = Runtime(tmp_path / "runtime.sqlite3")
    first = runtime.start_event("guild", "manager", "first", now=100)
    assert runtime.start_event("guild", "manager", "first", now=200) == first
    with pytest.raises(ValueError, match="already running"):
        runtime.start_event("guild", "manager", "different", now=200)
    with pytest.raises(ValueError, match="already running"):
        runtime.start_event("guild", "manager", now=200)
    assert runtime.event("guild", now=200) == first
    expired_replay = runtime.start_event("guild", "manager", "first", now=3700)
    assert expired_replay["id"] == first["id"] and expired_replay["active"] is False
    assert runtime.event("guild", now=3700) is None
    second = runtime.start_event("guild", "manager", "second", now=3700)
    assert second["active"] is True and second["ends_at"] == 7300
    old_replay = runtime.start_event("guild", "manager", "first", now=3800)
    assert old_replay["id"] == first["id"] and old_replay["active"] is False
    assert runtime.event("guild", now=3800) == second
    assert len(rows(runtime, "admin_events")) == len(rows(runtime, "audit")) == 2


@pytest.mark.parametrize("guild,actor", [("other", "manager"), ("guild", "other")])
def test_replay_cannot_cross_guild_or_actor(tmp_path, guild, actor):
    runtime = Runtime(tmp_path / "runtime.sqlite3")
    runtime.start_event("guild", "manager", "request", now=100)
    with pytest.raises(ValueError, match="different server or administrator"):
        runtime.start_event(guild, actor, "request", now=200)
    assert len(rows(runtime, "admin_events")) == 1


def test_stop_retains_audit_and_does_not_reopen_on_replay(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    runtime = Runtime(path)
    first = runtime.start_event("guild", "manager", "request", now=100)
    assert runtime.stop_event("other", "manager", now=200) is None
    assert runtime.stop_event("guild", "manager", now=99) is None
    stopped = runtime.stop_event("guild", "second-manager", now=200)
    assert stopped["id"] == first["id"] and stopped["active"] is False
    assert stopped["stopped_at"] == 200 and stopped["stopped_by"] == "second-manager"
    assert stopped["ends_at"] == 3700
    assert Runtime(path).event("guild", now=201) is None
    assert runtime.stop_event("guild", "manager", now=201) is None
    assert runtime.start_event("guild", "manager", "request", now=201) == stopped
    second = runtime.start_event("guild", "manager", "new-request", now=201)
    assert second["id"] != first["id"] and second["ends_at"] == 3801
    audit = rows(runtime, "audit")
    assert [row["action"] for row in audit] == ["start_event", "stop_event", "start_event"]
    assert audit[1]["occurred_at"] == 200 and audit[1]["actor_id"] == "second-manager"


def test_expired_stop_does_not_mutate_history(tmp_path):
    runtime = Runtime(tmp_path / "runtime.sqlite3")
    runtime.start_event("guild", "manager", "request", now=100)
    before = rows(runtime, "admin_events")
    assert runtime.stop_event("guild", "manager", now=3700) is None
    assert rows(runtime, "admin_events") == before
    assert len(rows(runtime, "audit")) == 1


def test_events_leave_schedule_pause_deadlines_and_activity_untouched(tmp_path):
    runtime = Runtime(tmp_path / "runtime.sqlite3")
    runtime.configure("guild", "channel", 900, "manager", now=100)
    runtime.record_activity("guild", "one", 1, now=101)
    runtime.record_activity("guild", "two", 2, now=102)
    runtime.record_activity("guild", "three", 3, now=103)
    runtime.pause("guild", "manager", True, now=104)
    config = runtime.get("guild")
    activity = rows(runtime, "spawn_activity")
    runtime.start_event("guild", "manager", "request", now=105)
    assert runtime.event("guild", now=106)["active"] is True
    runtime.stop_event("guild", "manager", now=107)
    runtime.start_event("guild", "manager", "other-request", now=108)
    assert runtime.event("guild", now=3708) is None
    assert runtime.get("guild") == config
    assert rows(runtime, "spawn_activity") == activity
    assert not runtime.due(now=100000) and not runtime.due_raids(now=100000)


@pytest.mark.parametrize("same_request", [False, True])
def test_concurrent_starts_commit_only_one_event(tmp_path, same_request):
    runtime = Runtime(tmp_path / "runtime.sqlite3")
    barrier = Barrier(2)

    def start(index):
        barrier.wait()
        try:
            return runtime.start_event("guild", "manager", "same" if same_request else str(index), now=100)
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, (1, 2)))
    successful = [result for result in results if isinstance(result, dict)]
    assert len(successful) == (2 if same_request else 1)
    assert len(rows(runtime, "admin_events")) == len(rows(runtime, "audit")) == 1
    if same_request:
        assert results[0] == results[1]
    else:
        assert "already running" in next(result for result in results if isinstance(result, str))


@pytest.mark.parametrize("method", ["start_event", "event", "stop_event", "event_retry"])
@pytest.mark.parametrize("bad_time", [float("nan"), float("inf"), float("-inf"), "invalid", True, object()])
def test_invalid_times_fail_without_writing(tmp_path, method, bad_time):
    runtime = Runtime(tmp_path / "runtime.sqlite3")
    args = ("guild",) if method in {"event", "event_retry"} else ("guild", "manager")
    with pytest.raises(ValueError, match="finite timestamp"):
        getattr(runtime, method)(*args, now=bad_time)
    assert rows(runtime, "admin_events") == rows(runtime, "audit") == []


@pytest.mark.parametrize("bad_time", [1e308, float(2**57)])
def test_unrepresentable_hour_is_rejected(tmp_path, bad_time):
    runtime = Runtime(tmp_path / "runtime.sqlite3")
    with pytest.raises(ValueError, match="one-hour duration"):
        runtime.start_event("guild", "manager", now=bad_time)
    assert rows(runtime, "admin_events") == []


def test_default_time_uses_wall_clock_for_start_lookup_and_stop(tmp_path, monkeypatch):
    monkeypatch.setattr("venator_vault.runtime.time.time", lambda: 100)
    runtime = Runtime(tmp_path / "runtime.sqlite3")
    started = runtime.start_event("guild", "manager")
    assert started["starts_at"] == 100 and started["ends_at"] == 3700
    monkeypatch.setattr("venator_vault.runtime.time.time", lambda: 200)
    assert runtime.event("guild")["id"] == started["id"]
    assert runtime.stop_event("guild", "manager")["stopped_at"] == 200
    assert runtime.event("guild") is None


def test_clock_rollback_does_not_stack_an_existing_hour(tmp_path):
    runtime = Runtime(tmp_path / "runtime.sqlite3")
    first = runtime.start_event("guild", "manager", "request", now=100)
    with pytest.raises(ValueError, match="already running"):
        runtime.start_event("guild", "manager", "other", now=99)
    assert runtime.event("guild", now=100) == first


def test_schema_one_migration_preserves_all_existing_runtime_records(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    runtime = Runtime(path)
    runtime.configure("guild", "channel", 900, "manager", now=100)
    runtime.record_activity("guild", "one", 1, now=101)
    runtime.bind("drop", "drop-id", "channel", "message")
    runtime.pause("guild", "manager", True, now=102)
    tables = ("guild_config", "spawn_activity", "audit", "message_bindings")
    original = {table: rows(runtime, table) for table in tables}
    with runtime.connection() as conn:
        conn.execute("DROP TABLE admin_events")
        conn.execute("PRAGMA user_version=1")
    migrated = Runtime(path)
    assert SCHEMA_VERSION == migrated.health()["schema_version"] == 2
    assert {table: rows(migrated, table) for table in tables} == original
    assert rows(migrated, "admin_events") == []
    assert migrated.start_event("guild", "manager", "request", now=103)["active"] is True


def test_newer_schema_is_rejected_without_migration(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    runtime = Runtime(path)
    runtime.start_event("guild", "manager", "request", now=100)
    original = rows(runtime, "admin_events")
    with runtime.connection() as conn:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION+1}")
    with pytest.raises(ValueError, match="newer schema"):
        Runtime(path)
    assert rows(runtime, "admin_events") == original
    with runtime.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION+1


def test_event_change_and_audit_commit_atomically(tmp_path):
    runtime = Runtime(tmp_path / "runtime.sqlite3")
    with runtime.connection() as conn:
        conn.execute("""CREATE TRIGGER reject_event_audit BEFORE INSERT ON audit
            WHEN NEW.action='start_event' BEGIN SELECT RAISE(ABORT,'audit unavailable'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
        runtime.start_event("guild", "manager", "request", now=100)
    assert rows(runtime, "admin_events") == rows(runtime, "audit") == []
    with runtime.connection() as conn:
        conn.execute("DROP TRIGGER reject_event_audit")
    original = runtime.start_event("guild", "manager", "request", now=100)
    with runtime.connection() as conn:
        conn.execute("""CREATE TRIGGER reject_event_audit BEFORE INSERT ON audit
            WHEN NEW.action='stop_event' BEGIN SELECT RAISE(ABORT,'audit unavailable'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
        runtime.stop_event("guild", "manager", now=101)
    assert runtime.event("guild", now=101) == original
    assert len(rows(runtime, "audit")) == 1


def test_event_retry_shortens_deadline_without_resampling_cycle(tmp_path):
    runtime = Runtime(tmp_path / "runtime.sqlite3")
    runtime.configure("guild", "channel", 900, "manager", now=100)
    runtime.record_activity("guild", "one", 1, now=101)
    before = runtime.get("guild")
    activity = rows(runtime, "spawn_activity")
    runtime.start_event("guild", "manager", "request", now=102)
    retried = runtime.event_retry("guild", now=103)
    assert retried == {**before, "next_drop": 163, "retrying": 1}
    assert rows(runtime, "spawn_activity") == activity
    # A failed immediate first draw must defer, even when already due.
    assert runtime.event_retry("guild", now=120) == {**retried, "next_drop": 180}
    assert runtime.event_retry("unconfigured", now=120) is None


def test_event_retry_preserves_pause_and_inactive_schedules(tmp_path):
    runtime = Runtime(tmp_path / "runtime.sqlite3")
    runtime.configure("guild", "channel", 900, "manager", now=100)
    original = runtime.get("guild")
    assert runtime.event_retry("guild", now=101) == original
    runtime.start_event("guild", "manager", "request", now=102)
    runtime.pause("guild", "manager", True, now=103)
    paused = runtime.get("guild")
    assert runtime.event_retry("guild", now=104) == paused
    runtime.pause("guild", "manager", False, now=105)
    resumed = runtime.get("guild")
    assert runtime.event_retry("guild", now=3702) == resumed
    runtime.start_event("guild", "manager", "second", now=3702)
    runtime.stop_event("guild", "manager", now=3703)
    assert runtime.event_retry("guild", now=3704) == resumed


def test_immediate_start_is_atomic_and_only_new_request_moves_deadline(tmp_path):
    runtime = Runtime(tmp_path / "runtime.sqlite3")
    runtime.configure("guild", "channel", 900, "manager", now=100)
    original = runtime.get("guild")
    started = runtime.start_event("guild", "manager", "request", now=101, immediate=True)
    immediate = runtime.get("guild")
    assert immediate == {**original, "next_drop": 101, "retrying": 1}
    assert runtime.event_retry("guild", now=101) == {**immediate, "next_drop": 161}
    runtime.advance("guild", now=102)
    advanced = runtime.get("guild")
    assert runtime.start_event("guild", "manager", "request", now=103, immediate=True) == started
    assert runtime.get("guild") == advanced
    runtime.stop_event("guild", "manager", now=104)
    with runtime.connection() as conn:
        conn.execute("""CREATE TRIGGER reject_event_audit BEFORE INSERT ON audit
            WHEN NEW.action='start_event' BEGIN SELECT RAISE(ABORT,'audit unavailable'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
        runtime.start_event("guild", "manager", "second", now=105, immediate=True)
    assert runtime.get("guild") == advanced
    assert runtime.event("guild", now=105) is None


def test_immediate_start_preserves_paused_or_absent_configuration(tmp_path):
    runtime = Runtime(tmp_path / "runtime.sqlite3")
    assert runtime.start_event("unconfigured", "manager", now=100, immediate=True)["active"] is True
    assert runtime.get("unconfigured") is None
    runtime.configure("guild", "channel", 900, "manager", now=100)
    runtime.pause("guild", "manager", True, now=101)
    paused = runtime.get("guild")
    runtime.start_event("guild", "manager", now=102, immediate=True)
    assert runtime.get("guild") == paused
