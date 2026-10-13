"""Exact model codes and documented model families, without fuzzy identity matches."""
from collections import defaultdict
from pathlib import Path

import pytest

from venator_vault.core import Game, GameError, capture_identity, capture_names, normalize_alias


@pytest.fixture
def game(tmp_path):
    return Game(tmp_path / "logical-names.sqlite3", Path(__file__).parents[1] / "data/cards.json")


@pytest.mark.parametrize("card,answer", [
    ("b2-rp-battle-droid", "B2"), ("b2-rp-battle-droid", "B-2"),
    ("b2-rp-battle-droid", "B2 Super Battle Droid"),
    ("b2-rp-battle-droid", "b2-super-battle-droid"), ("b2-rp-battle-droid", "B2 RP"),
    ("b2-rp-battle-droid", "Super Battle Droid"),
    ("super-battle-droid-rocket-trooper", "B2"),
    ("super-battle-droid-rocket-trooper", "B2 Super Battle Droid"),
    ("b1-series-rocket-battle-droid", "B1"),
    ("b1-series-rocket-battle-droid", "B1 Battle Droid"),
    ("b1-series-rocket-battle-droid", "B1 Rocket Battle Droid"),
    ("b1-battle-droid", "B1"), ("b2-super-battle-droid", "B2"),
    ("r2-d2", "R2 D2"), ("c-3po", "C3PO"), ("c-3po", "C 3 PO"),
    ("2-1b-droid", "2 1B"), ("bd-3000-luxury-droid", "BD3000"),
    ("fa-4-pilot-droid", "FA4"), ("fx-6-medical-droid", "FX-6"),
    ("gh-7-analysis-droid", "GH7"), ("id9-seeker-droid", "ID9"),
    ("nr-n99-persuader-class-tank-droid", "NRN99"),
    ("nr-n99-persuader-class-tank-droid", "NR-N99 Persuader Tank Droid"),
    ("ric-series-general-labor-droid", "RIC"),
    ("ric-series-general-labor-droid", "RIC General Labor Droid"),
    ("t-series-tactical-droid", "T Tactical Droid"),
    ("venator-class", "Venator Star Destroyer"),
    ("imperial-sentinel-class-shuttle", "Imperial Sentinel Shuttle"),
    ("r4-p17", "R4P17"), ("k-2so-kay-tuesso", "K2SO"),
])
def test_logical_answers_capture_the_actual_model_in_disposable_inventory(game, card, answer):
    drop = game.create_drop("guild", "channel", card)
    result = game.claim(drop["id"], "collector", answer)
    assert result["instance"]["card_id"] == card


@pytest.mark.parametrize("card,answer", [
    ("b2-rp-battle-droid", "B1"), ("b1-series-rocket-battle-droid", "B2"),
    ("b2-rp-battle-droid", "B1 Battle Droid"),
    ("b1-series-rocket-battle-droid", "B2 Super Battle Droid"),
    ("b2-super-battle-droid", "B2-RP"),
    ("b1-battle-droid", "B1 Rocket Battle Droid"),
    ("b2-rp-battle-droid", "RP"), ("b2-rp-battle-droid", "droid"),
    ("b2-rp-battle-droid", "battle droid"), ("b1-battle-droid", "battle droid"),
    ("b1-series-rocket-battle-droid", "battle droid"),
    ("r4-p17", "R2"), ("r2-d2", "R2D"), ("c-3po", "C3"),
    ("luke-skywalker", "Skywalker"), ("jango-fett", "Fett"),
    ("satele-shan", "Shan"), ("captain-rex", "commander"),
    ("captain-rex", "clone"), ("captain-rex", "droid"),
])
def test_wrong_family_subtype_generic_title_and_shared_surnames_still_fail(game, card, answer):
    drop = game.create_drop("guild", "channel", card)
    with pytest.raises(GameError) as error:
        game.claim(drop["id"], "collector", answer)
    assert error.value.code == "wrong_name"
    assert game.inventory("guild", "collector") == []
    assert game.get_drop(drop["id"])["remaining"] == drop["remaining"]


def minimal(key, name, **fields):
    return {"id": key, "subject_id": key, "category": "canon", "arena": "ground",
            "name": name, "aliases": [], **fields}


def test_complete_alphabetic_series_codes_work_without_arbitrary_suffixes_or_prefixes():
    catalog = {card["id"]: card for card in [
        minimal("kx", "KX-series Security Droid"), minimal("exd", "E-XD Infiltrator Droid"),
        minimal("asp", "ASP General Purpose Droid"),
        minimal("rx-security", "RX-series Security Droid"),
        minimal("rx-mining", "RX-series Mining Droid"),
    ]}
    names = capture_names(catalog)
    assert "kx" in names["kx"] and "kxsecuritydroid" in names["kx"]
    assert "exd" in names["exd"] and "xd" not in names["exd"]
    assert "asp" in names["asp"]
    assert "rx" not in names["rx-security"] | names["rx-mining"]
    for answers in names.values():
        assert not {"droid", "securitydroid", "series", "class"} & answers


def test_family_aliases_follow_future_editions_without_merging_collectible_subjects():
    catalog = {card["id"]: card for card in [
        minimal("b2-super-battle-droid", "B2 Super Battle Droid", aliases=["B2"]),
        minimal("b2-rp-battle-droid", "B2-RP Battle Droid"),
        minimal("rp-edition", "B2-RP Battle Droid — Future Edition", category="limited",
                subject_id="b2-rp-battle-droid"),
    ]}
    names = capture_names(catalog)
    assert "b2" in names["b2-rp-battle-droid"] & names["rp-edition"]
    assert "b2superbattledroid" in names["b2-rp-battle-droid"] & names["rp-edition"]
    assert "b2rp" in names["rp-edition"] and "b2rp" not in names["b2-super-battle-droid"]
    assert catalog["b2-rp-battle-droid"]["subject_id"] == "b2-rp-battle-droid"
    assert capture_identity(catalog["b2-rp-battle-droid"]) == "b2-super-battle-droid"
    assert "future" not in names["rp-edition"] and "edition" not in names["rp-edition"]


def test_every_printed_name_and_reviewed_alias_remains_valid_with_only_deliberate_family_overlap(game):
    identities = defaultdict(set)
    for key, card in game.catalog.items():
        for answer in [card["name"], *card["aliases"]]:
            assert normalize_alias(answer) in game.capture_names[key], (key, answer)
        for answer in game.capture_names[key]:
            identities[answer].add(capture_identity(card))
    for key, answers in game.capture_names.items():
        for answer in answers:
            assert len(identities[answer]) == 1 or answer == normalize_alias(game.catalog[key]["name"]), (key, answer)
    assert len(game.catalog) == 851
    assert game.catalog["b1-series-rocket-battle-droid"]["subject_id"] != game.catalog["b1-battle-droid"]["subject_id"]
