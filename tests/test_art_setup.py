"""Offline artwork setup: pinned sources, exact pixels, safe paths and resumable failure."""
import copy
import io
import json
import urllib.request
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from venator_vault import __main__ as ops
from venator_vault import art_setup as art


@pytest.fixture
def artwork(tmp_path):
    raw_buffer = io.BytesIO()
    Image.new('RGB', (30, 20), (17, 42, 99)).save(raw_buffer, format='PNG')
    raw = raw_buffer.getvalue()
    entry = {
        'path': 'assets/art/sample.png', 'subject_id': 'sample', 'width': 20, 'height': 30,
        'mode': 'RGB', 'source_image_url': 'https://lumiere-a.akamaihd.net/sample.png',
        'source_raw_sha256': art.digest(raw), 'pixel_sha256': art.digest(bytes((17, 42, 99)) * 600),
        'recipe': 'official-source-v1', 'status': 'available',
        'normalization': {'output_size': [20, 30], 'requested_crop_box': [0, 0, 30, 20],
                          'fit_mode': 'cover', 'crop_center': [.5, .5]},
    }
    cards = [{'id': identity, 'subject_id': 'sample', 'art': entry['path'], 'art_status': 'ready'}
             for identity in ('sample', 'sample-edition')]
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/cards.json').write_text(json.dumps({'cards': cards}))
    registry = {'format': 'kyberdex-reviewed-art-recipes', 'schema_version': 1,
                'assets': {card['id']: copy.deepcopy(entry) for card in cards}}
    sources = tmp_path / 'data/art-sources.json'
    sources.write_text(json.dumps(registry))
    return SimpleNamespace(root=tmp_path, raw=raw, entry=entry, registry=registry, sources=sources, cards=cards)


def write_registry(case):
    case.sources.write_text(json.dumps(case.registry))


def run(case, **kwargs):
    return art.setup_art(case.root, workers=1, progress=lambda message: None, **kwargs)


def test_setup_deduplicates_aliases_and_passes_existing_art_gate(artwork, monkeypatch):
    fetch = Mock(return_value=artwork.raw)
    monkeypatch.setattr(art, 'download', fetch)
    report = run(artwork)
    assert report == {'complete': True, 'definitions': 2, 'files': 1, 'installed': 1, 'reused': 0,
                      'failed': 0, 'errors': []}
    fetch.assert_called_once()
    manifest = ops._art_manifest(artwork.root)
    for card in artwork.cards:
        path = ops.verified_art_path(artwork.root, card, manifest)
        assert manifest[card['id']]['sha256'] == art.digest(path.read_bytes())
    assert manifest['sample']['sha256'] == manifest['sample-edition']['sha256']
    fetch.reset_mock(side_effect=True)
    fetch.side_effect = AssertionError('Verified artwork must not be downloaded again')
    assert run(artwork)['reused'] == 1
    fetch.assert_not_called()


@pytest.mark.parametrize('defect', ['raw', 'pixels'])
def test_hash_mismatch_never_registers_unreviewed_art(artwork, monkeypatch, defect):
    for entry in artwork.registry['assets'].values():
        entry['source_raw_sha256' if defect == 'raw' else 'pixel_sha256'] = '0' * 64
    write_registry(artwork)
    monkeypatch.setattr(art, 'download', lambda _: artwork.raw)
    report = run(artwork)
    assert not report['complete'] and report['failed'] == 1 and report['definitions'] == 0
    assert not (artwork.root / artwork.entry['path']).exists()
    assert ops._art_manifest(artwork.root) == {}
    state = json.loads((artwork.root / 'assets/art/manifest.json').read_text())
    assert state['setup'] == {'complete': False, 'failed': 1}


def test_interrupted_output_reuses_verified_raw_cache_then_repairs_manifest(artwork, monkeypatch):
    fetch = Mock(return_value=artwork.raw)
    monkeypatch.setattr(art, 'download', fetch)
    write = art.atomic_write
    def fail_output(path, data):
        if path.name == 'sample.png':
            raise OSError('simulated interrupted output')
        write(path, data)
    monkeypatch.setattr(art, 'atomic_write', fail_output)
    assert run(artwork)['failed'] == 1
    assert not (artwork.root / artwork.entry['path']).exists()
    fetch.reset_mock()
    fetch.side_effect = AssertionError('Resume should use pinned cached source bytes')
    monkeypatch.setattr(art, 'atomic_write', write)
    assert run(artwork)['complete']
    fetch.assert_not_called()
    assert len(ops._art_manifest(artwork.root)) == 2


def test_corrupt_local_file_and_raw_cache_are_redownloaded(artwork, monkeypatch):
    monkeypatch.setattr(art, 'download', lambda _: artwork.raw)
    assert run(artwork)['complete']
    (artwork.root / artwork.entry['path']).write_bytes(b'broken image')
    cache = artwork.root / '.cache/kyberdex-art/raw' / (artwork.entry['source_raw_sha256'] + '.blob')
    cache.write_bytes(b'broken cache')
    fetch = Mock(return_value=artwork.raw)
    monkeypatch.setattr(art, 'download', fetch)
    assert run(artwork)['installed'] == 1
    fetch.assert_called_once()


