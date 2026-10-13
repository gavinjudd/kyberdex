"""Player journeys, copy rolls, private feedback and command discovery."""
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from test_interactions import bot as usability_bot_fixture
from test_interactions import interaction, seed_card

from venator_vault.core import Game, GameError, balance_stats
from venator_vault.features import handle_component
from venator_vault.presentation import drop_panel, drop_view


@pytest.fixture
def bot(tmp_path, synthetic_asset_root):
    return usability_bot_fixture.__wrapped__(tmp_path, synthetic_asset_root)


@pytest.mark.parametrize('low,high',[(95,105),(100,100),(105,95)])
def test_copy_rolls_and_limited_bonus_persist_without_reroll(tmp_path,monkeypatch,low,high):
    from venator_vault import core
    source=Path(__file__).resolve().parents[1]/'data/cards.json'
    game=Game(tmp_path/'rolls.sqlite3',source)
    iterator=iter([low-95,high-95]*2)
    monkeypatch.setattr(core.secrets,'randbelow',lambda _bound:next(iterator))
    with game._tx() as db:
        base=game._mint(db,'g','a','captain-rex',1,'test')
        edition=game._mint(db,'g','a','rex-rishi',1,'test')
    card=edition['card']
    unbonused=balance_stats(card['id'],card['rarity'],card['role'],card['arena'],card['power_profile']['combat'])
    for stat,percent in [('attack',low),('health',high)]:
        assert base[stat]==(base['card'][stat]*percent+50)//100
        varied=(unbonused[stat]*percent+50)//100
        assert edition[stat]==(varied*110+50)//100
    reloaded=Game(game.db_path,source)
    assert reloaded.info('g','a',edition['id'])==edition
    with game._tx() as db:
        db.execute('UPDATE instances SET user_id=? WHERE id=?',('b',edition['id']))
    transferred=game.info('g','b',edition['id'])
    assert (transferred['attack'],transferred['health'])==(edition['attack'],edition['health'])
    with pytest.raises(GameError):
        game.info('g','a',edition['id'])


def test_old_copies_migrate_at_100_percent_without_changing_stats(bot):
    old=seed_card(bot,101)
    with closing(sqlite3.connect(bot.game.db_path)) as db, db:
        for column in ('attack_roll','health_roll','stat_roll_version'):
            db.execute(f'ALTER TABLE instances DROP COLUMN {column}')
        db.execute('PRAGMA user_version=4')
    restored=Game(bot.game.db_path,bot.settings.catalog)
    copy=restored.info('42','101',old['id'])
    assert copy['stat_roll_version']==0
    assert (copy['attack_roll'],copy['health_roll'])==(100,100)
    assert (copy['attack'],copy['health'])==(old['attack'],old['health'])


def test_captures_stay_private_and_keep_second_slot_and_commit_order(bot,monkeypatch):
    from venator_vault import core
    monkeypatch.setattr(core.secrets,'randbelow',lambda _bound:10)
    drop=bot.game.create_drop('42','10','captain-rex')
    first=bot.game.claim(drop['id'],'999','rex')['drop']
    panel=drop_panel(first).to_dict()
    assert panel['title']=='A card appeared'
    assert panel['image']['url']=='attachment://encounter.png'
    assert '999' in str(panel)
    assert 'Captain Rex' not in str(panel)
    assert 'Card stats' not in [field['name'] for field in panel['fields']]
    assert first['remaining']==1 and first['expires_at']==drop['expires_at']
    assert not drop_view(first).children[0].disabled
    # Identical clocks and reversed user IDs still preserve actual commit order.
    second=bot.game.claim(drop['id'],'001','rex',now=first['claims'][0]['claimed_at'])['drop']
    assert [claim['user_id'] for claim in second['claims']]==['999','001']
    assert second['status']=='claimed' and drop_view(second).children[0].disabled
    assert '<@999>' in str(drop_panel(second).to_dict()) and '<@001>' in str(drop_panel(second).to_dict())
    replay=bot.game.claim(drop['id'],'999','rex')
    assert replay['replayed'] and replay['instance']['attack_roll']==105
    assert replay['instance']['attack']==first['claims'][0]['attack']


