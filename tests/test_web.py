import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

MODULE = Path(__file__).resolve().parents[1] / 'src' / 'venator_vault' / 'web.py'
spec = importlib.util.spec_from_file_location('vault_public_web', MODULE)
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


class PublicWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for folder in ['web','data','assets/art']:
            (self.root / folder).mkdir(parents=True)
        (self.root / 'web/index.html').write_text('<h1>Public archive</h1>')
        (self.root / 'web/app.js').write_text('console.log("public");')
        (self.root / 'web/app.css').write_text('body{}')
        (self.root / 'web/favicon.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        (self.root / 'data/cards.json').write_text(json.dumps({'schema_version':1,'rarities':{'common':{'label':'Common','weight':100,'secret':'NO'}},'cards':[{'id':'clone','name':'Clone','art':'assets/art/clone.png','art_status':'ready','weight':100,'private_inventory':[111],'token':'NO','release':{'starts_at':'2026-01-01T00:00:00Z','ends_at':'2027-01-01T00:00:00Z','supply_cap':100,'reserved_by':'NO'}}],'players':[111],'credential':'NO'}))
        (self.root / 'assets/art/clone.png').write_bytes(b'PNGTEST')
        (self.root / 'secret.txt').write_text('TOPSECRET')
        self.server = web.create_server(self.root,port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()
    def request(self,path,method='GET',headers=None):
        try:
            response = urlopen(Request(self.url+path,method=method,headers=headers or {}),timeout=3)
        except HTTPError as response:
            with response:
                return response.code,response.read(),response.headers
        with response:
            return response.status,response.read(),response.headers
    def test_public_catalog_projects_definitions_only(self):
        code,body,headers=self.request('/api/catalog')
        self.assertEqual(code,200)
        data=json.loads(body)
        self.assertNotIn('players',data)
        self.assertNotIn('credential',data)
        self.assertNotIn('token',data['cards'][0])
        self.assertNotIn('private_inventory',data['cards'][0])
        self.assertNotIn('reserved_by',data['cards'][0]['release'])
        self.assertNotIn('secret',data['rarities']['common'])
        self.assertEqual(data['public']['art_available'],['clone'])
        self.assertEqual(data['cards'][0]['card_image'],'/cards/clone.png')
        self.assertEqual(headers['X-Content-Type-Options'],'nosniff')
    def test_only_fixed_public_paths_and_art_are_served(self):
        for path in ['/','/index.html','/app.css','/app.js','/favicon.svg','/assets/art/clone.png','/data/cards.json']:
            self.assertEqual(self.request(path)[0],200,path)
        for path in ['/secret.txt','/data/secret.txt','/src/venator_vault/core.py','/api/inventory','/assets/art/manifest.json','/../secret.txt','/%2e%2e/secret.txt','/assets/art/%2e%2e/%2e%2e/secret.txt','/%252e%252e/secret.txt']:
            code,body,_=self.request(path)
            self.assertEqual(code,404,path)
            self.assertNotIn(b'TOPSECRET',body)
    def test_reference_projection_excludes_credentials_and_unstructured_metadata(self):
        source = self.root/'data/cards.json'
        data = json.loads(source.read_text())
        data['cards'][0]['lore_sources'] = [
            'https://www.starwars.com/databank/clone-troopers',
            'https://user:TOPSECRET@www.starwars.com/private',
            'https://www.starwars.com/private?token=TOPSECRET',
            {'url':'https://www.starwars.com/private', 'token':'TOPSECRET'},
            'javascript:alert(1)',
        ]
        source.write_text(json.dumps(data))
        code, body, _ = self.request('/api/catalog')
        self.assertEqual(code, 200)
        card = json.loads(body)['cards'][0]
        self.assertEqual(card['references'], [{'url':'https://www.starwars.com/databank/clone-troopers',
                                               'label':'starwars.com'}])
        self.assertNotIn('lore_sources', card)
        self.assertNotIn(b'TOPSECRET', body)
    def test_power_profile_projects_only_public_ratings_and_explanation(self):
        source = self.root/'data/cards.json'
        data = json.loads(source.read_text())
        expected = {'combat': 5, 'basis': 'curated_design',
                    'rationale': 'Combat specialist with demonstrated Force abilities.'}
        data['cards'][0]['power_profile'] = {
            **expected, 'private_inventory': [111], 'token': 'TOPSECRET',
            'reviewer_notes': {'owner': 'TOPSECRET'},
        }
        source.write_text(json.dumps(data))
        code, body, _ = self.request('/api/catalog')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['cards'][0]['power_profile'], expected)
        self.assertNotIn(b'TOPSECRET', body)
        with patch.object(web.CardRenderer, 'render', autospec=True, return_value=b'PUBLIC') as render:
            self.assertEqual(self.request('/cards/clone.png')[0], 200)
            self.assertEqual(render.call_args.args[1]['power_profile'], expected)
        # Legacy prominence data is excluded even when it contains nested data.
        data['cards'][0]['power_profile']['importance'] = {'token': 'TOPSECRET'}
        source.write_text(json.dumps(data))
        code, body, _ = self.request('/api/catalog')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['cards'][0]['power_profile'], expected)
        self.assertNotIn(b'importance', body)
        self.assertNotIn(b'TOPSECRET', body)
    def test_power_ratings_require_integers_from_one_to_five(self):
        source = self.root/'data/cards.json'
        data = json.loads(source.read_text())
        for value in (0, 6, True, 2.5, '4', None):
            with self.subTest(value=value):
                data['cards'][0]['power_profile'] = {'combat': value}
                source.write_text(json.dumps(data))
                code, body, _ = self.request('/api/catalog')
                self.assertEqual(code, 503)
                self.assertEqual(json.loads(body)['error'], 'Public catalog is not ready')
    def test_nested_power_explanations_and_malformed_profiles_are_withheld(self):
        source = self.root/'data/cards.json'
        data = json.loads(source.read_text())
        profiles = [None, [], 'TOPSECRET', {},
                    {'combat': 3, 'basis': {'token': 'TOPSECRET'}},
                    {'combat': 3, 'rationale': ['TOPSECRET']},
                    {'combat': 3, 'basis': 'TOPSECRET'}]
        for profile in profiles:
            with self.subTest(profile=profile):
                data['cards'][0]['power_profile'] = profile
                source.write_text(json.dumps(data))
                code, body, _ = self.request('/api/catalog')
                self.assertEqual(code, 503)
                self.assertNotIn(b'TOPSECRET', body)
    def test_spawn_projection_excludes_live_supply_and_random_state(self):
        source = self.root/'data/cards.json'
        data = json.loads(source.read_text())
        data['spawn_model'] = {'schema_version': 2, 'formula': 'tier-first-v2',
                               'description': 'Draw a rarity, then a card in that tier.',
                               'random_state': 'TOPSECRET', 'reserved_supply': {'clone': 50}}
        data['rarities']['common'].update(spawn_weight=5800, collector_limit=2,
                                         live_captures={'player': 'TOPSECRET'})
        source.write_text(json.dumps(data))
        code, body, _ = self.request('/api/catalog')
        self.assertEqual(code, 200)
        public = json.loads(body)
        self.assertEqual(public['spawn_model'], {
            'schema_version': 2, 'formula': 'tier-first-v2',
            'description': 'Draw a rarity, then a card in that tier.'})
        self.assertEqual(public['rarities']['common']['spawn_weight'], 5800)
        self.assertEqual(public['rarities']['common']['collector_limit'], 2)
        self.assertNotIn(b'TOPSECRET', body)
        self.assertNotIn(b'reserved_supply', body)
    def lane_catalog(self):
        data = {'schema_version': 1,
                'spawn_model': {'schema_version': 3, 'formula': 'tier-first-release-lanes-v3',
                                'rate_revision': 2, 'limited_share_percent': 25, 'random_state': 'TOPSECRET'},
                'rarities': {'common': {'label': 'Common', 'spawn_weight': 5090, 'collector_limit': 2}},
                'cards': [{'id': 'ordinary', 'name': 'Ordinary', 'rarity': 'common',
                           'category': 'canon', 'weight': 120, 'release': None},
                          {'id': 'edition', 'name': 'Edition', 'rarity': 'common',
                           'category': 'limited', 'weight': 120, 'release_id': 'autumn-2026',
                           'release_enabled': True, 'minted': 7, 'reserved': 'TOPSECRET',
                           'release': {'starts_at': '2026-01-01T00:00:00Z',
                                       'ends_at': '2027-01-01T00:00:00Z', 'supply_cap': 180}}]}
        (self.root/'data/cards.json').write_text(json.dumps(data))
        return data
    def test_v3_activation_is_configuration_only_and_default_disabled(self):
        self.lane_catalog()
        code, body, _ = self.request('/api/catalog')
        self.assertEqual(code, 200)
        public = json.loads(body)
        self.assertEqual(public['spawn_model'], {'schema_version': 3,
                         'formula': 'tier-first-release-lanes-v3', 'rate_revision': 2, 'limited_share_percent': 25})
        self.assertIs(public['cards'][1]['release_enabled'], False)
        self.assertNotIn('release_enabled', public['cards'][0])
        self.assertEqual(public['cards'][1]['release_id'], 'autumn-2026')
        self.assertNotIn(b'TOPSECRET', body)
        self.assertNotIn('minted', public['cards'][1])
        self.assertNotIn('reserved', public['cards'][1])
        self.assertNotIn('enabled_releases', public)
        self.assertIn('cannot inspect', public['public']['rate_scope'])
        enabled = web.public_catalog(self.root, enabled_releases=('autumn-2026',))
        self.assertIs(enabled['cards'][1]['release_enabled'], True)
    def test_v3_enabled_release_is_forwarded_through_http_server(self):
        self.lane_catalog()
        server = web.create_server(self.root, port=0, enabled_releases=('autumn-2026',))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f'http://127.0.0.1:{server.server_port}/api/catalog', timeout=3) as response:
                self.assertIs(json.load(response)['cards'][1]['release_enabled'], True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    def test_v3_rejects_malformed_share_release_keys_and_wrong_weights(self):
        source = self.root/'data/cards.json'
        for share in (None, True, 0, 10, 25.0, '25', {'token':'TOPSECRET'}):
            with self.subTest(share=share):
                data = self.lane_catalog()
                data['spawn_model']['limited_share_percent'] = share
                source.write_text(json.dumps(data))
                self.assertEqual(self.request('/api/catalog')[0], 503)
        for key in (None, True, '', ' ', '../autumn', 'autumn 2026', {'token':'TOPSECRET'}):
            with self.subTest(key=key):
                data = self.lane_catalog()
                data['cards'][1]['release_id'] = key
                source.write_text(json.dumps(data))
                code, body, _ = self.request('/api/catalog')
                self.assertEqual(code, 503)
                self.assertNotIn(b'TOPSECRET', body)
        data = self.lane_catalog()
        del data['cards'][1]['release_id']
        source.write_text(json.dumps(data))
        self.assertEqual(self.request('/api/catalog')[0], 503)
        data = self.lane_catalog()
        data['rarities']['common']['spawn_weight'] = 5800
        source.write_text(json.dumps(data))
        self.assertEqual(self.request('/api/catalog')[0], 503)
    def test_enabled_release_arguments_are_strict_and_unknown_keys_fail(self):
        self.lane_catalog()
        for enabled in ('autumn-2026', ('typo',), (True,), (' autumn-2026 ',), ('',)):
            with self.subTest(enabled=enabled), self.assertRaises((TypeError, ValueError)):
                web.public_catalog(self.root, enabled_releases=enabled)
    def test_legacy_keyed_releases_gate_but_unkeyed_schema2_stays_compatible(self):
        data = self.lane_catalog()
        data['spawn_model'] = {'schema_version': 2, 'formula': 'tier-first-v2'}
        data['rarities']['common']['spawn_weight'] = 5800
        source = self.root/'data/cards.json'
        source.write_text(json.dumps(data))
        self.assertIs(web.public_catalog(self.root)['cards'][1]['release_enabled'], False)
        del data['cards'][1]['release_id']
        source.write_text(json.dumps(data))
        self.assertNotIn('release_enabled', web.public_catalog(self.root)['cards'][1])
    def test_web_cli_reads_explicit_release_environment(self):
        with patch.dict(web.os.environ, {'VAULT_ENABLED_RELEASES':'autumn-2026, winter-2026'}), \
             patch('sys.argv', ['web', '--root', str(self.root), '--port', '0']), \
             patch.object(web, 'create_server') as create:
            create.return_value.serve_forever.side_effect = KeyboardInterrupt
            web.main()
            self.assertEqual(create.call_args.kwargs['enabled_releases'], ('autumn-2026','winter-2026'))
            create.return_value.server_close.assert_called_once()
    def test_malformed_spawn_metadata_is_unavailable(self):
        source = self.root/'data/cards.json'
        data = json.loads(source.read_text())
        valid = {'schema_version': 2, 'formula': 'tier-first-v2'}
        for model in [None, [], {**valid, 'schema_version': True}, {**valid, 'formula': 'unknown'},
                      {**valid, 'description': {'token': 'TOPSECRET'}}]:
            with self.subTest(model=model):
                data['spawn_model'] = model
                source.write_text(json.dumps(data))
                code, body, _ = self.request('/api/catalog')
                self.assertEqual(code, 503)
                self.assertNotIn(b'TOPSECRET', body)
        data['spawn_model'] = valid
        for field, values in [('spawn_weight', [0, -1, True, 2.5, {'token': 'TOPSECRET'}]),
                              ('collector_limit', [0, 7, True, 2.5, {'token': 'TOPSECRET'}])]:
            for value in values:
                with self.subTest(field=field, value=value):
                    data['rarities']['common'] = {field: value}
                    source.write_text(json.dumps(data))
                    code, body, _ = self.request('/api/catalog')
                    self.assertEqual(code, 503)
                    self.assertNotIn(b'TOPSECRET', body)
    def test_collector_override_is_public_only_for_explicit_events(self):
        source = self.root/'data/cards.json'
        data = json.loads(source.read_text())
        card = data['cards'][0]
        card.update(collector_limit=6, category='limited', availability='seasonal')
        source.write_text(json.dumps(data))
        self.assertNotIn('collector_limit', json.loads(self.request('/api/catalog')[1])['cards'][0])
        card['event_id'] = 'test-event'
        source.write_text(json.dumps(data))
        self.assertEqual(json.loads(self.request('/api/catalog')[1])['cards'][0]['collector_limit'], 6)
        del card['event_id']
        card['event_only'] = True
        source.write_text(json.dumps(data))
        projected = json.loads(self.request('/api/catalog')[1])['cards'][0]
        self.assertEqual(projected['availability'], 'event_only')
        self.assertEqual(projected['collector_limit'], 6)
        for limit in (0, 7, True, 2.5, '2'):
            with self.subTest(limit=limit):
                card['collector_limit'] = limit
                source.write_text(json.dumps(data))
                self.assertEqual(self.request('/api/catalog')[0], 503)
    def test_art_symlink_cannot_escape_asset_directory(self):
        (self.root / 'assets/art/leak.png').symlink_to(self.root/'secret.txt')
        code,body,_=self.request('/assets/art/leak.png')
        self.assertEqual(code,404)
        self.assertNotIn(b'TOPSECRET',body)
    def test_mutating_methods_rejected(self):
        for method in ['POST','PUT','PATCH','DELETE','HEAD','OPTIONS']:
            self.assertEqual(self.request('/api/catalog',method)[0],405,method)
    def test_static_art_revalidates_without_retransferring_unchanged_bytes(self):
        code,body,headers=self.request('/assets/art/clone.png')
        self.assertEqual((code,body),(200,b'PNGTEST'))
        etag=headers['ETag']
        code,body,headers=self.request('/assets/art/clone.png',headers={'If-None-Match':etag})
        self.assertEqual((code,body),(304,b''))
        self.assertEqual(headers['ETag'],etag)
        (self.root/'assets/art/clone.png').write_bytes(b'REPLACED ORIGINAL ART')
        code,body,headers=self.request('/assets/art/clone.png',headers={'If-None-Match':etag})
        self.assertEqual((code,body),(200,b'REPLACED ORIGINAL ART'))
        self.assertNotEqual(headers['ETag'],etag)
        self.assertEqual(self.request('/api/catalog')[2]['Cache-Control'],'no-store')
    def test_missing_catalog_is_honest_unavailable(self):
        (self.root/'data/cards.json').unlink()
        code,body,_=self.request('/api/catalog')
        self.assertEqual(code,503)
        self.assertEqual(json.loads(body)['error'],'Public catalog is not ready')
        self.assertEqual(self.request('/assets/art/clone.png')[0],503)
    def test_revoked_art_is_withheld_even_if_old_bytes_and_etag_remain(self):
        code,body,headers=self.request('/assets/art/clone.png')
        self.assertEqual((code,body),(200,b'PNGTEST'))
        etag=headers['ETag']
        source=self.root/'data/cards.json'
        data=json.loads(source.read_text())
        for status in ['pending','visual_qa_rejected','generation_failed',None]:
            with self.subTest(status=status):
                data['cards'][0]['art_status']=status
                source.write_text(json.dumps(data))
                self.assertEqual(json.loads(self.request('/api/catalog')[1])['public']['art_available'],[])
                code,body,_=self.request('/assets/art/clone.png',headers={'If-None-Match':etag})
                self.assertEqual(code,404)
                self.assertNotIn(b'PNGTEST',body)
        data['cards'][0]['art_status']='ready'
        source.write_text(json.dumps(data))
        self.assertEqual(self.request('/assets/art/clone.png')[0],200)
    def test_unlisted_art_file_is_not_public(self):
        (self.root/'assets/art/orphan.png').write_bytes(b'UNREVIEWED')
        code,body,_=self.request('/assets/art/orphan.png')
        self.assertEqual(code,404)
        self.assertNotIn(b'UNREVIEWED',body)
    def test_duplicate_card_id_cannot_authorize_a_withdrawn_path(self):
        source=self.root/'data/cards.json'
        data=json.loads(source.read_text())
        data['cards'].append({'id':'clone','art':'assets/art/retired.png','art_status':'visual_qa_rejected'})
        source.write_text(json.dumps(data))
        (self.root/'assets/art/retired.png').write_bytes(b'WITHDRAWN')
        for path in ['/api/catalog','/assets/art/clone.png','/assets/art/retired.png','/cards/clone.png']:
            code,body,_=self.request(path)
            self.assertEqual(code,503,path)
            self.assertNotIn(b'WITHDRAWN',body)

    def test_composed_card_route_is_public_only_and_reauthorizes_before_etag(self):
        with patch.object(web.CardRenderer, 'render', autospec=True, return_value=b'COMPOSED-PUBLIC-CARD') as render:
            code, body, headers = self.request('/cards/clone.png')
            self.assertEqual((code, body), (200, b'COMPOSED-PUBLIC-CARD'))
            self.assertEqual(headers['Content-Type'], 'image/png')
            self.assertEqual(headers['Cache-Control'], 'no-cache')
            renderer, card = render.call_args.args
            self.assertEqual(card['id'], 'clone')
            self.assertEqual(card['art_status'], 'ready')
            self.assertNotIn('private_inventory', card)
            self.assertNotIn('token', card)
            self.assertNotIn('reserved_by', card['release'])
            etag = headers['ETag']
            self.assertEqual(self.request('/cards/clone.png', headers={'If-None-Match': etag})[:2], (304, b''))
            self.assertIs(render.call_args.args[0], renderer)
            source = self.root / 'data/cards.json'
            data = json.loads(source.read_text())
            for status in ('pending', 'visual_qa_rejected', 'generation_failed', None):
                with self.subTest(status=status):
                    data['cards'][0]['art_status'] = status
                    source.write_text(json.dumps(data))
                    projected = json.loads(self.request('/api/catalog')[1])
                    self.assertNotIn('card_image', projected['cards'][0])
                    self.assertEqual(self.request('/cards/clone.png', headers={'If-None-Match': etag})[0], 404)
                    self.assertEqual(self.request('/cards/clone.png', headers={'If-None-Match': '*'})[0], 404)
            self.assertEqual(render.call_count, 2)

    def test_composed_card_route_rejects_unlisted_paths_and_custom_payloads(self):
        with patch.object(web.CardRenderer, 'render', autospec=True, return_value=b'COMPOSED-PUBLIC-CARD') as render:
            for path in ('/cards/orphan.png', '/cards/%2e%2e/secret.png', '/cards/clone.png?user_id=111',
                         '/cards/clone.png?rarity=mythic', '/cards/clone.png?style=retro', '/cards/cache/private.png'):
                self.assertEqual(self.request(path)[0], 404, path)
            render.assert_not_called()

    def test_composed_card_render_failure_is_honest_and_does_not_disclose_error(self):
        with patch.object(web.CardRenderer, 'render', autospec=True, side_effect=ValueError('TOPSECRET path')):
            code, body, _ = self.request('/cards/clone.png')
            self.assertEqual(code, 404)
            self.assertNotIn(b'TOPSECRET', body)
            self.assertEqual(json.loads(body)['error'], 'Card image is unavailable')
    def test_data_symlink_cannot_escape_data_directory(self):
        (self.root/'data/cards.json').unlink()
        (self.root/'data/cards.json').symlink_to(self.root/'secret.txt')
        self.assertEqual(self.request('/api/catalog')[0],503)
    def test_malformed_nested_definitions_return_unavailable(self):
        source=self.root/'data/cards.json'
        data=json.loads(source.read_text())
        data['rarities']=[]
        source.write_text(json.dumps(data))
        self.assertEqual(self.request('/api/catalog')[0],503)
        data['rarities']={}
        data['cards'][0]['release']=['not','public','release','metadata']
        source.write_text(json.dumps(data))
        self.assertEqual(self.request('/api/catalog')[0],503)

if __name__=='__main__':
    unittest.main()