def test_failed_source_is_removed_from_existing_ready_manifest(artwork, monkeypatch):
    monkeypatch.setattr(art, 'download', lambda _: artwork.raw)
    assert run(artwork)['complete']
    (artwork.root / artwork.entry['path']).unlink()
    cache = artwork.root / '.cache/kyberdex-art/raw' / (artwork.entry['source_raw_sha256'] + '.blob')
    cache.unlink()
    monkeypatch.setattr(art, 'download', Mock(side_effect=OSError('offline')))
    assert not run(artwork)['complete']
    assert ops._art_manifest(artwork.root) == {}


@pytest.mark.parametrize('path', ['../outside.png', '/tmp/outside.png', 'assets/art/../../outside.png',
                                  'assets/art/subdir/sample.png', 'assets/art/sample.jpg'])
def test_path_rejection_precedes_download_or_writes(artwork, monkeypatch, path):
    artwork.registry['assets']['sample']['path'] = path
    write_registry(artwork)
    fetch = Mock(side_effect=AssertionError('No download for invalid registry'))
    monkeypatch.setattr(art, 'download', fetch)
    with pytest.raises(ValueError):
        run(artwork)
    fetch.assert_not_called()
    assert not (artwork.root / '.cache').exists()


def test_alias_recipe_drift_is_rejected(artwork):
    artwork.registry['assets']['sample-edition']['pixel_sha256'] = '0' * 64
    write_registry(artwork)
    with pytest.raises(ValueError, match='identical source recipes'):
        run(artwork)


def test_symlink_output_directory_is_rejected(artwork, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (artwork.root / 'assets').symlink_to(outside)
    with pytest.raises(ValueError, match='symlinks'):
        run(artwork)
    assert list(outside.iterdir()) == []


def test_incomplete_registry_cannot_claim_catalog_readiness(artwork):
    artwork.registry['assets'].pop('sample-edition')
    write_registry(artwork)
    with pytest.raises(ValueError, match='every catalog definition'):
        run(artwork)


def test_blocked_source_is_honest_and_does_not_request_network(artwork, monkeypatch):
    for entry in artwork.registry['assets'].values():
        entry.update(status='manual-source-required', source_image_url=None)
    write_registry(artwork)
    fetch = Mock(side_effect=AssertionError('No request for missing source'))
    monkeypatch.setattr(art, 'download', fetch)
    assert run(artwork)['failed'] == 1
    fetch.assert_not_called()


@pytest.mark.parametrize('url', ['http://lumiere-a.akamaihd.net/image.png', 'file:///tmp/image.png',
    'https://127.0.0.1/image.png', 'https://example.com/image.png',
    'https://user:password@lumiere-a.akamaihd.net/image.png',
    'https://lumiere-a.akamaihd.net:444/image.png', 'https://lumiere-a.akamaihd.net.evil.test/image.png'])
def test_source_host_restrictions_apply_before_redirect(url):
    with pytest.raises(ValueError, match='approved artwork host'):
        art.SafeRedirects().redirect_request(urllib.request.Request('https://lumiere-a.akamaihd.net/a'),
                                            None, 302, 'Found', {}, url)


class Response(io.BytesIO):
    def __init__(self, data, headers=None, url='https://lumiere-a.akamaihd.net/a'):
        super().__init__(data)
        self.headers = headers or {}
        self.url = url
    def geturl(self):
        return self.url


@pytest.mark.parametrize('headers', [{'Content-Length': '9999999999'}, {}])
def test_download_is_bounded_with_or_without_content_length(monkeypatch, headers):
    monkeypatch.setattr(art, 'MAX_DOWNLOAD', 32)
    opener = SimpleNamespace(open=lambda *_a, **_kw: Response(b'x' * 33, headers))
    monkeypatch.setattr(art.urllib.request, 'build_opener', lambda *_: opener)
    with pytest.raises(ValueError, match='size limit'):
        art.download('https://lumiere-a.akamaihd.net/a')


def test_download_checks_final_host_and_deadline(monkeypatch):
    opener = SimpleNamespace(open=lambda *_a, **_kw: Response(b'abc', url='https://127.0.0.1/a'))
    monkeypatch.setattr(art.urllib.request, 'build_opener', lambda *_: opener)
    with pytest.raises(ValueError, match='approved artwork host'):
        art.download('https://lumiere-a.akamaihd.net/a')
    opener.open = lambda *_a, **_kw: Response(b'abc')
    clock = iter([0, art.TRANSFER_TIMEOUT + 1])
    monkeypatch.setattr(art.time, 'monotonic', lambda: next(clock))
    with pytest.raises(ValueError, match='time limit'):
        art.download('https://lumiere-a.akamaihd.net/a')


def test_cli_reports_incomplete_without_starting_discord(artwork, monkeypatch, capsys):
    monkeypatch.setenv('VAULT_ROOT', str(artwork.root))
    monkeypatch.delenv('VAULT_GUILD_ID', raising=False)
    monkeypatch.setattr(art, 'download', Mock(side_effect=OSError('offline')))
    assert ops.main(['setup-art', '--workers', '1']) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)['complete'] is False
    assert 'Artwork failed' in output.err
    assert not list((artwork.root / 'state').glob('*.sqlite3'))


