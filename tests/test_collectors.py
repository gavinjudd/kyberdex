"""Dynamic encounter capacity, legacy migration and global supply boundaries."""
import contextlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from venator_vault.core import COLLECTOR_LIMITS, Game, GameError, collector_limit

NOW = 2_000_000_000


def definition(key, rarity="common", **extra):
    return {"id": key, "name": key.title(), "aliases": [key], "rarity": rarity,
            "role": "striker", "arena": "ground", "faction": "Republic", "category": "canon",
            "attack": 100, "health": 500, "speed": 70, "weight": 1, "release": None, **extra}


def release(cap):
    return {"starts_at": NOW-1000, "ends_at": NOW+1000, "supply_cap": cap}


@pytest.fixture
def game_case(tmp_path):
    path = tmp_path/"cards.json"
    cards = [definition(rarity, rarity) for rarity in COLLECTOR_LIMITS]
    cards += [definition("limited", "rare", category="limited", release=release(3)),
              definition("event", "mythic", category="legends", release=release(20),
                         event_id="archive", collector_limit=6)]
    path.write_text(json.dumps({"schema_version": 1, "cards": cards, "rarities": {}}))
    return tmp_path/"game.sqlite", path


def build(case):
    return Game(*case, enabled_events=("archive",))


def stock(game, key):
    with contextlib.closing(sqlite3.connect(game.db_path)) as connection:
        return connection.execute("SELECT minted,reserved FROM supply WHERE card_id=?", (key,)).fetchone()


@pytest.mark.parametrize("rarity,expected", list(COLLECTOR_LIMITS.items()))
def test_each_rarity_capacity_is_snapshotted_and_single_user_replays(game_case, rarity, expected):
    game = build(game_case)
    drop = game.create_drop("guild", "channel", rarity, now=NOW)
    assert (drop["collector_limit"], drop["max_collectors"], drop["remaining"]) == (expected,)*3
    assert stock(game, rarity) == (0, expected)
    first = game.claim(drop["id"], "a", rarity, now=NOW)
    replay = game.claim(drop["id"], "a", rarity, now=NOW)
    assert replay["replayed"] and replay["credits_earned"] == 0
    assert replay["instance"]["id"] == first["instance"]["id"]
    assert replay["drop"]["collector_limit"] == expected
    if expected == 2:
        game.claim(drop["id"], "b", rarity, now=NOW)
    with pytest.raises(GameError, match="full or expired"):
        game.claim(drop["id"], "c", rarity, now=NOW)
    assert stock(game, rarity) == (expected, 0)
    assert build(game_case).get_drop(drop["id"])["status"] == "claimed"


@pytest.mark.parametrize("card_id,limit,event_id", [("common", 2, None), ("mythic", 1, None),
                                                    ("event", 6, "archive")])
def test_concurrent_collectors_and_database_trigger_share_same_limit(game_case, card_id, limit, event_id):
    game = build(game_case)
    drop = game.create_drop("guild", "race", card_id, now=NOW, event_id=event_id)
    barrier = threading.Barrier(12)

    def claim(index):
        other = build(game_case)
        barrier.wait(timeout=10)
        try:
            return other.claim(drop["id"], str(index), card_id, now=NOW)
        except GameError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(claim, range(12)))
    assert sum(isinstance(result, dict) for result in outcomes) == limit
    assert outcomes.count("drop_closed") == 12-limit
    assert stock(game, card_id) == (limit, 0)
    stored = game.get_drop(drop["id"])
    assert len({claim["user_id"] for claim in stored["claims"]}) == limit
    with game._tx() as connection:
        extra = game._mint(connection, "guild", "extra", "common", NOW, "test fixture")
    with contextlib.closing(sqlite3.connect(game.db_path)) as connection, pytest.raises(sqlite3.IntegrityError, match="collector limit"):
        connection.execute("INSERT INTO claims VALUES(?,?,?,?)", (drop["id"], "extra", extra["id"], NOW))


