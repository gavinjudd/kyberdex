"""Exercise Discord boundaries against the real engine, without gateway access."""
import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from venator_vault.bot import CaptureModal, VaultBot
from venator_vault.config import Settings
from venator_vault.core import GameError
from venator_vault.features import copy_choices, duel_panel, handle_component, review_consumption
from venator_vault.presentation import drop_panel, drop_view


@pytest.fixture
def bot(tmp_path, synthetic_asset_root):
    root = synthetic_asset_root
    document = json.loads((root / "data/cards.json").read_text())
    document["cards"] = [card for card in document["cards"] if card["id"] in ("captain-rex", "commander-cody")]
    catalog = tmp_path / "cards.json"
    catalog.write_text(json.dumps(document))
    return VaultBot(Settings(root, tmp_path / "vault.sqlite3", catalog, None, 42))


def interaction(user=102, guild=42):
    response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(),
                               send_modal=AsyncMock(), is_done=lambda: True)
    return SimpleNamespace(user=SimpleNamespace(id=user, bot=False), guild_id=guild,
                           response=response, followup=SimpleNamespace(send=AsyncMock()))


def seed_card(bot, user, card="captain-rex"):
    with bot.game._tx() as connection:
        return bot.game._mint(connection, "42", str(user), card, time.time(), "interaction fixture")


@pytest.mark.asyncio
async def test_card_command_shares_an_owned_copy_publicly(bot):
    owned = seed_card(bot, 102)
    bot.card_image = AsyncMock(return_value=None)
    group = bot.tree.get_command("vault")
    event = interaction()
    await group.get_command("card").callback(group, event, "captain-rex")
    event.response.defer.assert_awaited_once_with(thinking=True)
    response = event.followup.send.await_args.kwargs
    assert response["ephemeral"] is False
    assert owned["id"] in str(response["embed"].to_dict())
    assert bot.card_image.await_args.args[1]["id"] == owned["id"]


@pytest.mark.asyncio
async def test_card_command_rejects_unowned_card_without_rendering(bot):
    seed_card(bot, 101)
    bot.card_image = AsyncMock(return_value=None)
    group = bot.tree.get_command("vault")
    with pytest.raises(GameError, match="currently own"):
        await group.get_command("card").callback(group, interaction(), "captain-rex")
    bot.card_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_card_autocomplete_includes_only_requesters_owned_definitions(bot):
    seed_card(bot, 102)
    seed_card(bot, 102)
    seed_card(bot, 101, "commander-cody")
    group = bot.tree.get_command("vault")
    choices = await group.autocomplete_owned_card(interaction(), "")
    assert [choice.value for choice in choices] == ["captain-rex"]


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [20, 60, 110, 179])
async def test_repeated_hints_are_private_and_leave_capture_window_intact(bot, monkeypatch, age):
    import discord

    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    drop = bot.game.create_drop("42", "10", "captain-rex", now=now-age, duration=180)
    original_panel = drop_panel(drop).to_dict()
    bot.refresh_drop = AsyncMock()
    source_message = SimpleNamespace(edit=AsyncMock())
    for user in (101, 102, 101):
        event = interaction(user=user)
        event.type = discord.InteractionType.component
        event.data = {"custom_id": f"vv:hint:{drop['id']}"}
        event.message = source_message
        await bot.on_interaction(event)
        event.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        response = event.followup.send.await_args.kwargs
        assert response["ephemeral"] is True
        assert "capture" in response["content"].lower()
        if age >= 35:
            assert f"<t:{int(drop['expires_at'])}:R>" in response["content"]
        assert bot.game.get_drop(drop["id"]) == drop
        assert drop_panel(bot.game.get_drop(drop["id"])).to_dict() == original_panel
        assert all(not button.disabled for button in drop_view(drop).children)
    source_message.edit.assert_not_awaited()
    bot.refresh_drop.assert_not_awaited()
    result = bot.game.claim(drop["id"], "102", "Captain Rex")
    assert result["remaining"] == 1
    assert result["drop"]["status"] == "active"
    assert result["drop"]["expires_at"] == drop["expires_at"]
    monkeypatch.setattr(time, "time", lambda: drop["expires_at"])
    expired = bot.game.get_drop(drop["id"])
    assert expired["status"] == "expired"
    assert expired["expires_at"] == drop["expires_at"]


