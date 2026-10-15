"""Restart and transport fault injection against scratch databases only."""
import asyncio
import itertools
import json
import socket
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from venator_vault import bot as gateway
from venator_vault import features, presentation
from venator_vault.config import Settings
from venator_vault.core import GameError

EVENT_IDS = itertools.count(1000)


@pytest.fixture
def factory(tmp_path, synthetic_asset_root):
    root = synthetic_asset_root
    doc = json.loads((root / "data/cards.json").read_text())
    doc["cards"] = [c for c in doc["cards"] if c["id"] in ("captain-rex", "commander-cody", "obi-wan-kenobi")]
    catalog = tmp_path / "cards.json"
    catalog.write_text(json.dumps(doc))
    settings = Settings(root, tmp_path / "game.sqlite3", catalog, None, 42)

    def create():
        bot = gateway.VaultBot(settings)
        bot.card_image = AsyncMock(return_value=None)
        return bot
    return create


@pytest.fixture
def bot(factory):
    return factory()


def own(bot, user="101", count=1):
    with bot.game._tx() as connection:
        return [bot.game._mint(connection, "42", user, "captain-rex", time.time(), "test fixture") for _ in range(count)]


def interaction(user=101, *, custom_id="", field="card-name", value="Captain Rex"):
    return SimpleNamespace(id=next(EVENT_IDS), type=discord.InteractionType.modal_submit,
        guild_id=42, user=SimpleNamespace(id=user, bot=False),
        data={"custom_id": custom_id, "components": [{"components": [{"custom_id": field, "value": value}]}]},
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock(), is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock()))


async def never_finishes(*args, **kwargs):
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_initial_sync_timeout_does_not_prevent_gateway_setup(bot, monkeypatch):
    monkeypatch.setattr(presentation, "DISCORD_IO_TIMEOUT", .01)
    monkeypatch.setattr(bot.tree, "sync", never_finishes)
    start = Mock()
    monkeypatch.setattr(bot.scheduler, "start", start)
    await asyncio.wait_for(bot.setup_hook(), .2)
    start.assert_called_once()
    assert not bot.commands_synced and bot.next_command_sync > time.monotonic()
    monkeypatch.setattr(bot.tree, "sync", AsyncMock(return_value=[]))
    assert await bot.sync_commands()
    assert bot.commands_synced


@pytest.mark.asyncio
async def test_service_ready_and_heartbeat_precede_network_login_and_shutdown_is_clean(bot, monkeypatch):
    notices = []
    monkeypatch.setattr(gateway, "notify_systemd", notices.append)
    monkeypatch.setattr(bot, "login", never_finishes)
    running = asyncio.create_task(bot.start("synthetic-login-fixture"))
    await asyncio.sleep(.02)
    assert any("READY=1" in notice for notice in notices)
    assert "WATCHDOG=1" in notices
    health = json.loads((bot.settings.database.parent / "health.json").read_text())
    assert not health["ready"] and health["lifecycle"] == "reconnecting"
    bot.request_shutdown()
    await asyncio.wait_for(running, .5)
    health = json.loads((bot.settings.database.parent / "health.json").read_text())
    assert health["lifecycle"] == "stopped" and not health["ready"]
    assert any("STOPPING=1" in notice for notice in notices)


@pytest.mark.asyncio
async def test_transient_login_failure_retries_without_recreating_state(bot, monkeypatch):
    item = own(bot)[0]
    monkeypatch.setattr(gateway, "LOGIN_RETRY_DELAY", .001)
    monkeypatch.setattr(gateway, "notify_systemd", lambda _: None)
    login = AsyncMock(side_effect=[OSError("synthetic outage"), None])
    connect = AsyncMock()
    monkeypatch.setattr(bot, "login", login)
    monkeypatch.setattr(bot, "connect", connect)
    await bot.start("synthetic-login-fixture")
    assert login.await_count == 2
    connect.assert_awaited_once()
    assert bot.game.info("42", "101", item["id"])["card_id"] == "captain-rex"


