"""Seasonal release keys are deliberate activation, never calendar-only launch."""
import copy
import json
import sqlite3
from contextlib import closing

import pytest

from venator_vault.core import Game, GameError

NOW = 2_000_000_000
POLICY = {"schema_version": 1, "activation": "explicit-release-keys"}


def definition(identity, **extra):
    return {"id": identity, "name": identity, "aliases": [], "rarity": "rare", "category": "canon",
            "arena": "ground", "faction": "Republic", "role": "striker", "attack": 100, "health": 400,
            "speed": 70, "weight": 50, "release": None, **extra}


@pytest.fixture
def catalog(tmp_path):
    value = {"schema_version": 1, "release_policy": POLICY, "cards": [
        definition("regular"), definition("edition", category="limited", release_id="autumn-2026",
            release={"starts_at": NOW-10, "ends_at": NOW+200, "supply_cap": 3})]}
    path = tmp_path / "cards.json"
    path.write_text(json.dumps(value))
    return path, value


def build(tmp_path, catalog, **kwargs):
    return Game(tmp_path / "state.sqlite3", catalog[0], **kwargs)


def test_disabled_release_stays_out_of_both_sampler_and_explicit_drop(tmp_path, catalog):
    game = build(tmp_path, catalog)
    assert [row["id"] for row in game.spawn_pool(now=NOW)] == ["regular"]
    with pytest.raises(GameError, match="outside its release/supply"):
        game.create_drop("guild", "channel", "edition", now=NOW)
    assert game._release_allowed(game.catalog["edition"], NOW) is False


def test_activated_release_obeys_calendar_last_copy_and_global_reservations(tmp_path, catalog):
    game = build(tmp_path, catalog, enabled_releases=("autumn-2026",))
    assert [row["id"] for row in game.spawn_pool(now=NOW-11)] == ["regular"]
    assert {row["id"] for row in game.spawn_pool(now=NOW)} == {"regular", "edition"}
    first = game.create_drop("guild", "one", "edition", now=NOW)
    second = game.create_drop("other", "two", "edition", now=NOW)
    assert (first["collector_limit"], second["collector_limit"]) == (2, 1)
    assert [row["id"] for row in game.spawn_pool(now=NOW)] == ["regular"]
    game.claim(first["id"], "a", "edition", now=NOW+1)
    game.claim(first["id"], "b", "edition", now=NOW+1)
    game.claim(second["id"], "c", "edition", now=NOW+1)
    assert [row["id"] for row in game.spawn_pool(now=NOW+2)] == ["regular"]
    assert game._release_allowed(game.catalog["edition"], NOW+200) is False


def test_disabling_release_after_restart_expires_drop_but_preserves_owned_copy(tmp_path, catalog):
    game = build(tmp_path, catalog, enabled_releases=("autumn-2026",))
    drop = game.create_drop("guild", "one", "edition", now=NOW)
    copy_id = game.claim(drop["id"], "a", "edition", now=NOW+1)["instance"]["id"]
    restarted = build(tmp_path, catalog)
    assert restarted.active_drops(now=NOW+2) == []
    with closing(sqlite3.connect(restarted.db_path)) as db:
        assert db.execute("SELECT minted,reserved FROM supply WHERE card_id='edition'").fetchone() == (1, 0)
        assert db.execute("SELECT status FROM drops WHERE id=?", (drop["id"],)).fetchone() == ("expired",)
    assert restarted.info("guild", "a", copy_id)["card_id"] == "edition"


def test_release_key_cannot_bypass_legends_event_route(tmp_path, catalog):
    path, value = catalog
    value["cards"][1].update(category="legends", event_id="archive")
    path.write_text(json.dumps(value))
    release_only = build(tmp_path, catalog, enabled_releases=("autumn-2026",))
    assert release_only.spawn_pool(now=NOW, event_id="archive") == []
    both = build(tmp_path, catalog, enabled_releases=("autumn-2026",), enabled_events=("archive",))
    assert [row["id"] for row in both.spawn_pool(now=NOW)] == ["regular"]
    assert [row["id"] for row in both.spawn_pool(now=NOW, event_id="archive")] == ["edition"]


@pytest.mark.parametrize("policy", [None, [], {}, {"schema_version": True, "activation": "explicit-release-keys"},
                                   {"schema_version": 1, "activation": "calendar"}])
def test_malformed_release_policy_is_rejected_before_storage(tmp_path, catalog, policy):
    path, value = catalog
    value["release_policy"] = policy
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="release policy"):
        build(tmp_path, catalog)
    assert not (tmp_path / "state.sqlite3").exists()


@pytest.mark.parametrize("key", [None, "", " ", "autumn 2026", "../autumn", True, 1])
def test_policy_requires_valid_limited_release_key(tmp_path, catalog, key):
    path, value = catalog
    value["cards"][1]["release_id"] = key
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="valid release key"):
        build(tmp_path, catalog)


def test_policy_rejects_missing_key_and_unknown_enabled_key(tmp_path, catalog):
    path, value = catalog
    with pytest.raises(ValueError, match="does not exist"):
        build(tmp_path, catalog, enabled_releases=("typo",))
    del value["cards"][1]["release_id"]
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="valid release key"):
        build(tmp_path, catalog)


@pytest.mark.parametrize("enabled", ["autumn-2026", (True,), ("",), (" autumn-2026 ",)])
def test_enabled_release_collection_is_strict(tmp_path, catalog, enabled):
    with pytest.raises((TypeError, ValueError), match="Enabled releases"):
        build(tmp_path, catalog, enabled_releases=enabled)


def test_key_always_gates_even_legacy_catalog_but_unkeyed_fixture_remains_compatible(tmp_path, catalog):
    path, value = catalog
    del value["release_policy"]
    path.write_text(json.dumps(value))
    assert [row["id"] for row in build(tmp_path, catalog).spawn_pool(now=NOW)] == ["regular"]
    legacy = copy.deepcopy(value)
    del legacy["cards"][1]["release_id"]
    path.write_text(json.dumps(legacy))
    assert {row["id"] for row in build(tmp_path, catalog).spawn_pool(now=NOW)} == {"regular", "edition"}