@pytest.mark.asyncio
async def test_hint_after_deadline_does_not_reopen_capture(bot, monkeypatch):
    import discord

    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    drop = bot.game.create_drop("42", "10", "captain-rex", now=now-181, duration=180)
    event = interaction()
    event.type = discord.InteractionType.component
    event.data = {"custom_id": f"vv:hint:{drop['id']}"}
    await bot.on_interaction(event)
    event.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert "has ended" in event.followup.send.await_args.kwargs["content"]
    assert bot.game.get_drop(drop["id"])["status"] == "expired"
    with pytest.raises(GameError, match="expired"):
        bot.game.claim(drop["id"], "102", "Captain Rex")


@pytest.mark.asyncio
async def test_runtime_storage_wait_does_not_block_discord_event_loop(bot, monkeypatch):
    released = threading.Event()

    def blocked_read(_guild):
        if not released.wait(1):
            raise RuntimeError("Event loop could not release a blocked storage worker")
        return {"guild_id": "42"}

    monkeypatch.setattr(bot.runtime, "get", blocked_read)
    asyncio.get_running_loop().call_later(0.02, released.set)
    result = await asyncio.wait_for(bot.runtime_call("get", 42), 0.5)
    assert result["guild_id"] == "42"


@pytest.mark.asyncio
async def test_autocomplete_returns_before_discord_deadline_when_storage_waits(bot, monkeypatch):
    async def slow_inventory(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(bot, "call", slow_inventory)
    choices = await asyncio.wait_for(copy_choices(bot, interaction(), "Rex"), 2.5)
    assert choices == []


@pytest.mark.asyncio
async def test_failed_raid_publication_retries_in_one_minute(bot, monkeypatch):
    started = time.time()
    bot.runtime.configure("42", "10", 900, "101", now=started-7201)
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    bot.commands_synced = True
    monkeypatch.setattr(bot, "post_drop", AsyncMock())
    monkeypatch.setattr(bot, "post_raid", AsyncMock(side_effect=ValueError("Temporary publication failure")))
    await bot.scheduler()
    bot.post_raid.assert_awaited_once()
    next_attempt = bot.runtime.get("42")["next_raid"]
    assert started+60 <= next_attempt <= time.time()+61


@pytest.mark.asyncio
async def test_duel_button_accepts_duel_and_updates_public_result(bot):
    left, right = seed_card(bot, 101), seed_card(bot, 102, "commander-cody")
    bot.game.set_deck("42", "101", "ground", [left["id"]])
    bot.game.set_deck("42", "102", "ground", [right["id"]])
    record = bot.game.duel_create("42", "101", "102", "ground")
    preview = duel_panel(record).to_dict()
    assert "Captain Rex" in str(preview) and "Commander Cody" in str(preview)
    message = SimpleNamespace(edit=AsyncMock())
    bot.get_channel = lambda _: SimpleNamespace(get_partial_message=lambda _: message)
    bot.runtime.bind("duel", record["id"], 11, 12)
    event = interaction()
    await handle_component(bot, event, "acceptduel", record["id"])
    assert bot.game.get_duel(record["id"])["status"] == "completed"
    event.response.defer.assert_awaited_once()
    message.edit.assert_awaited_once()
    public = message.edit.await_args.kwargs
    assert public["embed"].title == "Ground duel complete"
    assert public["attachments"][0].filename == "battle-log.txt"
    assert all(item.disabled for item in public["view"].children)
    # Replaying acceptance returns the same result without double scoring.
    before = [bot.game.profile("42", str(uid))["wins"] for uid in (101, 102)]
    await handle_component(bot, interaction(), "acceptduel", record["id"])
    assert [bot.game.profile("42", str(uid))["wins"] for uid in (101, 102)] == before


@pytest.mark.asyncio
async def test_wishlist_accepts_engine_card_dicts(bot):
    command = bot.tree.get_command("vault").get_command("wishlist")
    event = interaction()
    await command.callback(command.binding, event, "captain-rex", True)
    assert "Captain Rex" in event.followup.send.await_args.kwargs["embed"].description


@pytest.mark.asyncio
async def test_capture_replay_after_transfer_is_a_receipt_not_current_ownership(bot, monkeypatch):
    drop = bot.game.create_drop("42", "10", "captain-rex")
    original = bot.game.claim(drop["id"], "101", "Captain Rex")["instance"]
    other = seed_card(bot, 102, "commander-cody")
    trade = bot.game.trade_create("42", "101", "102")
    bot.game.trade_offer(trade["id"], "101", [original["id"]])
    trade = bot.game.trade_offer(trade["id"], "102", [other["id"]])
    bot.game.trade_accept(trade["id"], "101", trade["version"])
    bot.game.trade_accept(trade["id"], "102", trade["version"])
    before = {user: bot.game.profile("42", user) for user in ("101", "102")}
    monkeypatch.setattr(bot, "refresh_drop", AsyncMock())

    event = interaction(101)
    await CaptureModal(bot, drop["id"]).on_submit(event)
    embed = event.followup.send.await_args.kwargs["embed"]
    assert embed.title == "Previously captured · Captain Rex"
    assert "No additional card or rewards" in embed.description
    fields = {field.name: field.value for field in embed.fields}
    assert original["id"] in fields["Capture receipt"]
    assert "Your copy" not in fields
    assert bot.game.info("42", "102", original["id"])["user_id"] == "102"
    assert {user: bot.game.profile("42", user) for user in before} == before
    assert len(bot.game.get_drop(drop["id"])["claims"]) == 1
    event.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)


