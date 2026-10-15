"""Celestial: twenty staff-granted cards above Mythic that never enter normal play loops."""
from pathlib import Path

import pytest

from venator_vault.core import RARITY_POWER, Game, GameError

ROOT = Path(__file__).resolve().parents[1]
NOW = 2_000_000_000


@pytest.fixture
def game(tmp_path):
    return Game(tmp_path / "game.sqlite", ROOT / "data/cards.json")


def celestial(game):
    return [card for card in game.catalog.values() if card["rarity"] == "celestial"]


def test_twenty_celestial_cards_outclass_every_mythic(game):
    cards = celestial(game)
    assert len(cards) == 20
    assert RARITY_POWER["celestial"] > RARITY_POWER["mythic"]
    best_mythic = max(c["attack"] * c["health"] for c in game.catalog.values() if c["rarity"] == "mythic")
    assert all(c["attack"] * c["health"] > best_mythic for c in cards)
    assert all(c["category"] == "staff" and c["weight"] == 0 for c in cards)


def test_celestial_cards_never_drop_and_cannot_be_force_spawned(game):
    pool = {row["id"] for row in game.spawn_pool(now=NOW)}
    uniform = {row["id"] for row in game.spawn_pool(now=NOW, override_until=NOW + 600, uniform=True)}
    ids = {c["id"] for c in celestial(game)}
    assert not ids & pool and not ids & uniform
    with pytest.raises(GameError):
        game.create_drop("guild", "chan", "darth-vader-the-hallway", now=NOW, override_until=NOW + 600)


def test_only_staff_can_hand_them_out_and_they_stay_bound(game):
    grant = game.admin_grant("guild", "staff", "a", "darth-vader-the-hallway", now=NOW)
    copy_id = grant["instance_ids"][0]
    for call in (lambda: game.give("guild", "a", "b", [copy_id], now=NOW),
                 lambda: game.trade_offer(game.trade_create("guild", "a", "b", now=NOW)["id"], "a", [copy_id], now=NOW),
                 lambda: game.salvage("guild", "a", [copy_id], now=NOW)):
        with pytest.raises(GameError) as error:
            call()
        assert error.value.code == "bound_card"
    assert game.info("guild", "a", copy_id)["user_id"] == "a"
    deck = game.set_deck("guild", "a", "ground", [copy_id], now=NOW)
    assert deck["cards"][0]["card"]["rarity"] == "celestial"


def test_celestial_cards_do_not_change_regular_completion(game):
    game.admin_grant("guild", "staff", "a", "yoda-grand-master", now=NOW)
    profile = game.profile("guild", "a", now=NOW)
    assert profile["unique_cards"] == 0 and profile["celestial_cards"] == 1
    assert profile["catalog_count"] == 835
