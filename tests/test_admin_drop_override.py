"""Bounded administrator draws retain stock, capture rules and replay safety."""
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from venator_vault import core

NOW = 2_000_000_000


def definition(key, rarity="common", **fields):
    return {"id": key, "name": key, "aliases": [], "rarity": rarity, "category": "canon",
            "arena": "ground", "faction": "Republic", "role": "striker", "attack": 100,
            "health": 400, "speed": 70, "weight": 1, "release": None, **fields}


@pytest.fixture
def case(tmp_path):
    path = tmp_path / "cards.json"
    cards = [definition(rarity, rarity, weight=index+1) for index, rarity in enumerate(core.RARITIES)]
    cards += [definition("future", "rare", category="limited", release_id="future-season",
                        release={"starts_at": NOW+1000, "ends_at": NOW+2000, "supply_cap": 3}),
              definition("closed", "mythic", category="limited", release_id="closed-season",
                        release={"starts_at": NOW-100, "ends_at": NOW-1, "supply_cap": 2}),
              definition("exclusive", "legendary", category="legends", event_id="archive",
                        release={"starts_at": NOW+1000, "ends_at": NOW+2000, "supply_cap": 10},
                        collector_limit=3),
              definition("retired", "epic", weight=0)]
    path.write_text(json.dumps({"schema_version": 1, "release_policy": {
        "schema_version": 1, "activation": "explicit-release-keys"}, "cards": cards}))
    return tmp_path / "game.sqlite3", path


def stock(game, card_id):
    with closing(sqlite3.connect(game.db_path)) as connection:
        return connection.execute("SELECT minted,reserved FROM supply WHERE card_id=?", (card_id,)).fetchone()


def override(game, card_id, **kwargs):
    return game.create_drop("guild", "channel", card_id, now=NOW, override_until=NOW+3600, **kwargs)


def test_uniform_draw_includes_every_current_catalog_definition_equally(tmp_path):
    game = core.Game(tmp_path / "all-cards.sqlite3", Path(__file__).parents[1] / "data/cards.json")
    rows = game.spawn_pool(now=NOW, override_until=NOW+3600, uniform=True)
    assert len(rows) == len(game.catalog) == 851
    assert {row["id"] for row in rows} == game.catalog.keys()
    assert all(row["probability"] == pytest.approx(1/851) for row in rows)
    assert sum(row["probability"] for row in rows) == pytest.approx(1)

    limited_id = next(card["id"] for card in game.catalog.values() if card.get("release_id") == "summer-2027")

    class ChooseLimited:
        def choices(self, population, weights, k):
            assert k == 1 and weights == pytest.approx([1/851]*851)
            assert {card["id"] for card in population} == game.catalog.keys()
            return [next(card for card in population if card["id"] == limited_id)]

    game.rng = ChooseLimited()
    drawn = game.create_drop("guild", "channel", now=NOW, override_until=NOW+3600, uniform=True)
    assert drawn["card_id"] == limited_id and drawn["uniform"] == 1


def test_uniform_weights_ignore_tier_and_card_weights_but_retirement_and_whitelist_apply(case):
    game = core.Game(*case)
    normal = game.spawn_pool(now=NOW)
    assert {row["id"] for row in normal} == set(core.RARITIES)
    assert len({row["probability"] for row in normal}) > 1
    rows = game.spawn_pool(now=NOW, override_until=NOW+3600, uniform=True)
    assert {row["id"] for row in rows} == set(core.RARITIES) | {"future", "closed", "exclusive"}
    assert all(row["probability"] == pytest.approx(1/9) for row in rows)
    allowed = {"common", "closed", "future", "retired"}
    rows = game.spawn_pool(now=NOW, override_until=NOW+3600, uniform=True, allowed_card_ids=allowed)
    assert {row["id"] for row in rows} == {"common", "closed", "future"}
    assert all(row["probability"] == pytest.approx(1/3) for row in rows)
    with pytest.raises(core.GameError) as error:
        override(game, "exclusive", allowed_card_ids=allowed)
    assert error.value.code == "unavailable_card"


