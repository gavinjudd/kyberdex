"""Starter pack eligibility and progressive capture hints."""
from pathlib import Path

import pytest

from venator_vault.bot import capture_hint
from venator_vault.core import STARTER_CARDS, STARTER_CREDITS, Game

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def game(tmp_path):
    return Game(tmp_path / "game.sqlite", ROOT / "data/cards.json")


def test_starter_pack_once_gives_three_distinct_ground_commons_and_credits(game):
    pack = game.starter_pack("g", "new", now=1)
    assert len(pack["instances"]) == STARTER_CARDS
    cards = [item["card"] for item in pack["instances"]]
    assert len({card["subject_id"] for card in cards}) == STARTER_CARDS
    assert all(card["rarity"] == "common" and card["arena"] == "ground" and card["notability"] >= 4 for card in cards)
    assert game.profile("g", "new", now=1)["credits"] == STARTER_CREDITS
    assert game.starter_pack("g", "new", now=2) is None
    deck = game.set_deck("g", "new", "ground", [item["id"] for item in pack["instances"]], now=3)
    assert len(deck["cards"]) == 3


def test_established_collectors_do_not_get_a_starter_pack(game):
    for _ in range(3):
        game.admin_grant("g", "admin", "veteran", "b1-battle-droid", now=1)
    assert game.starter_pack("g", "veteran", now=2) is None


def test_hints_strengthen_over_time_without_revealing_early():
    drop = {"card": {"name": "Captain Rex", "faction": "Galactic Republic", "era": "Clone Wars", "arena": "ground"},
            "expires_at": 1000}
    early = capture_hint(drop, 25)
    assert "starts with" not in early and "pattern" not in early
    middle = capture_hint(drop, 50)
    assert "starts with **C**" in middle and "**2** words" in middle
    late = capture_hint(drop, 120)
    assert "C······ R··" in late
