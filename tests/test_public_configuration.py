"""Public setup has no built-in guild or credentials; gateway launch is explicit."""
import importlib.util
import io
import json
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest

from venator_vault import __main__ as ops
from venator_vault.config import Settings


@pytest.fixture
def provision():
    path = Path(__file__).resolve().parents[1] / 'deploy/provision-token.py'
    spec = importlib.util.spec_from_file_location('kyberdex_provision_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_configuration_can_be_loaded_offline_but_cannot_launch(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv('VAULT_GUILD_ID', raising=False)
    monkeypatch.delenv('VAULT_ACTIVITY_CHANNEL_IDS', raising=False)
    monkeypatch.setenv('VAULT_DATABASE', str(tmp_path / 'state/vault.sqlite3'))
    settings = Settings.from_env()
    assert settings.guild_id is None and settings.activity_channel_ids == ()
    assert ops.main(['run']) == 1
    assert 'VAULT_GUILD_ID' in capsys.readouterr().err
    assert not settings.database.exists()


@pytest.mark.parametrize('value', ['0', '-1', 'abc'])
def test_invalid_explicit_guild_rejected(monkeypatch, value):
    monkeypatch.setenv('VAULT_GUILD_ID', value)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_valid_explicit_guild_and_channels(monkeypatch):
    monkeypatch.setenv('VAULT_GUILD_ID', '42')
    monkeypatch.setenv('VAULT_ACTIVITY_CHANNEL_IDS', '10,11,10')
    settings = Settings.from_env()
    settings.require_discord_configuration()
    assert settings.guild_id == 42 and settings.activity_channel_ids == (10, 11)


@pytest.mark.parametrize('identity', [{'id': '43', 'bot': True}, {'id': '42', 'bot': False}])
def test_provision_rejects_identity_mismatch_without_persisting(provision, monkeypatch, tmp_path, identity):
    token_file = tmp_path / 'private/bot-token'
    monkeypatch.setattr(provision.sys, 'stdin', Mock(isatty=lambda: True))
    monkeypatch.setattr(provision.getpass, 'getpass', lambda _: 'x' * 40)
    monkeypatch.setattr(provision.urllib.request, 'urlopen', lambda *a, **k: io.StringIO(json.dumps(identity)))
    with pytest.raises(ValueError, match='explicitly selected bot'):
        provision.main(['--application-id', '42', '--token-file', str(token_file)])
    assert not token_file.exists()
    assert list(token_file.parent.iterdir()) == []


def test_provision_hidden_entry_verifies_identity_and_writes_private_once(provision, monkeypatch, tmp_path, capsys):
    token_file = tmp_path / 'private/bot-token'
    token = 'test-only-placeholder-' + 'x' * 30
    monkeypatch.setattr(provision.sys, 'stdin', Mock(isatty=lambda: True))
    prompt = Mock(return_value=token)
    monkeypatch.setattr(provision.getpass, 'getpass', prompt)
    def identity(request, timeout):
        assert timeout == 20
        assert request.full_url == 'https://discord.com/api/v10/users/@me'
        assert request.get_header('Authorization') == 'Bot ' + token
        return io.StringIO(json.dumps({'id': '42', 'bot': True}))
    monkeypatch.setattr(provision.urllib.request, 'urlopen', identity)
    arguments = ['--application-id', '42', '--token-file', str(token_file)]
    provision.main(arguments)
    assert token_file.read_text().strip() == token
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700
    output = capsys.readouterr().out
    assert token not in output and json.loads(output)['gateway_started'] is False
    with pytest.raises(ValueError, match='already exists'):
        provision.main(arguments)
    prompt.assert_called_once()


def test_provision_requires_identity_before_entry_or_network(provision, monkeypatch, tmp_path):
    monkeypatch.delenv('DISCORD_APPLICATION_ID', raising=False)
    entry = Mock(side_effect=AssertionError('No token input expected'))
    monkeypatch.setattr(provision.getpass, 'getpass', entry)
    with pytest.raises(ValueError, match='expected bot ID'):
        provision.main(['--token-file', str(tmp_path / 'private/bot-token')])
    entry.assert_not_called()


def test_provision_noninteractive_requires_explicit_stdin(provision, monkeypatch, tmp_path):
    monkeypatch.setattr(provision.sys, 'stdin', Mock(isatty=lambda: False))
    with pytest.raises(ValueError, match='interactive terminal'):
        provision.main(['--application-id', '42', '--token-file', str(tmp_path / 'private/bot-token')])
