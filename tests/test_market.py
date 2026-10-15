"""Fixed daily rotations and atomic, scarce, private credit purchases."""
import concurrent.futures
import datetime as dt
import random
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_interactions import bot as scratch_bot
from test_interactions import interaction

from venator_vault.core import MARKET_PRICES, Game, GameError
from venator_vault.features import handle_component, market_panel

NOW=dt.datetime(2026,10,9,12,tzinfo=dt.UTC).timestamp()


@pytest.fixture
def game(tmp_path):
    source=Path(__file__).resolve().parents[1]/'data/cards.json'
    engine=Game(tmp_path/'market.sqlite3',source)
    engine.source_path=source
    engine.rng=random.Random(1)
    return engine


def fund(game,user,amount=10000):
    with game._tx() as db:
        game._credit(db,'g',user,amount,'test-fund',user,NOW)


def first_offer(game,user='a'):
    return game.market('g',user,now=NOW)['offers'][0]


def test_rotation_is_fixed_shared_private_and_scarce(game):
    first=game.market('g','a',now=NOW)
    # random.Random(1).random() < .25, so this fixture includes the rare Epic slot.
    assert [o['rarity'] for o in first['offers']]==['common','common','uncommon','rare','epic']
    assert sum(o['stock'] for o in first['offers'])==15
    assert len({game.catalog[o['card_id']]['subject_id'] for o in first['offers']})==5
    for offer in first['offers']:
        assert offer['price']==MARKET_PRICES[offer['rarity']]
        assert all(k not in offer for k in ['attack','health','attack_roll','health_roll'])
        assert game._market_card(game.catalog[offer['card_id']])
    second=game.market('g','b',now=NOW+10)
    assert first['offers']==second['offers']
    restarted=Game(game.db_path,game.source_path)
    assert restarted.market('g','a',now=NOW+60)==first
    assert game.inventory('g','a')==[]
    assert 'ATK' not in str(market_panel(first).to_dict()) and ' HP ' not in str(market_panel(first).to_dict())
    assert len(market_panel(first))<=6000


def test_purchase_mints_once_debits_once_and_does_not_grant_capture_rewards(game,monkeypatch):
    from venator_vault import core
    fund(game,'a')
    offer=first_offer(game)
    monkeypatch.setattr(core.secrets,'randbelow',lambda _bound:10)
    bought=game.market_buy('g','a',offer['id'],now=NOW,request_id='r1')
    assert bought['credits']==10000-offer['price']
    assert bought['instance']['attack_roll']==105
    assert bought['instance']['origin'].startswith('market:')
    assert game.profile('g','a')['xp']==0
    replay=Game(game.db_path,game.source_path).market_buy('g','a',offer['id'],now=NOW+60,request_id='r1')
    assert replay['replayed'] and replay['instance']['id']==bought['instance']['id']
    assert len(game.inventory('g','a'))==1
    assert game.market('g','a',now=NOW)['purchased_card']==offer['name']
    with game._tx() as db:
        assert db.execute("SELECT COUNT(*) FROM ledger WHERE reason='market'").fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM claims').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM capture_history').fetchone()[0]==0
        assert db.execute('SELECT sold FROM market_offers WHERE id=?',(offer['id'],)).fetchone()[0]==1


def test_daily_limit_expiry_and_next_day_purchase(game):
    fund(game,'a')
    offers=game.market('g','a',now=NOW)['offers']
    game.market_buy('g','a',offers[0]['id'],now=NOW,request_id='r1')
    with pytest.raises(GameError) as error:
        game.market_buy('g','a',offers[1]['id'],now=NOW+60)
    assert error.value.code=='market_daily_limit'
    tomorrow=game.market('g','a',now=NOW+86400)
    assert not tomorrow['purchased_today'] and tomorrow['day']!='2026-10-09'
    assert {o['id'] for o in tomorrow['offers']}.isdisjoint(o['id'] for o in offers)
    with pytest.raises(GameError) as error:
        game.market_buy('g','a',offers[0]['id'],now=NOW+86400)
    assert error.value.code=='market_expired'
    # A delayed duplicate interaction from yesterday is still a receipt, not today's purchase.
    assert game.market_buy('g','a',offers[0]['id'],now=NOW+86400,request_id='r1')['replayed']
    game.market_buy('g','a',tomorrow['offers'][0]['id'],now=NOW+86400,request_id='r2')
    assert len(game.inventory('g','a'))==2