def test_sd_notify_uses_local_socket_without_a_network_dependency(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = "notify.sock"  # macOS temp roots can exceed AF_UNIX's pathname limit.
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(path)
        server.settimeout(.2)
        gateway.notify_systemd("READY=1\nWATCHDOG=1")
        assert server.recv(200) == b"READY=1\nWATCHDOG=1"


@pytest.mark.asyncio
async def test_stalled_scheduler_stops_watchdog_kicks(bot, monkeypatch):
    notices = []
    monkeypatch.setenv("WATCHDOG_USEC", "30000")
    monkeypatch.setattr(gateway, "SCHEDULER_STALE_AFTER", .05)
    monkeypatch.setattr(gateway, "notify_systemd", notices.append)
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    bot.commands_synced = True
    bot._scheduler_started = True
    monkeypatch.setattr(bot, "call", never_finishes)
    scheduler = asyncio.create_task(bot.scheduler())
    heartbeat = asyncio.create_task(bot.service_heartbeat())
    try:
        await asyncio.sleep(.23)
        assert notices.count("WATCHDOG=1") == 1
        assert any("Scheduler stalled" in value for value in notices)
        assert not bot.watchdog_healthy()
    finally:
        scheduler.cancel()
        heartbeat.cancel()
        await asyncio.gather(scheduler, heartbeat, return_exceptions=True)


@pytest.mark.asyncio
async def test_disconnected_scheduler_ticks_keep_watchdog_alive_without_network_io(bot, monkeypatch):
    notices = []
    monkeypatch.setenv("WATCHDOG_USEC", "30000")
    monkeypatch.setattr(gateway, "SCHEDULER_STALE_AFTER", .08)
    monkeypatch.setattr(gateway, "notify_systemd", notices.append)
    monkeypatch.setattr(bot, "is_ready", lambda: False)
    bot._scheduler_started = True
    bot.call = AsyncMock(side_effect=AssertionError("No storage/network operation while disconnected"))
    heartbeat = asyncio.create_task(bot.service_heartbeat())
    try:
        for _ in range(12):
            await bot.scheduler()
            await asyncio.sleep(.02)
        assert notices.count("WATCHDOG=1") >= 2
        assert bot.watchdog_healthy()
        bot.call.assert_not_awaited()
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


@pytest.mark.asyncio
async def test_published_drop_survives_hung_final_edit_without_timer_reset(bot, factory, monkeypatch):
    monkeypatch.setattr(presentation, "DISCORD_IO_TIMEOUT", .01)
    message = SimpleNamespace(id=901, edit=never_finishes)
    channel = SimpleNamespace(send=AsyncMock(return_value=message))
    monkeypatch.setattr(bot, "get_channel", lambda _: channel)
    await asyncio.wait_for(bot.post_drop("42", "10", "captain-rex"), .3)
    drop = bot.game.recent_drops("42")[0]
    assert drop["message_id"] == "901" and not drop["delivery_pending"]
    assert not bot.post_lock.locked()
    restored = factory().game.get_drop(drop["id"])
    assert restored["created_at"] == drop["created_at"]
    assert restored["expires_at"] == drop["expires_at"]
    assert not restored["delivery_pending"]


@pytest.mark.asyncio
async def test_pending_delivery_restart_keeps_same_drop_and_reuses_nonce(bot, factory, monkeypatch):
    pending = bot.game.create_drop("42", "10", "captain-rex", defer_publication=True)
    restored = factory()
    message = SimpleNamespace(id=902, edit=AsyncMock())
    channel = SimpleNamespace(send=AsyncMock(return_value=message))
    monkeypatch.setattr(restored, "get_channel", lambda _: channel)
    await restored.post_drop("42", "10")
    assert channel.send.await_args.kwargs["nonce"] == "vv:" + pending["id"]
    rows = restored.game.recent_drops("42")
    assert len(rows) == 1 and rows[0]["id"] == pending["id"]
    assert rows[0]["expires_at"] - rows[0]["created_at"] == restored.settings.drop_duration


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "transport_failure", "timeout", "cancellation"])
async def test_drop_upload_file_closes_on_every_delivery_exit(bot, monkeypatch, outcome):
    monkeypatch.setattr(presentation, "DISCORD_IO_TIMEOUT", .05)
    uploads = []
    entered = asyncio.Event()

    async def send(**kwargs):
        uploads.append(kwargs["file"])
        assert not uploads[-1].fp.closed
        entered.set()
        if outcome == "success":
            return SimpleNamespace(id=901, edit=AsyncMock())
        if outcome == "transport_failure":
            raise OSError("synthetic outage")
        await never_finishes()

    monkeypatch.setattr(bot, "get_channel", lambda _: SimpleNamespace(send=send))
    task = asyncio.create_task(bot.post_drop("42", "10", "captain-rex"))
    await asyncio.wait_for(entered.wait(), .5)
    if outcome == "cancellation":
        task.cancel()
    if outcome == "success":
        await task
    else:
        expected = asyncio.CancelledError if outcome == "cancellation" else (TimeoutError if outcome == "timeout" else OSError)
        with pytest.raises(expected):
            await task
    assert len(uploads) == 1 and uploads[0].fp.closed
    assert not bot.post_lock.locked()
    drop = bot.game.recent_drops("42")[0]
    assert bool(drop["delivery_pending"]) == (outcome != "success")


