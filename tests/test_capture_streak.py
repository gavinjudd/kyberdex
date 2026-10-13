"""Guild-wide direct-capture fairness, durable ordering and atomic races."""
import contextlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from venator_vault.core import Game, GameError

NOW = 2_000_000_000


@pytest.fixture
def game_case(tmp_path):
    cards = []
    for key, rarity, arena in (("common", "common", "ground"),
                               ("ship", "common", "fleet"),
                               ("uncommon", "uncommon", "ground"),
                               ("legendary", "legendary", "ground")):
        cards.append({"id": key, "name": key.title(), "aliases": [key],
                      "rarity": rarity, "role": "striker", "arena": arena,
                      "faction": "Republic", "category": "canon", "release": None,
                      "attack": 100, "health": 500, "speed": 70, "weight": 1})
    catalog = tmp_path / "cards.json"
    catalog.write_text(json.dumps({"schema_version": 1, "cards": cards, "rarities": {}}))
    return tmp_path / "game.sqlite", catalog


def drop(game, channel, *, guild="guild", card="common", now=NOW, **kwargs):
    return game.create_drop(guild, channel, card, now=now, **kwargs)


def catch(game, encounter, user, *, now=NOW, guess=None):
    return game.claim(encounter["id"], user, guess or encounter["card"]["name"], now=now)


def history(game, guild="guild"):
    with contextlib.closing(sqlite3.connect(game.db_path)) as connection:
        return connection.execute("SELECT sequence,drop_id,user_id FROM capture_history "
                                  "WHERE guild_id=? ORDER BY sequence", (guild,)).fetchall()


def blocked(game, encounter, user="a", *, now=NOW):
    with pytest.raises(GameError, match="You caught the last two cards") as error:
        catch(game, encounter, user, now=now)
    assert error.value.code == "capture_streak_limit"


def first_two(game):
    encounters = [drop(game, "first"), drop(game, "second", card="ship")]
    receipts = [catch(game, encounter, "a") for encounter in encounters]
    return encounters, receipts


def test_third_direct_capture_is_blocked_across_channels_and_arenas(game_case):
    game = Game(*game_case)
    first_two(game)
    third = drop(game, "third", card="legendary")
    before = game.profile("guild", "a", now=NOW)
    blocked(game, third)
    after = game.profile("guild", "a", now=NOW)
    assert (after["total_cards"], after["credits"], after["xp"]) == (2, 50, 20)
    assert after == before
    assert len(history(game)) == 2
    assert game.get_drop(third["id"])["remaining"] == 1
    with game._tx() as connection:
        assert tuple(connection.execute("SELECT minted,reserved FROM supply WHERE card_id='legendary'").fetchone()) == (0, 1)
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_other_collector_second_slot_resets_streak_and_starts_new_one(game_case):
    game = Game(*game_case)
    encounters, _ = first_two(game)
    third = drop(game, "third")
    blocked(game, third)
    catch(game, encounters[1], "b")
    assert game.get_drop(encounters[1]["id"])["remaining"] == 0
    catch(game, third, "a")
    catch(game, drop(game, "fourth"), "a")
    blocked(game, drop(game, "fifth"))
    assert [row[2] for row in history(game)] == ["a", "a", "b", "a", "a"]


def test_streak_denial_creates_no_guess_cooldown_or_supply_consumption(game_case):
    game = Game(*game_case)
    encounters, _ = first_two(game)
    third = drop(game, "third")
    with pytest.raises(GameError) as error:
        catch(game, third, "a", guess="wrong")
    assert error.value.code == "capture_streak_limit"
    with game._tx() as connection:
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        assert tuple(connection.execute("SELECT minted,reserved FROM supply WHERE card_id='common'").fetchone()) == (1, 3)
    catch(game, encounters[1], "b")
    assert not catch(game, third, "a")["replayed"]


def test_another_guild_neither_blocks_nor_resets_this_guild(game_case):
    game = Game(*game_case)
    first_two(game)
    catch(game, drop(game, "other-a", guild="other"), "a")
    catch(game, drop(game, "other-b", guild="other"), "b")
    blocked(game, drop(game, "third"))
    assert [row[2] for row in history(game, "other")] == ["a", "b"]


def test_replay_precedes_streak_check_and_never_changes_history(game_case):
    game = Game(*game_case)
    encounters, receipts = first_two(game)
    before = history(game)
    replay = catch(game, encounters[0], "a", now=NOW + 1000, guess="wrong")
    assert replay["replayed"] and replay["credits_earned"] == 0
    assert replay["instance"]["id"] == receipts[0]["instance"]["id"]
    assert history(game) == before
    blocked(game, drop(game, "third", now=NOW + 1000), now=NOW + 1000)
    other = drop(game, "other", now=NOW + 1000)
    catch(game, other, "b", now=NOW + 1000)
    reset_history = history(game)
    catch(game, encounters[1], "a", now=NOW + 1000)
    assert history(game) == reset_history
    catch(game, drop(game, "fourth", now=NOW + 1000), "a", now=NOW + 1000)
    catch(game, drop(game, "fifth", now=NOW + 1000), "a", now=NOW + 1000)


@pytest.mark.parametrize("failure", ["wrong_name", "drop_closed", "drop_not_published"])
def test_unsuccessful_other_player_attempt_does_not_reset(game_case, failure):
    game = Game(*game_case)
    first_two(game)
    encounter = drop(game, "failed", duration=1 if failure == "drop_closed" else 180,
                     defer_publication=failure == "drop_not_published")
    now = NOW + 1 if failure == "drop_closed" else NOW
    with pytest.raises(GameError) as error:
        catch(game, encounter, "b", now=now, guess="wrong" if failure == "wrong_name" else None)
    assert error.value.code == failure
    blocked(game, drop(game, "third", now=now), now=now)
    assert [row[2] for row in history(game)] == ["a", "a"]


