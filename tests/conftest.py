"""Keep numeric combat fixtures fixed; variation tests explicitly exercise roll boundaries."""
import pytest

from venator_vault import core


@pytest.fixture(autouse=True)
def fixed_copy_rolls(monkeypatch):
    monkeypatch.setattr(core.secrets, 'randbelow', lambda bound: bound // 2)


@pytest.fixture(scope='session')
def synthetic_asset_root(tmp_path_factory):
    """Local test-only pixels exercise real hash, size, render and delivery gates.

    These plain geometric images do not represent catalog subjects or certify the
    omitted production artwork. They exist only in pytest's temporary directory.
    """
    import hashlib
    import json
    import shutil
    from pathlib import Path

    from PIL import Image, ImageDraw

    source = Path(__file__).resolve().parents[1]
    root = tmp_path_factory.mktemp('synthetic-assets')
    shutil.copytree(source / 'data', root / 'data')
    for folder in ('fonts', 'finishes'):
        shutil.copytree(source / 'assets' / folder, root / 'assets' / folder)
    art_root = root / 'assets/art'
    art_root.mkdir()
    image = Image.new('RGB', (200, 300), '#153043')
    ImageDraw.Draw(image).rectangle((35, 70, 165, 220), outline='#8cf0dc', width=4)
    manifest = {}
    for card in json.loads((root / 'data/cards.json').read_text())['cards']:
        path = root / card['art']
        if not path.exists():
            image.save(path)
        manifest[card['id']] = {
            'path': card['art'], 'subject_id': card.get('subject_id'),
            'status': 'ready', 'visual_qa': 'verified', 'edition_render_qa': 'verified',
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'width': 200, 'height': 300,
            'test_fixture': True,
        }
    (art_root / 'manifest.json').write_text(json.dumps({'assets': manifest}))
    return root


@pytest.fixture(autouse=True)
def isolated_saved_setup(monkeypatch, tmp_path):
    """Never let a developer's real onboarding profile affect offline tests."""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path.parent / (tmp_path.name + '-config')))
