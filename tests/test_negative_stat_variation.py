"""Low-roll copies stay useful, permanent, and clearly disclosed to collectors."""

from pathlib import Path

import pytest

from venator_vault.core import Game, balance_stats
from venator_vault.presentation import card_panel

CATALOG = Path(__file__).resolve().parents[1] / "data/cards.json"


@pytest.mark.parametrize("card_id", ["captain-rex", "rex-rishi"])
def test_both_stats_can_roll_negative_and_stay_negative_after_restart(tmp_path, monkeypatch, card_id):
    from venator_vault import core

    game = Game(tmp_path / "negative-roll.sqlite3", CATALOG)
    bounds = []

    def lowest_roll(bound):
        bounds.append(bound)
        return 0

    monkeypatch.setattr(core.secrets, "randbelow", lowest_roll)
    with game._tx() as db:
        copy = game._mint(db, "scratch-guild", "scratch-player", card_id, 1, "scratch-test")
    assert bounds == [11, 11]
    assert (copy["attack_roll"], copy["health_roll"]) == (95, 95)
    card = copy["card"]
    assert 0 < copy["attack"] < card["attack"]
    assert 0 < copy["health"] < card["health"]
    assert copy["speed"] == card["speed"]
    copy_field = next(field.value for field in card_panel(card, copy).fields if field.name == "Copy stats")
    assert "Attack -5%" in copy_field and "Health -5%" in copy_field
    if card["category"] == "limited":
        ordinary = balance_stats(card["id"], card["rarity"], card["role"], card["arena"],
                                 card["power_profile"]["combat"])
        assert copy["attack"] > ordinary["attack"]
        assert copy["health"] > ordinary["health"]
        assert "+10% after these rolls" in copy_field
    # No new entropy is available on a read, so a reroll would fail the test.
    monkeypatch.setattr(core.secrets, "randbelow", lambda _: pytest.fail("Stored copies must not reroll"))
    restored = Game(game.db_path, CATALOG).info("scratch-guild", "scratch-player", copy["id"])
    assert restored == copy


def test_worst_roll_preserves_positive_stats_and_limited_advantage_across_catalog(tmp_path):
    game = Game(tmp_path / "catalog-floor.sqlite3", CATALOG)
    for card in game.catalog.values():
        stats = game.copy_stats(card, {"stat_roll_version": 1, "attack_roll": 95, "health_roll": 95})
        assert 0 < stats["attack"] < card["attack"], card["id"]
        assert 0 < stats["health"] < card["health"], card["id"]
        if card["category"] == "limited":
            ordinary = balance_stats(card["id"], card["rarity"], card["role"], card["arena"],
                                     card["power_profile"]["combat"])
            assert stats["attack"] > ordinary["attack"], card["id"]
            assert stats["health"] > ordinary["health"], card["id"]
