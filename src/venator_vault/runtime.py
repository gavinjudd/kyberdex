"""Small persistent scheduler state, kept separate from player transactions."""
from __future__ import annotations

import json
import math
import random
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2
ADMIN_EVENT_DURATION = 3600
MINIMUM_SPAWN_DELAY = 480
MAX_ACTIVITY_DISCOUNT = 60
ACTIVITY_CONTRIBUTION = 5
ACTIVITY_WINDOW = 300
ACTIVITY_USER_COOLDOWN = 120
ACTIVITY_USER_CYCLE_CAP = 4
ACTIVITY_MIN_USERS = 1


def spawn_bounds(interval, minimum_delay=MINIMUM_SPAWN_DELAY):
    """A nominal fifteen minutes samples a twelve-to-twenty minute cycle."""
    floor = max(MINIMUM_SPAWN_DELAY, int(minimum_delay))
    return max(floor, math.ceil(interval * 4 / 5)), max(floor, math.ceil(interval * 4 / 3))


class Runtime:
    def __init__(self, path: Path, *, minimum_delay=MINIMUM_SPAWN_DELAY, rng=None):
        self.path = path
        self.minimum_delay = max(MINIMUM_SPAWN_DELAY, int(minimum_delay))
        self.rng = rng or random.SystemRandom()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection(validate_existing=True) as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
                raise ValueError("The runtime database uses a newer schema; nothing migrated")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,
                interval_seconds INTEGER NOT NULL CHECK(interval_seconds BETWEEN 300 AND 86400),
                paused INTEGER NOT NULL DEFAULT 0, next_drop REAL NOT NULL,
                configured_by TEXT NOT NULL, updated_at REAL NOT NULL
            )""")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, occurred_at REAL NOT NULL,
                actor_id TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL
            )""")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS message_bindings (
                kind TEXT NOT NULL, entity_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                message_id TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY(kind,entity_id)
            )
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(guild_config)")}
            if "next_raid" not in columns:
                conn.execute("ALTER TABLE guild_config ADD COLUMN next_raid REAL NOT NULL DEFAULT 0")
            if "cycle_started_at" not in columns:
                conn.execute("ALTER TABLE guild_config ADD COLUMN cycle_started_at REAL NOT NULL DEFAULT 0")
                conn.execute("ALTER TABLE guild_config ADD COLUMN base_next_drop REAL NOT NULL DEFAULT 0")
                conn.execute("ALTER TABLE guild_config ADD COLUMN minimum_drop_at REAL NOT NULL DEFAULT 0")
                conn.execute("ALTER TABLE guild_config ADD COLUMN activity_discount_seconds INTEGER NOT NULL DEFAULT 0")
                conn.execute("ALTER TABLE guild_config ADD COLUMN retrying INTEGER NOT NULL DEFAULT 0")
                conn.execute("ALTER TABLE guild_config ADD COLUMN paused_at REAL")
                # Grandfather the exact legacy deadline, without allowing activity
                # to accelerate that partially observed cycle after migration.
                conn.execute("""UPDATE guild_config SET cycle_started_at=next_drop-interval_seconds,
                    base_next_drop=next_drop,minimum_drop_at=next_drop,
                    paused_at=CASE WHEN paused=1 THEN ? ELSE NULL END""", (time.time(),))
            conn.execute("""CREATE TABLE IF NOT EXISTS spawn_activity (
                guild_id TEXT NOT NULL, user_id TEXT NOT NULL,
                last_seen_at REAL NOT NULL, last_credit_at REAL,
                cycle_credits INTEGER NOT NULL DEFAULT 0 CHECK(cycle_credits BETWEEN 0 AND 4),
                last_message_id TEXT NOT NULL,
                PRIMARY KEY(guild_id,user_id),
                FOREIGN KEY(guild_id) REFERENCES guild_config(guild_id)
            )""")
            bindings_columns = {row[1] for row in conn.execute("PRAGMA table_info(message_bindings)")}
            if "active" not in bindings_columns:
                conn.execute("ALTER TABLE message_bindings ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            if "last_refresh" not in bindings_columns:
                conn.execute("ALTER TABLE message_bindings ADD COLUMN last_refresh REAL NOT NULL DEFAULT 0")
            conn.execute("""CREATE TABLE IF NOT EXISTS admin_events (
                id INTEGER PRIMARY KEY, guild_id TEXT NOT NULL,
                actor_id TEXT NOT NULL, request_id TEXT UNIQUE,
                starts_at REAL NOT NULL, ends_at REAL NOT NULL,
                current INTEGER NOT NULL DEFAULT 1 CHECK(current IN (0,1)),
                stopped_at REAL, stopped_by TEXT,
                CHECK(ends_at=starts_at+3600 AND ends_at>starts_at),
                CHECK((stopped_at IS NULL AND stopped_by IS NULL)
                    OR (stopped_at IS NOT NULL AND stopped_by IS NOT NULL AND current=0))
            )""")
            conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_current_admin_event
                ON admin_events(guild_id) WHERE current=1""")
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def connection(self, *, validate_existing=False):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            if validate_existing and (
                [row[0] for row in conn.execute("PRAGMA integrity_check")] != ["ok"]
                or conn.execute("PRAGMA foreign_key_check").fetchone() is not None
            ):
                raise ValueError("The runtime database failed integrity or foreign-key validation; nothing migrated")
            if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise ValueError("The runtime database could not enable WAL durability")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            if (conn.execute("PRAGMA synchronous").fetchone()[0] != 2
                or conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1):
                raise ValueError("The runtime database requires foreign keys and FULL synchronization")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def health(self):
        with self.connection() as conn:
            return {
                "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
                "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
                "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],
                "foreign_keys": bool(conn.execute("PRAGMA foreign_keys").fetchone()[0]),
                "foreign_key_violations": len(conn.execute("PRAGMA foreign_key_check").fetchall()),
                "integrity": conn.execute("PRAGMA quick_check").fetchone()[0],
                "tables": [row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")],
            }

    def configure(self, guild_id, channel_id, interval, actor_id, now=None):
        now = time.time() if now is None else now
        if not 300 <= interval <= 86400:
            raise ValueError("Drop interval must be 5 minutes to 24 hours.")
        with self.connection() as conn:
            existing = conn.execute("SELECT 1 FROM guild_config WHERE guild_id=?", (str(guild_id),)).fetchone()
            if existing:
                # Configuration changes apply to future cycles; re-running setup
                # cannot fish for a shorter random deadline or bypass a pause.
                conn.execute("""UPDATE guild_config SET channel_id=?,interval_seconds=?,configured_by=?,updated_at=?
                    WHERE guild_id=?""", (str(channel_id), interval, str(actor_id), now, str(guild_id)))
            else:
                due = now + self.rng.randint(*spawn_bounds(interval, self.minimum_delay))
                conn.execute("""INSERT INTO guild_config(guild_id,channel_id,interval_seconds,paused,next_drop,
                    configured_by,updated_at,next_raid,cycle_started_at,base_next_drop,minimum_drop_at)
                    VALUES (?,?,?,0,?,?,?,?,?,?,?)""",
                    (str(guild_id), str(channel_id), interval, due, str(actor_id), now, now+7200,
                     now, due, now+self.minimum_delay))
        self.audit(actor_id, "configure", {"guild_id": str(guild_id), "channel_id": str(channel_id), "interval": interval})
        return self.get(guild_id)

    def get(self, guild_id):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM guild_config WHERE guild_id=?", (str(guild_id),)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _event_time(now):
        try:
            timestamp = float(time.time() if now is None else now)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("An event requires a finite timestamp.") from exc
        if isinstance(now, bool) or not math.isfinite(timestamp):
            raise ValueError("An event requires a finite timestamp.")
        return timestamp

    @staticmethod
    def _event_state(row, now):
        result = dict(row)
        result["active"] = bool(row["current"] and row["stopped_at"] is None
            and row["starts_at"] <= now < row["ends_at"])
        return result

    def start_event(self, guild_id, actor_id, request_id=None, now=None, *, immediate=False):
        """Start one real-time hour; duplicate interaction IDs never extend it."""
        now = self._event_time(now)
        ends_at = now + ADMIN_EVENT_DURATION
        if (not math.isfinite(ends_at) or ends_at <= now
            or not math.isclose(ends_at-now, ADMIN_EVENT_DURATION, rel_tol=0, abs_tol=1e-6)):
            raise ValueError("The event timestamp cannot represent a one-hour duration.")
        guild_id, actor_id = str(guild_id), str(actor_id)
        request_id = str(request_id) if request_id is not None else None
        with self.connection() as conn:
            if request_id is not None:
                replay = conn.execute("SELECT * FROM admin_events WHERE request_id=?", (request_id,)).fetchone()
                if replay:
                    if replay["guild_id"] != guild_id or replay["actor_id"] != actor_id:
                        raise ValueError("That event request belongs to a different server or administrator.")
                    return self._event_state(replay, now)
            current = conn.execute("SELECT * FROM admin_events WHERE guild_id=? AND current=1",
                                   (guild_id,)).fetchone()
            if current and current["ends_at"] > now:
                raise ValueError("An equal-odds event is already running. It cannot be extended or stacked.")
            # Free the unique current slot only after natural expiry. The old
            # event and interaction ID remain available for history and replay.
            conn.execute("UPDATE admin_events SET current=0 WHERE guild_id=? AND current=1", (guild_id,))
            cursor = conn.execute("""INSERT INTO admin_events(guild_id,actor_id,request_id,starts_at,ends_at)
                VALUES (?,?,?,?,?)""", (guild_id, actor_id, request_id, now, ends_at))
            row = conn.execute("SELECT * FROM admin_events WHERE id=?", (cursor.lastrowid,)).fetchone()
            detail = {"id": row["id"], "guild_id": guild_id, "starts_at": now, "ends_at": ends_at}
            conn.execute("INSERT INTO audit(occurred_at,actor_id,action,detail) VALUES(?,?,?,?)",
                         (now, actor_id, "start_event", json.dumps(detail, separators=(",", ":"))))
            if immediate:
                conn.execute("""UPDATE guild_config SET next_drop=?,retrying=1
                    WHERE guild_id=? AND paused=0""", (now, guild_id))
            return self._event_state(row, now)

    def event(self, guild_id, now=None):
        """Return only the active wall-clock event; no catch-up after downtime."""
        now = self._event_time(now)
        with self.connection() as conn:
            row = conn.execute("""SELECT * FROM admin_events WHERE guild_id=? AND current=1
                AND stopped_at IS NULL AND starts_at<=? AND ends_at>?""", (str(guild_id), now, now)).fetchone()
            return self._event_state(row, now) if row else None

    def event_retry(self, guild_id, now=None):
        """Give an active hour a prompt retry without sampling a new cycle."""
        now = self._event_time(now)
        guild_id = str(guild_id)
        with self.connection() as conn:
            active = conn.execute("""SELECT 1 FROM admin_events WHERE guild_id=? AND current=1
                AND stopped_at IS NULL AND starts_at<=? AND ends_at>?""", (guild_id, now, now)).fetchone()
            if active:
                conn.execute("""UPDATE guild_config SET next_drop=?,retrying=1
                    WHERE guild_id=? AND paused=0""", (now+60, guild_id))
            row = conn.execute("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,)).fetchone()
            return dict(row) if row else None

    def stop_event(self, guild_id, actor_id, now=None):
        """End the current hour early without touching ordinary spawn state."""
        now = self._event_time(now)
        guild_id, actor_id = str(guild_id), str(actor_id)
        with self.connection() as conn:
            row = conn.execute("""SELECT * FROM admin_events WHERE guild_id=? AND current=1
                AND stopped_at IS NULL AND starts_at<=? AND ends_at>?""", (guild_id, now, now)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE admin_events SET current=0,stopped_at=?,stopped_by=? WHERE id=?",
                         (now, actor_id, row["id"]))
            detail = {"id": row["id"], "guild_id": guild_id, "starts_at": row["starts_at"],
                      "ends_at": row["ends_at"], "stopped_at": now}
            conn.execute("INSERT INTO audit(occurred_at,actor_id,action,detail) VALUES(?,?,?,?)",
                         (now, actor_id, "stop_event", json.dumps(detail, separators=(",", ":"))))
            stopped = conn.execute("SELECT * FROM admin_events WHERE id=?", (row["id"],)).fetchone()
            return self._event_state(stopped, now)

    def due(self, now=None):
        now = time.time() if now is None else now
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM guild_config WHERE paused=0 AND next_drop<=?", (now,))
                    if self._activity_count(conn, row, now) >= ACTIVITY_MIN_USERS]

    @staticmethod
    def _activity_count(conn, config, now):
        return conn.execute("""SELECT COUNT(*) FROM spawn_activity
            WHERE guild_id=? AND last_seen_at>=? AND last_seen_at<=?""",
            (config["guild_id"], max(now-ACTIVITY_WINDOW, config["cycle_started_at"]), now)).fetchone()[0]

    def activity_ready(self, guild_id, now=None):
        """Each automatic publication needs fresh human activity in its own cycle."""
        now = time.time() if now is None else now
        with self.connection() as conn:
            config = conn.execute("SELECT * FROM guild_config WHERE guild_id=?", (str(guild_id),)).fetchone()
            return bool(config and not config["paused"]
                        and self._activity_count(conn, config, now) >= ACTIVITY_MIN_USERS)

    def advance(self, guild_id, *, retry=False, now=None):
        now = time.time() if now is None else now
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM guild_config WHERE guild_id=?", (str(guild_id),)).fetchone()
            if not row:
                return
            if retry:
                conn.execute("UPDATE guild_config SET next_drop=MAX(next_drop,?),retrying=1 WHERE guild_id=?",
                             (now+60, str(guild_id)))
            elif now > row["cycle_started_at"]:
                due = now + self.rng.randint(*spawn_bounds(row["interval_seconds"], self.minimum_delay))
                conn.execute("""UPDATE guild_config SET next_drop=?,cycle_started_at=?,base_next_drop=?,
                    minimum_drop_at=?,activity_discount_seconds=0,retrying=0 WHERE guild_id=?""",
                    (due, now, due, now+self.minimum_delay, str(guild_id)))
                conn.execute("UPDATE spawn_activity SET cycle_credits=0 WHERE guild_id=?", (str(guild_id),))

    def pause(self, guild_id, actor_id, paused=True, now=None):
        now = time.time() if now is None else now
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM guild_config WHERE guild_id=?", (str(guild_id),)).fetchone()
            if row and bool(row["paused"]) != bool(paused):
                if paused:
                    conn.execute("UPDATE guild_config SET paused=1,paused_at=? WHERE guild_id=?", (now,str(guild_id)))
                else:
                    elapsed = max(0, now-(row["paused_at"] if row["paused_at"] is not None else now))
                    conn.execute("""UPDATE guild_config SET paused=0,paused_at=NULL,cycle_started_at=cycle_started_at+?,
                        next_drop=next_drop+?,base_next_drop=base_next_drop+?,minimum_drop_at=minimum_drop_at+?,
                        next_raid=next_raid+? WHERE guild_id=?""", (elapsed,elapsed,elapsed,elapsed,elapsed,str(guild_id)))
        self.audit(actor_id, "pause" if paused else "resume", {"guild_id": str(guild_id)})

    def record_activity(self, guild_id, user_id, message_id, *, now=None):
        """Metadata-only credit; caller filters approved channels and human messages."""
        now = time.time() if now is None else now
        guild_id, user_id = str(guild_id), str(user_id)
        message_number = int(message_id)
        if message_number <= 0 or not math.isfinite(now):
            raise ValueError("Activity requires a valid message ID and timestamp.")
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,)).fetchone()
            if not row or row["paused"] or now < row["cycle_started_at"]:
                return {"credited_seconds": 0, "reason": "inactive_cycle"}
            member = conn.execute("SELECT * FROM spawn_activity WHERE guild_id=? AND user_id=?",
                                  (guild_id,user_id)).fetchone()
            if member and message_number <= int(member["last_message_id"]):
                return {"credited_seconds": 0, "reason": "replayed_message"}
            conn.execute("""INSERT INTO spawn_activity(guild_id,user_id,last_seen_at,last_message_id)
                VALUES (?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,last_message_id=excluded.last_message_id""",
                (guild_id,user_id,now,str(message_number)))
            conn.execute("DELETE FROM spawn_activity WHERE guild_id=? AND last_seen_at<?",
                         (guild_id,min(now-86400,row["cycle_started_at"])))
            participants = self._activity_count(conn, row, now)
            if participants < ACTIVITY_MIN_USERS:
                return {"credited_seconds": 0, "reason": "needs_distinct_members"}
            # Overdue and retrying cycles still observe fresh chat so silence
            # cannot leave a due schedule permanently unable to become eligible.
            # Presence does not reroll or further accelerate those deadlines.
            if row["retrying"] or row["next_drop"] <= now:
                return {"credited_seconds": 0, "reason": "awaiting_spawn"}
            if member and member["cycle_credits"] >= ACTIVITY_USER_CYCLE_CAP:
                return {"credited_seconds": 0, "reason": "user_cycle_cap"}
            if member and member["last_credit_at"] is not None and now-member["last_credit_at"] < ACTIVITY_USER_COOLDOWN:
                return {"credited_seconds": 0, "reason": "user_cooldown"}
            discount = min(MAX_ACTIVITY_DISCOUNT,row["activity_discount_seconds"]+ACTIVITY_CONTRIBUTION)
            due = max(row["minimum_drop_at"], row["base_next_drop"]-discount, now)
            saved = row["next_drop"]-due
            if saved <= 0:
                return {"credited_seconds": 0, "reason": "cycle_limit"}
            conn.execute("""UPDATE guild_config SET next_drop=?,activity_discount_seconds=activity_discount_seconds+?
                WHERE guild_id=?""", (due,saved,guild_id))
            conn.execute("UPDATE spawn_activity SET last_credit_at=?,cycle_credits=cycle_credits+1 WHERE guild_id=? AND user_id=?",
                         (now,guild_id,user_id))
            return {"credited_seconds": saved, "reason": "credited"}

    def audit(self, actor_id, action, detail):
        with self.connection() as conn:
            conn.execute("INSERT INTO audit(occurred_at,actor_id,action,detail) VALUES(?,?,?,?)",
                         (time.time(), str(actor_id), action, json.dumps(detail, separators=(",", ":"))))

    def due_raids(self, now=None):
        now = time.time() if now is None else now
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM guild_config WHERE paused=0 AND next_raid<=?", (now,))]

    def advance_raid(self, guild_id, now=None, *, retry=False):
        now = time.time() if now is None else now
        with self.connection() as conn:
            conn.execute("UPDATE guild_config SET next_raid=? WHERE guild_id=?", (now+(60 if retry else 7200), str(guild_id)))

    def bind(self, kind, entity_id, channel_id, message_id):
        with self.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO message_bindings(kind,entity_id,channel_id,message_id,created_at) VALUES (?,?,?,?,?)",
                         (kind, str(entity_id), str(channel_id), str(message_id), time.time()))

    def bindings(self, kind):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM message_bindings WHERE kind=? AND active=1 ORDER BY last_refresh,created_at LIMIT 50", (kind,))]

    def refreshed(self, kind, entity_id, *, terminal=False):
        with self.connection() as conn:
            conn.execute("UPDATE message_bindings SET last_refresh=?,active=? WHERE kind=? AND entity_id=?",
                         (time.time(), int(not terminal), kind, str(entity_id)))

    def binding(self, kind, entity_id):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM message_bindings WHERE kind=? AND entity_id=?",
                               (kind, str(entity_id))).fetchone()
            return dict(row) if row else None