@pytest.mark.parametrize("card_id,capacity", [("future", 2), ("closed", 1), ("exclusive", 3)])
def test_manual_override_bypasses_activation_calendar_and_event_route_only(case, card_id, capacity):
    game = core.Game(*case)
    with pytest.raises(core.GameError) as error:
        game.create_drop("guild", "channel", card_id, now=NOW)
    assert error.value.code == "unavailable_card"
    drop = override(game, card_id)
    assert drop["max_collectors"] == capacity
    assert drop["override_until"] == NOW+3600
    assert drop["uniform"] == 0
    for index in range(capacity):
        receipt = game.claim(drop["id"], str(index), card_id, now=NOW+1)
        assert receipt["credits_earned"] == 25 and receipt["instance"]["card_id"] == card_id
    assert stock(game, card_id) == (capacity, 0)
    with pytest.raises(core.GameError) as error:
        game.claim(drop["id"], "extra", card_id, now=NOW+2)
    assert error.value.code == "drop_closed"


def test_rarity_filter_is_exact_and_selection_uses_published_uniform_pool(case):
    game = core.Game(*case)
    rows = game.spawn_pool(now=NOW, override_until=NOW+3600, uniform=True, rarity="rare")
    assert {row["id"] for row in rows} == {"rare", "future"}
    assert [row["probability"] for row in rows] == [.5, .5]

    class RecordingRandom:
        def choices(self, population, weights, k):
            assert {card["id"] for card in population} == {"rare", "future"}
            assert weights == [.5, .5] and k == 1
            return [next(card for card in population if card["id"] == "future")]

    game.rng = RecordingRandom()
    drop = override(game, None, rarity="rare", uniform=True)
    assert drop["card_id"] == "future" and drop["uniform"] == 0
    with pytest.raises(core.GameError) as error:
        game.create_drop("guild", "other", "closed", now=NOW, override_until=NOW+3600, rarity="rare")
    assert error.value.code == "unavailable_card"


def test_saved_deadline_survives_restart_and_get_drop_closes_at_its_own_boundary(case, monkeypatch):
    game = core.Game(*case)
    drop = game.create_drop("guild", "channel", "future", now=NOW, duration=3600, override_until=NOW+3000)
    assert drop["expires_at"] == NOW+3000  # The ordinary seasonal end is deliberately bypassed.
    game.claim(drop["id"], "one", "future", now=NOW+2001)
    restarted = core.Game(*case)
    monkeypatch.setattr(core.time, "time", lambda: NOW+2999)
    assert restarted.get_drop(drop["id"])["status"] == "active"
    assert stock(restarted, "future") == (1, 1)
    monkeypatch.setattr(core.time, "time", lambda: NOW+3000)
    assert restarted.get_drop(drop["id"])["status"] == "expired"
    assert stock(restarted, "future") == (1, 0)
    assert restarted.inventory("guild", "one")[0]["card_id"] == "future"
    # Neither saved override nor remaining stock changes normal-season eligibility.
    assert "future" not in {row["id"] for row in restarted.spawn_pool(now=NOW+3000)}


def test_deferred_publication_and_capture_are_clipped_to_saved_override(case):
    game = core.Game(*case)
    drop = game.create_drop("guild", "channel", "closed", now=NOW, duration=180,
                            override_until=NOW+40, defer_publication=True)
    assert (drop["delivery_deadline"], drop["expires_at"]) == (NOW+40, NOW+40)
    with pytest.raises(core.GameError) as error:
        game.claim(drop["id"], "one", "closed", now=NOW+1)
    assert error.value.code == "drop_not_published"
    published = core.Game(*case).publish_drop(drop["id"], "message", now=NOW+10)
    assert not published["delivery_pending"] and published["expires_at"] == NOW+40
    replay = game.publish_drop(drop["id"], "message", now=NOW+20)
    assert replay["expires_at"] == NOW+40
    with pytest.raises(core.GameError) as error:
        game.claim(drop["id"], "one", "closed", now=NOW+40)
    assert error.value.code == "drop_closed" and stock(game, "closed") == (0, 0)