@pytest.mark.asyncio
async def test_expired_capture_receipt_does_not_advertise_available_slots(bot, monkeypatch):
    started = time.time() - 400
    drop = bot.game.create_drop("42", "10", "captain-rex", now=started, duration=180)
    original = bot.game.claim(drop["id"], "101", "Captain Rex", now=started + 1)["instance"]
    before = bot.game.profile("42", "101")
    monkeypatch.setattr(bot, "refresh_drop", AsyncMock())
    event = interaction(101)
    await CaptureModal(bot, drop["id"]).on_submit(event)
    embed = event.followup.send.await_args.kwargs["embed"]
    assert embed.description.startswith("This encounter has ended.\n")
    assert "capture slots remain" not in embed.description
    assert original["id"] in next(field.value for field in embed.fields if field.name == "Capture receipt")
    assert bot.game.get_drop(drop["id"])["status"] == "expired"
    assert bot.game.profile("42", "101") == before


@pytest.mark.asyncio
@pytest.mark.parametrize(('card_id', 'expected_limit'), [('captain-rex', 2), ('anakin-skywalker', 1)])
async def test_capture_receipt_and_public_panel_use_stored_capacity(tmp_path, monkeypatch, card_id, expected_limit):
    root = Path(__file__).resolve().parents[1]
    document = json.loads((root / 'data/cards.json').read_text())
    document['cards'] = [card for card in document['cards'] if card['id'] == card_id]
    catalog = tmp_path / 'cards.json'
    catalog.write_text(json.dumps(document))
    bot = VaultBot(Settings(root, tmp_path / 'vault.sqlite3', catalog, None, 42))
    monkeypatch.setattr(bot, 'refresh_drop', AsyncMock())
    monkeypatch.setattr(bot, 'card_image', AsyncMock(return_value=None))
    drop = bot.game.create_drop('42', '10', card_id)
    assert drop['collector_limit'] == expected_limit
    initial = drop_panel(drop)
    slots = next(field.value for field in initial.fields if field.name == 'Capture slots')
    assert f'**0/{expected_limit}**' in slots
    assert ('One collector' if expected_limit == 1 else 'Up to 2 distinct collectors') in initial.description
    for index in range(expected_limit):
        modal = CaptureModal(bot, drop['id'])
        modal.answer._value = document['cards'][0]['name']
        event = interaction(101 + index)
        await modal.on_submit(event)
        receipt = event.followup.send.await_args.kwargs['embed']
        current = bot.game.get_drop(drop['id'])
        assert current['collector_limit'] == expected_limit
        remaining = expected_limit-index-1
        if remaining:
            assert receipt.description.startswith(f'{remaining} of {expected_limit} capture slots remain.')
        else:
            assert receipt.description.startswith('This encounter has ended.')
        public = drop_panel(current)
        slots = next(field.value for field in public.fields if field.name == 'Capture slots')
        assert f'**{index+1}/{expected_limit}**' in slots
    assert all(button.disabled for button in drop_view(current).children)