@pytest.mark.asyncio
async def test_recorded_raid_restores_cadence_after_schedule_write_crash(bot, factory, monkeypatch):
    clock = [2_000_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    bot.runtime.configure("42", "10", 900, "101", now=clock[0]-7201)
    message = SimpleNamespace(id=903)
    channel = SimpleNamespace(send=AsyncMock(return_value=message))
    monkeypatch.setattr(bot, "get_channel", lambda _: channel)
    monkeypatch.setattr(bot.runtime, "advance_raid", Mock(side_effect=OSError("synthetic schedule failure")))
    with pytest.raises(OSError, match="synthetic"):
        await bot.post_raid("42", "10")
    raid = bot.game.recent_raids("42")[0]
    assert raid["message_id"] == "903"
    assert channel.send.await_args.kwargs["nonce"] == "vv-raid:"+raid["id"]

    clock[0] += 601  # Signup and battle elapsed during the outage; no timer reset.
    restored = factory()
    channel.send.reset_mock()
    monkeypatch.setattr(restored, "get_channel", lambda _: channel)
    with pytest.raises(GameError) as error:
        await restored.post_raid("42", "10")
    assert error.value.code == "raid_not_due"
    assert restored.runtime.get("42")["next_raid"] == raid["created_at"]+7200
    saved = restored.game.get_raid(raid["id"])
    assert saved["status"] == "escaped"
    assert (saved["created_at"], saved["signup_ends_at"], saved["expires_at"]) == (
        raid["created_at"], raid["signup_ends_at"], raid["expires_at"])
    channel.send.assert_not_awaited()
    clock[0] = raid["created_at"]+7201
    await restored.post_raid("42", "10")
    channel.send.assert_awaited_once()
    assert len(restored.game.recent_raids("42")) == 2


@pytest.mark.asyncio
async def test_stale_raid_schedule_and_pause_cannot_accelerate_cadence(bot, monkeypatch):
    bot.commands_synced = True
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    stale = bot.runtime.configure("42", "10", 900, "101", now=time.time()-7201)
    bot.runtime.advance("42")
    bot.runtime.advance_raid("42")
    next_raid = bot.runtime.get("42")["next_raid"]
    monkeypatch.setattr(bot.runtime, "due_raids", lambda: [stale])
    channel = SimpleNamespace(send=AsyncMock())
    monkeypatch.setattr(bot, "get_channel", lambda _: channel)
    await bot.scheduler()
    assert bot.runtime.get("42")["next_raid"] == next_raid
    bot.runtime.pause("42", "101")
    with pytest.raises(GameError) as error:
        await bot.post_raid("42", "10")
    assert error.value.code == "raids_paused"
    channel.send.assert_not_awaited()
    assert bot.game.recent_raids("42") == []


@pytest.mark.asyncio
async def test_reconciliation_failure_retries_and_does_not_starve_trade_or_duel(bot, monkeypatch):
    for user in ("101", "102"):
        item = own(bot, user)[0]
        bot.game.set_deck("42", user, "ground", [item["id"]])
    raid = bot.game.raid_create("42", "10")
    bot.game.attach_raid_message(raid["id"], "1")
    trade = bot.game.trade_create("42", "101", "102")
    duel = bot.game.duel_create("42", "101", "102", "ground")
    bot.runtime.bind("trade", trade["id"], "10", "2")
    bot.runtime.bind("duel", duel["id"], "10", "3")
    edits = {1: AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=500, reason="fixture"), "fixture")),
             2: AsyncMock(), 3: AsyncMock()}
    channel = SimpleNamespace(get_partial_message=lambda key: SimpleNamespace(edit=edits[key]))
    monkeypatch.setattr(bot, "get_channel", lambda _: channel)
    assert await features.reconcile_raids(bot)
    assert "raid:"+raid["id"] not in bot.display_versions
    assert "raid:"+raid["id"] in bot.display_failures
    edits[2].assert_awaited_once()
    edits[3].assert_awaited_once()
    edits[1].side_effect = None
    await features.reconcile_raids(bot)
    assert "raid:"+raid["id"] in bot.display_versions
    assert not bot.display_failures


