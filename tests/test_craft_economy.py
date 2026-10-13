"""Launch recipe economics and real Discord interaction boundaries; no gateway."""
import concurrent.futures
import copy
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from venator_vault import core
from venator_vault.bot import VaultBot
from venator_vault.config import Settings
from venator_vault.features import copy_choices, handle_component, review_consumption, select_craft_ingredients


@pytest.fixture
def game(tmp_path):
    cards = []
    for rarity in core.RARITIES:
        cards.append({"id": rarity, "name": f"Fixture {rarity.title()}", "rarity": rarity,
                      "aliases": [], "category": "canon", "arena": "ground", "faction": "Republic",
                      "era": "Clone Wars", "role": "striker", "attack": 100, "health": 500, "speed": 70,
                      "weight": 1, "release": None})
    for name, changes in (
        ("other-faction", {"faction": "Empire"}),
        ("other-arena", {"arena": "fleet"}),
        ("meme", {"category": "meme"}),
        ("limited", {"category": "limited", "release": {
            "starts_at": "2020-01-01T00:00:00Z", "ends_at": "2030-01-01T00:00:00Z", "supply_cap": 100}}),
        ("legends", {"category": "legends", "event_id": "archive", "release": {
            "starts_at": "2020-01-01T00:00:00Z", "ends_at": "2030-01-01T00:00:00Z", "supply_cap": 100}}),
    ):
        cards.append({**cards[4], "id": name, "name": name, **changes})
    catalog = tmp_path / "cards.json"
    catalog.write_text(json.dumps({"schema_version": 1, "cards": cards, "rarities": {}}))
    return core.Game(tmp_path / "game.sqlite3", catalog)


def seed(game, card_id, count, credits=0, user="101"):
    """Exact fixture resources; this is not an acquisition-path simulation."""
    with game._tx() as connection:
        items = [game._mint(connection, "42", user, card_id, time.time(), "test fixture") for _ in range(count)]
        if credits:
            game._credit(connection, "42", user, credits, "test fixture", card_id, time.time())
    return [item["id"] for item in items]


def state(game, user="101"):
    profile = game.profile("42", user)
    return profile["credits"], {row["id"] for row in game.inventory("42", user, limit=100)}


@pytest.mark.parametrize("target,ingredient,count,cost", [
    ("uncommon", "common", 3, 150), ("rare", "uncommon", 3, 150),
    ("epic", "rare", 3, 300), ("legendary", "epic", 5, 750), ("mythic", "legendary", 7, 2000),
])
def test_published_tier_recipe_consumes_exact_resources_once(game, target, ingredient, count, cost):
    ids = seed(game, ingredient, count, cost + 17)
    quote = game.craft_quote(target)
    assert (quote["ingredients"], quote["cost"]) == (count, cost)
    with pytest.raises(core.GameError, match="exactly"):
        game.craft("42", "101", ids[:-1], target)
    assert state(game) == (cost + 17, set(ids))
    result = game.craft("42", "101", ids, target, expected_recipe_fingerprint=quote["fingerprint"])
    assert result["credits_spent"] == cost
    assert state(game) == (17, {result["instance"]["id"]})
    assert set(result["consumed"]) == set(ids)
    with pytest.raises(core.GameError) as replay:
        game.craft("42", "101", ids, target)
    assert replay.value.code == "not_owned"
    assert state(game) == (17, {result["instance"]["id"]})


@pytest.mark.parametrize("target,ingredient,count,cost", [("legendary", "epic", 5, 750), ("mythic", "legendary", 7, 2000)])
def test_high_tier_budget_and_three_copy_shortcut_reject_atomically(game, target, ingredient, count, cost):
    ids = seed(game, ingredient, count, cost - 1)
    before = state(game)
    with pytest.raises(core.GameError) as insufficient:
        game.craft("42", "101", ids, target)
    assert insufficient.value.code == "insufficient_credits"
    assert state(game) == before
    with pytest.raises(core.GameError) as obsolete:
        game.craft("42", "101", ids[:3], target)
    assert obsolete.value.code == "invalid_recipe"
    assert state(game) == before


