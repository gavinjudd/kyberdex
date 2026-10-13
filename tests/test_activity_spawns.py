"""Metadata-only activity scheduling against isolated databases, never Discord."""
import asyncio
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from venator_vault.bot import ACTIVITY_ADMISSION_COOLDOWN, MAX_ACTIVITY_AUTHORS, CaptureModal, VaultBot
from venator_vault.config import Settings
from venator_vault.core import GameError
from venator_vault.runtime import Runtime, spawn_bounds


class BoundRng:
    def __init__(self, high=False):
        self.high = high
        self.calls = []

    def randint(self, lower, upper):
        self.calls.append((lower, upper))
        return upper if self.high else lower


class NoReroll:
    def randint(self, *_):
        raise AssertionError("A restart, replay, pause or configuration update must not reroll this cycle")


@pytest.fixture
def runtime(tmp_path):
    return Runtime(tmp_path / "runtime.sqlite3", rng=BoundRng())


@pytest.fixture
def bot(tmp_path, synthetic_asset_root):
    root = synthetic_asset_root
    document = json.loads((root / "data/cards.json").read_text())
    document["cards"] = [card for card in document["cards"] if card["id"] == "captain-rex"]
    catalog = tmp_path / "cards.json"
    catalog.write_text(json.dumps(document))
    return VaultBot(Settings(root, tmp_path / "vault.sqlite3", catalog, None, 42, activity_channels=(10,11)))


def seed_activity(runtime, now=1001, guild="42"):
    runtime.record_activity(guild, "2", 102, now=now)


@pytest.mark.parametrize("interval,floor,bounds", [(900,480,(720,1200)),(1800,480,(1440,2400)),
    (300,480,(480,480)),(900,900,(900,1200)),(86400,480,(69120,115200))])
def test_jitter_bounds_respect_absolute_and_capture_minimum(interval, floor, bounds):
    assert spawn_bounds(interval,floor) == bounds


@pytest.mark.parametrize("high,delay", [(False,720),(True,1200)])
def test_sampled_cycle_survives_restart_and_configuration_changes(tmp_path, high, delay):
    path = tmp_path / "runtime.sqlite3"
    runtime = Runtime(path, rng=BoundRng(high))
    original = runtime.configure("42","10",900,"owner",now=1000)
    assert original["next_drop"] == 1000+delay
    restored = Runtime(path,rng=NoReroll())
    assert restored.get("42") == original
    updated = restored.configure("42","11",1800,"owner",now=1100)
    assert updated["next_drop"] == original["next_drop"]
    assert updated["base_next_drop"] == original["base_next_drop"]
    assert updated["interval_seconds"] == 1800 and updated["channel_id"] == "11"
    restored.advance("42",now=1000)
    assert restored.get("42")["next_drop"] == original["next_drop"]
    restored.rng = BoundRng()
    restored.advance("42",now=2000)
    assert restored.get("42")["next_drop"] == 3440
    assert restored.rng.calls == [(1440,2400)]
    restored.advance("42",now=2000)
    assert len(restored.rng.calls) == 1


def test_one_recent_chatter_qualifies_and_repeated_messages_keep_small_speed_credit(runtime):
    runtime.configure("42","10",900,"owner",now=1000)
    assert not runtime.activity_ready("42",now=1000)
    assert runtime.record_activity("42","one",1,now=1001)["credited_seconds"] == 5
    assert runtime.activity_ready("42",now=1001)
    for message_id in range(2,31):
        assert runtime.record_activity("42","one",message_id,now=1000+message_id)["credited_seconds"] == 0
    assert runtime.get("42")["next_drop"] == 1715
    assert not runtime.activity_ready("42",now=1400)
    assert runtime.record_activity("42","one",31,now=1400)["credited_seconds"] == 5
    assert runtime.activity_ready("42",now=1400)


def test_quiet_overdue_cycle_waits_for_fresh_chat_without_reroll(runtime):
    saved = runtime.configure("42", "10", 900, "owner", now=1000)
    runtime.rng = NoReroll()
    for now in (saved["next_drop"], 3000, 86400):
        assert runtime.due(now=now) == []
    assert runtime.record_activity("42", "one", 1, now=86400)["reason"] == "awaiting_spawn"
    restored = Runtime(runtime.path, rng=NoReroll())
    assert restored.get("42") == saved
    assert restored.due(now=86400) == [saved]
    assert restored.record_activity("42", "one", 1, now=86800)["reason"] == "replayed_message"
    assert restored.due(now=86800) == []


