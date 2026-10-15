"""Launch configuration and schedule boundaries without Discord access."""
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from venator_vault.bot import VaultBot
from venator_vault.config import Settings
from venator_vault.core import Game, GameError
from venator_vault.presentation import drop_panel, drop_view
from venator_vault.runtime import Runtime


@pytest.fixture
def settings(tmp_path, synthetic_asset_root):
    root = synthetic_asset_root
    document = json.loads((root / "data/cards.json").read_text())
    document["cards"] = [card for card in document["cards"] if card["id"] == "captain-rex"]
    catalog = tmp_path / "cards.json"
    catalog.write_text(json.dumps(document))
    return Settings(root, tmp_path / "vault.sqlite3", catalog, None, 42, spawn_interval=1800)


def interaction():
    return SimpleNamespace(guild_id=42, user=SimpleNamespace(id=101, bot=False),
                           guild=SimpleNamespace(me=object()),
                           response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True),
                           followup=SimpleNamespace(send=AsyncMock()))


def channel():
    permissions = SimpleNamespace(view_channel=True, send_messages=True, embed_links=True,
                                  attach_files=True, read_message_history=True)
    return SimpleNamespace(id=10, mention="<#10>", permissions_for=lambda _: permissions)


def test_environment_parses_disabled_default_and_explicit_release_keys(monkeypatch):
    for key in ("VAULT_ENABLED_RELEASES", "VAULT_SPAWN_INTERVAL", "VAULT_DROP_DURATION"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings.from_env()
    assert settings.enabled_releases == ()
    assert (settings.spawn_interval, settings.drop_duration) == (900, 180)
    monkeypatch.setenv("VAULT_ENABLED_RELEASES", " autumn-2026, winter-2026, ")
    assert Settings.from_env().enabled_releases == ("autumn-2026", "winter-2026")


def test_environment_rejects_window_longer_than_interval(monkeypatch):
    monkeypatch.setenv("VAULT_SPAWN_INTERVAL", "300")
    monkeypatch.setenv("VAULT_DROP_DURATION", "301")
    with pytest.raises(ValueError, match="cannot exceed"):
        Settings.from_env()
    monkeypatch.setenv("VAULT_DROP_DURATION", "300")
    assert Settings.from_env().drop_duration == 300


@pytest.mark.asyncio
async def test_setup_uses_settings_default_and_explicit_option(settings):
    bot = VaultBot(settings)
    command = bot.tree.get_command("vault-admin").get_command("setup")
    event = interaction()
    await command.callback(command.binding, event, channel())
    assert bot.runtime.get("42")["interval_seconds"] == 1800
    assert "randomly sample 24–40 minutes" in event.followup.send.await_args.kwargs["content"]
    original_due = bot.runtime.get("42")["next_drop"]
    await command.callback(command.binding, interaction(), channel(), 10)
    assert bot.runtime.get("42")["interval_seconds"] == 600
    assert bot.runtime.get("42")["next_drop"] == original_due


@pytest.mark.asyncio
async def test_setup_rejects_overlapping_window_without_mutating_schedule(settings):
    configured = Settings(**{**vars(settings), "drop_duration": 600})
    bot = VaultBot(configured)
    bot.runtime.configure("42", "10", 1800, "owner", now=100)
    before = bot.runtime.get("42")
    command = bot.tree.get_command("vault-admin").get_command("setup")
    with pytest.raises(ValueError, match="shorter than the capture window"):
        await command.callback(command.binding, interaction(), channel(), 5)
    assert bot.runtime.get("42") == before


def test_restart_rejects_saved_interval_shorter_than_new_capture_duration(settings):
    Runtime(settings.database.with_name("runtime.sqlite3")).configure("42", "10", 300, "owner", now=100)
    with pytest.raises(ValueError, match="saved encounter interval"):
        VaultBot(Settings(**{**vars(settings), "drop_duration": 600}))


@pytest.mark.asyncio
async def test_paused_scheduled_post_cannot_publish_or_reserve(settings, monkeypatch):
    bot = VaultBot(settings)
    bot.runtime.configure("42", "10", 900, "owner", now=100)
    bot.runtime.pause("42", "owner", True)
    send = AsyncMock()
    monkeypatch.setattr(bot, "get_channel", lambda _: SimpleNamespace(send=send))
    with pytest.raises(GameError, match="paused"):
        await bot.post_drop("42", "10", "captain-rex")
    send.assert_not_awaited()
    assert bot.game.active_drops() == []


@pytest.mark.asyncio
async def test_restart_after_downtime_posts_once_then_reanchors_future_schedule(settings, monkeypatch):
    import time

    now = time.time()
    runtime = Runtime(settings.database.with_name("runtime.sqlite3"))
    runtime.configure("42", "10", 900, "owner", now=now - 86400)
    for index in range(3):
        runtime.record_activity("42", str(index), 100+index, now=now)
    bot = VaultBot(settings)
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    bot.commands_synced = True
    posted = SimpleNamespace(id=321, edit=AsyncMock())

    async def send(**kwargs):
        kwargs["file"].close()
        return posted

    outgoing = AsyncMock(side_effect=send)
    monkeypatch.setattr(bot, "get_channel", lambda _: SimpleNamespace(send=outgoing))
    monkeypatch.setattr(bot, "refresh_drop", AsyncMock())
    monkeypatch.setattr(bot, "post_raid", AsyncMock())
    await bot.scheduler()
    outgoing.assert_awaited_once()
    published = bot.game.active_drops()[0]
    saved = runtime.get("42")
    assert saved["cycle_started_at"] == published["created_at"]
    assert published["created_at"]+720 <= saved["next_drop"] <= published["created_at"]+1200
    await bot.scheduler()
    outgoing.assert_awaited_once()
    assert len(bot.game.active_drops()) == 1
    assert runtime.get("42") == saved


@pytest.mark.asyncio
async def test_scheduled_encounter_cannot_accelerate_schedule_even_after_drop_fills(settings, monkeypatch):
    import time

    bot = VaultBot(settings)
    bot.runtime.configure("42", "10", 900, "owner", now=time.time()-1201)

    async def send(**kwargs):
        kwargs["file"].close()
        return SimpleNamespace(id=321, edit=AsyncMock(), jump_url="https://discord.com/channels/42/10/321")

    outgoing = AsyncMock(side_effect=send)
    monkeypatch.setattr(bot, "get_channel", lambda _: SimpleNamespace(send=outgoing))
    command = bot.tree.get_command("vault-admin").get_command("spawn")
    await command.callback(command.binding, interaction())
    first = bot.game.active_drops()[0]
    for user in ("a", "b"):
        bot.game.claim(first["id"], user, "Captain Rex")
    config = bot.runtime.get("42")
    with pytest.raises(GameError, match="due in") as error:
        await bot.post_drop('42', '10')
    assert error.value.code == "spawn_not_due"
    assert bot.runtime.get("42") == config
    outgoing.assert_awaited_once()
    with bot.runtime.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE action='manual_spawn'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_stale_due_snapshot_does_not_shorten_the_new_schedule(settings, monkeypatch):
    import time

    bot = VaultBot(settings)
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    bot.commands_synced = True
    stale = bot.runtime.configure("42", "10", 900, "owner", now=time.time()-1201)
    bot.runtime.advance("42")
    next_due = bot.runtime.get("42")["next_drop"]
    monkeypatch.setattr(bot.runtime, "due", lambda: [stale])
    outgoing = AsyncMock()
    monkeypatch.setattr(bot, "get_channel", lambda _: SimpleNamespace(send=outgoing))
    await bot.scheduler()
    assert bot.runtime.get("42")["next_drop"] == next_due
    outgoing.assert_not_awaited()
    assert bot.game.active_drops() == []


@pytest.mark.asyncio
async def test_retry_reuses_stable_nonce_within_existing_pending_drop(settings, monkeypatch):
    bot = VaultBot(settings)
    nonces = []

    async def send(**kwargs):
        kwargs["file"].close()
        nonces.append(kwargs["nonce"])
        if len(nonces) == 1:
            raise OSError("Simulated loss of a successful remote response")
        return SimpleNamespace(id=321, edit=AsyncMock())

    monkeypatch.setattr(bot, "get_channel", lambda _: SimpleNamespace(send=send))
    with pytest.raises(OSError):
        await bot.post_drop("42", "10", "captain-rex")
    pending = bot.game.active_drops()[0]
    await bot.post_drop("42", "10")
    assert nonces == ["vv:"+pending["id"]] * 2
    assert len(bot.game.active_drops()) == 1
    assert bot.game.get_drop(pending["id"])["message_id"] == "321"


def publication_game(tmp_path, *, seasonal=False, enabled=True):
    card = {"id": "test-card", "name": "Test Card", "aliases": [], "rarity": "rare", "category": "canon",
            "role": "striker", "arena": "ground", "faction": "Republic", "attack": 100, "health": 400,
            "speed": 70, "weight": 1, "release": None}
    if seasonal:
        card.update(category="limited", release_id="test-season",
                    release={"starts_at": 2_000_000_000-100, "ends_at": 2_000_000_000+90, "supply_cap": 3})
    path = tmp_path / "publication.json"
    path.write_text(json.dumps({"schema_version": 1, "cards": [card]}))
    return Game(tmp_path / "publication.sqlite3", path,
                enabled_releases=("test-season",) if seasonal and enabled else ())


def test_pending_publication_blocks_claims_and_starts_full_window_once(tmp_path):
    game = publication_game(tmp_path)
    now = 2_000_000_000
    pending = game.create_drop("g", "c", now=now, defer_publication=True)
    assert pending["delivery_pending"] and pending["expires_at"] == now+120
    assert "Capture opens" in drop_panel(pending, now=now).description
    with pytest.raises(GameError, match="getting ready"):
        game.claim(pending["id"], "a", "Test Card", now=now+30)
    with pytest.raises(GameError, match="Publish this encounter"):
        game.attach_message(pending["id"], "m")
    published = game.publish_drop(pending["id"], "m", now=now+60)
    assert not published["delivery_pending"]
    assert (published["created_at"], published["expires_at"]) == (now+60, now+240)
    assert game.publish_drop(pending["id"], "m", now=now+90) == published
    with pytest.raises(GameError, match="already bound"):
        game.publish_drop(pending["id"], "other", now=now+91)
    for user in ("a", "b"):
        game.claim(pending["id"], user, "Test Card", now=now+100)
    with pytest.raises(GameError, match="full or expired"):
        game.claim(pending["id"], "c", "Test Card", now=now+101)


def test_pending_and_published_restart_keep_delivery_bounds(tmp_path):
    now = 2_000_000_000
    game = publication_game(tmp_path)
    pending = game.create_drop("g", "c", now=now, duration=300, defer_publication=True)
    restarted = publication_game(tmp_path)
    assert restarted.get_drop(pending["id"])["delivery_deadline"] == now+120
    published = restarted.publish_drop(pending["id"], "m", now=now+90)
    assert published["expires_at"] == now+390
    again = publication_game(tmp_path)
    assert again.publish_drop(pending["id"], "m", now=now+100) == published
    assert again.last_published_drop("g")["id"] == pending["id"]


@pytest.mark.parametrize("seasonal,disabled,at", [(False, False, 120), (True, False, 90), (True, True, 30)])
def test_failed_or_disabled_delivery_releases_reservation_once(tmp_path, seasonal, disabled, at):
    now = 2_000_000_000
    game = publication_game(tmp_path, seasonal=seasonal)
    pending = game.create_drop("g", "c", now=now, defer_publication=True)
    if disabled:
        game = publication_game(tmp_path, seasonal=True, enabled=False)
    with pytest.raises(GameError, match="could not be delivered"):
        game.publish_drop(pending["id"], "late-message", now=now+at)
    game.active_drops(now=now+at+1)
    with closing(sqlite3.connect(game.db_path)) as db:
        assert db.execute("SELECT minted,reserved FROM supply").fetchone() == (0, 0)
        assert db.execute("SELECT status,message_id FROM drops").fetchone() == ("expired", "late-message")
    with pytest.raises(GameError, match="full or expired"):
        game.claim(pending["id"], "a", "Test Card", now=now+at+2)


def test_successful_publication_never_extends_beyond_release_end(tmp_path):
    game = publication_game(tmp_path, seasonal=True)
    now = 2_000_000_000
    pending = game.create_drop("g", "c", now=now, defer_publication=True)
    published = game.publish_drop(pending["id"], "m", now=now+60)
    assert (published["created_at"], published["expires_at"]) == (now+60, now+90)


def test_schema2_migration_keeps_original_publication_clock_and_claims(tmp_path):
    game = publication_game(tmp_path)
    now = 2_000_000_000
    drop = game.create_drop("g", "c", now=now)
    game.attach_message(drop["id"], "m")
    copy_id = game.claim(drop["id"], "a", "Test Card", now=now+1)["instance"]["id"]
    with closing(sqlite3.connect(game.db_path)) as db:
        db.execute("DROP INDEX published_drop_time")
        db.execute("DROP TRIGGER published_capture")
        for name in ("delivery_pending", "delivery_deadline", "capture_duration"):
            db.execute(f"ALTER TABLE drops DROP COLUMN {name}")
        db.execute("PRAGMA user_version=2")
        db.commit()
    migrated = publication_game(tmp_path)
    stored = migrated.get_drop(drop["id"])
    assert migrated.health()["schema_version"] == 8
    assert (stored["created_at"], stored["expires_at"], stored["delivery_pending"]) == (now, now+180, 0)
    assert stored["claims"][0]["instance_id"] == copy_id
    assert migrated.info("g", "a", copy_id)["card_id"] == "test-card"


@pytest.mark.asyncio
async def test_delayed_send_retries_disabled_then_publishes_full_capture_window(settings, monkeypatch):
    import time

    clock = [2_000_000_000]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    bot = VaultBot(settings)
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    bot.commands_synced = True
    bot.runtime.configure("42", "10", 900, "owner", now=clock[0]-1201)
    for index in range(3):
        bot.runtime.record_activity("42", str(index), 100+index, now=clock[0])
    sent = []
    message = SimpleNamespace(id=321, edit=AsyncMock())

    async def send(**kwargs):
        kwargs["file"].close()
        sent.append(kwargs)
        assert all(button.disabled for button in kwargs["view"].children)
        if len(sent) == 1:
            raise OSError("Simulated send failure")
        return message

    monkeypatch.setattr(bot, "get_channel", lambda _: SimpleNamespace(send=send))
    monkeypatch.setattr(bot, "refresh_drop", AsyncMock())
    await bot.scheduler()
    pending = bot.game.active_drops()[0]
    assert pending["delivery_pending"]
    assert bot.runtime.get("42")["next_drop"] == clock[0]+60
    clock[0] += 60
    await bot.scheduler()
    published = bot.game.get_drop(pending["id"])
    assert published["expires_at"] == clock[0]+180
    assert sent[0]["nonce"] == sent[1]["nonce"]
    assert not any(button.disabled for button in message.edit.await_args.kwargs["view"].children)
    saved = bot.runtime.get("42")
    assert saved["cycle_started_at"] == published["created_at"] == clock[0]
    assert clock[0]+720 <= saved["next_drop"] <= clock[0]+1200
    assert not saved["retrying"]


@pytest.mark.asyncio
async def test_activation_failure_retries_same_nonce_and_drop(settings, monkeypatch):
    bot = VaultBot(settings)
    message = SimpleNamespace(id=321, edit=AsyncMock())
    nonces = []

    async def send(**kwargs):
        kwargs["file"].close()
        nonces.append(kwargs["nonce"])
        return message

    monkeypatch.setattr(bot, "get_channel", lambda _: SimpleNamespace(send=send))
    original = bot.game.publish_drop
    calls = []

    def publish(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise sqlite3.OperationalError("Simulated storage failure after HTTP acceptance")
        return original(*args, **kwargs)

    monkeypatch.setattr(bot.game, "publish_drop", publish)
    with pytest.raises(sqlite3.OperationalError):
        await bot.post_drop("42", "10", "captain-rex")
    pending = bot.game.active_drops()[0]
    assert pending["delivery_pending"] and pending["message_id"] is None
    await bot.post_drop("42", "10")
    assert len(bot.game.active_drops()) == 1
    assert nonces == ["vv:"+pending["id"]] * 2
    message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_edit_failure_after_publication_recovers_without_resetting_clock(settings, monkeypatch):
    from venator_vault.features import reconcile_raids

    bot = VaultBot(settings)
    message = SimpleNamespace(id=321, edit=AsyncMock(side_effect=[OSError("Simulated edit failure"), None]))

    async def send(**kwargs):
        kwargs["file"].close()
        return message

    monkeypatch.setattr(bot, "get_channel", lambda _: SimpleNamespace(send=send, get_partial_message=lambda _: message))
    await bot.post_drop("42", "10", "captain-rex")
    published = bot.game.active_drops()[0]
    assert not published["delivery_pending"] and published["message_id"] == "321"
    await reconcile_raids(bot)
    assert message.edit.await_count == 2
    assert bot.game.get_drop(published["id"])["expires_at"] == published["expires_at"]
    assert not any(button.disabled for button in message.edit.await_args.kwargs["view"].children)


@pytest.mark.asyncio
async def test_committed_publication_recovers_cadence_after_schedule_write_failure(settings, monkeypatch):
    import time

    bot = VaultBot(settings)
    bot.runtime.configure("42", "10", 900, "owner", now=time.time()-1201)
    for index in range(3):
        bot.runtime.record_activity("42", str(index), 100+index)

    async def send(**kwargs):
        kwargs["file"].close()
        return SimpleNamespace(id=321, edit=AsyncMock())

    monkeypatch.setattr(bot, "get_channel", lambda _: SimpleNamespace(send=send))
    monkeypatch.setattr(bot.runtime, "advance", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("schedule failed")))
    with pytest.raises(OSError):
        await bot.post_drop("42", "10", "captain-rex")
    published = bot.game.active_drops()[0]
    for user in ("a", "b"):
        bot.game.claim(published["id"], user, "Captain Rex")
    restarted = VaultBot(settings)
    outgoing = AsyncMock()
    monkeypatch.setattr(restarted, "get_channel", lambda _: SimpleNamespace(send=outgoing))
    with pytest.raises(GameError, match="due in"):
        await restarted.post_drop("42", "10", "captain-rex")
    outgoing.assert_not_awaited()
    saved = restarted.runtime.get("42")
    assert saved["cycle_started_at"] == published["created_at"]
    assert published["created_at"]+720 <= saved["next_drop"] <= published["created_at"]+1200
    assert restarted.game.get_drop(published["id"])["expires_at"] == published["expires_at"]
    with pytest.raises(GameError, match="due in"):
        await restarted.post_drop("42", "10", "captain-rex")
    assert restarted.runtime.get("42") == saved
    outgoing.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_hint_reveals_no_answer_or_faction(settings, monkeypatch):
    import discord

    bot = VaultBot(settings)
    pending = bot.game.create_drop("42", "10", "captain-rex", defer_publication=True)
    assert all(button.disabled for button in drop_view(pending).children)
    event = interaction()
    event.type = discord.InteractionType.component
    event.data = {"custom_id": "vv:hint:"+pending["id"]}
    await bot.on_interaction(event)
    event.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    content = event.followup.send.await_args.kwargs["content"]
    assert "getting ready" in content and "Rex" not in content and "Republic" not in content


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["fetch", "send"])
async def test_publication_http_calls_cannot_outlive_delivery_deadline(settings, monkeypatch, stage):
    import asyncio

    from venator_vault import core

    monkeypatch.setattr(core, "DELIVERY_TIMEOUT", 0.25)
    bot = VaultBot(settings)
    cancelled = []

    async def stall(*_args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            if "file" in kwargs:
                kwargs["file"].close()
            cancelled.append(stage)

    if stage == "fetch":
        monkeypatch.setattr(bot, "get_channel", lambda _: None)
        monkeypatch.setattr(bot, "fetch_channel", stall)
    else:
        monkeypatch.setattr(bot, "get_channel", lambda _: SimpleNamespace(send=stall))
    with pytest.raises(TimeoutError):
        await bot.post_drop("42", "10", "captain-rex")
    assert cancelled == [stage]
    assert bot.game.active_drops() == []
    with closing(sqlite3.connect(bot.game.db_path)) as db:
        assert db.execute("SELECT minted,reserved FROM supply").fetchone() == (0, 0)
        assert db.execute("SELECT status,delivery_pending,message_id FROM drops").fetchone() == ("expired", 1, None)