def test_insufficient_funds_forged_guild_and_retired_offer_have_no_side_effects(game):
    offer=first_offer(game)
    with pytest.raises(GameError) as error:
        game.market_buy('g','a',offer['id'],now=NOW)
    assert error.value.code=='insufficient_credits'
    assert game.market('g','a',now=NOW)['offers'][0]['sold']==0
    fund(game,'a')
    with pytest.raises(GameError):
        game.market_buy('foreign','a',offer['id'],now=NOW)
    game.catalog[offer['card_id']]['weight']=0
    assert not game.market('g','a',now=NOW)['offers'][0]['available']
    with pytest.raises(GameError) as error:
        game.market_buy('g','a',offer['id'],now=NOW)
    assert error.value.code=='market_unavailable'
    assert game.profile('g','a')['credits']==10000
    assert game.inventory('g','a')==[]


def test_concurrent_last_stock_and_same_player_double_click(game):
    offer=first_offer(game)
    for n in range(10):
        fund(game,str(n))
    def buy(user):
        other=Game(game.db_path,game.source_path)
        try:
            return other.market_buy('g',user,offer['id'],now=NOW,request_id='req-'+user)
        except GameError as error:
            return error.code
    with concurrent.futures.ThreadPoolExecutor(10) as pool:
        outcomes=list(pool.map(buy,map(str,range(10))))
    assert sum(isinstance(r,dict) for r in outcomes)==offer['stock']
    assert sum(r=='market_sold_out' for r in outcomes if isinstance(r,str))==10-offer['stock']
    with game._tx() as db:
        assert db.execute('SELECT COUNT(*) FROM market_purchases').fetchone()[0]==offer['stock']
        assert db.execute("SELECT SUM(delta) FROM ledger WHERE reason='market'").fetchone()[0]==-offer['stock']*offer['price']
    winner=next(str(i) for i,r in enumerate(outcomes) if isinstance(r,dict))
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        replay=list(pool.map(buy,[winner,winner]))
    assert all(row['replayed'] for row in replay)
    assert len(game.inventory('g',winner))==1


def test_concurrent_different_offers_still_one_purchase_per_player(game):
    fund(game,'a')
    offers=game.market('g','a',now=NOW)['offers'][:2]
    def buy(offer):
        try:
            return Game(game.db_path,game.source_path).market_buy('g','a',offer['id'],now=NOW)
        except GameError as error:
            return error.code
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results=list(pool.map(buy,offers))
    assert sum(isinstance(row,dict) for row in results)==1
    assert 'market_daily_limit' in results
    assert len(game.inventory('g','a'))==1


def test_failed_mint_rolls_back_stock_ledger_and_balance(game,monkeypatch):
    fund(game,'a')
    offer=first_offer(game)
    def fail(*_args):
        raise RuntimeError('synthetic power/worker failure before commit')
    monkeypatch.setattr(game,'_mint',fail)
    with pytest.raises(RuntimeError):
        game.market_buy('g','a',offer['id'],now=NOW)
    assert game.profile('g','a')['credits']==10000
    assert game.market('g','a',now=NOW)['offers'][0]['sold']==0
    with game._tx() as db:
        assert db.execute('SELECT COUNT(*) FROM market_purchases').fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM ledger WHERE reason='market'").fetchone()[0]==0


