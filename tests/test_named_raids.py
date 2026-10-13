"""Named bosses: risk, contribution, migration, and exactly-once payouts."""
import concurrent.futures
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from venator_vault.core import RAID_TIERS, Game, GameError
from venator_vault.features import raid_panel


@pytest.fixture
def game(tmp_path):
    cards = []
    for key, role, attack, health in [('striker','striker',100,500), ('guardian','guardian',100,500),
                                       ('support','support',100,500), ('weak','striker',10,50),
                                       ('fleet','striker',100,500), ('boss','striker',200,2000)]:
        card = {'id': key,'name': key.title(),'aliases': [],'subject_id': key,'category': 'canon','rarity': 'common',
                    'arena': 'fleet' if key=='fleet' else 'ground','role': role,'faction': 'Independent',
                    'attack': attack,'health': health,'speed': 70,'weight': 1}
        if key=='boss':
            card['raid_boss'] = {'difficulty': 'apex','move': 'counterstrike'}
        cards.append(card)
    path=tmp_path/'cards.json'
    path.write_text(json.dumps({'schema_version': 1,'cards': cards}))
    result = Game(tmp_path/'vault.sqlite3',path)
    result.catalog_path = path
    return result


def deck(game,user,ids=('striker','guardian','support')):
    with game._tx() as db:
        instances=[game._mint(db,'g',user,key,1,'synthetic-raid-test')['id'] for key in ids]
    return game.set_deck('g',user,game.catalog[ids[0]]['arena'],instances,now=1)


def encounter(game,users=('a',),move='counterstrike',difficulty='apex'):
    game.catalog['boss']['raid_boss'].update(move=move,difficulty=difficulty)
    raid=game.raid_create('g','c',now=100,signup=10,duration=300,boss_id='boss')
    for user in users:
        game.raid_join(raid['id'],user,now=100)
    return game.get_raid(raid['id'],now=110)


def finish(game,raid):
    for turn in range(3):
        for member in raid['participants']:
            current=game.get_raid(raid['id'],now=110+turn*15)
            if current['status']!='active':
                return current
            player=next(p for p in current['participants'] if p['user_id']==member['user_id'])
            if player['combat']['hp']>0:
                game.raid_attack(raid['id'],member['user_id'],now=110+turn*15)
    return game.get_raid(raid['id'],now=155)


@pytest.mark.parametrize('difficulty',list(RAID_TIERS))
def test_hp_and_pool_match_published_math_and_no_idle_reward(game,difficulty):
    for user in ('a','b','idle'):
        deck(game,user)
    raid=encounter(game,('a','b','idle'),difficulty=difficulty)
    tier=RAID_TIERS[difficulty]
    assert raid['max_hp']==max(400,math.ceil(310*3*3*tier['effort']))
    assert raid['boss']['credit_pool']==math.floor(tier['credits']*math.sqrt(3))
    # Simulate removal of idle roster pressure only to isolate settlement math.
    with game._tx() as db:
        db.execute('UPDATE raids SET hp=? WHERE id=?',(310*4,raid['id']))
    final=finish(game,{**raid,'participants':raid['participants'][:2]})
    assert final['status']=='defeated'
    assert {r['user_id'] for r in final['rewards']}=={'a','b'}
    assert sum(r['credits'] for r in final['rewards'])==math.floor(tier['credits']*math.sqrt(2))
    assert all(1<=r['xp']<=tier['xp'] for r in final['rewards'])
    assert game.profile('g','idle')['credits']==0


@pytest.mark.parametrize('move,multiplier', [('counterstrike',1),('onslaught',1),('choke',1),
                                             ('lightning',1.15),('ion',1),('superlaser',1)])
def test_counter_guardian_support_and_move_math(game,move,multiplier):
    deck(game,'a')
    raid=encounter(game,move=move)
    first=game.raid_attack(raid['id'],'a',now=110)
    assert first['damage_dealt']==310
    assert first['retaliation_damage']==round(200*multiplier*.9)
    assert first['repaired_hp']==75
    member=first['participants'][0]
    assert member['combat']['hp']==1500-first['retaliation_damage']+75
    assert member['combat']['damage_factor']==(.9 if move=='choke' else 1)
    cooldown=15 if move=='ion' else 10
    assert first['attack_cooldown']==cooldown
    with pytest.raises(GameError) as error:
        game.raid_attack(raid['id'],'a',now=110+cooldown-1)
    assert error.value.code=='attack_cooldown'
    second=game.raid_attack(raid['id'],'a',now=110+cooldown)
    assert second['damage_dealt']==(279 if move=='choke' else 310)
    multiplier=2 if move=='superlaser' else 1.2 if move=='onslaught' else multiplier
    assert second['retaliation_damage']==round(200*multiplier*.9)
    assert f'{cooldown} seconds apart' in raid_panel(second).description


