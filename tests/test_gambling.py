"""Credit gambling: house edge, limits, ledger integrity, and no effect on cards."""
import math
from itertools import product

import pytest

from venator_vault import core
from venator_vault.core import (
    GAMBLE_DAILY_CAP,
    SLOT_KYBER_PAIR,
    SLOT_PAIR,
    SLOT_SYMBOLS,
    WAGER_HOUSE_PERCENT,
    Game,
    GameError,
)

NOW = 2_000_000_000


@pytest.fixture
def game(tmp_path):
    from pathlib import Path
    game = Game(tmp_path / "game.sqlite", Path(__file__).resolve().parents[1] / "data/cards.json")
    with game._tx() as connection:
        for user in ("a", "b", "c"):
            game._credit(connection, "g", user, 20_000, "fixture", user, NOW)
    return game


def balance(game, user):
    return game.profile("g", user, now=NOW)["credits"]


def test_house_keeps_an_edge_on_every_solo_game():
    assert core.COINFLIP_WIN_CHANCE * 2 < 1
    total = sum(w for _, w, _ in SLOT_SYMBOLS)
    ev = 0.0
    for reels in product(SLOT_SYMBOLS, repeat=3):
        p = math.prod(w for _, w, _ in reels) / total ** 3
        names = [name for name, _, _ in reels]
        top = max(set(names), key=names.count)
        if names.count(top) == 3:
            ev += p * reels[0][2]
        elif names.count(top) == 2:
            ev += p * (SLOT_KYBER_PAIR if top == "Kyber Crystal" else SLOT_PAIR)
    assert 0.85 < ev < 0.97


def test_coinflip_moves_only_credits_and_records_the_bet(game):
    cards_before = len(game.inventory("g", "a"))
    result = game.coinflip("g", "a", 100, "heads", now=NOW, request_id="r1")
    assert balance(game, "a") == 20_000 - 100 + result["payout"]
    assert result["payout"] in (0, 200)
    assert len(game.inventory("g", "a")) == cards_before
    again = game.coinflip("g", "a", 100, "heads", now=NOW, request_id="r1")
    assert again["replayed"] and balance(game, "a") == 20_000 - 100 + result["payout"]


def test_slots_payout_matches_the_published_table(game):
    class Fixed:
        def choices(self, population, weights, k):
            return ["Kyber Crystal"] * 3
    game.rng = Fixed()
    result = game.slots("g", "a", 50, now=NOW)
    assert result["payout"] == 5000 and result["jackpot"]


def test_limits_and_pause(game):
    for bad in (5, 5000):
        with pytest.raises(GameError, match="Bets are"):
            game.coinflip("g", "a", bad, "heads", now=NOW)
    for i in range(GAMBLE_DAILY_CAP // 1000):
        game.coinflip("g", "a", 1000, "tails", now=NOW + i)
    with pytest.raises(GameError) as error:
        game.coinflip("g", "a", 10, "tails", now=NOW + 100)
    assert error.value.code == "gamble_cap"
    game.coinflip("g", "a", 10, "tails", now=NOW + 86_400)  # resets the next UTC day
    game.set_gambling("g", False)
    with pytest.raises(GameError) as error:
        game.slots("g", "b", 10, now=NOW)
    assert error.value.code == "gambling_paused"


def test_cannot_bet_credits_you_do_not_have(game):
    with pytest.raises(GameError) as error:
        game.coinflip("g", "broke", 10, "heads", now=NOW)
    assert error.value.code == "insufficient_credits"


def test_wager_settles_both_sides_atomically_with_house_cut(game):
    wager = game.wager_create("g", "a", "b", 500, now=NOW)
    with pytest.raises(GameError):
        game.wager_respond(wager["id"], "a", True, now=NOW)  # challenger can't accept their own
    with pytest.raises(GameError):
        game.wager_respond(wager["id"], "c", True, now=NOW)
    settled = game.wager_respond(wager["id"], "b", True, now=NOW + 1)
    winner, loser = (("a", "b") if settled["winner"] == "a" else ("b", "a"))
    prize = 1000 - 1000 * WAGER_HOUSE_PERCENT // 100
    assert balance(game, winner) == 20_000 - 500 + prize and balance(game, loser) == 19_500
    with pytest.raises(GameError):
        game.wager_respond(wager["id"], "b", True, now=NOW + 2)


def test_wager_expires_or_cancels_without_moving_credits(game):
    first = game.wager_create("g", "a", "b", 300, now=NOW)
    assert game.get_wager(first["id"], now=NOW + 121)["status"] == "expired"
    second = game.wager_create("g", "a", "b", 300, now=NOW + 200)
    assert game.wager_respond(second["id"], "a", False, now=NOW + 201)["status"] == "cancelled"
    assert balance(game, "a") == balance(game, "b") == 20_000
