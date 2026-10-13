"""Published tier-first odds match actual draws after eligibility filtering."""
import json
from datetime import UTC, datetime

import pytest

from venator_vault import core

NOW = 1_800_000_000
EXPECTED_TIER_WEIGHTS = {
    "common": 5400, "uncommon": 2200, "rare": 1780,
    "epic": 420, "legendary": 160, "mythic": 40,
}


def card(identity, rarity="common", weight=1, **fields):
    return {
        "id": identity, "name": identity, "aliases": [], "rarity": rarity,
        "category": "canon", "arena": "ground", "faction": "Republic", "role": "striker",
        "attack": 100, "health": 500, "speed": 50, "weight": weight, "release": None,
        **fields,
    }


def release(start=-100, end=100, cap=2):
    return {
        "starts_at": datetime.fromtimestamp(NOW+start, UTC).isoformat(),
        "ends_at": datetime.fromtimestamp(NOW+end, UTC).isoformat(),
        "supply_cap": cap,
    }


@pytest.fixture
def game_factory(tmp_path):
    sequence = 0

    def build(cards, enabled_events=()):
        nonlocal sequence
        sequence += 1
        catalog = tmp_path/f"cards-{sequence}.json"
        catalog.write_text(json.dumps({"schema_version": 1, "cards": cards}))
        return core.Game(tmp_path/f"game-{sequence}.sqlite", catalog, enabled_events=enabled_events)

    return build


def probabilities(game, **kwargs):
    return {item["id"]: item["probability"] for item in game.spawn_pool(now=NOW, **kwargs)}


def test_tier_chances_are_independent_of_number_of_card_definitions(game_factory):
    assert core.RARITY_SPAWN_WEIGHTS == EXPECTED_TIER_WEIGHTS
    for counts in ({rarity: 1 for rarity in EXPECTED_TIER_WEIGHTS},
                   {"common": 80, "uncommon": 30, "rare": 15, "epic": 5, "legendary": 2, "mythic": 1}):
        cards = [card(f"{rarity}-{index}", rarity) for rarity, count in counts.items() for index in range(count)]
        game = game_factory(cards)
        rows = probabilities(game)
        assert sum(rows.values()) == pytest.approx(1)
        for rarity, tier_weight in EXPECTED_TIER_WEIGHTS.items():
            aggregate = sum(rows[item["id"]] for item in cards if item["rarity"] == rarity)
            assert aggregate == pytest.approx(tier_weight/10000), rarity


def test_card_weights_only_split_their_own_tier_and_absent_tiers_renormalize(game_factory):
    game = game_factory([
        card("common-light", weight=1), card("common-heavy", weight=3),
        card("mythic-light", "mythic", 2), card("mythic-heavy", "mythic", 6),
        card("retired-legendary", "legendary", 0),
    ])
    rows = probabilities(game)
    assert "retired-legendary" not in rows
    assert rows["common-heavy"] == pytest.approx(rows["common-light"]*3)
    assert rows["mythic-heavy"] == pytest.approx(rows["mythic-light"]*3)
    assert rows["common-light"]+rows["common-heavy"] == pytest.approx(5400/5440)
    assert rows["mythic-light"]+rows["mythic-heavy"] == pytest.approx(40/5440)


def test_supply_reservations_and_release_windows_update_tier_eligibility(game_factory):
    game = game_factory([
        card("ordinary"), card("limited", "rare", category="limited", release=release()),
        card("zero-weight", "mythic", 0),
        card("future", "legendary", category="limited", release=release(start=10, end=100)),
        card("closed", "epic", category="limited", release=release(start=-100, end=-1)),
    ])
    before = probabilities(game)
    assert set(before) == {"ordinary", "limited"}
    assert before["limited"] == pytest.approx(1780/7180)
    game.create_drop("guild", "reserved", "limited", now=NOW, duration=2)
    assert probabilities(game) == {"ordinary": 1.0}
    expired = {row["id"]: row for row in game.spawn_pool(now=NOW+2)}
    assert expired["limited"]["remaining_supply"] == 2
    assert expired["limited"]["probability"] == pytest.approx(1780/7180)
    second = game.create_drop("guild", "minted", "limited", now=NOW+3, duration=2)
    game.claim(second["id"], "collector", "limited", now=NOW+3)
    # The final copy remains obtainable through a smaller encounter; published
    # odds and slot counts reflect the actual remaining supply.
    after = {row["id"]: row for row in game.spawn_pool(now=NOW+5)}
    assert after["limited"]["probability"] == pytest.approx(1780/7180)
    assert after["limited"]["remaining_supply"] == 1
    assert after["limited"]["collector_limit"] == 1
    assert after["limited"]["configured_collector_limit"] == 2
    final = game.create_drop("guild", "final-copy", "limited", now=NOW+6)
    assert final["remaining"] == 1
    assert {row["id"]: row["probability"] for row in game.spawn_pool(now=NOW+6)} == {"ordinary": 1.0}
    captured = game.claim(final["id"], "final-collector", "limited", now=NOW+6)
    assert captured["remaining"] == 0
    assert {row["id"]: row["probability"] for row in game.spawn_pool(now=NOW+7)} == {"ordinary": 1.0}


def test_explicit_event_route_and_activation_are_required_before_tier_selection(game_factory):
    cards = [card("ordinary"), card(
        "archive", "mythic", category="legends", event_id="archive-event",
        release=release(), collector_limit=2,
    )]
    disabled = game_factory(cards)
    assert probabilities(disabled, event_id="archive-event") == {}
    enabled = game_factory(cards, enabled_events=("archive-event",))
    assert probabilities(enabled) == {"ordinary": 1.0}
    assert probabilities(enabled, event_id="archive-event") == {"archive": 1.0}
    enabled.create_drop("guild", "archive", now=NOW, event_id="archive-event")
    assert probabilities(enabled, event_id="archive-event") == {}
    assert probabilities(enabled) == {"ordinary": 1.0}
    with pytest.raises(core.GameError) as error:
        enabled.create_drop("guild", "no-supply", now=NOW, event_id="archive-event")
    assert error.value.code == "no_available_cards"


def test_random_draw_uses_exactly_the_published_eligible_distribution(game_factory):
    game = game_factory([
        card("common-light", weight=1), card("common-heavy", weight=3),
        card("legendary", "legendary", 25), card("mythic", "mythic", 100),
        card("retired", "epic", 0),
    ])
    published = probabilities(game)

    class RecordingRandom:
        def choices(self, population, weights, k):
            assert k == 1
            total = sum(weights)
            sampled = {item["id"]: weight/total for item, weight in zip(population, weights, strict=True)}
            assert sampled == pytest.approx(published)
            assert "retired" not in sampled
            return [next(item for item in population if item["id"] == "mythic")]

    game.rng = RecordingRandom()
    drawn = game.create_drop("guild", "sampled", now=NOW)
    assert drawn["card_id"] == "mythic"


def test_empty_eligible_pool_reports_no_probability_and_refuses_draw(game_factory):
    game = game_factory([card("retired", weight=0)])
    assert probabilities(game) == {}
    with pytest.raises(core.GameError) as error:
        game.create_drop("guild", "empty", now=NOW)
    assert error.value.code == "no_available_cards"