def test_previous_cycle_and_future_presence_cannot_enable_a_spawn(runtime):
    runtime.configure("42", "10", 900, "owner", now=1000)
    seed_activity(runtime, now=1500)
    runtime.advance("42", now=1510)
    assert not runtime.activity_ready("42", now=1520)
    seed_activity(runtime, now=1600, guild="43")
    assert not runtime.activity_ready("42", now=1520)
    for index in range(3):
        runtime.record_activity("42", str(index), 200+index, now=3000)
    assert not runtime.activity_ready("42", now=2500)
    assert runtime.due(now=3000)
    assert not runtime.due(now=3301)


def test_presence_still_qualifies_when_speedup_is_exhausted(runtime):
    runtime.configure("42", "10", 900, "owner", now=1000)
    with ThreadPoolExecutor(max_workers=12) as workers:
        list(workers.map(lambda number: runtime.record_activity("42", str(number), 100+number, now=1100), range(30)))
    saved = runtime.get("42")
    assert saved["activity_discount_seconds"] == 60
    assert not runtime.due(now=1660)
    for index in range(3):
        assert runtime.record_activity("42", str(index), 200+index, now=1660)["credited_seconds"] == 0
    assert runtime.due(now=1660) == [saved]
    assert runtime.get("42") == saved


def test_small_configured_window_keeps_the_eight_minute_safety_floor(runtime):
    saved = runtime.configure("42", "10", 300, "owner", now=1000)
    seed_activity(runtime, now=1479)
    assert runtime.get("42")["next_drop"] == saved["minimum_drop_at"] == 1480
    assert not runtime.due(now=1479)
    assert runtime.due(now=1480)


def test_user_cooldown_cap_and_delayed_replay_survive_restart(runtime):
    runtime.configure("42","10",900,"owner",now=1000)
    seed_activity(runtime)
    assert runtime.record_activity("42","2",104,now=1120)["reason"] == "user_cooldown"
    assert runtime.record_activity("42","2",105,now=1121)["credited_seconds"] == 5
    restored = Runtime(runtime.path,rng=NoReroll())
    assert restored.record_activity("42","2",103,now=1241)["reason"] == "replayed_message"
    assert restored.record_activity("42","2",106,now=1241)["credited_seconds"] == 5
    restored.record_activity("42","0",107,now=1300)
    restored.record_activity("42","1",108,now=1300)
    assert restored.record_activity("42","2",109,now=1361)["credited_seconds"] == 5
    assert restored.record_activity("42","2",110,now=1481)["reason"] == "user_cycle_cap"
    with restored.connection() as connection:
        row=connection.execute("SELECT * FROM spawn_activity WHERE guild_id='42' AND user_id='2'").fetchone()
        assert row["cycle_credits"] == 4 and row["last_message_id"] == "110"


def test_concurrent_members_cannot_exceed_one_minute_cycle_discount(runtime):
    runtime.configure("42","10",900,"owner",now=1000)
    with ThreadPoolExecutor(max_workers=12) as workers:
        list(workers.map(lambda number:runtime.record_activity("42",str(number),1000+number,now=1100),range(30)))
    stored = runtime.get("42")
    assert stored["activity_discount_seconds"] == 60
    assert stored["next_drop"] == 1660
    assert stored["next_drop"] > stored["minimum_drop_at"] == 1480
    with runtime.connection() as connection:
        assert connection.execute("SELECT SUM(cycle_credits) FROM spawn_activity").fetchone()[0] == 12
    assert runtime.record_activity("42","new",2000,now=1101)["reason"] == "cycle_limit"
    assert runtime.get("42") == stored


def test_activity_never_retroactively_schedules_before_arriving_message(runtime):
    runtime.configure("42","10",900,"owner",now=1000)
    assert runtime.record_activity("42","a",1,now=1710)["credited_seconds"] == 5
    assert runtime.get("42")["next_drop"] == 1715
    assert runtime.record_activity("42","b",2,now=1713)["credited_seconds"] == 2
    assert runtime.get("42")["next_drop"] == 1713
    assert runtime.record_activity("42","c",3,now=1714)["reason"] == "awaiting_spawn"


