"""Interactive setup uses synthetic identities, local state and mocked REST only."""
import io
import json
import stat
import urllib.error
from dataclasses import replace
from unittest.mock import Mock

import pytest

from venator_vault import __main__ as ops
from venator_vault import art_setup
from venator_vault import setup as wizard
from venator_vault.config import Settings, load_profile, profile_directory
from venator_vault.runtime import Runtime

TOKEN = 'fixture-only-not-a-real-credential-' + 'x' * 30


class Dialogue:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.questions = []
        self.lines = []
    def prompt(self, text):
        self.questions.append(text)
        return next(self.answers)
    def output(self, text):
        self.lines.append(text)


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path / 'checkout'
    root.mkdir()
    monkeypatch.setenv('VAULT_ROOT', str(root))
    for key in ('VAULT_GUILD_ID', 'DISCORD_TOKEN_FILE', 'VAULT_ACTIVITY_CHANNEL_IDS', 'VAULT_DATABASE'):
        monkeypatch.delenv(key, raising=False)
    settings = Settings.from_env()
    guild = {'id': '42', 'name': 'Example Guild'}
    member = {'user': {'id': '100'}, 'roles': ['6']}
    roles = [{'id': '42', 'permissions': str(wizard.VIEW)},
             {'id': '6', 'permissions': str(wizard.BOT_PERMISSIONS)}]
    channels = [
        {'id': '10', 'name': 'cards', 'position': 0, 'type': 0, 'permission_overwrites': []},
        {'id': '11', 'name': 'general', 'position': 1, 'type': 0, 'permission_overwrites': []},
        {'id': '12', 'name': 'private', 'position': 2, 'type': 0,
         'permission_overwrites': [{'id': '42', 'type': 0, 'deny': str(wizard.VIEW), 'allow': '0'}]},
        {'id': '13', 'name': 'forum', 'position': 3, 'type': 15, 'permission_overwrites': []},
    ]
    responses = {'/users/@me': {'id': '100', 'bot': True, 'username': 'Example Bot'},
                 '/guilds/42': guild, '/guilds/42/members/100': member,
                 '/guilds/42/roles': roles, '/guilds/42/channels': channels}
    def request(path, token):
        assert token == TOKEN
        return responses[path]
    fetch = Mock(side_effect=request)
    monkeypatch.setattr(art_setup, 'setup_art', Mock(return_value={'complete': True, 'failed': 0}))
    gates = dict.fromkeys(('launch_art_ready', 'launch_catalog_ready', 'launch_presentation_ready',
                          'launch_balance_ready', 'launch_economy_ready', 'launch_storage_ready'), True)
    def readiness(selected, require_token):
        assert require_token
        assert ops.token_permissions(selected.token_file, required=True)['safe_permissions']
        return gates
    monkeypatch.setattr(ops, 'readiness', Mock(side_effect=readiness))
    return settings, responses, fetch


def setup(case, answers, **kwargs):
    settings, _, request = case
    dialog = Dialogue(answers)
    result = wizard.run_setup(settings, prompt=dialog.prompt, hidden=lambda: TOKEN,
                              output=dialog.output, request=request, **kwargs)
    return result, dialog


