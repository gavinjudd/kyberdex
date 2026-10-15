"""Rejected-name capture and manager review through the real Discord adapter."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from venator_vault.bot import CaptureModal, VaultBot
from venator_vault.config import Settings
from venator_vault.core import GameError, normalize_alias

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def factory(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=1_791_849_600.)
    monkeypatch.setattr(time, "time", lambda: clock.now)
    document = json.loads((ROOT / "data/cards.json").read_text())
    document["cards"] = [card for card in document["cards"]
                         if card["id"] in ("captain-rex", "commander-cody")]
    catalog = tmp_path / "cards.json"
    catalog.write_text(json.dumps(document))
    settings = Settings(ROOT, tmp_path / "vault.sqlite3", catalog, None, 42)
    bots = []

    def create():
        bot = VaultBot(settings)
        bot.card_image = AsyncMock(return_value=None)
        bot.refresh_drop = AsyncMock()
        bots.append(bot)
        return bot

    yield create, clock
    for bot in bots:
        await bot.close()


def interaction(identity=1000, *, user=101, guild=42, manager=True, automated=False,
                custom_id="", guess=""):
    return SimpleNamespace(
        id=identity, guild_id=guild, type=discord.InteractionType.modal_submit,
        user=SimpleNamespace(id=user, bot=automated),
        permissions=SimpleNamespace(manage_guild=manager),
        data={"custom_id": custom_id, "components": [{"components": [
            {"custom_id": "card-name", "value": guess}]}]},
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(),
                                 send_modal=AsyncMock(), is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock()))


def private_response(event):
    event.followup.send.assert_awaited_once()
    event.response.send_message.assert_not_awaited()
    response = event.followup.send.await_args.kwargs
    assert response["ephemeral"] is True
    assert response["allowed_mentions"].to_dict() == {"parse": []}
    return response


def reject(bot, drop, guess, *, user="101", request_id=None):
    with pytest.raises(GameError) as error:
        bot.game.claim(drop["id"], user, guess, request_id=request_id)
    assert error.value.code == "wrong_name"


@pytest.mark.asyncio
async def test_wrong_modal_submission_records_exact_attempt_and_private_rejection(factory):
    create, clock = factory
    bot = create()
    drop = bot.game.create_drop("42", "10", "captain-rex")
    modal = CaptureModal(bot, drop["id"])
    modal.answer._value = "Captain Missingname"
    event = interaction()

    await modal.on_submit(event)

    event.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert "not accepted" in private_response(event)["content"]
    with closing(sqlite3.connect(bot.settings.database)) as connection:
        row = connection.execute("""SELECT drop_id,guild_id,channel_id,user_id,card_id,
            guess,normalized_guess,request_id,created_at FROM capture_name_failures""").fetchone()
    assert row == (drop["id"], "42", "10", "101", "captain-rex", "Captain Missingname",
                   normalize_alias("Captain Missingname"), "1000", clock.now)
    assert bot.game.inventory("42", "101") == []
    assert bot.game.get_drop(drop["id"])["remaining"] == drop["remaining"]
    bot.refresh_drop.assert_not_awaited()
    bot.card_image.assert_not_awaited()
    assert create().game.failed_guesses("42")[0]["attempts"] == 1


@pytest.mark.asyncio
async def test_old_modal_routes_after_restart_and_redelivery_records_only_once(factory):
    create, clock = factory
    original = create()
    drop = original.game.create_drop("42", "10", "captain-rex")
    custom_id = CaptureModal(original, drop["id"]).custom_id
    restored = create()
    event = interaction(custom_id=custom_id, guess="Missingname")

    await restored.on_interaction(event)
    duplicate = CaptureModal(restored, drop["id"])
    duplicate.answer._value = "Missingname"
    await duplicate.on_submit(event)
    assert "not accepted" in private_response(event)["content"]

    # A new process has no in-memory modal cache; the Discord request ID remains
    # deduplicated even after its ordinary guess cooldown has elapsed.
    clock.now += 4
    restarted = create()
    replay = interaction(custom_id=custom_id, guess="Missingname")
    await restarted.on_interaction(replay)
    assert "already recorded" in private_response(replay)["content"]
    assert restarted.game.failed_guesses("42")[0]["attempts"] == 1
    with closing(sqlite3.connect(restarted.settings.database)) as connection:
        assert connection.execute("SELECT request_id FROM capture_name_failures").fetchall() == [("1000",)]


@pytest.mark.asyncio
async def test_manager_review_exports_expected_name_guess_and_historical_matching_rules(factory):
    create, clock = factory
    bot = create()
    drop = bot.game.create_drop("42", "10", "captain-rex")
    reject(bot, drop, "Captain Missingname", request_id="first")
    clock.now += 1
    reject(bot, drop, "CAPTAIN MISSINGNAME!", user="102", request_id="second")
    group = bot.tree.get_command("vault-admin")
    command = group.get_command("failed-names")
    event = interaction()

    assert await group.interaction_check(event)
    await command.callback(group, event)

    event.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    response = private_response(event)
    embed = response["embed"]
    assert embed.title == "Rejected capture names"
    assert len(embed.fields) == 1
    assert embed.fields[0].name == "Expected: Captain Rex"
    assert "CAPTAIN MISSINGNAME!" in embed.fields[0].value
    assert "2 attempts · 2 players" in embed.fields[0].value
    assert "Still rejected by current rules" in embed.fields[0].value
    assert response["file"].filename == "rejected-capture-names.json"
    payload = json.load(response["file"].fp)
    assert (payload["page"], payload["page_size"], payload["card_id"]) == (1, 5, None)
    row, = payload["groups"]
    assert row["latest_guess"] == "CAPTAIN MISSINGNAME!"
    assert row["expected_name_then"] == "Captain Rex"
    assert row["latest_drop_id"] == drop["id"]
    assert row["accepted_names_then"] == row["accepted_names"] == sorted(bot.game.capture_names["captain-rex"])
    assert "rex" in row["accepted_names"]
    assert row["accepted_now"] is False
    assert bot.game.inventory("42", "101") == []


@pytest.mark.asyncio
async def test_review_identifies_names_accepted_since_original_rejection(factory):
    create, _ = factory
    bot = create()
    drop = bot.game.create_drop("42", "10", "captain-rex")
    reject(bot, drop, "Missingname")
    document = json.loads(bot.settings.catalog.read_text())
    next(card for card in document["cards"] if card["id"] == "captain-rex")["aliases"].append("Missingname")
    bot.settings.catalog.write_text(json.dumps(document))
    restarted = create()
    command = restarted.tree.get_command("vault-admin").get_command("failed-names")
    event = interaction()

    await command.callback(command.binding, event)

    response = private_response(event)
    assert "Accepted by current rules" in response["embed"].fields[0].value
    row, = json.load(response["file"].fp)["groups"]
    assert row["accepted_now"] is True
    assert "missingname" in row["accepted_names"]
    assert "missingname" not in row["accepted_names_then"]


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [
    {"manager": False}, {"automated": True}, {"guild": 99}, {"guild": None},
])
async def test_manager_gate_and_autocomplete_deny_untrusted_contexts(factory, context):
    create, _ = factory
    group = create().tree.get_command("vault-admin")
    event = interaction(**context)

    assert group.default_permissions.manage_guild
    assert not await group.interaction_check(event)
    assert await group.autocomplete_failed_card(event, "rex") == []
    response = private_response(event)
    assert "embed" not in response and "file" not in response
    event.response.defer.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_card_filter_and_autocomplete_resolve_aliases_without_cross_card_rows(factory):
    create, _ = factory
    bot = create()
    for channel, card_id in enumerate(("captain-rex", "commander-cody"), start=10):
        drop = bot.game.create_drop("42", str(channel), card_id)
        reject(bot, drop, "Missingname")
    group = bot.tree.get_command("vault-admin")
    choices = await group.autocomplete_failed_card(interaction(), "ct-7567")
    assert [(choice.name, choice.value) for choice in choices] == [("Captain Rex", "captain-rex")]
    assert await group.autocomplete_failed_card(interaction(), "not-a-card") == []
    command = group.get_command("failed-names")
    event = interaction()

    await command.callback(group, event, 1, "commander-cody")

    payload = json.load(private_response(event)["file"].fp)
    assert payload["card_id"] == "commander-cody"
    assert [row["card_id"] for row in payload["groups"]] == ["commander-cody"]
    invalid = interaction(1001)
    with pytest.raises(GameError):
        await command.callback(group, invalid, 1, "not-a-card")
    invalid.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_escapes_mentions_and_markdown_without_changing_exported_evidence(factory):
    create, _ = factory
    bot = create()
    drop = bot.game.create_drop("42", "10", "captain-rex")
    guess = "**Missingname** @everyone <@123456789012345678>"
    reject(bot, drop, guess)
    command = bot.tree.get_command("vault-admin").get_command("failed-names")
    event = interaction()

    await command.callback(command.binding, event)

    response = private_response(event)
    value = response["embed"].fields[0].value
    assert "@everyone" not in value and "<@123456789012345678>" not in value
    assert r"\*\*Missingname\*\*" in value
    assert json.load(response["file"].fp)["groups"][0]["latest_guess"] == guess
    bot.refresh_drop.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_review_explains_when_logging_started_and_exports_empty_page(factory):
    create, _ = factory
    bot = create()
    command = bot.tree.get_command("vault-admin").get_command("failed-names")
    event = interaction()

    await command.callback(command.binding, event)

    response = private_response(event)
    assert "earlier guesses were not saved" in response["embed"].description
    assert not response["embed"].fields
    assert json.load(response["file"].fp) == {"page": 1, "page_size": 5, "card_id": None, "groups": []}