def test_pause_and_resume_freeze_sample_and_cannot_fish_earlier_deadline(runtime):
    runtime.configure("42","10",900,"owner",now=1000)
    seed_activity(runtime)
    before = runtime.get("42")
    runtime.pause("42","owner",True,now=1100)
    runtime.rng = NoReroll()
    runtime.configure("42","11",600,"owner",now=1150)
    assert runtime.get("42")["paused"] == 1
    assert runtime.record_activity("42","another",300,now=1150)["reason"] == "inactive_cycle"
    runtime.pause("42","owner",True,now=1200)
    runtime.pause("42","owner",False,now=1400)
    after = runtime.get("42")
    for key in ("next_drop","cycle_started_at","base_next_drop","minimum_drop_at","next_raid"):
        assert after[key] == before[key]+300
    assert after["activity_discount_seconds"] == before["activity_discount_seconds"]
    runtime.pause("42","owner",False,now=1500)
    assert runtime.get("42") == after


def test_retries_keep_credit_history_and_accept_presence_without_speedup(runtime):
    runtime.configure("42","10",900,"owner",now=1000)
    seed_activity(runtime)
    before = runtime.get("42")
    runtime.rng = NoReroll()
    runtime.advance("42",retry=True,now=1701)
    after = runtime.get("42")
    assert after["next_drop"] == 1761 and after["retrying"] == 1
    assert after["base_next_drop"] == before["base_next_drop"]
    assert after["activity_discount_seconds"] == 5
    assert runtime.record_activity("42","extra",999,now=1710)["reason"] == "awaiting_spawn"
    assert runtime.get("42") == after
    assert runtime.activity_ready("42",now=1761)
    runtime.advance("42",retry=True,now=1690)
    assert runtime.get("42")["next_drop"] == 1761


def test_guilds_do_not_share_recent_members_or_discounts(runtime):
    for guild in ("42","43"):
        runtime.configure(guild,"10",900,"owner",now=1000)
    runtime.record_activity("42","a",1,now=1001)
    assert runtime.activity_ready("42",now=1001)
    assert not runtime.activity_ready("43",now=1001)
    assert runtime.get("43")["activity_discount_seconds"] == 0
    assert runtime.record_activity("43","c",3,now=1001)["credited_seconds"] == 5
    assert runtime.record_activity("42","c",4,now=1001)["credited_seconds"] == 5
    assert runtime.get("43")["activity_discount_seconds"] == 5
    assert runtime.record_activity("unconfigured","a",5,now=1002)["reason"] == "inactive_cycle"