def test_late_publication_acknowledgement_does_not_extend_an_event(case, monkeypatch):
    game = core.Game(*case)
    drop = game.create_drop("guild", "channel", "future", now=NOW, override_until=NOW+5, defer_publication=True)
    with pytest.raises(core.GameError) as error:
        game.publish_drop(drop["id"], "late-message", now=NOW+5)
    assert error.value.code == "delivery_expired"
    monkeypatch.setattr(core.time, "time", lambda: NOW+6)
    stored = core.Game(*case).get_drop(drop["id"])
    assert stored["message_id"] == "late-message" and stored["status"] == "expired"
    assert stored["expires_at"] == NOW+5 and stock(game, "future") == (0, 0)


def test_global_reservations_and_lifetime_supply_remain_bounded_across_admin_channels(case):
    game = core.Game(*case)
    first = override(game, "future")
    second = game.create_drop("other", "channel", "future", now=NOW, override_until=NOW+3600)
    assert (first["collector_limit"], second["collector_limit"]) == (2, 1)
    assert stock(game, "future") == (0, 3)
    assert "future" not in {row["id"] for row in game.spawn_pool(now=NOW, override_until=NOW+3600, uniform=True)}
    with pytest.raises(core.GameError) as error:
        game.create_drop("third", "channel", "future", now=NOW, override_until=NOW+3600)
    assert error.value.code == "unavailable_card"
    game.claim(first["id"], "a", "future", now=NOW+1)
    game.claim(first["id"], "b", "future", now=NOW+1)
    game.claim(second["id"], "c", "future", now=NOW+1)
    assert stock(game, "future") == (3, 0)
    assert "future" not in {row["id"] for row in game.spawn_pool(now=NOW+2, override_until=NOW+3602, uniform=True)}


def test_concurrent_overrides_cannot_overreserve_the_final_units(case):
    game = core.Game(*case)
    barrier = threading.Barrier(8)

    def reserve(index):
        other = core.Game(*case)
        barrier.wait(timeout=10)
        try:
            return other.create_drop(str(index), "channel", "future", now=NOW, override_until=NOW+3600)
        except core.GameError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(reserve, range(8)))
    assert sorted(result["collector_limit"] for result in results if isinstance(result, dict)) == [1, 2]
    assert results.count("unavailable_card") == 6
    assert stock(game, "future") == (0, 3)


def test_request_replay_survives_capture_deadline_and_changed_art_allowlist_without_allocating(case):
    game = core.Game(*case)
    drop = override(game, "closed", request_id="admin-request")
    game.claim(drop["id"], "one", "closed", now=NOW+1)
    replay = core.Game(*case).create_drop("guild", "channel", "closed", now=NOW+4000,
        override_until=NOW+7600, request_id="admin-request", allowed_card_ids=())
    assert replay["id"] == drop["id"] and replay["status"] == "claimed"
    assert replay["override_until"] == NOW+3600 and stock(game, "closed") == (1, 0)
    with closing(sqlite3.connect(game.db_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM drops").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM instances").fetchone()[0] == 1


def test_request_replay_after_expiry_preserves_original_deadline(case):
    game = core.Game(*case)
    original = override(game, "future", request_id="admin-request")
    replay = game.create_drop("guild", "channel", "future", now=NOW+200,
        override_until=NOW+3800, request_id="admin-request")
    assert replay["id"] == original["id"] and replay["expires_at"] == NOW+180
    assert replay["override_until"] == NOW+3600 and replay["status"] == "expired"
    assert stock(game, "future") == (0, 0)


@pytest.mark.parametrize("changed", [{"guild_id": "other"}, {"channel_id": "other"}, {"card_id": "rare"},
    {"override_until": None}, {"uniform": True}, {"rarity": "rare"}, {"duration": 181},
    {"defer_publication": True}, {"event_id": "archive"}, {"anchors_schedule": False}])
def test_request_id_cannot_be_reused_for_different_scope_or_draw_rules(case, changed):
    game = core.Game(*case)
    override(game, "future", request_id="request")
    args = {"guild_id": "guild", "channel_id": "channel", "card_id": "future", "now": NOW,
            "override_until": NOW+3600, "request_id": "request", **changed}
    with pytest.raises(core.GameError) as error:
        game.create_drop(**args)
    assert error.value.code == "request_conflict" and stock(game, "future") == (0, 2)


def test_concurrent_retries_of_one_admin_interaction_create_one_encounter(case):
    game = core.Game(*case)
    barrier = threading.Barrier(8)

    def retry(index):
        other = core.Game(*case)
        barrier.wait(timeout=10)
        return other.create_drop("guild", "channel", "future", now=NOW+index,
                                override_until=NOW+3600+index, request_id="request")

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(retry, range(8)))
    assert len({drop["id"] for drop in results}) == 1
    assert len({drop["override_until"] for drop in results}) == 1
    assert stock(game, "future") == (0, 2)