@pytest.mark.asyncio
async def test_hung_raid_edit_is_bounded_and_other_restore_work_completes(bot, monkeypatch):
    monkeypatch.setattr(presentation, "DISCORD_IO_TIMEOUT", .01)
    raid = bot.game.raid_create("42", "10")
    bot.game.attach_raid_message(raid["id"], "1")
    channel = SimpleNamespace(get_partial_message=lambda _: SimpleNamespace(edit=never_finishes))
    monkeypatch.setattr(bot, "get_channel", lambda _: channel)
    await asyncio.wait_for(features.reconcile_raids(bot), .3)
    assert "raid:"+raid["id"] in bot.display_failures
    assert bot.watchdog_healthy()


@pytest.mark.asyncio
async def test_restore_batches_are_fair_and_complete_before_new_publication(bot, monkeypatch):
    order = []
    bot.commands_synced = True
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    bot.runtime.configure("42", "10", 900, "101", now=time.time()-1201)
    for index in range(3):
        bot.runtime.record_activity("42", str(index), 100+index)

    async def restore(_):
        order.append("restore")
        return order.count("restore") >= 2

    async def post(*_):
        order.append("publish")

    monkeypatch.setattr(features, "reconcile_raids", restore)
    monkeypatch.setattr(bot, "post_drop", post)
    await bot.scheduler()
    assert order == ["restore"] and bot.recovery_pending
    await bot.scheduler()
    assert order == ["restore", "restore", "publish"] and not bot.recovery_pending


@pytest.mark.asyncio
async def test_more_than_one_restore_batch_cannot_starve_later_messages(bot, monkeypatch):
    ids = []
    for number in range(10):
        drop = bot.game.create_drop("42", str(number), "captain-rex")
        bot.game.attach_message(drop["id"], str(100+number))
        ids.append(drop["id"])
    calls = []

    async def failed(drop):
        calls.append(drop["id"])
        return False

    monkeypatch.setattr(bot, "refresh_drop", failed)
    assert not await features.reconcile_raids(bot)
    assert len(calls) == features.RECONCILE_BATCH
    assert await features.reconcile_raids(bot)
    assert set(calls) == set(ids)


@pytest.mark.asyncio
async def test_failed_backlog_larger_than_cache_finishes_recovery_before_new_drops(bot, monkeypatch):
    ids = set()
    for number in range(160):
        trade = bot.game.trade_create("42", str(1000+number*2), str(1001+number*2), now=time.time()-1000)
        bot.runtime.bind("trade", trade["id"], "10", str(1000+number))
        ids.add(trade["id"])
    bot.runtime.configure("42", "10", 900, "101", now=time.time()-1201)
    for index in range(3):
        bot.runtime.record_activity("42", str(index), 100+index)
    bot.commands_synced = True
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    attempted = set()

    async def failed(trader, record):
        attempted.add(record["id"])
        return False

    async def publish(guild, channel):
        assert attempted == ids  # One bounded restoration attempt precedes new publication.
        bot.runtime.advance(guild)

    monkeypatch.setattr(features, "refresh_trade", failed)
    posted = AsyncMock(side_effect=publish)
    monkeypatch.setattr(bot, "post_drop", posted)
    for _ in range(25):
        await bot.scheduler()
    assert attempted == ids and not bot.recovery_pending
    posted.assert_awaited_once()
    assert len(bot.display_attempts) <= 150
    with bot.runtime.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM message_bindings WHERE active=1").fetchone()[0] == 160
        assert connection.execute("SELECT MIN(last_refresh) FROM message_bindings").fetchone()[0] >= bot.recovery_started_at
    # Reconnect requires a new pass even though all durable timestamps are old.
    await bot.on_resumed()
    attempted.clear()
    await bot.scheduler()
    assert bot.recovery_pending and len(attempted) == features.RECONCILE_BATCH
    for _ in range(24):
        await bot.scheduler()
    assert attempted == ids and not bot.recovery_pending
    posted.assert_awaited_once()


