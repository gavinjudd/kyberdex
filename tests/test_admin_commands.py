"""Exercise manager controls without sending anything to a live Discord server."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from venator_vault.bot import VaultBot
from venator_vault.config import Settings
from venator_vault.core import GameError
from venator_vault.presentation import drop_panel

ROOT = Path(__file__).resolve().parents[1]


def interaction(identity=100, *, manager=True, guild=42, rarity=''):
    return SimpleNamespace(id=identity, guild_id=guild, user=SimpleNamespace(id=101, bot=False),
                           permissions=SimpleNamespace(manage_guild=manager),
                           namespace=SimpleNamespace(rarity=rarity),
                           response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True),
                           followup=SimpleNamespace(send=AsyncMock()))


@pytest.fixture
def harness(tmp_path, monkeypatch, synthetic_asset_root):
    import time
    clock = SimpleNamespace(now=1_791_518_400.)
    monkeypatch.setattr(time, 'time', lambda: clock.now)
    settings = Settings(synthetic_asset_root, tmp_path/'vault.sqlite3', synthetic_asset_root/'data/cards.json', None, 42,
                        enabled_releases=('autumn-2026', 'winter-2026', 'spring-2027', 'summer-2027'))
    bot = VaultBot(settings)
    bot.runtime.configure('42', '10', 900, 'owner')
    messages = []

    async def send(**kwargs):
        message = SimpleNamespace(id=1000+len(messages), edit=AsyncMock(), jump_url='https://discord.test/encounter')
        messages.append((message, kwargs))
        return message

    channel = SimpleNamespace(send=AsyncMock(side_effect=send),
                              get_partial_message=lambda identity: next(message for message, _ in messages if message.id == identity))
    monkeypatch.setattr(bot, 'get_channel', lambda _: channel)
    return bot, clock, channel, messages


@pytest.mark.asyncio
async def test_immediate_spawn_selects_mythic_before_due_and_replays_same_message(harness):
    bot, clock, channel, messages = harness
    assert bot.runtime.get('42')['next_drop'] > clock.now
    command = bot.tree.get_command('vault-admin').get_command('spawn')
    event = interaction()
    await command.callback(command.binding, event, 'executor', 'mythic')
    drop = bot.game.active_drops('42')[0]
    assert (drop['card_id'], drop['collector_limit'], drop['uniform']) == ('executor', 1, 0)
    assert bot.runtime.get('42')['next_drop'] > clock.now
    before = bot.game.health()
    await command.callback(command.binding, event, 'executor', 'mythic')
    assert bot.game.health() == before
    assert len(messages) == 1 and channel.send.await_count == 1
    with pytest.raises(GameError, match='different card'):
        await command.callback(command.binding, event, 'darth-vader', 'mythic')


@pytest.mark.asyncio
async def test_paused_force_does_not_shift_resume_or_become_recovery_anchor(harness):
    bot, clock, _, messages = harness
    clock.now += 100
    bot.runtime.pause('42', 'owner', True)
    before = bot.runtime.get('42')
    clock.now += 80
    await bot.post_drop('42', '10', 'captain-rex', force=True, request_id='paused-one')
    assert bot.runtime.get('42') == before
    assert bot.game.active_drops('42')[0]['schedule_anchor'] == 0
    assert bot.game.last_published_drop('42') is None
    clock.now += 181
    bot.runtime.pause('42', 'owner', False)
    resumed = bot.runtime.get('42')
    clock.now = resumed['next_drop']
    for index in range(3):
        bot.runtime.record_activity('42', str(index), 100+index, now=clock.now)
    await bot.post_drop('42', '10')
    assert len(messages) == 2


@pytest.mark.asyncio
async def test_force_preserves_open_capture_and_rejects_rarity_mismatch(harness):
    bot, _, _, messages = harness
    with pytest.raises(GameError, match='selected rarity'):
        await bot.post_drop('42', '10', 'executor', force=True, rarity='common')
    await bot.post_drop('42', '10', 'captain-rex', force=True)
    original = bot.game.active_drops('42')[0]
    with pytest.raises(GameError, match='already open'):
        await bot.post_drop('42', '10', 'executor', force=True)
    assert bot.game.active_drops('42')[0] == original and len(messages) == 1


@pytest.mark.asyncio
async def test_admin_permissions_and_autocomplete_filter_aliases_rarity_and_season(harness):
    bot, _, _, _ = harness
    group = bot.tree.get_command('vault-admin')
    assert group.default_permissions.manage_guild
    assert not await group.interaction_check(interaction(manager=False))
    assert await group.autocomplete_spawn_card(interaction(manager=False), 'vader') == []
    assert await group.autocomplete_spawn_card(interaction(guild=99), 'vader') == []
    await bot.admin_card_ids()
    rows = await group.autocomplete_spawn_card(interaction(rarity='mythic'), 'vader')
    assert 'darth-vader' in {row.value for row in rows}
    assert all(bot.game.catalog[row.value]['rarity'] == 'mythic' for row in rows)
    assert await group.autocomplete_spawn_card(interaction(), 'summer') == []
    bot.runtime.start_event('42', 'owner', 'active-hour')
    rows = await group.autocomplete_spawn_card(interaction(rarity='legendary'), 'revan')
    assert 'darth-revan-summer-2027' in {row.value for row in rows}
    assert all(len(row.name) <= 100 for row in rows)


@pytest.mark.asyncio
async def test_event_start_posts_uniform_and_never_extends_on_repeat(harness):
    bot, clock, _, messages = harness
    command = bot.tree.get_command('vault-admin').get_command('event')
    await command.callback(command.binding, interaction(), 'start')
    event = bot.runtime.event('42')
    assert event['ends_at'] == clock.now+3600
    drop = bot.game.active_drops('42')[0]
    assert drop['uniform'] == 1 and drop['override_until'] == event['ends_at']
    panel = drop_panel(drop)
    assert any(field.name == 'Special event' and 'Equal-chance hour' in field.value for field in panel.fields)
    assert drop['card']['name'] not in panel.description
    clock.now += 10
    await command.callback(command.binding, interaction(101), 'start')
    assert bot.runtime.event('42')['ends_at'] == event['ends_at'] and len(messages) == 1


@pytest.mark.asyncio
async def test_future_edition_requires_event_and_normal_odds_resume_at_exact_end(harness):
    bot, clock, _, _ = harness
    with pytest.raises(GameError, match='outside its release'):
        await bot.post_drop('42', '10', 'executor-summer-2027', force=True)
    event = bot.runtime.start_event('42', 'owner', 'hour')
    await bot.post_drop('42', '10', 'executor-summer-2027', force=True)
    drop = bot.game.active_drops('42')[0]
    assert drop['override_until'] == event['ends_at'] and drop['uniform'] == 0
    clock.now = event['ends_at']
    await bot.post_drop('42', '10', force=True)
    normal = bot.game.active_drops('42')[0]
    assert normal['override_until'] is None and normal['uniform'] == 0


@pytest.mark.asyncio
async def test_event_cannot_start_while_paused(harness):
    bot, _, _, messages = harness
    bot.runtime.pause('42', 'owner', True)
    before = bot.runtime.get('42')
    command = bot.tree.get_command('vault-admin').get_command('event')
    with pytest.raises(ValueError, match='paused'):
        await command.callback(command.binding, interaction(), 'start')
    assert bot.runtime.event('42') is None and bot.runtime.get('42') == before and not messages


@pytest.mark.asyncio
async def test_pre_event_publication_recovery_does_not_delay_first_event_draw(harness):
    bot, clock, _, messages = harness
    clock.now += 10
    # Simulate durable publication followed by failure before runtime advance.
    old = bot.game.create_drop('42', '10', 'captain-rex', defer_publication=True)
    bot.game.publish_drop(old['id'], '998')
    clock.now += 10
    command = bot.tree.get_command('vault-admin').get_command('event')
    await command.callback(command.binding, interaction(), 'start')
    clock.now += 171
    for index in range(3):
        bot.runtime.record_activity('42', str(index), 100+index, now=clock.now)
    await bot.post_drop('42', '10')
    assert len(messages) == 1 and bot.game.active_drops('42')[0]['uniform'] == 1


@pytest.mark.asyncio
async def test_event_waits_for_open_capture_with_prompt_saved_retry(harness):
    bot, clock, _, messages = harness
    await bot.post_drop('42', '10', 'captain-rex', force=True)
    command = bot.tree.get_command('vault-admin').get_command('event')
    await command.callback(command.binding, interaction(), 'start')
    assert bot.runtime.event('42')
    assert bot.runtime.get('42')['next_drop'] == clock.now+60
    assert len(messages) == 1
    clock.now += 181
    for index in range(3):
        bot.runtime.record_activity('42', str(index), 100+index, now=clock.now)
    await bot.post_drop('42', '10')
    assert len(messages) == 2 and bot.game.active_drops('42')[0]['uniform'] == 1


@pytest.mark.asyncio
async def test_stop_cancels_unpublished_stock_and_retains_published_capture(harness):
    bot, clock, _, _ = harness
    hour = bot.runtime.start_event('42', 'owner', 'hour')
    pending = bot.game.create_drop('42', '10', 'executor-summer-2027', defer_publication=True,
                                   override_until=hour['ends_at'], request_id='pending-event')
    command = bot.tree.get_command('vault-admin').get_command('event')
    await command.callback(command.binding, interaction(), 'stop')
    assert bot.game.get_drop(pending['id'])['status'] == 'expired'
    stock = next(row for row in bot.game.spawn_pool(now=clock.now, override_until=clock.now+100, uniform=True)
                 if row['id'] == 'executor-summer-2027')
    assert stock['remaining_supply'] == 32
    next_hour = bot.runtime.start_event('42', 'owner', 'next-hour')
    await bot.post_drop('42', '10', 'executor-summer-2027', force=True)
    published = bot.game.active_drops('42')[0]
    await command.callback(command.binding, interaction(102), 'stop')
    assert bot.game.get_drop(published['id'])['expires_at'] == published['expires_at']
    assert published['override_until'] == next_hour['ends_at']


@pytest.mark.asyncio
async def test_recovery_cancels_stopped_pending_even_if_stop_crashed_between_databases(harness):
    bot, _, _, messages = harness
    hour = bot.runtime.start_event('42', 'owner', 'hour')
    pending = bot.game.create_drop('42', '10', 'executor-summer-2027', defer_publication=True,
                                   override_until=hour['ends_at'])
    bot.runtime.stop_event('42', 'owner')
    await bot.post_drop('42', '10', 'captain-rex', force=True)
    assert bot.game.get_drop(pending['id'])['status'] == 'expired'
    assert bot.game.active_drops('42')[0]['card_id'] == 'captain-rex' and len(messages) == 1


@pytest.mark.asyncio
async def test_autocomplete_cancellation_keeps_one_shared_art_validation_task(harness, monkeypatch):
    bot, _, _, _ = harness
    import venator_vault.__main__ as operations
    real = operations.verified_art_path
    count = []

    def verify(*args):
        count.append(args[1]['id'])
        return real(*args)

    monkeypatch.setattr(operations, 'verified_art_path', verify)
    task = asyncio.create_task(bot.admin_card_ids())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    ready = await bot.admin_card_ids()
    assert len(ready) == 851 and len(count) == 851