def test_override_preserves_two_consecutive_capture_limit(case):
    game = core.Game(*case)
    first = override(game, "closed")
    game.claim(first["id"], "one", "closed", now=NOW+1)
    second = game.create_drop("guild", "second", "mythic", now=NOW+2, override_until=NOW+3602)
    game.claim(second["id"], "one", "mythic", now=NOW+3)
    third = game.create_drop("guild", "third", "rare", now=NOW+4, override_until=NOW+3604)
    with pytest.raises(core.GameError) as error:
        game.claim(third["id"], "one", "rare", now=NOW+5)
    assert error.value.code == "capture_streak_limit"
    assert stock(game, "rare") == (0, 2)
    game.claim(third["id"], "another", "rare", now=NOW+6)
    game.claim(third["id"], "one", "rare", now=NOW+7)
    assert stock(game, "rare") == (2, 0)


@pytest.mark.parametrize("deadline", [NOW, NOW-1, NOW+3600.01])
def test_new_overrides_must_end_in_the_next_hour(case, deadline):
    game = core.Game(*case)
    with pytest.raises(core.GameError) as error:
        game.create_drop("guild", "channel", "future", now=NOW, override_until=deadline)
    assert error.value.code == "invalid_override" and stock(game, "future") == (0, 0)
    with pytest.raises(core.GameError):
        game.spawn_pool(now=NOW, override_until=deadline, uniform=True)


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), True])
def test_nonfinite_or_boolean_override_deadlines_are_rejected(case, deadline):
    game = core.Game(*case)
    with pytest.raises(ValueError):
        game.create_drop("guild", "channel", "future", now=NOW, override_until=deadline)
    assert stock(game, "future") == (0, 0)


def test_uniform_requires_a_deadline_and_empty_or_invalid_pools_never_create_drops(case):
    game = core.Game(*case)
    with pytest.raises(core.GameError, match="bounded event deadline"):
        game.create_drop("guild", "channel", now=NOW, uniform=True)
    with pytest.raises(core.GameError) as error:
        game.create_drop("guild", "channel", now=NOW, override_until=NOW+3600,
                         uniform=True, allowed_card_ids={"retired"})
    assert error.value.code == "no_available_cards"
    with pytest.raises(core.GameError) as error:
        game.spawn_pool(now=NOW, override_until=NOW+3600, rarity="wrong")
    assert error.value.code == "invalid_rarity"
    with pytest.raises(ValueError, match="boolean"):
        game.spawn_pool(now=NOW, override_until=NOW+3600, uniform=1)
    with pytest.raises(TypeError, match="collection"):
        game.spawn_pool(now=NOW, override_until=NOW+3600, allowed_card_ids="future")


def test_schema_six_migration_preserves_ordinary_saved_drops_and_reservations(case):
    game = core.Game(*case)
    old = game.create_drop("guild", "channel", "common", now=NOW)
    game.claim(old["id"], "one", "common", now=NOW+1)
    with closing(sqlite3.connect(game.db_path)) as connection:
        connection.execute("DROP INDEX drop_request")
        for column in ("override_until", "request_id", "request_signature", "schedule_anchor", "uniform"):
            connection.execute(f"ALTER TABLE drops DROP COLUMN {column}")
        connection.execute("PRAGMA user_version=6")
        connection.commit()
    restarted = core.Game(*case)
    saved = next(drop for drop in restarted.active_drops(now=NOW+2) if drop["id"] == old["id"])
    assert saved["override_until"] is None and saved["request_id"] is None and saved["uniform"] == 0
    assert saved["expires_at"] == old["expires_at"] and len(saved["claims"]) == 1
    assert stock(restarted, "common") == (1, 1)
    assert restarted.health()["schema_version"] == 7