def test_daily_craft_salvage_and_completed_trade_do_not_reset(game_case):
    game = Game(*game_case)
    first_two(game)
    with game._tx() as connection:
        supplies = [game._mint(connection, "guild", "b", "common", NOW, "fixture") for _ in range(6)]
        game._credit(connection, "guild", "b", 1000, "fixture", "funds", NOW)
    before = history(game)
    game.daily("guild", "b", now=NOW)
    game.craft("guild", "b", [item["id"] for item in supplies[:3]], "uncommon", now=NOW)
    game.salvage("guild", "b", [supplies[3]["id"]], now=NOW)
    owned = game.inventory("guild", "a")
    trade = game.trade_create("guild", "a", "b", now=NOW)
    game.trade_offer(trade["id"], "a", [owned[0]["id"]], now=NOW)
    trade = game.trade_offer(trade["id"], "b", [supplies[4]["id"]], now=NOW)
    game.trade_accept(trade["id"], "a", trade["version"], now=NOW)
    result = game.trade_accept(trade["id"], "b", trade["version"], now=NOW)
    assert result["status"] == "completed"
    assert history(game) == before
    blocked(game, drop(game, "third"))


def test_restart_vacuum_and_backward_timestamps_preserve_commit_order(game_case):
    game = Game(*game_case)
    first_two(game)
    game = Game(*game_case)
    blocked(game, drop(game, "third"))
    catch(game, drop(game, "reset", now=NOW - 10), "b", now=NOW - 10)
    catch(game, drop(game, "fourth", now=NOW - 20), "a", now=NOW - 20)
    catch(game, drop(game, "fifth", now=NOW - 30), "a", now=NOW - 30)
    before = history(game)
    with contextlib.closing(sqlite3.connect(game.db_path)) as connection:
        connection.execute("VACUUM")
    restarted = Game(*game_case)
    assert history(restarted) == before
    blocked(restarted, drop(restarted, "sixth", now=NOW - 40), now=NOW - 40)
    assert [row[2] for row in before] == ["a", "a", "b", "a", "a"]


def test_failed_capture_transaction_rolls_back_history_and_mint(game_case, monkeypatch):
    game = Game(*game_case)
    catch(game, drop(game, "first"), "a")
    second = drop(game, "second")
    before = history(game)
    credit = game._credit

    def failed_credit(*args, **kwargs):
        raise RuntimeError("injected after claim insert")

    monkeypatch.setattr(game, "_credit", failed_credit)
    with pytest.raises(RuntimeError, match="injected"):
        catch(game, second, "a")
    assert history(game) == before
    assert len(game.inventory("guild", "a")) == 1
    assert game.get_drop(second["id"])["remaining"] == 2
    monkeypatch.setattr(game, "_credit", credit)
    catch(game, second, "a")
    blocked(game, drop(game, "third"))


def test_concurrent_same_actor_on_different_drops_cannot_cross_two(game_case):
    game = Game(*game_case)
    catch(game, drop(game, "first"), "a")
    encounters = [drop(game, f"race-{index}") for index in range(8)]
    games = [Game(*game_case) for _ in encounters]
    barrier = threading.Barrier(len(encounters))

    def attempt(index):
        barrier.wait(timeout=10)
        try:
            return catch(games[index], encounters[index], "a")
        except GameError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=len(encounters)) as pool:
        results = list(pool.map(attempt, range(len(encounters))))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("capture_streak_limit") == 7
    assert len(history(game)) == len(game.inventory("guild", "a")) == 2
    assert game.profile("guild", "a", now=NOW)["credits"] == 50


def test_concurrent_reset_and_third_capture_obey_serial_commit_order(game_case):
    game = Game(*game_case)
    encounters, _ = first_two(game)
    third = drop(game, "third")
    games = [Game(*game_case), Game(*game_case)]
    barrier = threading.Barrier(2)

    def attempt(index):
        barrier.wait(timeout=10)
        try:
            return catch(games[index], encounters[1] if index == 0 else third, "b" if index == 0 else "a")
        except GameError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert isinstance(results[0], dict)
    assert isinstance(results[1], dict) or results[1] == "capture_streak_limit"
    catch(game, third, "a")  # succeeds after the reset or replays its earlier receipt
    assert [row[2] for row in history(game)] == ["a", "a", "b", "a"]


def test_legacy_claims_without_credit_ledger_migrate_once_and_keep_caps(game_case):
    game = Game(*game_case)
    encounters, receipts = first_two(game)
    with contextlib.closing(sqlite3.connect(game.db_path)) as connection:
        connection.execute("DROP TRIGGER record_capture_history")
        connection.execute("DROP TABLE capture_history")
        connection.execute("DELETE FROM ledger WHERE reason='capture'")
        connection.execute("PRAGMA user_version=3")
        connection.commit()
    game = Game(*game_case)
    assert game.health()["schema_version"] == 7
    before = history(game)
    assert [row[2] for row in before] == ["a", "a"]
    blocked(game, drop(game, "third"))
    assert catch(game, encounters[0], "a")["instance"]["id"] == receipts[0]["instance"]["id"]
    assert game.get_drop(encounters[0]["id"])["collector_limit"] == 2
    restarted = Game(*game_case)
    assert history(restarted) == before
    catch(restarted, encounters[1], "b")
    assert [row[2] for row in history(restarted)] == ["a", "a", "b"]
    with restarted._tx() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