@pytest.mark.asyncio
async def test_public_refresh_keeps_blind_art_and_both_collectors(bot,monkeypatch):
    seed=bot.game.create_drop('42','10','captain-rex')
    bot.game.attach_message(seed['id'],'100')
    bot.game.claim(seed['id'],'101','rex')
    final=bot.game.claim(seed['id'],'102','rex')['drop']
    message=SimpleNamespace(edit=AsyncMock())
    bot.get_channel=lambda _id:SimpleNamespace(get_partial_message=lambda _id:message)
    bot.card_image=AsyncMock(return_value=discord.File(__import__('io').BytesIO(b'PNG'),filename='card.png'))
    assert await bot.refresh_drop(final)
    posted=message.edit.await_args.kwargs
    assert len(posted['attachments'])==1
    assert posted['embed'].image.url=='attachment://encounter.png'
    bot.card_image.assert_not_awaited()
    assert 'Card stats' not in [field.name for field in posted['embed'].fields]
    assert 'Captain Rex' not in str(posted['embed'].to_dict())
    assert '101' in str(posted['embed'].to_dict()) and '102' in str(posted['embed'].to_dict())
    assert not any(not child.disabled for child in posted['view'].children)


@pytest.mark.asyncio
async def test_join_squad_builds_deck_and_joins_atomically(bot):
    owned=seed_card(bot,101)
    raid=bot.game.raid_create('42','10')
    bot.get_channel=lambda _id:None
    event=interaction(101)
    await handle_component(bot,event,'joinraid',raid['id'])
    ui=event.followup.send.await_args.kwargs
    assert 'Build your Ground deck'==ui['embed'].title
    assert owned['id'] in [o.value for o in ui['view'].children[0].options]
    key=next(k for k,v in bot.pending_confirmations.items() if v.get('kind')=='deck_picker')
    choose=interaction(101)
    choose.edit_original_response=AsyncMock()
    choose.data={'values':[owned['id']]}
    await handle_component(bot,choose,'deckpick',key)
    save=interaction(101)
    save.edit_original_response=AsyncMock()
    await handle_component(bot,save,'decksave',key)
    joined=bot.game.get_raid(raid['id'])
    assert [m['user_id'] for m in joined['participants']]==['101']
    assert bot.game.deck('42','101','ground')['cards'][0]['id']==owned['id']
    assert key not in bot.pending_confirmations


@pytest.mark.asyncio
async def test_picker_rejects_other_owner_stale_inventory_and_expired_signup(bot):
    owned=seed_card(bot,101)
    raid=bot.game.raid_create('42','10')
    await handle_component(bot,interaction(101),'joinraid',raid['id'])
    key=next(iter(bot.pending_confirmations))
    pending=bot.pending_confirmations[key]
    thief=interaction(102)
    thief.data={'values':[owned['id']]}
    with pytest.raises(ValueError,match='another player'):
        await handle_component(bot,thief,'deckpick',key)
    pending['selected']=[owned['id']]
    with bot.game._tx() as db:
        db.execute('UPDATE raids SET signup_ends_at=? WHERE id=?',(time.time()-1,raid['id']))
    save=interaction(101)
    save.edit_original_response=AsyncMock()
    with pytest.raises(GameError):
        await handle_component(bot,save,'decksave',key)
    # No halfway-saved deck if signup is already over.
    assert bot.game.deck('42','101','ground')['cards']==[]


@pytest.mark.asyncio
async def test_command_choices_alias_search_and_empty_raid_have_navigation(bot):
    seed_card(bot,101)
    group=bot.tree.get_command('vault')
    expected={'help','catalog','card','collection','complain','favorite','wishlist','status',
              'profile','daily','recipes','salvage','craft','trade','deck','duel','raid','market'}
    assert {cmd.name for cmd in group.commands}==expected
    for cmd,param in [('catalog','rarity'),('collection','rarity'),('deck','arena'),('duel','arena')]:
        assert group.get_command(cmd).get_parameter(param).choices
    for cmd,param in [('catalog','search'),('collection','search'),('card','card'),('card','copy'),
                      ('favorite','instance_id'),('wishlist','card'),('craft','target_card'),
                      ('craft','instance_ids'),('deck','instance_ids'),('salvage','instance_ids')]:
        assert group.get_command(cmd).get_parameter(param).autocomplete
    assert await group.autocomplete_owned_card(interaction(101),'rex')
    assert not await group.autocomplete_owned_card(interaction(101,guild=99),'rex')
    event=interaction(101)
    await group.get_command('raid').callback(event)
    ui=event.followup.send.await_args.kwargs
    assert 'No raid running'==ui['embed'].title
    assert {'Ground deck','Fleet deck'}<={child.label for child in ui['view'].children}