@pytest.mark.parametrize("field,value", [("cost", 2100), ("ingredients", 6)])
def test_quote_rejects_changed_price_or_count_before_consumption(game, monkeypatch, field, value):
    ids = seed(game, "legendary", 7, 3000)
    quote = game.craft_quote("mythic")
    before = state(game)
    monkeypatch.setitem(core.CRAFT_RECIPES, "mythic", {**core.CRAFT_RECIPES["mythic"], field: value})
    with pytest.raises(core.GameError) as changed:
        game.craft("42", "101", ids, "mythic", expected_recipe_fingerprint=quote["fingerprint"])
    assert changed.value.code == "stale_recipe"
    assert state(game) == before


@pytest.mark.parametrize("ineligible", ["other-faction", "other-arena", "meme", "limited"])
def test_larger_recipe_keeps_type_and_affiliation_guards(game, ineligible):
    ids = seed(game, "legendary", 6, 2000) + seed(game, ineligible, 1)
    before = state(game)
    with pytest.raises(core.GameError) as invalid:
        game.craft("42", "101", ids, "mythic")
    assert invalid.value.code == "invalid_recipe"
    assert state(game) == before


def test_larger_recipe_keeps_protection_guards(game):
    ids = seed(game, "legendary", 7, 2000)
    game.set_favorite("42", "101", ids[-1], True)
    before = state(game)
    with pytest.raises(core.GameError) as protected:
        game.craft("42", "101", ids, "mythic")
    assert protected.value.code == "protected_card"
    assert state(game) == before


def test_concurrent_seven_copy_craft_issues_one_mythic_and_one_debit(game):
    ids = seed(game, "legendary", 7, 4000)
    barrier = threading.Barrier(4)

    def attempt(_):
        barrier.wait(timeout=5)
        try:
            return game.craft("42", "101", ids, "mythic")
        except core.GameError as error:
            return error.code

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    successes = [result for result in results if isinstance(result, dict)]
    assert len(successes) == 1 and results.count("not_owned") == 3
    assert state(game) == (2000, {successes[0]["instance"]["id"]})


def test_epic_to_mythic_chain_needs_35_epics_and_7250_credits(game):
    ids = seed(game, "epic", 35, 7250)
    intermediate = [game.craft("42", "101", ids[index:index+5], "legendary")["instance"]["id"]
                    for index in range(0, 35, 5)]
    result = game.craft("42", "101", intermediate, "mythic")
    assert state(game) == (0, {result["instance"]["id"]})
    with game._tx() as connection:
        spent, operations = connection.execute("SELECT SUM(delta),COUNT(*) FROM ledger WHERE reason='craft'").fetchone()
        consumed = connection.execute("SELECT COUNT(*) FROM instances WHERE consumed_at IS NOT NULL").fetchone()[0]
    assert (spent, operations, consumed) == (-7250, 8, 42)


@pytest.fixture
def bot(game, tmp_path):
    root = Path(__file__).resolve().parents[1]
    bot = VaultBot(Settings(root, tmp_path / "game.sqlite3", tmp_path / "cards.json", None, 42))
    bot.card_image = AsyncMock(return_value=None)
    return bot


def interaction(user=101, guild=42, values=None):
    return SimpleNamespace(user=SimpleNamespace(id=user, bot=False), guild_id=guild,
        data={"values": values or []}, namespace=SimpleNamespace(),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock()))


