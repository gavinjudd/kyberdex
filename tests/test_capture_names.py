"""Real catalogue matching and ownership checks against disposable player data."""
from pathlib import Path

import pytest

from venator_vault.core import Game, GameError


@pytest.fixture
def game(tmp_path):
    return Game(tmp_path / "names.sqlite3", Path(__file__).resolve().parents[1] / "data/cards.json")


@pytest.mark.parametrize("card,answer", [
    ("lindon-javes", "Commander Lindon Javes"), ("lindon-javes", "lindon"),
    ("lindon-javes", "javes"), ("lindon-javes", "commander lindon"),
    ("lindon-javes", "commander javes"), ("lindon-javes", "LÍNDON JAVES!"),
    ("lindon-javes", "CommanderJaves"), ("lindon-javes", "Commander-Javes"),
    ("lindon-javes", "Javes, Lindon"),
    ("captain-rex", "rex"), ("commander-cody", "cody"),
    ("luminara-unduli", "Master Luminara"), ("luminara-unduli", "Unduli"),
    ("grand-admiral-thrawn", "admiral thrawn"), ("satele-shan", "Satele"),
    ("jedi-knight-revan", "Revan"), ("revan-shadow-of-revan", "Revan"),
    ("venator-class", "Venator"), ("x-wing", "T-65"), ("r2-d2", "r2 d2"),
    ("at-hauler", "Hauler"), ("at-hauler", "Imperial AT Hauler"),
    ("phoenix-home-rebel-capital-ship", "Pelta"),
    ("phoenix-home-rebel-capital-ship", "Pelta-class frigate"),
    ("kylo-ren-s-command-shuttle", "Upsilon"),
    ("kylo-ren-s-command-shuttle", "Upsilon-class command shuttle"),
    ("kylo-ren-s-command-shuttle", "Kylo rens command shuttle"),
])
def test_distinctive_names_and_optional_titles_can_capture(game, card, answer):
    drop = game.create_drop("42", "10", card)
    result = game.claim(drop["id"], "101", answer)
    assert result["instance"]["card_id"] == card


@pytest.mark.parametrize("card,answer", [
    ("lindon-javes", "commander"), ("lindon-javes", "lindo"),
    ("lindon-javes", "Commander Lindon Skywalker"), ("lindon-javes", "javes anakin"),
    ("lindon-javes", "jave"),
    ("luke-skywalker", "skywalker"), ("satele-shan", "shan"),
    ("boba-fett", "fett"), ("jango-fett", "fett"),
    ("x-wing", "starfighter"), ("venator-class", "class"), ("b1-battle-droid", "droid"),
    ("captain-rex", "clone"), ("captain-rex", "trooper"), ("captain-rex", ""),
    ("phoenix-home-rebel-capital-ship", "capital"),
])
def test_generic_ambiguous_and_incorrect_parts_do_not_capture(game, card, answer):
    drop = game.create_drop("42", "10", card)
    with pytest.raises(GameError) as failure:
        game.claim(drop["id"], "101", answer)
    assert failure.value.code == "wrong_name"
    assert game.get_drop(drop["id"])["remaining"] == drop["remaining"]
    assert game.inventory("42", "101") == []


def test_owned_card_lookup_and_choices_are_scoped_and_current(game):
    with game._tx() as db:
        rex = game._mint(db, "42", "101", "captain-rex", 1, "test")
        game._mint(db, "42", "101", "captain-rex", 2, "test")
        game._mint(db, "42", "102", "commander-cody", 1, "test")
        game._mint(db, "other", "101", "lindon-javes", 1, "test")
    assert [card["id"] for card in game.owned_cards("42", "101")] == ["captain-rex"]
    assert game.owned_cards("42", "101", "cody") == []
    assert game.owned_card("42", "101", "captain-rex")["id"] == rex["id"]
    for card in ("commander-cody", "lindon-javes", "not-a-card"):
        with pytest.raises(GameError) as failure:
            game.owned_card("42", "101", card)
        assert failure.value.code == "not_owned"
    with game._tx() as db:
        db.execute("UPDATE instances SET consumed_at=3 WHERE guild_id='42' AND user_id='101'")
    assert game.owned_cards("42", "101") == []
    with pytest.raises(GameError, match="currently own"):
        game.owned_card("42", "101", "captain-rex")


def test_all_printed_names_and_registered_nicknames_are_accepted(game):
    from venator_vault.core import normalize_alias
    assert len(game.catalog) == 871
    for key, card in game.catalog.items():
        for answer in [card['name'], *card['aliases']]:
            assert normalize_alias(answer) in game.capture_names[key], (key, answer)


@pytest.mark.parametrize('card,answers', [
    ('lindon-javes', ['Lindon', 'Javes', 'Lindon Javes', 'Commander Lindon', 'Commander Javes']),
    ('luke-skywalker', ['Luke', 'Luke Skywalker', 'Jedi Luke']),
    ('jango-fett', ['Jango', 'Jango Fett']),
    ('grand-admiral-thrawn', ['Thrawn', 'Admiral Thrawn', 'Grand Admiral Thrawn']),
    ('gunny', ['Kierah', 'Koovah', 'Kierah Koovah', 'Gunny']),
])
def test_name_combination_capture_paths(game, card, answers):
    # Real claim processing with disposable inventories, not only a string-list check.
    for index, answer in enumerate(answers):
        drop = game.create_drop('42', f'combo-{index}', card)
        result = game.claim(drop['id'], f'user-{index}', answer)
        assert result['instance']['card_id'] == card


def test_generated_shortcuts_have_one_subject_and_titles_do_not_rescue_collisions(game):
    from collections import defaultdict

    from venator_vault.core import capture_identity, normalize_alias
    subjects = defaultdict(set)
    for key, answers in game.capture_names.items():
        for answer in answers:
            subjects[answer].add(capture_identity(game.catalog[key]))
    for key, answers in game.capture_names.items():
        for answer in answers:
            assert len(subjects[answer]) == 1 or answer == normalize_alias(game.catalog[key]['name']), (key, answer)
    for card, answer in [('luke-skywalker','Jedi Skywalker'),('jango-fett','Commander Fett'),
                         ('lindon-javes','Commander Lindon Javes Skywalker'),
                         ('grand-admiral-thrawn','Grand Admiral'),('x-wing','Wing'),
                         ('the-raddus','Raddus')]:
        drop = game.create_drop('42', 'invalid-'+card, card)
        with pytest.raises(GameError) as error:
            game.claim(drop['id'], 'tester', answer)
        assert error.value.code == 'wrong_name'


def test_title_last_name_can_disambiguate_known_titled_identities():
    from venator_vault.core import capture_names
    catalog = {key:{"id": key,"subject_id": key,"category": 'canon',"arena": 'ground',"name": name,"aliases": []}
               for key,name in [('a','General Alice Example'),('b','Captain Bob Example')]}
    names = capture_names(catalog)
    assert 'example' not in names['a'] | names['b']
    assert 'generalexample' in names['a'] and 'generalexample' not in names['b']
    assert 'captainexample' in names['b'] and 'captainexample' not in names['a']
    assert 'aliceexample' in names['a'] and 'bobexample' in names['b']
