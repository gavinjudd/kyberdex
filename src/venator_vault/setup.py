"""Interactive setup with user-only token entry and read-only Discord discovery."""
from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import shlex
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import warnings
from dataclasses import replace
from pathlib import Path

from .config import load_profile, private_file, profile_directory
from .runtime import Runtime

VIEW = 1 << 10
SEND = 1 << 11
EMBED = 1 << 14
ATTACH = 1 << 15
HISTORY = 1 << 16
ADMIN = 1 << 3
BOT_PERMISSIONS = VIEW | SEND | EMBED | ATTACH | HISTORY
ACTIVITY_PERMISSIONS = VIEW | HISTORY


class SetupError(ValueError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise SetupError('Discord redirected a credentialed request; setup stopped safely.')


def discord_get(path, token):
    if not isinstance(path, str) or not re.fullmatch(
            r'/users/@me|/guilds/[1-9][0-9]{0,20}(?:/(?:roles|channels|members/[1-9][0-9]{0,20}))?', path):
        raise SetupError('Only approved read-only Discord setup routes are allowed.')
    request = urllib.request.Request('https://discord.com/api/v10' + path, headers={
        'Authorization': 'Bot ' + token, 'User-Agent': 'KyberDex setup/1.0'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
            if response.geturl() != request.full_url:
                raise SetupError('Discord redirected a credentialed request; setup stopped safely.')
            payload = response.read(2 * 1024 * 1024 + 1)
            if len(payload) > 2 * 1024 * 1024:
                raise SetupError('Discord setup response exceeded its size limit.')
            return json.loads(payload)
    except urllib.error.HTTPError as error:
        messages = {401: 'Discord rejected that bot token.', 403: 'The bot cannot access this server.',
                    404: 'Discord could not find that server or bot membership.',
                    429: 'Discord rate-limited setup. Wait briefly and run setup again.'}
        raise SetupError(messages.get(error.code, f'Discord returned HTTP {error.code}; try setup again.'),
                         error.code) from None
    except (urllib.error.URLError, TimeoutError):
        raise SetupError('Discord could not be reached. Check your connection and run setup again.') from None
    except (UnicodeError, json.JSONDecodeError):
        raise SetupError('Discord returned an invalid setup response. Try again shortly.') from None


def hidden_token():
    if not sys.stdin.isatty():
        raise SetupError('Run setup in an interactive terminal; token entry cannot use echoed or piped input.')
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            return getpass.getpass('Discord bot token (hidden): ').strip()
        except getpass.GetPassWarning:
            raise SetupError('Hidden token entry is unavailable in this terminal; setup stopped without reading it.') from None


def yes(prompt, question, *, default=True):
    suffix = ' [Y/n]: ' if default else ' [y/N]: '
    while True:
        answer = prompt(question + suffix).strip().lower()
        if not answer:
            return default
        if answer in ('y', 'yes'):
            return True
        if answer in ('n', 'no'):
            return False


def numeric_id(value):
    return (isinstance(value, str) and value.isascii() and value.isdecimal()
            and not value.startswith('0') and 0 < int(value) < 10**21)


def permissions(guild_id, role_rows, role_ids, channel, member_id=None):
    role_ids = {str(guild_id), *map(str, role_ids)}
    value = 0
    for role in role_rows:
        if str(role['id']) in role_ids:
            value |= int(role['permissions'])
    if value & ADMIN:
        return BOT_PERMISSIONS | ADMIN
    overwrites = channel.get('permission_overwrites', [])
    everyone = next((row for row in overwrites if str(row['id']) == str(guild_id) and row['type'] == 0), None)
    if everyone:
        value = (value & ~int(everyone['deny'])) | int(everyone['allow'])
    allow = deny = 0
    for row in overwrites:
        if row['type'] == 0 and str(row['id']) != str(guild_id) and str(row['id']) in role_ids:
            allow |= int(row['allow'])
            deny |= int(row['deny'])
    value = (value & ~deny) | allow
    if member_id is not None:
        member = next((row for row in overwrites if row['type'] == 1 and str(row['id']) == str(member_id)), None)
        if member:
            value = (value & ~int(member['deny'])) | int(member['allow'])
    return value


def discover(guild_id, identity, token, request):
    guild = request(f'/guilds/{guild_id}', token)
    member = request(f'/guilds/{guild_id}/members/{identity["id"]}', token)
    roles = request(f'/guilds/{guild_id}/roles', token)
    channels = request(f'/guilds/{guild_id}/channels', token)
    if not isinstance(guild, dict) or str(guild.get('id')) != guild_id or not isinstance(member, dict):
        raise SetupError('Discord returned an unexpected server membership response.')
    if not isinstance(roles, list) or not isinstance(channels, list) or not any(str(row.get('id')) == guild_id for row in roles):
        raise SetupError('Discord returned incomplete server permission data.')
    encounter, activity = [], []
    for channel in channels:
        if channel.get('type') not in (0, 5) or not numeric_id(channel.get('id')):
            continue
        public = permissions(guild_id, roles, (), channel)
        effective = permissions(guild_id, roles, member.get('roles', ()), channel, identity['id'])
        if not public & VIEW:
            continue
        if effective & ACTIVITY_PERMISSIONS == ACTIVITY_PERMISSIONS:
            activity.append(channel)
        if effective & BOT_PERMISSIONS == BOT_PERMISSIONS:
            encounter.append(channel)
    key = lambda channel: (channel.get('position', 0), int(channel['id']))
    return guild, sorted(encounter, key=key), sorted(activity, key=key)


def invite_url(bot_id, guild_id):
    return 'https://discord.com/oauth2/authorize?' + urllib.parse.urlencode({
        'client_id': bot_id, 'scope': 'bot applications.commands', 'permissions': BOT_PERMISSIONS,
        'integration_type': 0, 'guild_id': guild_id, 'disable_guild_select': 'true'})


def select_channel(channels, default, prompt, output, label):
    if not channels:
        raise SetupError(f'No public text channel has the required bot permissions for {label.lower()}. '
                         'Allow View Channel, Send Messages, Embed Links, Attach Files and Read Message History, then retry.')
    identities = {channel['id']: channel for channel in channels}
    selected = str(default) if str(default) in identities else channels[0]['id']
    output(f'{label} choices:')
    for index, channel in enumerate(channels, 1):
        output(f'  {index}. #{channel.get("name", channel["id"])} ({channel["id"]})')
    while True:
        value = prompt(f'{label} [#{identities[selected].get("name", selected)}]: ').strip()
        if not value:
            return selected
        if value in identities:
            return value
        if value.isdecimal() and 1 <= int(value) <= len(channels):
            return channels[int(value)-1]['id']
        output('Choose a listed channel number or channel ID.')


def _private_directory(directory):
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        raise SetupError('Configuration paths cannot contain symlinks.')
    for parent in reversed((directory, *directory.parents)):
        if not parent.exists():
            parent.mkdir(mode=0o700)
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise SetupError('The setup profile directory must be owned and mode 0700.')


def _atomic_private(path, data):
    if path.exists() or path.is_symlink():
        private_file(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.setup-', delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _profile_values(settings):
    return {'VAULT_DATABASE': str(settings.database), 'DISCORD_TOKEN_FILE': str(settings.token_file),
            'VAULT_GUILD_ID': str(settings.guild_id), 'VAULT_SPAWN_INTERVAL': str(settings.spawn_interval),
            'VAULT_DROP_DURATION': str(settings.drop_duration), 'VAULT_PUBLIC_URL': settings.vault_url,
            'VAULT_ENABLED_EVENTS': ','.join(settings.enabled_events),
            'VAULT_ENABLED_RELEASES': ','.join(settings.enabled_releases),
            'VAULT_ACTIVITY_CHANNEL_IDS': ','.join(map(str, settings.activity_channel_ids))}


def check_environment(settings):
    values = _profile_values(settings)
    for key in values:
        if key not in os.environ:
            continue
        supplied = os.environ[key].strip()
        if key in ('VAULT_ACTIVITY_CHANNEL_IDS', 'VAULT_ENABLED_EVENTS', 'VAULT_ENABLED_RELEASES'):
            supplied = ','.join(dict.fromkeys(part.strip() for part in supplied.split(',') if part.strip()))
        if key == 'VAULT_PUBLIC_URL':
            supplied = supplied.rstrip('/')
        if supplied != values[key]:
            raise SetupError(f'Your exported {key} conflicts with the selected setup. Unset {key} and run setup again; nothing was saved.')


def save_profile(settings, identity, encounter, token, *, replace_token):
    directory = profile_directory(settings.root)
    _private_directory(directory)
    profile = directory / 'settings.json'
    previous = load_profile(settings.root)
    new_token = None
    if replace_token:
        # A unique filename keeps the previous saved profile and credential usable
        # until the new profile commits, including after a failed disk write.
        new_token = directory / ('bot-token-' + secrets.token_hex(8))
        settings = replace(settings, token_file=new_token)
        _atomic_private(new_token, (token + '\n').encode('ascii'))
    document = {'version': 1, 'root': str(settings.root), 'bot_id': identity['id'],
                'encounter_channel_id': encounter, 'settings': _profile_values(settings)}
    try:
        if previous:
            _atomic_private(directory / 'settings.previous.json', (json.dumps(previous, indent=2) + '\n').encode())
        _atomic_private(profile, (json.dumps(document, indent=2) + '\n').encode())
    except BaseException:
        if new_token is not None:
            new_token.unlink(missing_ok=True)
        raise
    return settings


def run_setup(settings, *, no_start=False, prompt=None, hidden=None, output=print, request=None):
    """Called while holding the shared bot worker lock; returns whether to start."""
    from . import __main__ as ops
    from .art_setup import setup_art

    if prompt is None and not sys.stdin.isatty():
        raise SetupError('Run setup in an interactive terminal. For the installer, use a terminal with /dev/tty.')
    prompt = prompt or input
    hidden = hidden or hidden_token
    request = request or discord_get
    previous = load_profile(settings.root)
    output('KyberDex setup — bot token, server, channels, artwork, then launch.')
    output('Create a Discord application and copy its Bot token: https://discord.com/developers/applications')
    output('No Administrator permission or privileged Message Content/Members intent is required.')
    reused_token = False
    token = None
    if settings.token_file and settings.token_file.exists() and yes(prompt, 'Keep the existing protected bot token?'):
        token = settings.read_token()
        reused_token = not settings.token_file.resolve().is_relative_to(settings.root.resolve())
        if not reused_token:
            output('The existing token is inside this checkout. Saving setup will copy it to protected user configuration; the original file remains.')
    while True:
        if token is None:
            token = hidden().strip()
        if not 30 <= len(token) <= 1024 or not token.isascii() or any(char.isspace() for char in token):
            output('That token format is invalid. Copy the Bot token and try again; it will stay hidden.')
            token = None
            reused_token = False
            continue
        try:
            identity = request('/users/@me', token)
            if (not isinstance(identity, dict) or identity.get('bot') is not True
                    or not numeric_id(identity.get('id'))):
                raise SetupError('The supplied credential is not a Discord bot token.')
            break
        except SetupError as error:
            if error.status != 401:
                raise
            output(str(error) + ' Copy the current Bot token and try again.')
            token = None
            reused_token = False
    if (previous.get('bot_id') and previous['bot_id'] != identity['id']
            and not yes(prompt, 'This token belongs to a different bot. Replace the saved bot identity?', default=False)):
        raise SetupError('Setup cancelled; the saved bot identity is unchanged.')
    output(f'Bot verified: {identity.get("username", identity["id"])} ({identity["id"]}).')
    output('For the server ID, enable Discord Settings → Advanced → Developer Mode, then right-click your server → Copy Server ID.')
    default = str(settings.guild_id) if settings.guild_id else ''
    saved_guild = previous.get('settings', {}).get('VAULT_GUILD_ID') or default
    while True:
        guild_id = prompt(f'Discord server ID{f" [{default}]" if default else ""}: ').strip() or default
        if not numeric_id(guild_id):
            output('Enter the numeric server ID, not an invite link or channel ID.')
            continue
        if saved_guild and guild_id != saved_guild:
            if settings.database.exists():
                raise SetupError('This checkout already has a game database. Use a separate checkout for another server; saved setup is unchanged.')
            if not yes(prompt, 'Change the saved server? The previous profile will be preserved.', default=False):
                continue
        default = guild_id
        try:
            guild, channels, activity_channels = discover(guild_id, identity, token, request)
            break
        except SetupError as error:
            if error.status not in (403, 404):
                raise
            output(str(error))
            output('Install this bot in your server using this link, then return here:')
            output(invite_url(identity['id'], guild_id))
            if not yes(prompt, 'Retry server verification after installing the bot?'):
                raise SetupError('Setup cancelled before saving. Run setup again after inviting the bot.')
    output(f'Server verified: {guild.get("name", guild_id)}.')
    runtime_path = settings.database.with_name('runtime.sqlite3')
    runtime = Runtime(runtime_path, minimum_delay=settings.drop_duration)
    existing = runtime.get(guild_id)
    encounter = select_channel(channels, existing['channel_id'] if existing else None,
                               prompt, output, 'Encounter channel')
    activity_default = settings.activity_channel_ids[0] if settings.activity_channel_ids else encounter
    activity = select_channel(activity_channels, activity_default, prompt, output, 'Activity channel')
    selected_activity = (int(activity),)
    if len(settings.activity_channel_ids) > 1 and int(activity) == settings.activity_channel_ids[0]:
        available = {int(channel['id']) for channel in activity_channels}
        if not set(settings.activity_channel_ids).issubset(available):
            raise SetupError('Some saved activity channels are no longer public or accessible. Fix their permissions or explicitly select a replacement activity channel.')
        selected_activity = settings.activity_channel_ids
    interval = existing['interval_seconds'] if existing else settings.spawn_interval
    selected = replace(settings, guild_id=int(guild_id), activity_channels=selected_activity, spawn_interval=interval)
    if not reused_token:
        # This path is checked before writing; the final filename is unique and
        # deliberately cannot agree with an exported old token-file override.
        selected = replace(selected, token_file=profile_directory(settings.root) / 'bot-token-new')
    check_environment(selected)
    if not yes(prompt, f'Save setup for server {guild_id}, encounters in {encounter}, activity in {activity}?'):
        raise SetupError('Setup cancelled; no credentials or profile were saved.')
    selected = save_profile(selected, identity, encounter, token, replace_token=not reused_token)
    if not existing or existing['channel_id'] != encounter:
        runtime.configure(guild_id, encounter, interval, 'local-setup')
    if existing and existing['paused']:
        output('Automatic encounters remain paused. A server manager can resume them with /vault-admin pause paused:false.')
    output('Protected setup saved. Preparing artwork; successful downloads are reused on reruns.')
    art = setup_art(selected.root, progress=output)
    if not art['complete']:
        output(f'Artwork setup is incomplete ({art["failed"]} failures). Your saved settings and verified downloads are preserved.')
        output('Resume with: .venv/bin/python -m venator_vault setup (the bot has not started).')
        return {'ready': False, 'start': False}
    report = ops.readiness(selected, require_token=True)
    gates = ('launch_art_ready', 'launch_catalog_ready', 'launch_presentation_ready',
             'launch_balance_ready', 'launch_economy_ready', 'launch_storage_ready')
    failed = [name for name in gates if not report[name]]
    if failed:
        raise SetupError('Launch checks failed: ' + ', '.join(failed) + '. Saved setup is preserved; the bot has not started.')
    output('Setup and launch checks passed. Restart later with:')
    output(f'cd {shlex.quote(str(selected.root))} && ./start.sh')
    start = not no_start and yes(prompt, 'Start KyberDex now? Keep this terminal open while it runs.')
    return {'ready': True, 'start': start}