def test_final_edition_unit_forms_one_slot_drop_and_reservations_remain_global(game_case):
    game = build(game_case)
    first = game.create_drop("one", "channel", "limited", now=NOW, duration=2)
    assert first["collector_limit"] == 2
    pool = {row["id"]: row for row in game.spawn_pool(now=NOW)}
    assert pool["limited"]["remaining_supply"] == 1
    assert pool["limited"]["collector_limit"] == 1
    assert pool["limited"]["configured_collector_limit"] == 2
    second = game.create_drop("two", "channel", "limited", now=NOW, duration=10)
    assert second["collector_limit"] == 1 and stock(game, "limited") == (0, 3)
    with pytest.raises(GameError, match="outside its release/supply"):
        game.create_drop("three", "channel", "limited", now=NOW)
    game.claim(first["id"], "one", "limited", now=NOW)
    game.claim(second["id"], "two", "limited", now=NOW)
    assert stock(game, "limited") == (2, 1)
    game.active_drops(now=NOW+2)
    assert stock(game, "limited") == (2, 0)
    final = game.create_drop("three", "channel", "limited", now=NOW+3)
    assert final["collector_limit"] == 1
    game.claim(final["id"], "three", "limited", now=NOW+3)
    assert stock(game, "limited") == (3, 0)
    assert "limited" not in {row["id"] for row in game.spawn_pool(now=NOW+3)}


def test_concurrent_channels_cannot_overreserve_last_edition_units(game_case):
    game = build(game_case)
    barrier = threading.Barrier(8)

    def reserve(index):
        other = build(game_case)
        barrier.wait(timeout=10)
        try:
            return other.create_drop(str(index), "channel", "limited", now=NOW)
        except GameError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(reserve, range(8)))
    drops = [result for result in outcomes if isinstance(result, dict)]
    assert sorted(drop["collector_limit"] for drop in drops) == [1, 2]
    assert outcomes.count("unavailable_card") == 6
    assert stock(game, "limited") == (0, 3)


def test_event_override_snapshot_survives_catalog_change_and_restart(game_case):
    game = build(game_case)
    first = game.create_drop("guild", "old", "event", now=NOW, event_id="archive")
    game.claim(first["id"], "old", "event", now=NOW)
    document = json.loads(game_case[1].read_text())
    next(card for card in document["cards"] if card["id"] == "event")["collector_limit"] = 1
    game_case[1].write_text(json.dumps(document))
    restarted = build(game_case)
    stored = next(drop for drop in restarted.active_drops(now=NOW) if drop["id"] == first["id"])
    assert stored["collector_limit"] == 6
    new = restarted.create_drop("guild", "new", "event", now=NOW, event_id="archive")
    assert new["collector_limit"] == 1
    assert stock(restarted, "event") == (1, 6)
    restarted.active_drops(now=NOW+180)
    assert stock(restarted, "event") == (1, 0)


@pytest.mark.parametrize("value", [True, False, 0, 7, -1, 1.5, "2", None, []])
def test_event_override_rejects_noninteger_or_out_of_range_values(value):
    with pytest.raises(ValueError, match="integer from 1 through 6"):
        collector_limit(definition("event", event_id="archive", collector_limit=value))


@pytest.mark.parametrize("category", ["canon", "meme", "limited"])
def test_regular_or_seasonal_cards_cannot_override_the_rarity_policy(category):
    with pytest.raises(ValueError, match="event-exclusive"):
        collector_limit(definition("not-event", category=category, collector_limit=1))


@pytest.mark.parametrize("value", [True, 0, 1, 2.0, "2", None])
def test_published_collector_metadata_cannot_disagree_with_engine(game_case, value):
    document = json.loads(game_case[1].read_text())
    document["rarities"] = {"common": {"collector_limit": value}}
    game_case[1].write_text(json.dumps(document))
    with pytest.raises(ValueError, match="published collector policy"):
        build(game_case)


