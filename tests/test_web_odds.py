"""Execute the actual archive JavaScript against public payloads with a tiny DOM.

The harness clicks card details and checks player-visible odds, so a Python-only
sampler test cannot conceal a stale browser formula or activation label.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from venator_vault.web import public_catalog

NODE = shutil.which('node')
APP = Path(__file__).resolve().parents[1] / 'web/app.js'
pytestmark = pytest.mark.skipif(NODE is None, reason='Node is required to execute browser odds JavaScript')
NOW = '2026-10-08T12:00:00Z'
V3 = {'schema_version': 3, 'formula': 'tier-first-release-lanes-v3', 'rate_revision': 3, 'limited_share_percent': 25}
WEIGHTS = dict(zip(('common','uncommon','rare','epic','legendary','mythic'), (5400,2200,1780,420,160,40)))
REVISION_2_WEIGHTS = dict(zip(WEIGHTS, (5090,2500,1800,420,150,40)))

HARNESS = r"""
const fs=require('fs'),vm=require('vm');
const payload=JSON.parse(fs.readFileSync(0,'utf8'));
const elements=new Map();
function element(id){
  if(!elements.has(id))elements.set(id,{id,innerHTML:'',textContent:'',hidden:false,value:'',
    options:[{textContent:'All'}],handlers:{},addEventListener(event,fn){this.handlers[event]=fn;},
    querySelectorAll(){return [];},setAttribute(){},showModal(){},close(){},focus(){},select(){}});
  return elements.get(id);
}
const document={getElementById:element,querySelectorAll(){return [];},querySelector(){return null;},addEventListener(){}};
const context={document,console,Intl,Date,Set,Map,Number,String,Object,Array,Math,Boolean,
  matchMedia(){return {matches:false,addEventListener(){}};},
  fetch:async()=>({ok:true,json:async()=>payload}),setTimeout,clearTimeout,navigator:{}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
setImmediate(()=>{
  const details={};
  for(const card of payload.cards){
    element('card-grid').handlers.click({target:{closest(){return {dataset:{card:card.id}};}}});
    details[card.id]=element('card-detail').innerHTML;
  }
  process.stdout.write(JSON.stringify({details,rarity:element('rarity-chances').textContent,formula:element('rate-formula').textContent,
    state:element('catalog-state').innerHTML,result:element('result-count').textContent}));
});
"""


def card(identity, rarity='common', weight=1, **extra):
    return {'id': identity, 'name': identity, 'rarity': rarity, 'weight': weight,
            'category': 'canon', 'arena': 'ground', 'aliases': [], 'release': None, **extra}


def edition(identity='edition', rarity='common', weight=1, **extra):
    return card(identity, rarity, weight, category='limited', release_id='autumn',
                release={'starts_at':'2026-10-01T00:00:00Z', 'ends_at':'2026-11-01T00:00:00Z','supply_cap':100},
                **extra)


def visible(tmp_path, cards, *, enabled=('autumn',), spawn_model=V3, now=NOW):
    (tmp_path/'data').mkdir(exist_ok=True)
    rarities={c['rarity']:{'spawn_weight':WEIGHTS[c['rarity']], 'collector_limit':2} for c in cards}
    if spawn_model['schema_version']==2:
        old=dict(zip(WEIGHTS,(5800,2500,1000,500,150,50)))
        for r,definition in rarities.items():definition['spawn_weight']=old[r]
    elif spawn_model.get('rate_revision', 1)==1:
        old=dict(zip(WEIGHTS,(5150,2500,1800,400,110,40)))
        for r,definition in rarities.items():definition['spawn_weight']=old[r]
    elif spawn_model.get('rate_revision')==2:
        for r,definition in rarities.items():definition['spawn_weight']=REVISION_2_WEIGHTS[r]
    (tmp_path/'data/cards.json').write_text(json.dumps({'cards':cards,'rarities':rarities,'spawn_model':spawn_model}))
    payload=public_catalog(tmp_path,enabled_releases=enabled)
    payload['public']['served_at']=now
    result=subprocess.run([NODE,'-e',HARNESS,str(APP)],input=json.dumps(payload),text=True,
                          capture_output=True,check=True,timeout=10)
    rendered=json.loads(result.stdout)
    assert rendered['result']!='Catalog unavailable',rendered
    return rendered


def chance(rendered, identity):
    match=re.search(r'Conditional card chance <b>([\d.,]+)%</b>',rendered['details'][identity])
    return float(match[1].replace(',',''))/100 if match else None


def test_v3_weighted_lanes_preserve_tier_mass(tmp_path):
    rendered=visible(tmp_path,[card('regular-light',weight=1),card('regular-heavy',weight=3),
                               edition('limited-light',weight=1),edition('limited-heavy',weight=3)])
    assert chance(rendered,'regular-light')==pytest.approx(.1875)
    assert chance(rendered,'regular-heavy')==pytest.approx(.5625)
    assert chance(rendered,'limited-light')==pytest.approx(.0625)
    assert chance(rendered,'limited-heavy')==pytest.approx(.1875)
    assert '25%' in rendered['rarity'] and '75%' in rendered['rarity']
    assert 'cannot inspect private minted or reserved stock' in rendered['rarity']
    assert 'share to the other' in rendered['rarity']
    assert 'group share' in rendered['formula']


def test_disabled_release_and_zero_weight_do_not_dilute_ordinary_odds(tmp_path):
    rendered=visible(tmp_path,[card('ordinary'),edition(),card('retired',weight=0)],enabled=())
    assert chance(rendered,'ordinary')==1
    assert chance(rendered,'edition') is None
    assert chance(rendered,'retired') is None
    assert 'Seasonal release disabled' in rendered['details']['edition']


def test_only_limited_lane_uses_entire_tier(tmp_path):
    rendered=visible(tmp_path,[edition('a',weight=1),edition('b',weight=3)])
    assert chance(rendered,'a')==.25
    assert chance(rendered,'b')==.75


@pytest.mark.parametrize(('now','active'),[
    ('2026-09-30T23:59:59Z',False),('2026-10-01T00:00:00Z',True),
    ('2026-10-31T23:59:59Z',True),('2026-11-01T00:00:00Z',False)])
def test_release_dates_use_inclusive_start_exclusive_end(tmp_path,now,active):
    rendered=visible(tmp_path,[card('ordinary'),edition()],now=now)
    assert chance(rendered,'edition')==(.25 if active else None)
    assert chance(rendered,'ordinary')==(.75 if active else 1)


def test_event_only_cards_never_get_ordinary_odds_even_with_release_enabled(tmp_path):
    legend=edition('legend');legend.update(category='legends',event_id='archive',availability='event_only')
    rendered=visible(tmp_path,[card('ordinary'),legend])
    assert chance(rendered,'ordinary')==1
    assert chance(rendered,'legend') is None
    assert 'separately enabled event' in rendered['details']['legend']


def test_approved_game_card_keeps_legends_label_and_regular_pool_odds(tmp_path):
    approved = card('darth-revan', 'legendary', availability='regular',
                    continuity='Star Wars Legends — user-approved Old Republic game continuity')
    rendered = visible(tmp_path, [approved], enabled=())
    assert chance(rendered, 'darth-revan') == 1
    detail = rendered['details']['darth-revan']
    assert 'Standard pool' in detail
    assert 'Approved game continuity · Legends' in detail
    assert approved['continuity'] in detail
    assert '<span>Canon</span>' not in detail
    assert 'activation required' not in detail


def test_v2_uses_flat_weights_instead_of_release_lanes(tmp_path):
    rendered=visible(tmp_path,[card('ordinary'),edition(weight=3)],
                     spawn_model={'schema_version':2,'formula':'tier-first-v2'})
    assert chance(rendered,'ordinary')==.25
    assert chance(rendered,'edition')==.75
    assert 'card weights divide that tier' in rendered['rarity']
    assert 'group share' not in rendered['formula']


def test_v3_keeps_global_tier_weights_before_lane_split(tmp_path):
    rendered=visible(tmp_path,[card(r,r) for r in WEIGHTS]+[edition(rarity='rare')])
    for rarity,weight in WEIGHTS.items():
        assert chance(rendered,rarity)==pytest.approx(weight/10000*(.75 if rarity=='rare' else 1))
    assert chance(rendered,'edition')==pytest.approx(.178*.25)


def test_revision_two_public_catalog_keeps_its_published_odds_and_lanes(tmp_path):
    rendered = visible(tmp_path, [card(r, r) for r in WEIGHTS]+[edition(rarity='rare')],
                       spawn_model=dict(V3, rate_revision=2))
    for rarity, weight in REVISION_2_WEIGHTS.items():
        assert chance(rendered, rarity) == pytest.approx(weight/10000*(.75 if rarity == 'rare' else 1))
    assert chance(rendered, 'edition') == pytest.approx(.18*.25)


@pytest.mark.parametrize('explicit_revision', [False, True])
def test_historical_v3_public_catalog_keeps_original_odds_and_release_lanes(tmp_path, explicit_revision):
    model = {key: value for key, value in V3.items() if key != 'rate_revision'}
    if explicit_revision:
        model['rate_revision'] = 1
    rendered = visible(tmp_path, [card(r, r) for r in WEIGHTS]+[edition()], spawn_model=model)
    assert chance(rendered, 'common') == pytest.approx(.515*.75, abs=1e-5)
    assert chance(rendered, 'edition') == pytest.approx(.515*.25, abs=1e-5)
    assert chance(rendered, 'epic') == .04
    assert chance(rendered, 'legendary') == pytest.approx(.011)
    assert chance(rendered, 'mythic') == .004


@pytest.mark.parametrize('revision', [0, 4, 99, True, False, 2.0, '2', None])
def test_public_catalog_rejects_unknown_or_malformed_rate_revision(tmp_path, revision):
    with pytest.raises(ValueError, match='spawn rate revision'):
        visible(tmp_path, [card('ordinary')], enabled=(), spawn_model=dict(V3, rate_revision=revision))


def test_expired_only_tier_is_removed_from_normalization(tmp_path):
    rendered=visible(tmp_path,[card('ordinary'),edition(rarity='mythic')],now='2027-01-01T00:00:00Z')
    assert chance(rendered,'ordinary')==1
    assert chance(rendered,'edition') is None
