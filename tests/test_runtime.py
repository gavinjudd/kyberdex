import sqlite3
from contextlib import closing

from venator_vault.runtime import Runtime


def test_schedule_survives_restart_and_pause(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    saved = Runtime(path).configure("guild", "channel", 900, "owner", now=100)
    due = saved["next_drop"]
    assert 100+720 <= due <= 100+1200
    restored = Runtime(path)
    assert restored.get("guild") == saved
    assert not restored.due(now=due-1)
    assert not restored.due(now=due)
    for index in range(3):
        restored.record_activity("guild", str(index), index+1, now=due)
    assert restored.due(now=due)[0]["channel_id"] == "channel"
    restored.advance("guild", now=due)
    assert due+720 <= restored.get("guild")["next_drop"] <= due+1200
    assert not restored.due(now=due+1)
    restored.pause("guild", "owner", True)
    assert not Runtime(path).due(now=10**12)


def test_failed_post_retry_is_bounded_and_not_full_interval(tmp_path):
    runtime = Runtime(tmp_path / "runtime.sqlite3")
    due = runtime.configure("g", "c", 900, "u", now=0)["next_drop"]
    runtime.advance("g", now=due, retry=True)
    assert runtime.get("g")["next_drop"] == due+60
    assert runtime.get("g")["base_next_drop"] == due


def test_message_recovery_rotates_all_active_tables_and_retains_exact_lookup(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    runtime = Runtime(path)
    for index in range(75):
        runtime.bind("trade", str(index), "channel", str(index+100))
    first = runtime.bindings("trade")
    assert len(first) == 50
    for row in first:
        runtime.refreshed("trade", row["entity_id"])
    second = Runtime(path).bindings("trade")
    assert len({row["entity_id"] for row in first+second}) == 75
    runtime.refreshed("trade", "0", terminal=True)
    assert runtime.binding("trade", "0")["message_id"] == "100"
    assert "0" not in {row["entity_id"] for row in runtime.bindings("trade")}


def test_restart_completes_an_interrupted_message_binding_migration(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("""CREATE TABLE message_bindings (
            kind TEXT NOT NULL, entity_id TEXT NOT NULL, channel_id TEXT NOT NULL,
            message_id TEXT NOT NULL, created_at REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(kind,entity_id))""")
        connection.execute("INSERT INTO message_bindings VALUES ('trade','one','channel','message',100,1)")
        connection.commit()
    restored = Runtime(path)
    rows = restored.bindings("trade")
    assert len(rows) == 1 and rows[0]["message_id"] == "message"
    assert rows[0]["last_refresh"] == 0
    restored.refreshed("trade", "one", terminal=True)
    assert Runtime(path).bindings("trade") == []