def test_one_of_one_edition_is_valid_and_expires_without_losing_stock(game_case):
    document = json.loads(game_case[1].read_text())
    next(card for card in document["cards"] if card["id"] == "limited")["release"]["supply_cap"] = 1
    game_case[1].write_text(json.dumps(document))
    game = build(game_case)
    drop = game.create_drop("guild", "single", "limited", now=NOW, duration=1)
    assert drop["collector_limit"] == 1 and stock(game, "limited") == (0, 1)
    game.active_drops(now=NOW+1)
    assert stock(game, "limited") == (0, 0)


LEGACY_SCHEMA = """
CREATE TABLE supply(card_id TEXT PRIMARY KEY,minted INTEGER NOT NULL DEFAULT 0,reserved INTEGER NOT NULL DEFAULT 0);
CREATE TABLE drops(id TEXT PRIMARY KEY,guild_id TEXT NOT NULL,channel_id TEXT NOT NULL,card_id TEXT NOT NULL,
 message_id TEXT,created_at REAL NOT NULL,expires_at REAL NOT NULL,status TEXT NOT NULL);
CREATE TABLE instances(id TEXT PRIMARY KEY,guild_id TEXT NOT NULL,user_id TEXT NOT NULL,card_id TEXT NOT NULL,
 acquired_at REAL NOT NULL,origin TEXT NOT NULL,favorite INTEGER NOT NULL DEFAULT 0,locked_by TEXT,consumed_at REAL);
CREATE TABLE claims(drop_id TEXT NOT NULL REFERENCES drops(id),user_id TEXT NOT NULL,
 instance_id TEXT NOT NULL UNIQUE REFERENCES instances(id),claimed_at REAL NOT NULL,PRIMARY KEY(drop_id,user_id));
CREATE TRIGGER two_collectors BEFORE INSERT ON claims
 WHEN (SELECT COUNT(*) FROM claims WHERE drop_id=NEW.drop_id)>=2
 BEGIN SELECT RAISE(ABORT,'two collector limit'); END;
PRAGMA user_version=1;
"""


def test_legacy_two_slot_migration_preserves_claims_supply_and_new_one_slot_policy(game_case):
    with contextlib.closing(sqlite3.connect(game_case[0])) as connection:
        connection.executescript(LEGACY_SCHEMA)
        connection.execute("INSERT INTO supply VALUES('mythic',1,1)")
        connection.execute("INSERT INTO supply VALUES('legendary',0,2)")
        connection.execute("INSERT INTO drops VALUES('legacy','guild','old','mythic',NULL,?,?,'active')", (NOW-10, NOW+100))
        connection.execute("INSERT INTO drops VALUES('abandoned','guild','idle','legendary',NULL,?,?,'active')", (NOW-10, NOW+1))
        connection.execute("INSERT INTO instances VALUES('old-copy','guild','original','mythic',?,'drop:legacy',1,NULL,NULL)", (NOW-9,))
        connection.execute("INSERT INTO claims VALUES('legacy','original','old-copy',?)", (NOW-9,))
        connection.commit()
    game = build(game_case)
    legacy = game.get_drop("legacy")
    assert (legacy["collector_limit"], legacy["max_collectors"], legacy["remaining"]) == (2, 2, 1)
    assert legacy["claims"][0]["instance_id"] == "old-copy"
    assert game.info("guild", "original", "old-copy")["favorite"]
    replay = game.claim("legacy", "original", "mythic", now=NOW)
    assert replay["replayed"] and replay["credits_earned"] == 0
    game.claim("legacy", "second", "mythic", now=NOW)
    assert stock(game, "mythic") == (2, 0)
    game.active_drops(now=NOW+1)
    assert stock(game, "legendary") == (0, 0)
    new = game.create_drop("guild", "old", "mythic", now=NOW+2)
    assert new["collector_limit"] == 1
    restarted = build(game_case)
    assert restarted.health()["schema_version"] == 7
    assert restarted.get_drop("legacy")["collector_limit"] == 2
    assert stock(restarted, "mythic") == (2, 1)
    with contextlib.closing(sqlite3.connect(game_case[0])) as connection:
        triggers = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert "collector_capacity" in triggers and "two_collectors" not in triggers
