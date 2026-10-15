"""Durable, private evidence for missing capture aliases; never auto-accept guesses."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from venator_vault.__main__ import backup, restore
from venator_vault.config import Settings
from venator_vault.core import Game, GameError, normalize_alias
from venator_vault.runtime import Runtime

NOW = 2_000_000_000


@pytest.fixture
def case(tmp_path):
    cards = [{"id": key, "name": key.title(), "aliases": [], "rarity": "common",
              "category": "canon", "arena": "ground", "faction": "Republic", "role": "striker",
              "attack": 100, "health": 400, "speed": 70, "weight": 1, "release": None}
             for key in ("alpha", "beta")]
    catalog = tmp_path / "cards.json"
    catalog.write_text(json.dumps({"schema_version": 1, "cards": cards}))
    settings = Settings(tmp_path, tmp_path / "game.sqlite3", catalog, None, 42)
    return Game(settings.database, catalog), settings


def encounter(game, channel="channel", **kwargs):
    return game.create_drop(kwargs.pop("guild", "42"), channel, kwargs.pop("card", "alpha"), now=NOW, **kwargs)


def reject(game, drop, user="a", guess="Missing alias", now=NOW, code="wrong_name", **kwargs):
    with pytest.raises(GameError) as error:
        game.claim(drop["id"], user, guess, now=now, **kwargs)
    assert error.value.code == code
    return error.value


def records(game):
    with closing(sqlite3.connect(game.db_path)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM capture_name_failures ORDER BY sequence")]


def cooldown(game, drop, user="a"):
    with closing(sqlite3.connect(game.db_path)) as connection:
        return connection.execute("SELECT next_at FROM attempts WHERE drop_id=? AND user_id=?",
                                  (drop["id"], user)).fetchone()


def test_wrong_name_preserves_exact_evidence_and_cooldown_after_restart(case):
    game, settings = case
    drop = encounter(game)
    raw = "  Ｍｉｓｓｉｎｇ Álias?!  "
    reject(game, drop, guess=raw, request_id="100")
    reopened = Game(settings.database, settings.catalog)
    row, = records(reopened)
    assert (row["guess"], row["normalized_guess"], row["expected_name"]) == (raw, normalize_alias(raw), "Alpha")
    assert (row["guild_id"], row["channel_id"], row["drop_id"], row["user_id"], row["card_id"]) == (
        "42", "channel", drop["id"], "a", "alpha")
    assert row["created_at"] == NOW and row["request_id"] == "100"
    assert json.loads(row["accepted_names"]) == sorted(game.capture_names["alpha"])
    assert row["matcher_version"]
    reject(reopened, drop, guess="Alpha", now=NOW+2, code="guess_cooldown")
    assert len(records(reopened)) == 1 and cooldown(reopened, drop) == (NOW+3,)
    assert not reopened.claim(drop["id"], "a", "Alpha", now=NOW+3)["replayed"]
    assert len(records(reopened)) == 1


@pytest.mark.parametrize("guess", ["", "!!!", "x"*100])
def test_empty_and_boundary_length_wrong_names_are_preserved(case, guess):
    game, _ = case
    reject(game, encounter(game), guess=guess)
    assert records(game)[0]["guess"] == guess
    assert game.failed_guesses("42")[0]["accepted_now"] is False


def test_oversized_input_is_rejected_before_storage(case):
    game, _ = case
    drop = encounter(game)
    reject(game, drop, guess="x"*101, code="invalid_guess")
    assert records(game) == [] and cooldown(game, drop) is None


@pytest.mark.parametrize("reason", ["drop_closed", "drop_not_published", "capture_streak_limit", "guess_cooldown"])
def test_non_name_denials_are_excluded(case, reason):
    game, _ = case
    options = {"defer_publication": True} if reason == "drop_not_published" else {}
    drop = encounter(game, **options)
    when = NOW
    if reason == "drop_closed":
        when = drop["expires_at"]
    elif reason == "capture_streak_limit":
        for index in range(2):
            other = encounter(game, f"other-{index}")
            game.claim(other["id"], "a", "Alpha", now=NOW)
    elif reason == "guess_cooldown":
        reject(game, drop)
        when += 1
    before = records(game)
    reject(game, drop, now=when, code=reason, request_id="denied")
    assert records(game) == before


def test_closed_full_drop_and_success_replay_are_not_name_failures(case):
    game, _ = case
    drop = encounter(game)
    for user in ("a", "b"):
        game.claim(drop["id"], user, "Alpha", now=NOW)
    assert game.claim(drop["id"], "a", "Wrong", now=NOW+1000, request_id="replay")["replayed"]
    reject(game, drop, user="c", code="drop_closed")
    assert records(game) == []


def test_duplicate_submission_survives_restart_without_new_cooldown_or_capture(case):
    game, settings = case
    drop = encounter(game)
    reject(game, drop, request_id="same")
    reopened = Game(settings.database, settings.catalog)
    error = reject(reopened, drop, request_id="same", now=NOW+10)
    assert error.details == {"retry_after": 0, "replayed": True}
    assert len(records(reopened)) == 1 and cooldown(reopened, drop) == (NOW+3,)
    assert reopened.get_drop(drop["id"])["remaining"] == 2
    assert not reopened.claim(drop["id"], "a", "Alpha", now=NOW+10, request_id="new")["replayed"]


@pytest.mark.parametrize("changed", ["drop", "user", "guess"])
def test_request_collision_rejects_changed_scope(case, changed):
    game, _ = case
    drop = encounter(game)
    reject(game, drop, request_id="request")
    target = encounter(game, "other", guild="other") if changed == "drop" else drop
    reject(game, target, user="b" if changed == "user" else "a",
           guess="Other" if changed == "guess" else "Missing alias", now=NOW+10,
           request_id="request", code="request_conflict")
    assert len(records(game)) == 1 and cooldown(game, drop) == (NOW+3,)


def test_duplicate_concurrent_submissions_record_exactly_one_failure(case):
    game, settings = case
    drop = encounter(game)
    def submit(_):
        other = Game(settings.database, settings.catalog)
        return reject(other, drop, request_id="same").details
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    assert sum(bool(result.get("replayed")) for result in results) == 7
    assert len(records(game)) == 1 and cooldown(game, drop) == (NOW+3,)


def test_aggregation_groups_normalized_names_and_isolates_guild_and_card(case):
    game, _ = case
    drop = encounter(game)
    for index, (user, guess) in enumerate((("a", "Missing alias"), ("b", "MISSING-ALIAS!"), ("a", " missing alias "))):
        reject(game, drop, user=user, guess=guess, now=NOW+index*3)
    reject(game, encounter(game, "other-card", card="beta"), now=NOW+10)
    reject(game, encounter(game, "other-guild", guild="99"), now=NOW+20)
    rows = game.failed_guesses("42")
    assert [row["card_id"] for row in rows] == ["beta", "alpha"]
    alpha, = game.failed_guesses("42", card_id="alpha")
    assert (alpha["attempts"], alpha["players"], alpha["latest_guess"]) == (3, 2, " missing alias ")
    assert (alpha["first_at"], alpha["last_at"]) == (NOW, NOW+6)
    assert alpha["normalized_guess"] == "missingalias" and not alpha["accepted_now"]
    assert "user_id" not in alpha and "guild_id" not in alpha
    assert game.failed_guesses("missing") == []
    assert game.failed_guesses("99")[0]["attempts"] == 1


def test_review_paginates_five_groups_with_stable_tie_order(case):
    game, _ = case
    drop = encounter(game)
    for index in range(7):
        reject(game, drop, user=f"user-{index}", guess=f"Unknown {index}")
    assert [row["latest_guess"] for row in game.failed_guesses("42")] == [f"Unknown {i}" for i in range(6, 1, -1)]
    assert [row["latest_guess"] for row in game.failed_guesses("42", page=2)] == ["Unknown 1", "Unknown 0"]
    assert game.failed_guesses("42", page=3) == []


def test_review_detects_newly_accepted_alias_without_rewriting_history(case):
    game, settings = case
    drop = encounter(game)
    reject(game, drop, request_id="historic")
    before = records(game)
    document = json.loads(settings.catalog.read_text())
    document["cards"][0]["aliases"] = ["Missing alias"]
    settings.catalog.write_text(json.dumps(document))
    updated = Game(settings.database, settings.catalog)
    row, = updated.failed_guesses("42")
    assert row["accepted_now"] and "missingalias" in row["accepted_names"]
    assert "missingalias" not in row["accepted_names_then"]
    assert records(updated) == before
    reject(updated, drop, now=NOW+10, request_id="historic")
    assert not updated.claim(drop["id"], "a", "Missing alias", now=NOW+10, request_id="fresh")["replayed"]


def test_catalog_insert_failure_rolls_back_cooldown_and_game_state(case):
    game, _ = case
    drop = encounter(game)
    with closing(sqlite3.connect(game.db_path)) as connection:
        connection.execute("""CREATE TRIGGER fail_capture_catalog BEFORE INSERT ON capture_name_failures
            BEGIN SELECT RAISE(ABORT,'simulated write failure'); END""")
        connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="simulated write failure"):
        game.claim(drop["id"], "a", "Missing alias", now=NOW)
    assert records(game) == [] and cooldown(game, drop) is None
    assert game.get_drop(drop["id"])["remaining"] == 2


def test_schema_seven_upgrade_preserves_saved_state_without_fake_backfill(case):
    game, settings = case
    drop = encounter(game)
    game.claim(drop["id"], "a", "Alpha", now=NOW)
    reject(game, drop, user="b")
    with closing(sqlite3.connect(game.db_path)) as connection:
        connection.execute("DROP TABLE capture_name_failures")
        connection.execute("PRAGMA user_version=7")
        connection.commit()
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        before = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    upgraded = Game(settings.database, settings.catalog)
    with closing(sqlite3.connect(game.db_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (8,)
        assert {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables} == before
    assert records(upgraded) == []
    assert cooldown(upgraded, drop, "b") == (NOW+3,)


def test_paired_backup_restore_retains_failed_names_and_runtime(case, tmp_path):
    game, settings = case
    runtime = Runtime(settings.database.with_name("runtime.sqlite3"))
    runtime.configure("42", "channel", 900, "manager", now=NOW)
    runtime_before = runtime.get("42")
    drop = encounter(game)
    reject(game, drop, request_id="before-backup")
    before = records(game)
    destination = tmp_path / "snapshot"
    backup(settings, destination)
    reject(game, drop, guess="Another alias", now=NOW+3, request_id="after-backup")
    runtime.pause("42", "manager", True, now=NOW+3)
    restore(settings, destination, apply=True)
    restored = Game(settings.database, settings.catalog)
    assert records(restored) == before and restored.failed_guesses("42")[0]["attempts"] == 1
    assert Runtime(settings.database.with_name("runtime.sqlite3")).get("42") == runtime_before
    assert cooldown(restored, drop) == (NOW+3,)