@pytest.mark.asyncio
async def test_profile_exposes_distinct_subject_progress_separately_from_event_cards(bot):
    seed_card(bot, 102)
    command = bot.tree.get_command("vault").get_command("profile")
    event = interaction()
    await command.callback(event)
    event.response.defer.assert_awaited_once_with(ephemeral=True)
    body = event.followup.send.await_args.kwargs["embed"].description
    count = bot.game.profile("42", "102")["regular_subject_count"]
    assert f"**1 / {count}** regular subjects" in body
    assert "regular card treatments" in body and "event cards" in body


@pytest.mark.asyncio
async def test_deck_clear_recovers_changed_arena_without_losing_owned_card(bot):
    item = seed_card(bot, 102)
    bot.game.set_deck("42", "102", "ground", [item["id"]])
    bot.game.catalog[item["card_id"]]["arena"] = "fleet"
    command = bot.tree.get_command("vault").get_command("deck")
    event = interaction()
    await command.callback(event, "ground", clear=True)
    event.response.defer.assert_awaited_once_with(ephemeral=True)
    assert bot.game.deck("42", "102", "ground")["cards"] == []
    assert bot.game.info("42", "102", item["id"])["user_id"] == "102"
    assert "No cards selected" in event.followup.send.await_args.kwargs["embed"].description


@pytest.mark.asyncio
async def test_deck_clear_with_selection_is_rejected_without_changing_saved_deck(bot):
    item = seed_card(bot, 102)
    bot.game.set_deck("42", "102", "ground", [item["id"]])
    command = bot.tree.get_command("vault").get_command("deck")
    with pytest.raises(ValueError, match="either clear or"):
        await command.callback(interaction(), "ground", instance_ids=item["id"], clear=True)
    assert bot.game.deck("42", "102", "ground")["cards"][0]["id"] == item["id"]


@pytest.mark.asyncio
async def test_consumption_review_is_bound_to_owner_and_not_replayed(bot):
    seed_card(bot, 101)
    extra = seed_card(bot, 101)
    original = interaction(101)
    await review_consumption(bot, original, "salvage", extra["id"])
    assert "credits" in original.followup.send.await_args.kwargs["embed"].description
    key = next(iter(bot.pending_confirmations))
    with pytest.raises(ValueError, match="Only the collector"):
        await handle_component(bot, interaction(102), "confirm", key)
    assert bot.game.info("42", "101", extra["id"])
    await handle_component(bot, interaction(101), "confirm", key)
    assert len(bot.game.inventory("42", "101")) == 1
    with pytest.raises(ValueError, match="expired"):
        await handle_component(bot, interaction(101), "confirm", key)


