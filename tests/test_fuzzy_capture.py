"""Small typos and possessive names are accepted; other cards' names never are."""
from pathlib import Path

import pytest

from venator_vault.core import Game, edit_distance, normalize_alias, untitled_name

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def game(tmp_path_factory):
    return Game(tmp_path_factory.mktemp("fuzzy") / "game.sqlite", ROOT / "data/cards.json")


def accepted(game, card_id, guess):
    return bool({normalize_alias(guess), untitled_name(guess)} & game.capture_names[card_id]) or game.close_spelling(card_id, guess)


@pytest.mark.parametrize("card_id,guess", [
    ("leia-hoth", "Leia orgona"), ("acclamator-class", "Aclimator"), ("darth-vader", "darth vadar"),
    ("kylo-rens-modified-tie-whisper", "TIE whisper"), ("wat-tambor", "watt tambor"),
    ("anakin-skywalker-s-podracer", "Anakins podracer"),
])
def test_real_near_misses_are_accepted(game, card_id, guess):
    assert accepted(game, card_id, guess)


@pytest.mark.parametrize("card_id,guess", [
    ("kylo-rens-modified-tie-whisper", "Tie silencer"), ("kylo-rens-modified-tie-whisper", "TIE"),
    ("rex-umbara", "hawk"), ("lott-dod", "Nute gunray"), ("captain-rex", "captain cody"),
    ("kanan-s-speeder-bike", "Kanan"), ("acclamator-class", "Acclaimer"),
])
def test_wrong_or_vague_answers_stay_rejected(game, card_id, guess):
    assert not accepted(game, card_id, guess)


def test_no_card_accepts_another_identitys_exact_name_by_typo(game):
    from venator_vault.core import capture_identity
    names = {}
    for key, answers in game.capture_names.items():
        for answer in answers:
            names.setdefault(answer, set()).add(capture_identity(game.catalog[key]))
    sample = list(game.catalog)[::7]
    for key in sample:
        identity = capture_identity(game.catalog[key])
        for answer, owners in names.items():
            if identity not in owners and len(answer) >= 6 and abs(len(answer) - 10) < 8:
                assert not game.close_spelling(key, answer), (key, answer)


def test_edit_distance_counts_swaps_and_stops_early():
    assert edit_distance("acclamator", "acclimator", 2) == 1
    assert edit_distance("vadre", "vader", 2) == 1
    assert edit_distance("abcdefgh", "zzzzzzzz", 2) == 3