def test_paused_manual_encounter_is_not_a_recovery_anchor_for_normal_spawn_schedule(case):
    game = core.Game(*case)
    scheduled = game.create_drop("guild", "channel", "common", now=NOW, defer_publication=True)
    scheduled = game.publish_drop(scheduled["id"], "scheduled-message", now=NOW+1)
    manual = game.create_drop("guild", "manual", "mythic", now=NOW+2,
                              defer_publication=True, anchors_schedule=False, request_id="manual")
    manual = game.publish_drop(manual["id"], "manual-message", now=NOW+3)
    assert scheduled["schedule_anchor"] == 1 and manual["schedule_anchor"] == 0
    restarted = core.Game(*case)
    assert restarted.last_published_drop("guild")["id"] == scheduled["id"]
    assert restarted.last_published_drop("other") is None
    with pytest.raises(ValueError, match="boolean"):
        game.create_drop("other", "channel", "common", now=NOW, anchors_schedule=0)


def test_chosen_card_does_not_claim_to_be_a_uniform_random_draw(case):
    game = core.Game(*case)
    chosen = override(game, "future", uniform=True)
    assert chosen["uniform"] == 0 and chosen["override_until"] == NOW+3600


def test_stopping_event_cancels_only_matching_unpublished_reservations(case):
    game = core.Game(*case)
    pending = override(game, "future", defer_publication=True, request_id="pending")
    published = game.create_drop("guild", "published", "closed", now=NOW,
                                override_until=NOW+3600, defer_publication=True)
    game.publish_drop(published["id"], "message", now=NOW+1)
    other_event = game.create_drop("guild", "other-event", "rare", now=NOW,
                                  override_until=NOW+3500, defer_publication=True)
    other_guild = game.create_drop("other", "channel", "epic", now=NOW,
                                  override_until=NOW+3600, defer_publication=True)
    cancelled = game.cancel_pending_overrides("guild", NOW+3600, now=NOW+2)
    assert [drop["id"] for drop in cancelled] == [pending["id"]]
    assert cancelled[0]["status"] == "expired" and stock(game, "future") == (0, 0)
    assert game.cancel_pending_overrides("guild", NOW+3600, now=NOW+3) == []
    active = {drop["id"] for drop in game.active_drops(now=NOW+3)}
    assert active == {published["id"], other_event["id"], other_guild["id"]}
    game.claim(published["id"], "one", "closed", now=NOW+4)
    assert stock(game, "closed") == (1, 0)
    with pytest.raises(core.GameError) as error:
        game.publish_drop(pending["id"], "late-message", now=NOW+4)
    assert error.value.code == "delivery_expired" and stock(game, "future") == (0, 0)
    # The freed channel can immediately resume normal encounters.
    normal = game.create_drop("guild", "channel", "common", now=NOW+5)
    assert normal["override_until"] is None


def test_request_lookup_recovers_original_encounter_after_mode_changes(case, monkeypatch):
    game = core.Game(*case)
    pending = override(game, "future", request_id="request", defer_publication=True)
    monkeypatch.setattr(core.time, "time", lambda: NOW+1)
    assert game.drop_for_request("unknown", "guild", "channel") is None
    assert game.drop_for_request("request", "guild", "channel")["id"] == pending["id"]
    game.cancel_pending_overrides("guild", NOW+3600, now=NOW+1)
    original = core.Game(*case).drop_for_request("request", "guild", "channel")
    assert original["id"] == pending["id"] and original["status"] == "expired"
    assert original["override_until"] == NOW+3600 and stock(game, "future") == (0, 0)
    for guild, channel in (("other", "channel"), ("guild", "other")):
        with pytest.raises(core.GameError) as error:
            game.drop_for_request("request", guild, channel)
        assert error.value.code == "request_conflict"