@pytest.mark.asyncio
async def test_foreign_guild_component_cannot_accept_trade(bot):
    record = bot.game.trade_create("42", "101", "102")
    with pytest.raises(ValueError, match="another server"):
        await handle_component(bot, interaction(102, 99), "accept0", record["id"])
    assert bot.game.get_trade(record["id"])["status"] == "open"


@pytest.mark.asyncio
async def test_stale_trade_revision_never_accepts_changed_offer(bot):
    card = seed_card(bot, 101)
    record = bot.game.trade_create("42", "101", "102")
    bot.game.trade_offer(record["id"], "101", [card["id"]])
    with pytest.raises(GameError) as raised:
        await handle_component(bot, interaction(102), f"accept{record['version']}", record["id"])
    assert raised.value.code == "stale_trade"


@pytest.mark.asyncio
async def test_copy_autocomplete_handles_full_names_and_selected_copies(bot):
    rex = seed_card(bot, 101)
    cody = seed_card(bot, 101, "commander-cody")
    event = interaction(101)
    full_name = await copy_choices(bot, event, "Captain Rex")
    assert [choice.value for choice in full_name] == [rex["id"]]
    expanded = await copy_choices(bot, event, rex["id"] + " Commander Cody")
    assert [choice.value for choice in expanded] == [rex["id"] + " " + cody["id"]]


@pytest.mark.asyncio
async def test_offer_selector_updates_only_the_named_collectors_offer(bot):
    rex = seed_card(bot, 101)
    record = bot.game.trade_create("42", "101", "102")
    event = interaction(101)
    event.data = {"values": [rex["id"]]}
    await handle_component(bot, event, "offerpick", record["id"])
    trade = bot.game.get_trade(record["id"])
    assert [row["id"] for row in trade["offers"]["101"]] == [rex["id"]]
    assert trade["offers"]["102"] == []


@pytest.mark.asyncio
async def test_owned_card_defers_and_renders_only_public_card_fields(bot, monkeypatch):
    event = interaction(101)
    seed_card(bot, 101)
    card = bot.game.catalog["captain-rex"]
    card.update(private_inventory=["SECRET-COPY"], user_id="SECRET-OWNER", art_layout="published-window")

    def render(public):
        event.response.defer.assert_awaited_once_with(thinking=True)
        assert public["id"] == "captain-rex" and public["attack"] == card["attack"]
        assert public["art_layout"] == "published-window"
        assert "private_inventory" not in public and "user_id" not in public
        return b"COMPOSED-PUBLIC-CARD"

    monkeypatch.setattr(bot.card_renderer, "render", render)
    command = bot.tree.get_command("vault").get_command("card")
    await command.callback(command.binding, event, "captain-rex")
    delivered = event.followup.send.await_args.kwargs
    assert delivered["ephemeral"] is False
    assert delivered["embed"].image.url == "attachment://card.png"
    assert delivered["file"].fp.read() == b"COMPOSED-PUBLIC-CARD"
    delivered["file"].close()


@pytest.mark.asyncio
async def test_successful_capture_attaches_card_without_copy_or_owner_metadata(bot, monkeypatch):
    drop = bot.game.create_drop("42", "10", "captain-rex")
    payloads = []

    def render(public):
        payloads.append(public)
        return b"COMPOSED-PUBLIC-CARD"

    monkeypatch.setattr(bot.card_renderer, "render", render)
    monkeypatch.setattr(bot, "refresh_drop", AsyncMock())
    modal = CaptureModal(bot, drop["id"])
    modal.answer._value = "Captain Rex"
    first = interaction(101)
    await modal.on_submit(first)
    delivered = first.followup.send.await_args.kwargs
    assert delivered["embed"].title == "Captured · Captain Rex"
    assert delivered["embed"].image.url == "attachment://card.png"
    assert delivered["file"].filename == "card.png"
    delivered["file"].close()
    before = bot.game.profile("42", "101")
    second = interaction(101)
    await modal.on_submit(second)
    replay = second.followup.send.await_args.kwargs
    assert replay["embed"].title == "Previously captured · Captain Rex"
    replay["file"].close()
    assert bot.game.profile("42", "101") == before
    assert len(bot.game.get_drop(drop["id"])["claims"]) == 1
    assert payloads[0] == payloads[1]
    assert not {"user_id", "instance_id", "acquired_at", "favorite", "locked_by"} & payloads[0].keys()


