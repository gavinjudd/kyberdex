"""Exercise the real, armed four-season calendar without writing player state."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from venator_vault.core import Game, GameError, timestamp

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'data/cards.json'
WINDOWS = {
    'autumn-2026': ('2026-10-08T00:00:00Z', '2026-11-01T04:00:00Z'),
    'winter-2026': ('2026-12-01T05:00:00Z', '2027-01-01T05:00:00Z'),
    'spring-2027': ('2027-03-01T05:00:00Z', '2027-04-01T04:00:00Z'),
    'summer-2027': ('2027-06-01T04:00:00Z', '2027-07-01T04:00:00Z'),
}


@pytest.fixture
def armed(tmp_path):
    return Game(tmp_path / 'seasonal.sqlite3', CATALOG, enabled_releases=tuple(WINDOWS))


def seasonal_ids(game, now):
    return {row['id'] for row in game.spawn_pool(now=now) if game.catalog[row['id']].get('release_id')}


@pytest.mark.parametrize('key', WINDOWS)
def test_every_armed_season_opens_and_closes_at_its_exact_boundaries(armed, key):
    start, end = map(timestamp, WINDOWS[key])
    expected = {card['id'] for card in armed.catalog.values() if card.get('release_id') == key}
    assert len(expected) == 4
    assert all((card['release']['starts_at'], card['release']['ends_at']) == WINDOWS[key]
               for card in armed.catalog.values() if card.get('release_id') == key)
    assert seasonal_ids(armed, start-1) == set()
    assert seasonal_ids(armed, start) == expected
    assert seasonal_ids(armed, end-1) == expected
    assert seasonal_ids(armed, end) == set()
    assert sum(row['probability'] for row in armed.spawn_pool(now=start)) == pytest.approx(1)


def test_public_configuration_keeps_releases_disabled_until_opted_in(armed, tmp_path):
    line = next(line for line in (ROOT / '.env.example').read_text().splitlines()
                if line.startswith('VAULT_ENABLED_RELEASES='))
    assert line.partition('=')[2] == ''
    assert set(armed.enabled_releases) == set(WINDOWS)
    off = Game(tmp_path / 'unarmed.sqlite3', CATALOG)
    assert off.enabled_events == frozenset()
    for start, _ in WINDOWS.values():
        assert seasonal_ids(off, timestamp(start)) == set()
    # A named edition never starts a new year by resetting its lifetime stock.
    assert seasonal_ids(armed, timestamp('2028-06-15T04:00:00Z')) == set()


def test_end_of_season_after_downtime_expires_slots_but_preserves_owned_copy(armed):
    start, end = map(timestamp, WINDOWS['summer-2027'])
    drop = armed.create_drop('scratch-guild', 'scratch-channel', 'hk-47-summer-2027', now=end-30)
    owned = armed.claim(drop['id'], 'scratch-player', 'hk47', now=end-20)['instance']
    restarted = Game(armed.db_path, CATALOG, enabled_releases=tuple(WINDOWS))
    assert restarted.active_drops(now=end+60) == []
    with closing(sqlite3.connect(armed.db_path)) as db, db:
        assert db.execute("SELECT minted,reserved FROM supply WHERE card_id='hk-47-summer-2027'").fetchone() == (1, 0)
    assert restarted.info('scratch-guild', 'scratch-player', owned['id']) == owned
    with pytest.raises(GameError, match='ended|expired'):
        restarted.claim(drop['id'], 'late-player', 'hk47', now=end+61)
    assert start < end


def test_summer_shares_base_art_reference_bonus_and_identity(armed, synthetic_asset_root):
    manifest = json.loads((synthetic_asset_root / 'assets/art/manifest.json').read_text())['assets']
    for card in armed.catalog.values():
        if card.get('release_id') != 'summer-2027':
            continue
        base = armed.catalog[card['variant_of']]
        for field in ('rarity', 'role', 'ability', 'arena', 'subject_id', 'continuity', 'art'):
            assert card[field] == base[field]
        assert card['power_profile']['combat'] == base['power_profile']['combat']
        assert 'raid_boss' not in card
        assert card['attack'] == (base['attack']*110+50)//100
        assert card['health'] == (base['health']*110+50)//100
        assert manifest[card['id']]['sha256'] == manifest[base['id']]['sha256']
        assert manifest[card['id']]['edition_render_qa'] == 'verified'