@pytest.mark.asyncio
async def test_native_seven_copy_selection_review_and_confirmation(bot):
    ids = seed(bot.game, "legendary", 7, 2000)
    first = interaction()
    await select_craft_ingredients(bot, first, "mythic")
    picker = first.followup.send.await_args.kwargs["view"].children[0]
    assert picker.min_values == picker.max_values == 7
    assert {option.value for option in picker.options} == set(ids)
    selection_key = picker.custom_id.split(":")[-1]
    selected = interaction(values=ids)
    await handle_component(bot, selected, "craftpick", selection_key)
    review = selected.followup.send.await_args.kwargs["embed"].description
    assert "7 Legendary copies" in review and "2,000 credits" in review
    key = next(iter(bot.pending_confirmations))
    assert bot.pending_confirmations[key]["kwargs"]["expected_recipe_fingerprint"]
    await handle_component(bot, interaction(), "confirm", key)
    owned = bot.game.inventory("42", "101")
    assert len(owned) == 1 and owned[0]["card_id"] == "mythic"
    assert bot.game.profile("42", "101")["credits"] == 0
    with pytest.raises(ValueError, match="expired"):
        await handle_component(bot, interaction(), "confirm", key)


@pytest.mark.asyncio
async def test_private_selection_cannot_be_used_by_another_collector(bot):
    ids = seed(bot.game, "legendary", 7, 2000)
    await select_craft_ingredients(bot, interaction(), "mythic")
    key = next(iter(bot.pending_confirmations))
    before = state(bot.game)
    with pytest.raises(ValueError, match="Only the collector"):
        await handle_component(bot, interaction(user=102, values=ids), "craftpick", key)
    assert state(bot.game) == before
    assert key in bot.pending_confirmations


@pytest.mark.asyncio
async def test_pending_confirmation_cannot_charge_a_changed_recipe(bot, monkeypatch):
    ids = seed(bot.game, "legendary", 7, 3000)
    await review_consumption(bot, interaction(), "craft", " ".join(ids), "mythic")
    key = next(iter(bot.pending_confirmations))
    before = state(bot.game)
    monkeypatch.setitem(core.CRAFT_RECIPES, "mythic", {"ingredients": 7, "cost": 2100})
    with pytest.raises(core.GameError) as stale:
        await handle_component(bot, interaction(), "confirm", key)
    assert stale.value.code == "stale_recipe"
    assert state(bot.game) == before


@pytest.mark.asyncio
async def test_large_recipe_autocomplete_never_exceeds_discord_value_limit(bot):
    ids = seed(bot.game, "legendary", 7)
    choices = await copy_choices(bot, interaction(), " ".join(ids[:4]) + " ", limit=7)
    assert choices and all(len(choice.value) <= 100 for choice in choices)
    choices = await copy_choices(bot, interaction(), " ".join(ids[:5]) + " ", limit=7)
    assert choices == []  # Native seven-item selector or explicit IDs handles this case.


@pytest.mark.asyncio
async def test_launch_target_selector_filters_unreachable_and_special_targets(bot):
    command = bot.tree.get_command("vault").get_command("craft")
    callback = command._params["target_card"].autocomplete
    choices = await callback(interaction(), "")
    assert {choice.value for choice in choices} == {"uncommon", "rare", "epic", "legendary", "mythic"}
    # Removing the only regular Epic ingredient makes Legendary unreachable.
    bot.game.catalog["epic"] = {**bot.game.catalog["epic"], "faction": "Unrelated"}
    choices = await callback(interaction(), "legendary")
    assert choices == []


@pytest.mark.asyncio
async def test_launch_review_does_not_offer_event_or_limited_crafting(bot):
    ids = seed(bot.game, "epic", 5, 750)
    for target in ("legends", "limited", "meme"):
        with pytest.raises(ValueError, match="Launch crafting"):
            await review_consumption(bot, interaction(), "craft", " ".join(ids), target)
    assert not bot.pending_confirmations
    assert state(bot.game) == (750, set(ids))


def test_recipe_publication_is_tiered_and_does_not_advertise_launch_event_crafting(game):
    recipes = game.recipes()
    assert recipes["targets"]["legendary"] == {"ingredients": 5, "cost": 750}
    assert recipes["targets"]["mythic"] == {"ingredients": 7, "cost": 2000}
    assert "event cards are unavailable in launch crafting" in recipes["requirements"]
    # Caller mutation cannot silently alter future quotes.
    original = copy.deepcopy(core.CRAFT_RECIPES)
    recipes["targets"]["mythic"]["cost"] = 1
    assert core.CRAFT_RECIPES == original
