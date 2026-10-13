"""Read-only public catalog service. No player data, credentials or database access."""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from venator_vault.card_render import CardRenderer
from venator_vault.references import lore_references

CARD_FIELDS = frozenset(('id','name','aliases','rarity','category','arena','faction','era','role','attack','health','speed','ability','ability_name','ability_description','description','art','set','weight','release','release_id','continuity','event_id','availability','subject_type','subject_id'))
RARITY_FIELDS = frozenset(('label','weight','value','accent','power_multiplier','spawn_weight','collector_limit'))
POWER_PROFILE_FIELDS = frozenset(('combat', 'basis', 'rationale'))
POWER_PROFILE_BASES = frozenset(('curated_design', 'default_design', 'depiction_override'))
ASSET = re.compile(r'^assets/art/[a-z0-9][a-z0-9_-]*\.(?:png|webp|jpg|jpeg)$')
CARD_IMAGE = re.compile(r'^/cards/([a-z0-9][a-z0-9_-]*)\.png$')
RELEASE_KEY = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')
SPAWN_WEIGHTS_V3 = dict(zip(('common','uncommon','rare','epic','legendary','mythic'),
                          (5400,2200,1780,420,160,40)))
SPAWN_WEIGHTS_V3_REVISION_1 = dict(zip(SPAWN_WEIGHTS_V3, (5150,2500,1800,400,110,40)))
SPAWN_WEIGHTS_V3_REVISION_2 = dict(zip(SPAWN_WEIGHTS_V3, (5090,2500,1800,420,150,40)))


def contained_file(base: Path, relative: str) -> Path | None:
    """Reject symlinks/traversal escaping the one allowed directory."""
    root = base.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def public_power_profile(profile: object) -> dict:
    """Expose combat capability without legacy prominence or private metadata."""
    if not isinstance(profile, dict):
        raise TypeError('Invalid public power profile')
    result = {key: value for key, value in profile.items() if key in POWER_PROFILE_FIELDS}
    value = result.get('combat')
    if type(value) is not int or not 1 <= value <= 5:
        raise ValueError('Invalid public power rating')
    for field in ('basis', 'rationale'):
        if field in result and not isinstance(result[field], str):
            raise ValueError('Invalid public power explanation')
    if 'basis' in result and result['basis'] not in POWER_PROFILE_BASES:
        raise ValueError('Invalid public power basis')
    return result


def public_spawn_model(model: object) -> dict:
    """Publish only the versioned draw rule, never live counters or RNG state."""
    if not isinstance(model, dict):
        raise TypeError('Invalid public spawn model')
    version = model.get('schema_version')
    if type(version) is not int or version not in (2, 3):
        raise ValueError('Invalid public spawn schema')
    formula = {2:'tier-first-v2', 3:'tier-first-release-lanes-v3'}[version]
    if model.get('formula') != formula:
        raise ValueError('Invalid public spawn formula')
    result = {'schema_version': version, 'formula': formula}
    if version == 3:
        revision = model.get('rate_revision', 1)
        if type(revision) is not int or revision not in (1, 2, 3):
            raise ValueError('Invalid public spawn rate revision')
        result['rate_revision'] = revision
        share = model.get('limited_share_percent')
        if type(share) is not int or share != 25:
            raise ValueError('Invalid public seasonal allocation')
        result['limited_share_percent'] = share
    if 'description' in model:
        if not isinstance(model['description'], str):
            raise TypeError('Invalid public spawn description')
        result['description'] = model['description']
    return result