@pytest.mark.asyncio
async def test_saved_capture_modal_routes_after_restart_and_deduplicates_dispatch(bot, factory, monkeypatch):
    drop = bot.game.create_drop("42", "10", "captain-rex")
    first = bot.game.claim(drop["id"], "101", "Captain Rex")
    restored = factory()
    monkeypatch.setattr(restored, "refresh_drop", AsyncMock())
    event = interaction(custom_id=f"vv:capture-submit:{drop['id']}")
    await restored.on_interaction(event)
    modal = gateway.CaptureModal(restored, drop["id"])
    modal.answer._value = "Captain Rex"
    await modal.on_submit(event)  # Same event can also hit Discord's live Modal dispatcher.
    event.followup.send.assert_awaited_once()
    assert event.followup.send.await_args.kwargs["embed"].title.startswith("Previously captured")
    assert restored.game.profile("42", "101")["credits"] == 25
    assert restored.game.inventory("42", "101")[0]["id"] == first["instance"]["id"]
    assert restored.game.get_drop(drop["id"])["expires_at"] == drop["expires_at"]


@pytest.mark.asyncio
async def test_saved_offer_modal_routes_after_restart_with_ownership_checks(bot, factory):
    item = own(bot)[0]
    trade = bot.game.trade_create("42", "101", "102")
    restored = factory()
    event = interaction(custom_id=f"vv:offer-submit:{trade['id']}", field="copies", value=item["id"])
    await restored.on_interaction(event)
    record = restored.game.get_trade(trade["id"])
    assert record["offers"]["101"][0]["id"] == item["id"]
    outsider = interaction(103, custom_id=f"vv:offer-submit:{trade['id']}", field="copies", value=item["id"])
    await restored.on_interaction(outsider)
    assert "Only the two named" in outsider.followup.send.await_args.kwargs["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["craft", "salvage"])
async def test_private_consumption_review_expires_safely_after_restart(bot, factory, method):
    items = own(bot, count=3)
    ids = " ".join(item["id"] for item in items)
    await features.review_consumption(bot, interaction(), method, ids, "obi-wan-kenobi" if method=="craft" else None)
    key = next(iter(bot.pending_confirmations))
    restored = factory()
    with pytest.raises(ValueError, match="bot restarted"):
        await features.handle_component(restored, interaction(), "confirm", key)
    assert len(restored.game.inventory("42", "101")) == 3


@pytest.mark.asyncio
async def test_orphan_trade_recovers_for_both_parties_but_not_outsiders(bot, factory):
    item = own(bot)[0]
    trade = bot.game.trade_create("42", "101", "102")
    bot.game.trade_offer(trade["id"], "101", [item["id"]])
    restored = factory()
    command = restored.tree.get_command("vault").get_command("trade")
    for user in (101, 102):
        event = interaction(user)
        await command.callback(event)
        assert trade["id"] in event.followup.send.await_args.kwargs["embed"].footer.text
    outsider = interaction(103)
    await command.callback(outsider)
    assert "no open trade" in outsider.followup.send.await_args.kwargs["content"]
    assert restored.game.active_trades("different-guild", "101") == []
    with pytest.raises(ValueError, match="Only the named"):
        await command.callback(interaction(103), trade_id=trade["id"])
    assert restored.game.active_trades("42", "101", now=time.time()+3700) == []
    assert restored.game.info("42", "101", item["id"])["locked_by"] is None


@pytest.mark.asyncio
async def test_reconnect_invalidates_display_cache_without_resetting_persisted_timers(bot):
    drop = bot.game.create_drop("42", "10", "captain-rex")
    bot.display_versions["drop:"+drop["id"]] = ("active", 0)
    bot.display_attempts["drop:"+drop["id"]] = 10
    await bot.on_resumed()
    assert bot.recovery_pending and not bot.display_versions and not bot.display_attempts
    assert bot.game.get_drop(drop["id"])["expires_at"] == drop["expires_at"]