def test_snapshot_survives_price_change_and_bought_copy_consumption(game,monkeypatch):
    fund(game,'a')
    offer=first_offer(game)
    monkeypatch.setitem(MARKET_PRICES,'common',9999)
    bought=game.market_buy('g','a',offer['id'],now=NOW,request_id='r1')
    assert bought['credits_spent']==250
    with game._tx() as db:
        db.execute('UPDATE instances SET consumed_at=? WHERE id=?',(NOW+10,bought['instance']['id']))
    assert game.market_buy('g','a',offer['id'],now=NOW+20,request_id='r1')['replayed']
    assert game.market('g','a',now=NOW+20)['purchased_today']


def test_v5_migration_preserves_old_copies_and_balances(game):
    fund(game,'a')
    with game._tx() as db:
        copy=game._mint(db,'g','a','captain-rex',NOW,'fixture')
    with closing(sqlite3.connect(game.db_path)) as db, db:
        db.execute('DROP TABLE market_purchases');db.execute('DROP TABLE market_offers');db.execute('DROP TABLE market_days')
        db.execute('PRAGMA user_version=5')
    upgraded=Game(game.db_path,game.source_path)
    assert upgraded.health()['schema_version']==8
    assert upgraded.info('g','a',copy['id'])==copy
    assert upgraded.profile('g','a')['credits']==10000


@pytest.mark.asyncio
async def test_market_confirmation_owner_and_private_purchase_receipt(tmp_path, synthetic_asset_root):
    bot=scratch_bot.__wrapped__(tmp_path, synthetic_asset_root)
    bot.game.rng=random.Random(1)
    with bot.game._tx() as db:
        bot.game._credit(db,'42','101',10000,'fixture','credit',NOW)
    bot.card_image=AsyncMock(return_value=None)
    command=bot.tree.get_command('vault').get_command('market')
    event=interaction(101)
    await command.callback(event)
    ui=event.followup.send.await_args.kwargs
    assert ui['ephemeral']
    assert ui['embed'].title=='Daily market'
    offer=bot.game.market('42','101')['offers'][0]
    choose=interaction(101)
    choose.data={'values':[offer['id']]}
    await handle_component(bot,choose,'marketpick','101')
    review=choose.followup.send.await_args.kwargs
    assert review['embed'].title=='Review your purchase'
    assert 'ATK' not in str(review['embed'].to_dict())
    key=next(k for k,v in bot.pending_confirmations.items() if v['kind']=='market')
    with pytest.raises(ValueError,match='another player'):
        await handle_component(bot,interaction(102),'marketbuy',key)
    confirm=interaction(101);confirm.id=7654
    await handle_component(bot,confirm,'marketbuy',key)
    result=confirm.followup.send.await_args.kwargs
    assert result['ephemeral'] and result['embed'].title.startswith('Purchased')
    assert any(field.name=='Combat' for field in result['embed'].fields)
    assert len(result['embed'])<=6000
    assert len(bot.game.inventory('42','101'))==1
    await handle_component(bot,confirm,'marketbuy',key)
    assert len(bot.game.inventory('42','101'))==1


def test_epic_gate_and_power_loss_before_purchase_commit(game):
    import os
    import subprocess
    import sys

    game.rng=random.Random(2)
    fund(game,'a')
    market=game.market('g','a',now=NOW)
    assert len(market['offers'])==4
    offer=market['offers'][0]
    script='''
import os,sys
from venator_vault.core import Game
engine=Game(sys.argv[1],sys.argv[2])
original=engine._mint
def crash(*args):
    original(*args)
    os._exit(17)
engine._mint=crash
engine.market_buy('g','a',sys.argv[3],now=float(sys.argv[4]))
'''
    env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
    child=subprocess.run([sys.executable,'-c',script,game.db_path,str(game.source_path),offer['id'],str(NOW)],env=env,check=False,timeout=20)
    assert child.returncode==17
    restarted=Game(game.db_path,game.source_path)
    assert restarted.profile('g','a')['credits']==10000
    assert restarted.inventory('g','a')==[]
    assert restarted.market('g','a',now=NOW)['offers'][0]['sold']==0
    assert restarted.health()['foreign_key_violations']==0