def test_atomic_write_leaves_no_temporary_files(artwork):
    path = artwork.root / 'assets/art/example.png'
    art.atomic_write(path, b'first')
    art.atomic_write(path, b'second')
    assert path.read_bytes() == b'second'
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize('field,value', [('path', None), ('width', None), ('pixel_sha256', None),
                                        ('source_raw_sha256', None)])
def test_malformed_registry_values_fail_before_any_download(artwork, field, value):
    artwork.registry['assets']['sample'][field] = value
    write_registry(artwork)
    with pytest.raises(ValueError):
        run(artwork)


def test_pdf_source_extracts_only_selected_embedded_image(artwork):
    from pypdf import PdfReader
    buffer = io.BytesIO()
    Image.new('RGB', (30, 20), (17, 42, 99)).save(buffer, format='PDF')
    raw = buffer.getvalue()
    reader = PdfReader(io.BytesIO(raw))
    name = reader.pages[0].images.keys()[0]
    entry = {**artwork.entry, 'source_format': 'pdf-image', 'pdf_page': 0, 'pdf_image_name': name}
    decoded = art.decode_source(raw, entry)
    assert decoded.mode == 'RGB' and decoded.size == (30, 20)
    assert all(abs(a - b) <= 2 for a, b in zip(decoded.getpixel((15, 10)), (17, 42, 99), strict=True))
    with pytest.raises(ValueError, match='selected page'):
        art.decode_source(raw, {**entry, 'pdf_page': 1})
    with pytest.raises(ValueError, match='could not be decoded'):
        art.decode_source(raw, {**entry, 'pdf_image_name': '/missing'})
    with pytest.raises(ValueError, match='could not be decoded'):
        art.decode_source(b'not a PDF', entry)


def test_setup_cli_refuses_to_modify_art_while_gateway_worker_is_locked(artwork, monkeypatch):
    monkeypatch.setenv('VAULT_ROOT', str(artwork.root))
    settings = ops.Settings.from_env()
    with ops.worker_lock(settings), pytest.raises(SystemExit, match='already running'):
        ops.main(['setup-art'])
    assert not (artwork.root / '.cache').exists()


def test_http_failure_reports_status_without_logging_request_details(artwork, monkeypatch):
    import urllib.error
    failure = urllib.error.HTTPError('https://example.test/private-query', 429, 'limit', {}, None)
    monkeypatch.setattr(art, 'download', Mock(side_effect=failure))
    report = run(artwork)
    assert report['errors'] == [{'id': 'sample', 'error': 'Source download returned HTTP 429.'}]


def test_official_contain_preserves_subject_and_uses_recorded_matte(artwork):
    entry = {**artwork.entry, 'width': 1024, 'height': 1536, 'normalization': {
        'output_size': [1024, 1536], 'requested_crop_box': [0, 0, 30, 20], 'fit_mode': 'contain',
        'placement': {'size': [1024, 683], 'xy': [0, 200], 'matte_rgb': [4, 12, 28]},
    }}
    image = art.transform(artwork.raw, entry)
    assert image.getpixel((512, 500)) == (17, 42, 99)
    assert image.getpixel((512, 100)) == image.getpixel((512, 1000)) == (4, 12, 28)
    entry['normalization']['placement']['size'] = [1024, 684]
    with pytest.raises(ValueError, match='placement differs'):
        art.transform(artwork.raw, entry)


def test_published_window_preserves_subject_with_dark_background_and_feather(artwork):
    entry = {**artwork.entry, 'width': 100, 'height': 150, 'recipe': 'published-window-v1',
             'normalization': {'canvas': [100, 150], 'crop_box': [0, 0, 30, 20], 'brightness': 1.0,
                               'foreground_window': [10, 40, 90, 100], 'placement_size': [80, 53],
                               'placement_xy': [10, 43], 'feather_px': 10}}
    image = art.transform(artwork.raw, entry)
    assert image.size == (100, 150)
    assert image.getpixel((50, 70)) == (17, 42, 99)
    assert image.getpixel((0, 0)) == image.getpixel((10, 43)) == (7, 19, 45)
    entry['normalization']['placement_xy'] = [10, 44]
    with pytest.raises(ValueError, match='placement differs'):
        art.transform(artwork.raw, entry)