def test_legacy_schema_migration_preserves_due_and_disables_legacy_cycle_acceleration(tmp_path):
    path=tmp_path/"legacy.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("""CREATE TABLE guild_config(guild_id TEXT PRIMARY KEY,channel_id TEXT NOT NULL,
            interval_seconds INTEGER NOT NULL,paused INTEGER NOT NULL DEFAULT 0,next_drop REAL NOT NULL,
            configured_by TEXT NOT NULL,updated_at REAL NOT NULL,next_raid REAL NOT NULL DEFAULT 0)""")
        connection.execute("INSERT INTO guild_config VALUES('42','10',900,0,1700,'owner',800,8000)")
        connection.commit()
    runtime=Runtime(path,rng=NoReroll())
    assert runtime.health()["schema_version"] == 2
    original=runtime.get("42")
    assert original["next_drop"] == original["base_next_drop"] == original["minimum_drop_at"] == 1700
    for number in range(8):
        assert runtime.record_activity("42",str(number),100+number,now=1000)["credited_seconds"] == 0
    assert runtime.get("42")["next_drop"] == 1700
    assert Runtime(path,rng=NoReroll()).get("42") == runtime.get("42")


def test_new_publication_resets_cycle_cap_but_keeps_replay_history_and_user_cooldown(runtime):
    runtime.configure("42","10",900,"owner",now=1000)
    seed_activity(runtime)
    runtime.advance("42",now=1010)
    with runtime.connection() as connection:
        row=connection.execute("SELECT * FROM spawn_activity WHERE guild_id='42' AND user_id='2'").fetchone()
        assert row["cycle_credits"] == 0 and row["last_credit_at"] == 1001
    assert runtime.record_activity("42","2",102,now=1011)["reason"] == "replayed_message"
    # Participants must chat again in the new cycle; prior-cycle traffic does not qualify.
    assert runtime.record_activity("42","2",103,now=1012)["reason"] == "user_cooldown"
    assert runtime.activity_ready("42",now=1012)
    runtime.record_activity("42","0",104,now=1013)
    runtime.record_activity("42","1",105,now=1014)
    assert runtime.record_activity("42","2",106,now=1015)["reason"] == "user_cooldown"
    assert runtime.record_activity("42","2",107,now=1121)["credited_seconds"] == 5


def test_clock_rollback_cannot_count_future_presence_or_accelerate_before_cycle_anchor(runtime):
    runtime.configure("42","10",900,"owner",now=1000)
    runtime.record_activity("42","a",1,now=1400)
    runtime.record_activity("42","b",2,now=1401)
    assert not runtime.activity_ready("42",now=1200)
    assert runtime.record_activity("42","c",3,now=1200)["credited_seconds"] == 5
    saved=runtime.get("42")
    assert runtime.record_activity("42","d",4,now=999)["reason"] == "inactive_cycle"
    assert runtime.get("42") == saved
    with runtime.connection() as connection:
        assert connection.execute("SELECT 1 FROM spawn_activity WHERE user_id='d'").fetchone() is None
    # One genuinely current chatter qualifies despite unrelated future rows.
    assert runtime.activity_ready("42",now=1200)
    assert runtime.record_activity("42","e",5,now=1201)["credited_seconds"] == 5
    assert runtime.record_activity("42","f",6,now=1202)["credited_seconds"] == 5


class MetadataMessage(SimpleNamespace):
    @property
    def content(self):
        raise AssertionError("Message content must never be accessed")


def message(now, user=1, message_id=100):
    return MetadataMessage(id=message_id,guild=SimpleNamespace(id=42,default_role=object()),
        channel=SimpleNamespace(id=10,permissions_for=lambda _:SimpleNamespace(view_channel=True)),
        author=SimpleNamespace(id=user,bot=False,created_at=datetime.fromtimestamp(now-10*86400,UTC)),
        webhook_id=None,type=discord.MessageType.default,interaction_metadata=None,
        created_at=datetime.fromtimestamp(now,UTC))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["bot","webhook","system","slash","other_guild","unlisted_channel","private_channel","new_account","old_message","direct_message"])
async def test_gateway_ignores_ineligible_message_metadata(bot, monkeypatch, kind):
    now=time.time();event=message(now)
    if kind=="bot":event.author.bot=True
    elif kind=="webhook":event.webhook_id=99
    elif kind=="system":event.type=discord.MessageType.pins_add
    elif kind=="slash":event.interaction_metadata=object()
    elif kind=="other_guild":event.guild.id=43
    elif kind=="unlisted_channel":event.channel.id=12
    elif kind=="private_channel":event.channel.permissions_for=lambda _:SimpleNamespace(view_channel=False)
    elif kind=="new_account":event.author.created_at=datetime.fromtimestamp(now-86400,UTC)
    elif kind=="old_message":event.created_at=datetime.fromtimestamp(now-61,UTC)
    elif kind=="direct_message":event.guild=None
    store=AsyncMock();monkeypatch.setattr(bot,"runtime_call",store)
    await bot.on_message(event)
    store.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_credits_only_metadata_and_retains_nonprivileged_intents(bot, monkeypatch):
    now=float(int(time.time()))
    monkeypatch.setattr("venator_vault.bot.time.time",lambda:now)
    bot.runtime.rng=BoundRng();bot.runtime.configure("42","10",900,"owner",now=now)
    assert bot.intents.guild_messages and not bot.intents.message_content and not bot.intents.members
    assert not bot.runtime.activity_ready("42",now=now)
    await bot.on_message(message(now))
    assert bot.runtime.activity_ready("42",now=now)
    assert bot.runtime.get("42")["activity_discount_seconds"] == 5


@pytest.mark.asyncio
async def test_busy_activity_is_dropped_without_queue_and_cancellation_does_not_free_worker_slot(bot,monkeypatch):
    entered=asyncio.Event();release=asyncio.Event()

    async def delayed(*_args,**_kwargs):
        entered.set()
        await release.wait()

    store=AsyncMock(side_effect=delayed);monkeypatch.setattr(bot,"runtime_call",store)
    now=time.time()
    first=asyncio.create_task(bot.on_message(message(now,1,1)))
    await asyncio.wait_for(entered.wait(),1)
    try:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(asyncio.gather(*(bot.on_message(message(now,index,index))
            for index in range(2,1002))),1)
        store.assert_awaited_once()
        assert not bot._activity_task.done()
        assert len(bot._activity_last_admitted) == 1
    finally:
        release.set()
        await asyncio.wait_for(bot._activity_task,1)
    await bot.on_message(message(time.time(),1003,1003))
    assert store.await_count == 2


@pytest.mark.asyncio
async def test_activity_admission_throttles_each_author_and_bounds_cache(bot,monkeypatch):
    store=AsyncMock();monkeypatch.setattr(bot,"runtime_call",store)
    # Datetime rounds to microseconds. Keep the admission fixture's wall clock
    # fixed so a just-created message cannot round into the future and be dropped.
    now=float(int(time.time()))
    monkeypatch.setattr("venator_vault.bot.time.time",lambda:now)
    await bot.on_message(message(now,1,1))
    await bot.on_message(message(now,1,2))
    store.assert_awaited_once()
    bot._activity_last_admitted[1] -= ACTIVITY_ADMISSION_COOLDOWN
    await bot.on_message(message(now,1,3))
    assert store.await_count == 2
    for index in range(2,MAX_ACTIVITY_AUTHORS+4):
        await bot.on_message(message(time.time(),index,index+100))
    assert len(bot._activity_last_admitted) == MAX_ACTIVITY_AUTHORS
    assert 1 not in bot._activity_last_admitted


@pytest.mark.asyncio
async def test_activity_worker_error_releases_admission_for_later_traffic(bot,monkeypatch):
    store=AsyncMock(side_effect=[OSError("storage unavailable"),None])
    monkeypatch.setattr(bot,"runtime_call",store)
    await bot.on_message(message(time.time(),1,1))
    await bot.on_message(message(time.time(),2,2))
    assert store.await_count == 2 and bot._activity_task.done()


@pytest.mark.asyncio
async def test_public_status_hides_exact_due_but_manager_can_inspect(bot):
    saved=bot.runtime.configure("42","10",900,"owner",now=time.time())
    command=bot.tree.get_command("vault").get_command("status")
    event=SimpleNamespace(guild_id=42,permissions=SimpleNamespace(manage_guild=False),
        response=SimpleNamespace(defer=AsyncMock(),is_done=lambda:True),followup=SimpleNamespace(send=AsyncMock()))
    await command.callback(command.binding,event)
    text=event.followup.send.await_args.kwargs["embed"].description
    assert "<t:" not in text and str(int(saved["next_drop"])) not in text
    assert "hidden, variable" in text and "at least one person" in text
    assert "waiting for recent chat" in text and "at most one minute" in text
    event.permissions.manage_guild=True
    await command.callback(command.binding,event)
    assert f"<t:{int(saved['next_drop'])}:R>" in event.followup.send.await_args.kwargs["embed"].description


@pytest.mark.asyncio
async def test_completed_publication_recovery_cannot_reroll_or_remove_activity_discount(bot, monkeypatch):
    now=time.time();bot.runtime.rng=BoundRng();bot.runtime.configure("42","10",900,"owner",now=now-1000)
    drop=bot.game.create_drop("42","10","captain-rex",now=now)
    bot.game.attach_message(drop["id"],"901")
    bot.runtime.advance("42",now=drop["created_at"])
    seed_activity(bot.runtime,now=now+1)
    saved=bot.runtime.get("42")
    bot.runtime.rng=NoReroll()
    with pytest.raises(GameError,match="due in"):
        await bot.post_drop("42","10")
    assert bot.runtime.get("42") == saved
    assert saved["activity_discount_seconds"] == 5
    restarted=VaultBot(bot.settings)
    assert restarted.runtime.get("42") == saved
    assert restarted.game.get_drop(drop["id"])["expires_at"] == drop["expires_at"]


@pytest.mark.asyncio
async def test_publication_before_schedule_commit_recovers_one_sample_and_original_capture_window(bot, monkeypatch):
    now=time.time();bot.runtime.rng=BoundRng()
    bot.runtime.configure("42","10",900,"owner",now=now-86400)
    pending=bot.game.create_drop("42","10","captain-rex",now=now-10,defer_publication=True)
    published=bot.game.publish_drop(pending["id"],"901",now=now)
    # Simulated crash between the durable publication and its separate schedule update.
    restarted=VaultBot(bot.settings);restarted.runtime.rng=BoundRng()
    outgoing=AsyncMock();monkeypatch.setattr(restarted,"get_channel",lambda _:SimpleNamespace(send=outgoing))
    with pytest.raises(GameError,match="due in"):
        await restarted.post_drop("42","10")
    saved=restarted.runtime.get("42")
    assert saved["next_drop"] == published["created_at"]+720
    assert restarted.runtime.rng.calls == [(720,1200)]
    restarted.runtime.rng=NoReroll()
    with pytest.raises(GameError,match="due in"):
        await restarted.post_drop("42","10")
    outgoing.assert_not_awaited()
    assert restarted.game.get_drop(pending["id"])["expires_at"] == published["expires_at"]
    assert restarted.runtime.get("42") == saved


@pytest.mark.asyncio
async def test_downtime_posts_once_and_samples_from_actual_publication_without_catchup(bot, monkeypatch):
    now=time.time();bot.runtime.rng=BoundRng(high=True)
    bot.runtime.configure("42","10",900,"owner",now=now-86400)
    restarted=VaultBot(bot.settings);restarted.runtime.rng=BoundRng()
    monkeypatch.setattr(restarted,"is_ready",lambda:True)
    restarted.commands_synced=True
    outgoing=AsyncMock(return_value=SimpleNamespace(id=901,edit=AsyncMock()))
    monkeypatch.setattr(restarted,"get_channel",lambda _:SimpleNamespace(send=outgoing))
    monkeypatch.setattr(restarted,"refresh_drop",AsyncMock(return_value=True))
    monkeypatch.setattr(restarted,"post_raid",AsyncMock())
    seed_activity(restarted.runtime, now=now)
    await restarted.scheduler()
    outgoing.assert_awaited_once()
    published=restarted.game.active_drops()[0]
    assert restarted.runtime.get("42")["next_drop"] == published["created_at"]+720
    assert restarted.runtime.rng.calls == [(720,1200)]
    await restarted.scheduler()
    outgoing.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_never_publishes_without_chat_and_silence_is_not_an_error(bot, monkeypatch):
    now = time.time()
    bot.runtime.configure("42", "10", 900, "owner", now=now-86400)
    saved = bot.runtime.get("42")
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    bot.commands_synced = True
    monkeypatch.setattr(bot, "post_raid", AsyncMock())
    outgoing = AsyncMock()
    monkeypatch.setattr(bot, "get_channel", lambda _: SimpleNamespace(send=outgoing))
    await bot.scheduler()
    await bot.scheduler()
    outgoing.assert_not_awaited()
    assert bot.runtime.get("42") == saved
    assert bot.game.active_drops("42") == []
    assert bot.last_error is None


@pytest.mark.asyncio
async def test_publication_rechecks_chat_when_due_snapshot_has_gone_stale(bot, monkeypatch):
    now = time.time()
    monkeypatch.setattr("venator_vault.bot.time.time", lambda: now)
    bot.runtime.configure("42", "10", 900, "owner", now=now-86400)
    seed_activity(bot.runtime, now=now)
    assert bot.runtime.due(now=now)
    now += 301
    outgoing = AsyncMock()
    monkeypatch.setattr(bot, "get_channel", lambda _: SimpleNamespace(send=outgoing))
    with pytest.raises(GameError) as error:
        await bot.post_drop("42", "10")
    assert error.value.code == "activity_required"
    outgoing.assert_not_awaited()
    assert bot.game.active_drops("42") == []


@pytest.mark.asyncio
async def test_chat_is_rechecked_after_slow_channel_lookup_before_send(bot, monkeypatch):
    now = time.time()
    monkeypatch.setattr("venator_vault.bot.time.time", lambda: now)
    bot.runtime.configure("42", "10", 900, "owner", now=now-86400)
    seed_activity(bot.runtime, now=now)
    outgoing = AsyncMock()

    async def fetch(_):
        nonlocal now
        now += 301
        return SimpleNamespace(send=outgoing)

    monkeypatch.setattr(bot, "get_channel", lambda _: None)
    monkeypatch.setattr(bot, "fetch_channel", fetch)
    with pytest.raises(GameError) as error:
        await bot.post_drop("42", "10")
    assert error.value.code == "activity_required"
    outgoing.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_modal_rejects_bot_actor_before_reading_or_claiming_drop(bot, monkeypatch):
    event=SimpleNamespace(id=901,guild_id=42,user=SimpleNamespace(id=10,bot=True),
        response=SimpleNamespace(send_message=AsyncMock(),is_done=lambda:False))
    calls=AsyncMock();monkeypatch.setattr(bot,"call",calls)
    await CaptureModal(bot,"drop-not-read").on_submit(event)
    calls.assert_not_awaited()
    event.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_help_explains_consecutive_capture_rule_and_second_slots(bot):
    event=SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock(),is_done=lambda:False))
    command=bot.tree.get_command("vault").get_command("help")
    await command.callback(command.binding,event)
    embed=event.response.send_message.await_args.kwargs["embed"]
    fairness=next(field.value for field in embed.fields if field.name=="How drops work")
    assert "two consecutive captures in this server" in fairness
    assert "another member to capture successfully" in fairness and "second slot counts" in fairness


@pytest.mark.asyncio
async def test_pause_confirmation_waits_for_inflight_publication_and_blocks_later_posts(bot,monkeypatch):
    bot.runtime.configure("42","10",900,"owner",now=time.time()-1201)
    seed_activity(bot.runtime, now=time.time())
    entered=asyncio.Event();release=asyncio.Event();order=[]

    async def send(**_kwargs):
        entered.set()
        await release.wait()
        order.append("published")
        return SimpleNamespace(id=901,edit=AsyncMock())

    async def confirmed(**_kwargs):
        order.append("pause confirmed")

    monkeypatch.setattr(bot,"get_channel",lambda _:SimpleNamespace(send=send))
    event=SimpleNamespace(guild_id=42,user=SimpleNamespace(id=1,bot=False),
        response=SimpleNamespace(defer=AsyncMock(),is_done=lambda:True),
        followup=SimpleNamespace(send=AsyncMock(side_effect=confirmed)))
    command=bot.tree.get_command("vault-admin").get_command("pause")
    publication=asyncio.create_task(bot.post_drop("42","10","captain-rex"))
    await asyncio.wait_for(entered.wait(),1)
    pause=asyncio.create_task(command.callback(command.binding,event,True))
    try:
        await asyncio.sleep(0)
        event.followup.send.assert_not_awaited()
        assert not bot.runtime.get("42")["paused"]
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(publication,pause),2)
    assert order == ["published","pause confirmed"]
    assert bot.runtime.get("42")["paused"]
    with pytest.raises(GameError,match="paused"):
        await bot.post_drop("42","10")
    assert order == ["published","pause confirmed"]


def test_activity_channels_require_explicit_configuration(monkeypatch):
    monkeypatch.delenv("VAULT_ACTIVITY_CHANNEL_IDS",raising=False)
    monkeypatch.delenv("VAULT_GUILD_ID",raising=False)
    assert Settings.from_env().guild_id is None
    assert Settings.from_env().activity_channel_ids == ()
    monkeypatch.setenv("VAULT_GUILD_ID","42")
    assert Settings.from_env().activity_channel_ids == ()
    monkeypatch.setenv("VAULT_ACTIVITY_CHANNEL_IDS","10, 11,10")
    assert Settings.from_env().activity_channel_ids == (10,11)
    monkeypatch.setenv("VAULT_ACTIVITY_CHANNEL_IDS","")
    assert Settings.from_env().activity_channel_ids == ()
    monkeypatch.setenv("VAULT_ACTIVITY_CHANNEL_IDS","-1")
    with pytest.raises(ValueError):Settings.from_env()