def public_catalog(project_root: Path, *, enabled_releases=()) -> dict:
    source = contained_file(project_root / 'data', 'cards.json')
    if source is None:
        raise FileNotFoundError('Catalog not ready')
    raw = json.loads(source.read_text(encoding='utf-8'))
    if not isinstance(raw, dict) or not isinstance(raw.get('cards'), list):
        raise TypeError('Invalid public catalog')
    if isinstance(enabled_releases, str):
        raise TypeError('Enabled releases must be a collection of release keys')
    enabled_releases = frozenset(enabled_releases)
    if any(not isinstance(key, str) or not RELEASE_KEY.fullmatch(key) for key in enabled_releases):
        raise ValueError('Invalid enabled release key')
    spawn_model = public_spawn_model(raw['spawn_model']) if 'spawn_model' in raw else None
    release_policy = raw.get('release_policy')
    if 'release_policy' in raw and (not isinstance(release_policy, dict)
            or type(release_policy.get('schema_version')) is not int
            or release_policy.get('schema_version') != 1
            or release_policy.get('activation') != 'explicit-release-keys'):
        raise ValueError('Invalid public release policy')
    require_release_key = bool(release_policy is not None or (spawn_model and spawn_model['schema_version'] == 3))
    raw_rarities = raw.get('rarities', {})
    if not isinstance(raw_rarities, dict):
        raise TypeError('Invalid public rarity definitions')
    cards = []
    available = []
    seen_ids = set()
    known_releases = set()
    for raw_card in raw['cards']:
        if not isinstance(raw_card, dict) or not isinstance(raw_card.get('id'), str):
            continue
        if raw_card['id'] in seen_ids:
            raise ValueError('Duplicate public card ID')
        seen_ids.add(raw_card['id'])
        card = {k:v for k,v in raw_card.items() if k in CARD_FIELDS}
        if 'power_profile' in raw_card:
            card['power_profile'] = public_power_profile(raw_card['power_profile'])
        event_only = (raw_card.get('event_id') or raw_card.get('event_only') or
                      raw_card.get('category') == 'legends' or
                      raw_card.get('availability') == 'event_only')
        if raw_card.get('event_only'):
            card['availability'] = 'event_only'
        if event_only and 'collector_limit' in raw_card:
            limit = raw_card['collector_limit']
            if type(limit) is not int or not 1 <= limit <= 6:
                raise ValueError('Invalid public collector limit')
            card['collector_limit'] = limit
        card['references'] = lore_references(raw_card)
        release = card.get('release')
        if isinstance(release,dict):
            card['release'] = {k:v for k,v in release.items() if k in ('starts_at','ends_at','supply_cap')}
        elif release is not None:
            raise ValueError('Invalid public release definition')
        release_id = raw_card.get('release_id')
        if 'release_id' in raw_card or (require_release_key and card.get('category') == 'limited'):
            if not isinstance(release_id, str) or not RELEASE_KEY.fullmatch(release_id) or not release:
                raise ValueError('Invalid public release key')
            known_releases.add(release_id)
            # Configuration status only: never expose minted/reserved supply or
            # accept a stored card's claimed activation status as authority.
            card['release_enabled'] = release_id in enabled_releases
        if raw_card.get('art_status') == 'ready' and ASSET.fullmatch(str(card.get('art',''))):
            relative = str(card['art']).removeprefix('assets/art/')
            if contained_file(project_root / 'assets' / 'art', relative):
                available.append(card['id'])
                if CARD_IMAGE.fullmatch(f"/cards/{card['id']}.png"):
                    card['card_image'] = f"/cards/{card['id']}.png"
        cards.append(card)
    if enabled_releases - known_releases:
        raise ValueError('Enabled release does not exist in the public catalog')
    rarities = {k:{field:value for field,value in v.items() if field in RARITY_FIELDS} for k,v in raw_rarities.items() if isinstance(v,dict)}
    expected_weights = (SPAWN_WEIGHTS_V3_REVISION_1
                        if spawn_model and spawn_model.get('rate_revision') == 1 else
                        SPAWN_WEIGHTS_V3_REVISION_2
                        if spawn_model and spawn_model.get('rate_revision') == 2 else SPAWN_WEIGHTS_V3)
    for key, rarity in rarities.items():
        if 'spawn_weight' in rarity and (type(rarity['spawn_weight']) is not int or rarity['spawn_weight'] <= 0):
            raise ValueError('Invalid public rarity spawn weight')
        if (spawn_model and spawn_model['schema_version'] == 3 and 'spawn_weight' in rarity
                and rarity['spawn_weight'] != expected_weights.get(key)):
            raise ValueError('Public rarity weight does not match the spawn model')
        if 'collector_limit' in rarity and (type(rarity['collector_limit']) is not int or
                                           not 1 <= rarity['collector_limit'] <= 6):
            raise ValueError('Invalid public rarity collector limit')
    result = {'schema_version':raw.get('schema_version',1),'rarities':rarities,'cards':cards,'public':{'served_at':datetime.now(UTC).isoformat(),'art_available':available,'rate_scope':'Conditional on release activation, date and available supply. Empty tiers and lanes are excluded. This public archive cannot inspect private minted/reserved stock; exhaustion may change actual odds.'}}
    if spawn_model is not None:
        result['spawn_model'] = spawn_model
    return result