def test_defeated_squad_cannot_attack_repair_revive_or_lose_owned_cards(game):
    deck(game,'a',('weak',))
    deck(game,'b')
    raid=encounter(game,('a','b'))
    before=game.inventory('g','a')
    first=game.raid_attack(raid['id'],'a',now=110)
    a=next(p for p in first['participants'] if p['user_id']=='a')
    assert a['combat']['hp']==0 and first['repaired_hp']==0
    assert first['retaliation_damage']==50
    with pytest.raises(GameError) as error:
        game.raid_attack(raid['id'],'a',now=120)
    assert error.value.code=='squad_defeated'
    assert game.inventory('g','a')==before
    assert first['status']=='active' and not first['rewards']


def test_weak_solo_roster_fails_without_reward(game):
    deck(game,'a',('weak',))
    raid=encounter(game)
    assert raid['max_hp']>raid['boss']['roster_potential']
    final=finish(game,raid)
    assert final['status']=='failed' and not final['rewards']
    assert game.profile('g','a')['credits']==0


def test_snapshot_restart_and_killing_blow_no_counter(game):
    deck(game,'a')
    raid=encounter(game,move='choke')
    game.attach_raid_message(raid['id'],'m','https://example.com/boss.png')
    game.raid_attack(raid['id'],'a',now=110)
    document=json.loads(game.catalog_path.read_text())
    document['cards'][-1].update(attack=9999,health=99999)
    game.catalog_path.write_text(json.dumps(document))
    restarted=Game(game.db_path,game.catalog_path)
    saved=restarted.get_raid(raid['id'],now=120)
    assert saved['boss']['attack']==200 and saved['boss']['health']==2000
    assert saved['boss']['image_url']=='https://example.com/boss.png'
    assert saved['participants'][0]['combat']['damage_factor']==.9
    restarted.raid_attack(raid['id'],'a',now=120)
    final=restarted.raid_attack(raid['id'],'a',now=130)
    assert final['status']=='defeated' and final['retaliation_damage']==0
    assert sum(r['credits'] for r in final['rewards'])==1000
    rewards=final['rewards']
    with pytest.raises(GameError):
        restarted.raid_attack(raid['id'],'a',now=140)
    assert restarted.get_raid(raid['id'],now=140)['rewards']==rewards


def test_concurrent_final_attacks_settle_once(game):
    for user in ('a','b'):
        deck(game,user)
    raid=encounter(game,('a','b'))
    with game._tx() as db:
        db.execute('UPDATE raids SET hp=100 WHERE id=?',(raid['id'],))
    def attack(user):
        try:
            return Game(game.db_path,game.catalog_path).raid_attack(raid['id'],user,now=110)
        except GameError as error:
            return error.code
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        outcomes=list(pool.map(attack,('a','b')))
    assert sum(isinstance(r,dict) for r in outcomes)==1
    final=game.get_raid(raid['id'],now=111)
    assert final['hp']==0 and len(final['rewards'])==1
    assert sum(r['credits'] for r in final['rewards'])==1000
    with game._tx() as db:
        assert db.execute("SELECT COUNT(*) FROM ledger WHERE reason='raid'").fetchone()[0]==1


def test_v4_migration_preserves_active_generic_raid(game):
    del game.catalog['boss']['raid_boss']
    deck(game,'a')
    raid=game.raid_create('g','c',now=100,signup=10,duration=300)
    game.raid_join(raid['id'],'a',now=100)
    with closing(sqlite3.connect(game.db_path)) as db, db:
        db.execute('ALTER TABLE raids DROP COLUMN boss')
        db.execute('ALTER TABLE raid_members DROP COLUMN combat')
        db.execute('PRAGMA user_version=4')
    restarted=Game(game.db_path,game.catalog_path)
    saved=restarted.get_raid(raid['id'],now=110)
    assert restarted.health()['schema_version']==7
    assert saved['boss'] is None and saved['participants'][0]['combat'] is None
    assert saved['max_hp']==math.ceil(310*3*.85)
    for when in (110,120,130):
        final=restarted.raid_attack(raid['id'],'a',now=when)
    assert final['rewards']==[{'user_id': 'a','credits': 200,'xp': 25}]


def test_arena_selection_and_minimum_deck_requirements(game):
    with pytest.raises(GameError,match='arena'):
        game.raid_create('g','c','fleet',now=100,boss_id='boss')
    raid=game.raid_create('g','c',now=100,boss_id='boss')
    with pytest.raises(GameError) as error:
        game.raid_join(raid['id'],'a',now=100)
    assert error.value.code=='empty_deck'
    deck(game,'a',('fleet',))
    with pytest.raises(GameError) as error:
        game.raid_join(raid['id'],'a',now=100)
    assert error.value.code=='empty_deck'


def test_production_boss_catalog_and_tier_mass(tmp_path):
    production=Game(tmp_path/'prod.sqlite3',Path(__file__).resolve().parents[1]/'data/cards.json')
    assert len(production.raid_bosses('ground'))==11
    assert len(production.raid_bosses('fleet'))==6
    assert sum(t['weight'] for t in RAID_TIERS.values())==100
    for boss in production.raid_bosses():
        assert boss['art_status']=='ready'
        assert boss['category']=='canon'