@pytest.mark.asyncio
async def test_deck_command_shows_both_saved_squads_and_direct_arena_without_help(bot):
    ground = seed_card(bot, 101)
    bot.game.catalog['commander-cody']['arena'] = 'fleet'
    fleet = seed_card(bot, 101, 'commander-cody')
    bot.game.set_deck('42', '101', 'ground', [ground['id']])
    bot.game.set_deck('42', '101', 'fleet', [fleet['id']])
    group = bot.tree.get_command('vault')
    command = group.get_command('deck')
    assert not command.get_parameter('arena').required
    event = interaction(101)
    await command.callback(event)
    response = event.followup.send.await_args.kwargs
    assert response['ephemeral'] is True
    assert response['embed'].title == 'Your squads'
    assert 'Captain Rex' in response['embed'].fields[0].value
    assert 'Commander Cody' in response['embed'].fields[1].value
    assert {child.label for child in response['view'].children} == {'Edit Ground deck', 'Edit Fleet deck'}
    direct = interaction(101)
    await command.callback(direct, arena='fleet')
    response = direct.followup.send.await_args.kwargs
    assert response['embed'].title == 'Your Fleet squad'
    assert fleet['id'] in response['embed'].description
    assert 'Captain Rex' not in response['embed'].description
    assert bot.game.deck('42', '101', 'ground')['cards'][0]['id'] == ground['id']
    assert bot.game.deck('42', '101', 'fleet')['cards'][0]['id'] == fleet['id']
    with pytest.raises(ValueError, match='Choose Ground or Fleet'):
        await command.callback(interaction(101), clear=True)


@pytest.mark.asyncio
async def test_collection_pages_preserve_filters_and_owner(bot):
    for _ in range(14):
        seed_card(bot,101)
    group=bot.tree.get_command('vault')
    event=interaction(101)
    await group.get_command('collection').callback(group,event,search='rex')
    key=next(k for k,v in bot.pending_confirmations.items() if v.get('kind')=='browse')
    state=bot.pending_confirmations[key]
    assert state['has_next'] and state['search']=='rex'
    next_page=interaction(101)
    next_page.edit_original_response=AsyncMock()
    await handle_component(bot,next_page,'browsenext',key)
    assert state['page']==2 and not state['has_next']
    with pytest.raises(ValueError,match='another player'):
        await handle_component(bot,interaction(102),'browseprev',key)


def test_feedback_private_storage_idempotence_quota_and_review(bot):
    game=bot.game
    first=game.complain('42','101','Capture failed for Rex',now=100,request_id='interaction-1')
    assert game.complain('42','101','Capture failed for Rex',now=101,request_id='interaction-1')['id']==first['id']
    with pytest.raises(GameError) as error:
        game.complain('42','101','another report',now=101)
    assert error.value.code=='complaint_cooldown'
    for n in range(1,5):
        game.complain('42','101',f'another report {n}',now=100+n*60)
    with pytest.raises(GameError) as error:
        game.complain('42','101','too many reports',now=400)
    assert error.value.code=='complaint_limit'
    assert game.complaints('other')==[]
    with pytest.raises(GameError):
        game.resolve_complaint('other','manager',first['id'],now=450)
    game.resolve_complaint('42','manager',first['id'],now=450)
    assert first['id'] not in [row['id'] for row in game.complaints('42')]
    with game._tx() as db:
        assert db.execute('SELECT COUNT(*) FROM complaints').fetchone()[0]==5


@pytest.mark.asyncio
async def test_complaint_command_ack_is_private_and_admin_review_requires_manage_server(bot):
    group=bot.tree.get_command('vault')
    event=interaction(101)
    event.id=12345
    await group.get_command('complain').callback(group,event,'Capture failed for Rex')
    ui=event.followup.send.await_args.kwargs
    assert ui['ephemeral'] and 'Saved your report' in ui['content']
    assert 'Capture failed for Rex' not in ui['content']
    admin=bot.tree.get_command('vault-admin')
    denied=interaction(101)
    denied.permissions=SimpleNamespace(manage_guild=False)
    assert not await admin.interaction_check(denied)
    allowed=interaction(101)
    allowed.permissions=SimpleNamespace(manage_guild=True)
    assert await admin.interaction_check(allowed)


@pytest.mark.asyncio
async def test_deleted_finished_capture_post_does_not_retry_or_remove_inventory(bot):
    owned=seed_card(bot,101)
    drop=bot.game.create_drop('42','10','captain-rex',now=time.time()-200,duration=180)
    bot.game.attach_message(drop['id'],'999')
    error=discord.NotFound(SimpleNamespace(status=404,reason='Not Found'),{'code':10008,'message':'Unknown Message'})
    message=SimpleNamespace(edit=AsyncMock(side_effect=error))
    bot.get_channel=lambda _id:SimpleNamespace(get_partial_message=lambda _id:message)
    assert await bot.refresh_drop(bot.game.get_drop(drop["id"]))
    assert bot.game.info('42','101',owned['id'])['user_id']=='101'