def make_handler(project_root: Path, *, enabled_releases=()):
    project_root = Path(project_root).resolve()
    if isinstance(enabled_releases, str):
        raise TypeError('Enabled releases must be a collection of release keys')
    enabled_releases = tuple(enabled_releases)
    renderer = CardRenderer(project_root)
    class PublicHandler(BaseHTTPRequestHandler):
        server_version = 'KyberDex/1'
        sys_version = ''
        def log_message(self, *_args):
            pass  # Never log request query strings or arbitrary user text.
        def send_body(self, status, body, content_type, *, etag=None):
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            if status != 304:
                self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store' if content_type.startswith('application/json') else 'no-cache')
            if etag is not None:
                self.send_header('ETag', etag)
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)
        def failure(self,status,message):
            self.send_body(status,json.dumps({'error':message}).encode(),'application/json; charset=utf-8')
        def do_GET(self):
            request = urlsplit(self.path)
            path = unquote(request.path)
            if path in ('/api/catalog','/data/cards.json'):
                try:
                    data = public_catalog(project_root, enabled_releases=enabled_releases)
                except (OSError,ValueError,TypeError,KeyError):
                    self.failure(503,'Public catalog is not ready')
                    return
                self.send_body(200,json.dumps(data,ensure_ascii=False).encode(),'application/json; charset=utf-8')
                return
            card_match = CARD_IMAGE.fullmatch(path)
            if card_match:
                if request.query:
                    self.failure(404, 'Not found')
                    return
                try:
                    catalog = public_catalog(project_root, enabled_releases=enabled_releases)
                except (OSError, ValueError, TypeError, KeyError):
                    self.failure(503, 'Public catalog is not ready')
                    return
                card_id = card_match.group(1)
                card = next((card for card in catalog['cards'] if card['id'] == card_id), None)
                if card is None or card_id not in catalog['public']['art_available']:
                    self.failure(404, 'Not found')
                    return
                try:
                    # Current public readiness is checked before renderer cache or ETag.
                    body = renderer.render({**card, 'art_status': 'ready'})
                except (OSError, ValueError):
                    self.failure(404, 'Card image is unavailable')
                    return
                etag = '"' + hashlib.sha256(body).hexdigest() + '"'
                requested = {value.strip() for value in self.headers.get('If-None-Match', '').split(',')}
                if etag in requested or '*' in requested:
                    self.send_body(304, b'', 'image/png', etag=etag)
                    return
                self.send_body(200, body, 'image/png', etag=etag)
                return
            names = {'/':'index.html','/index.html':'index.html','/app.js':'app.js','/app.css':'app.css','/favicon.svg':'favicon.svg'}
            if path in names:
                file = contained_file(project_root / 'web', names[path])
            elif ASSET.fullmatch(path.lstrip('/')):
                try:
                    catalog = public_catalog(project_root, enabled_releases=enabled_releases)
                except (OSError, ValueError, TypeError, KeyError):
                    self.failure(503, 'Public catalog is not ready')
                    return
                ready = set(catalog['public']['art_available'])
                allowed = {card.get('art') for card in catalog['cards'] if card['id'] in ready}
                if path.removeprefix('/') not in allowed:
                    self.failure(404, 'Not found')
                    return
                file = contained_file(project_root / 'assets' / 'art',path.removeprefix('/assets/art/'))
            else:
                self.failure(404,'Not found')
                return
            if file is None:
                self.failure(404,'Not found')
                return
            mime = mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
            if mime.startswith(('text/','application/javascript')):
                mime += '; charset=utf-8'
            try:
                stat = file.stat()
                etag = f'W/"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
                requested = {value.strip() for value in self.headers.get('If-None-Match', '').split(',')}
                if etag in requested or '*' in requested:
                    self.send_body(304, b'', mime, etag=etag)
                    return
                self.send_body(200, file.read_bytes(), mime, etag=etag)
            except OSError:
                self.failure(404,'Not found')
        def reject_method(self):
            self.failure(405,'Only GET is supported')
        do_POST = reject_method
        do_PUT = reject_method
        do_PATCH = reject_method
        do_DELETE = reject_method
        do_OPTIONS = reject_method
        do_HEAD = reject_method
    return PublicHandler


class BoundedPublicServer(ThreadingHTTPServer):
    """Small worker budget and socket deadlines suit the Pi preview service."""
    daemon_threads = True
    def __init__(self, *args, max_workers=8, **kwargs):
        self._slots = threading.BoundedSemaphore(max_workers)
        super().__init__(*args, **kwargs)
    def get_request(self):
        request, address = super().get_request()
        request.settimeout(15)
        return request, address
    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise
    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def create_server(project_root: Path, host='127.0.0.1', port=8765, *, enabled_releases=()):
    server = BoundedPublicServer((host,port),make_handler(Path(project_root), enabled_releases=enabled_releases))
    server.daemon_threads = True
    return server


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[2])
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--enabled-releases',default=os.environ.get('VAULT_ENABLED_RELEASES',''),
                        help='Comma-separated seasonal release keys (default: VAULT_ENABLED_RELEASES)')
    args=parser.parse_args()
    enabled_releases=tuple(key.strip() for key in args.enabled_releases.split(',') if key.strip())
    server=create_server(args.root,args.host,args.port,enabled_releases=enabled_releases)
    print(f'Public catalog preview: http://{args.host}:{args.port}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__=='__main__':
    main()