@pytest.mark.asyncio
async def test_committed_capture_stays_successful_when_card_render_fails(bot, monkeypatch):
    drop = bot.game.create_drop("42", "10", "captain-rex")
    monkeypatch.setattr(bot.card_renderer, "render", Mock(side_effect=RuntimeError("render failure")))
    monkeypatch.setattr(bot, "refresh_drop", AsyncMock())
    event = interaction(101)
    modal = CaptureModal(bot, drop["id"])
    modal.answer._value = "Captain Rex"
    await modal.on_submit(event)
    delivered = event.followup.send.await_args.kwargs
    assert delivered["embed"].title == "Captured · Captain Rex"
    assert "file" not in delivered and not delivered["embed"].image.url
    assert len(bot.game.inventory("42", "101")) == 1
    assert len(bot.game.get_drop(drop["id"])["claims"]) == 1
    profile = bot.game.profile("42", "101")
    assert (profile["credits"], profile["xp"]) == (25, 10)


@pytest.mark.asyncio
async def test_active_drop_uses_blind_source_art_and_never_named_card_render(bot, monkeypatch):
    renderer = Mock(side_effect=AssertionError("Named card must not appear in active encounter"))
    monkeypatch.setattr(bot.card_renderer, "render", renderer)
    channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=321, edit=AsyncMock())))
    monkeypatch.setattr(bot, "get_channel", lambda _: channel)
    await bot.post_drop("42", "10", "captain-rex")
    delivered = channel.send.await_args.kwargs
    try:
        assert delivered["file"].filename == "encounter.png"
        assert Path(delivered["file"].fp.name).resolve() == (bot.settings.root / "assets/art/captain-rex.png").resolve()
        assert delivered["embed"].title == "A card appeared"
        assert "Captain Rex" not in str(delivered["embed"].to_dict())
        assert delivered["embed"].image.url == "attachment://encounter.png"
        renderer.assert_not_called()
    finally:
        delivered["file"].close()


@pytest.mark.asyncio
async def test_committed_craft_stays_successful_when_card_render_fails(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    document = json.loads((root / "data/cards.json").read_text())
    document["cards"] = [c for c in document["cards"] if c["id"] in ("captain-rex", "obi-wan-kenobi")]
    catalog = tmp_path / "cards.json"
    catalog.write_text(json.dumps(document))
    bot = VaultBot(Settings(root, tmp_path / "vault.sqlite3", catalog, None, 42))
    copies = [seed_card(bot, 101)["id"] for _ in range(3)]
    with bot.game._tx() as connection:
        bot.game._credit(connection, "42", "101", 345, "test fixture", "epic-craft-budget", time.time())
    monkeypatch.setattr(bot.card_renderer, "render", Mock(side_effect=OSError("image unavailable")))
    original = interaction(101)
    await review_consumption(bot, original, "craft", " ".join(copies), "obi-wan-kenobi")
    key = next(iter(bot.pending_confirmations))
    event = interaction(101)
    await handle_component(bot, event, "confirm", key)
    delivered = event.followup.send.await_args.kwargs
    assert delivered["embed"].title == "Obi-Wan Kenobi"
    assert "file" not in delivered and not delivered["embed"].image.url
    owned = bot.game.inventory("42", "101")
    assert len(owned) == 1 and owned[0]["card_id"] == "obi-wan-kenobi"
    balance = bot.game.profile("42", "101")["credits"]
    assert balance == 45
    with pytest.raises(ValueError, match="expired"):
        await handle_component(bot, interaction(101), "confirm", key)
    assert bot.game.profile("42", "101")["credits"] == balance