def test_first_setup_saves_private_profile_configures_runtime_and_defaults_to_start(case):
    settings, _, request = case
    result, dialog = setup(case, ['42', '', '', '', ''])
    assert result == {'ready': True, 'start': True}
    selected = Settings.from_env()
    assert selected.guild_id == 42 and selected.activity_channel_ids == (10,)
    assert selected.token_file.is_relative_to(profile_directory(settings.root))
    assert not selected.token_file.is_relative_to(settings.root)
    assert selected.read_token() == TOKEN
    assert stat.S_IMODE(selected.token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(selected.token_file.parent.stat().st_mode) == 0o700
    profile = profile_directory(settings.root) / 'settings.json'
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    assert TOKEN not in profile.read_text() and TOKEN not in '\n'.join(dialog.lines + dialog.questions)
    state = Runtime(selected.database.with_name('runtime.sqlite3')).get('42')
    assert state['channel_id'] == '10' and state['configured_by'] == 'local-setup'
    assert state['paused'] == 0
    assert all('#private' not in line and '#forum' not in line for line in dialog.lines)
    assert any('./start.sh' in line for line in dialog.lines)
    assert request.call_count == 5


def test_rerun_keeps_token_paused_state_and_next_deadline(case):
    setup(case, ['42', '', '', ''], no_start=True)
    selected = Settings.from_env()
    runtime = Runtime(selected.database.with_name('runtime.sqlite3'))
    runtime.pause('42', 'fixture', True)
    before = runtime.get('42')
    secret = Mock(side_effect=AssertionError('Existing token should be reused'))
    dialog = Dialogue(['', '', '', '', ''])
    result = wizard.run_setup(selected, prompt=dialog.prompt, hidden=secret, output=dialog.output,
                              request=case[2], no_start=True)
    assert result == {'ready': True, 'start': False}
    assert Runtime(runtime.path).get('42') == before
    assert any('remain paused' in line for line in dialog.lines)
    assert Settings.from_env().token_file == selected.token_file
    assert len(list(selected.token_file.parent.glob('bot-token-*'))) == 1
    secret.assert_not_called()


def test_selecting_channels_by_number_and_id_persists_selection(case):
    result, _ = setup(case, ['42', '2', '10', ''], no_start=True)
    assert result['ready']
    selected = Settings.from_env()
    assert selected.activity_channel_ids == (10,)
    assert Runtime(selected.database.with_name('runtime.sqlite3')).get('42')['channel_id'] == '11'


def test_invalid_token_and_server_id_retry_without_echoing_input(case):
    settings, responses, _ = case
    tokens = iter(['short', TOKEN, TOKEN])
    calls = 0
    def request(path, token):
        nonlocal calls
        assert token == TOKEN
        calls += 1
        if calls == 1:
            raise wizard.SetupError('Discord rejected that bot token.', 401)
        return responses[path]
    dialog = Dialogue(['https://invite.example', '42', '', '', ''])
    result = wizard.run_setup(settings, prompt=dialog.prompt, hidden=lambda: next(tokens),
                              output=dialog.output, request=request, no_start=True)
    assert result['ready']
    assert TOKEN not in '\n'.join(dialog.lines)
    assert any('token format is invalid' in line for line in dialog.lines)
    assert any('numeric server ID' in line for line in dialog.lines)


def test_guild_absent_provides_minimal_invite_and_rechecks_membership(case):
    settings, responses, _ = case
    attempts = 0
    def request(path, token):
        nonlocal attempts
        if path == '/guilds/42':
            attempts += 1
            if attempts == 1:
                raise wizard.SetupError('No access.', 403)
        return responses[path]
    dialog = Dialogue(['42', '', '', '', '', ''])
    result = wizard.run_setup(settings, prompt=dialog.prompt, hidden=lambda: TOKEN,
                              output=dialog.output, request=request, no_start=True)
    assert result['ready'] and attempts == 2
    url = next(line for line in dialog.lines if line.startswith('https://discord.com/oauth2/authorize?'))
    assert 'permissions=117760' in url and 'guild_id=42' in url and 'client_id=100' in url
    assert 'applications.commands' in url and 'disable_guild_select=true' in url
    assert TOKEN not in url


def test_missing_permissions_refuses_save_and_artwork(case):
    settings, responses, _ = case
    responses['/guilds/42/roles'][1]['permissions'] = str(wizard.VIEW)
    with pytest.raises(wizard.SetupError, match='No public text channel'):
        setup(case, ['42'])
    assert load_profile(settings.root) == {}
    art_setup.setup_art.assert_not_called()


def test_art_failure_keeps_verified_setup_for_resume_without_start(case):
    settings, _, _ = case
    art_setup.setup_art.return_value = {'complete': False, 'failed': 2}
    result, dialog = setup(case, ['42', '', '', ''])
    assert result == {'ready': False, 'start': False}
    assert Settings.from_env().read_token() == TOKEN
    assert load_profile(settings.root)['encounter_channel_id'] == '10'
    assert any('has not started' in line for line in dialog.lines)
    ops.readiness.assert_not_called()


def test_cancel_save_does_not_persist_token_or_profile(case):
    settings, _, _ = case
    with pytest.raises(wizard.SetupError, match='cancelled'):
        setup(case, ['42', '', '', 'n'])
    assert load_profile(settings.root) == {}
    assert not profile_directory(settings.root).exists()
    art_setup.setup_art.assert_not_called()


@pytest.mark.parametrize('key,value', [('VAULT_GUILD_ID', '43'), ('VAULT_ACTIVITY_CHANNEL_IDS', '11'),
                                      ('DISCORD_TOKEN_FILE', '/nonexistent/old-token')])
def test_conflicting_environment_cannot_launch_different_setup(case, monkeypatch, key, value):
    settings, _, _ = case
    monkeypatch.setenv(key, value)
    with pytest.raises(wizard.SetupError, match='Unset ' + key):
        setup(case, ['42', '', ''])
    assert load_profile(settings.root) == {}
    art_setup.setup_art.assert_not_called()


def test_profile_load_merges_explicit_environment_and_never_reads_token(case, monkeypatch):
    setup(case, ['42', '', '', ''], no_start=True)
    original = Settings.from_env()
    monkeypatch.setenv('VAULT_GUILD_ID', '43')
    monkeypatch.setenv('VAULT_ACTIVITY_CHANNEL_IDS', '11, 11')
    monkeypatch.setattr(Settings, 'read_token', Mock(side_effect=AssertionError('Do not read token during config')))
    selected = Settings.from_env()
    assert selected.guild_id == 43 and selected.activity_channel_ids == (11,)
    assert selected.token_file == original.token_file


def test_foreground_start_only_after_setup_worker_lock_released(case, monkeypatch):
    settings, _, _ = case
    called = []
    def fake_setup(configured, **kwargs):
        with pytest.raises(SystemExit, match='already running'):
            ops.worker_lock(configured)
        called.append('setup')
        return {'ready': True, 'start': True}
    monkeypatch.setattr(wizard, 'run_setup', fake_setup)
    # Stop at the run configuration boundary; its normal locked preflight remains intact.
    def require(configured):
        with ops.worker_lock(settings):
            called.append('run')
        raise ValueError('controlled launch stop')
    monkeypatch.setattr(Settings, 'require_discord_configuration', require)
    assert ops.main(['setup']) == 1
    assert called == ['setup', 'run']


def test_changing_server_with_existing_game_database_is_refused(case):
    setup(case, ['42', '', '', ''], no_start=True)
    selected = Settings.from_env()
    selected.database.touch()
    previous = load_profile(selected.root)
    dialog = Dialogue(['', '43'])
    with pytest.raises(wizard.SetupError, match='separate checkout'):
        wizard.run_setup(selected, prompt=dialog.prompt, output=dialog.output, request=case[2], no_start=True)
    assert load_profile(selected.root) == previous


def test_token_replacement_requires_choice_and_preserves_previous_profile(case):
    setup(case, ['42', '', '', ''], no_start=True)
    selected = Settings.from_env()
    dialog = Dialogue(['n', '', '', '', ''])
    wizard.run_setup(selected, prompt=dialog.prompt, hidden=lambda: TOKEN, output=dialog.output,
                     request=case[2], no_start=True)
    changed = Settings.from_env()
    assert changed.token_file != selected.token_file
    assert selected.token_file.read_text().strip() == TOKEN
    previous = json.loads((profile_directory(selected.root) / 'settings.previous.json').read_text())
    assert previous['settings']['DISCORD_TOKEN_FILE'] == str(selected.token_file)


def test_permission_order_public_filter_and_member_denies():
    roles = [{'id': '42', 'permissions': str(wizard.VIEW)},
             {'id': '6', 'permissions': str(wizard.BOT_PERMISSIONS)},
             {'id': '7', 'permissions': '0'}]
    channel = {'permission_overwrites': [
        {'type': 0, 'id': '42', 'allow': '0', 'deny': str(wizard.SEND)},
        {'type': 0, 'id': '6', 'allow': '0', 'deny': str(wizard.ATTACH)},
        {'type': 0, 'id': '7', 'allow': str(wizard.SEND | wizard.ATTACH), 'deny': '0'},
        {'type': 1, 'id': '100', 'allow': '0', 'deny': str(wizard.EMBED)}]}
    value = wizard.permissions('42', roles, ['6', '7'], channel, '100')
    assert value & wizard.SEND and value & wizard.ATTACH and not value & wizard.EMBED
    assert wizard.permissions('42', roles, [], channel) & wizard.VIEW
    roles[1]['permissions'] = str(wizard.ADMIN)
    assert wizard.permissions('42', roles, ['6'], channel, '100') & wizard.BOT_PERMISSIONS == wizard.BOT_PERMISSIONS


def test_saved_profile_bad_permissions_and_symlinks_fail_closed(case):
    setup(case, ['42', '', '', ''], no_start=True)
    root = case[0].root
    path = profile_directory(root) / 'settings.json'
    path.chmod(0o644)
    with pytest.raises(ValueError, match='private regular'):
        Settings.from_env()
    path.chmod(0o600)
    other = path.with_name('real-settings.json')
    path.rename(other)
    path.symlink_to(other)
    with pytest.raises(ValueError, match='symlinks'):
        Settings.from_env()


def test_config_directory_inside_checkout_is_rejected(case, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(case[0].root / '.config'))
    with pytest.raises(ValueError, match='outside the checkout'):
        Settings.from_env()


def test_hidden_input_requires_terminal_and_refuses_getpass_echo_fallback(monkeypatch):
    monkeypatch.setattr(wizard.sys, 'stdin', Mock(isatty=lambda: False))
    with pytest.raises(wizard.SetupError, match='interactive terminal'):
        wizard.hidden_token()
    monkeypatch.setattr(wizard.sys, 'stdin', Mock(isatty=lambda: True))
    def no_hidden(_prompt):
        import warnings
        warnings.warn('unsafe echo', wizard.getpass.GetPassWarning)
        pytest.fail('Warnings must abort before fallback reads a token')
    monkeypatch.setattr(wizard.getpass, 'getpass', no_hidden)
    with pytest.raises(wizard.SetupError, match='Hidden token entry'):
        wizard.hidden_token()


def test_credentialed_redirects_are_refused():
    with pytest.raises(wizard.SetupError, match='redirected'):
        wizard.NoRedirect().redirect_request(None, None, 302, 'Found', {}, 'https://other.example')


def test_http_error_never_exposes_token_or_body(monkeypatch):
    failure = urllib.error.HTTPError('https://discord.com/api/v10/users/@me', 401, TOKEN, {}, io.BytesIO(TOKEN.encode()))
    opener = Mock()
    opener.open.side_effect = failure
    monkeypatch.setattr(wizard.urllib.request, 'build_opener', lambda *_: opener)
    with pytest.raises(wizard.SetupError) as captured:
        wizard.discord_get('/users/@me', TOKEN)
    assert captured.value.status == 401 and TOKEN not in str(captured.value)


@pytest.mark.parametrize('route', ['https://evil.example/api', '//evil.example', '/users/@me?token=x',
                                   '/guilds/42/messages', '/guilds/42/../43'])
def test_setup_rest_rejects_unapproved_routes_before_constructing_request(monkeypatch, route):
    opener = Mock(side_effect=AssertionError('No HTTP client for unapproved routes'))
    monkeypatch.setattr(wizard.urllib.request, 'build_opener', opener)
    with pytest.raises(wizard.SetupError, match='approved read-only'):
        wizard.discord_get(route, TOKEN)
    opener.assert_not_called()


def test_saved_interval_env_conflict_is_explicit(case, monkeypatch):
    selected = replace(case[0], token_file=profile_directory(case[0].root) / 'token',
                       guild_id=42, activity_channels=(10,), spawn_interval=1200)
    monkeypatch.setenv('VAULT_SPAWN_INTERVAL', '900')
    with pytest.raises(wizard.SetupError, match='Unset VAULT_SPAWN_INTERVAL'):
        wizard.check_environment(selected)


def test_environment_override_cannot_bypass_saved_server_database_guard(case, monkeypatch):
    setup(case, ['42', '', '', ''], no_start=True)
    original = Settings.from_env()
    original.database.touch()
    previous = load_profile(original.root)
    monkeypatch.setenv('VAULT_GUILD_ID', '43')
    overridden = Settings.from_env()
    assert overridden.guild_id == 43
    dialog = Dialogue(['', ''])
    with pytest.raises(wizard.SetupError, match='separate checkout'):
        wizard.run_setup(overridden, prompt=dialog.prompt, output=dialog.output, request=case[2])
    assert load_profile(original.root) == previous


def test_existing_token_inside_checkout_is_copied_to_protected_external_profile(case):
    settings, _, request = case
    local = settings.root / 'local-bot-token'
    local.write_text(TOKEN + '\n')
    local.chmod(0o600)
    selected = replace(settings, token_file=local)
    dialog = Dialogue(['', '42', '', '', ''])
    secret = Mock(side_effect=AssertionError('The verified existing token is being reused'))
    result = wizard.run_setup(selected, prompt=dialog.prompt, hidden=secret, output=dialog.output,
                              request=request, no_start=True)
    assert result['ready']
    saved = Settings.from_env()
    assert not saved.token_file.is_relative_to(settings.root)
    assert saved.read_token() == TOKEN and local.read_text().strip() == TOKEN
    secret.assert_not_called()
