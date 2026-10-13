"""User-invoked token entry with explicit bot identity; never starts the gateway."""
import argparse
import getpass
import json
import os
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application-id', default=os.environ.get('DISCORD_APPLICATION_ID'),
                        help='Expected Discord application/bot user ID (or DISCORD_APPLICATION_ID).')
    parser.add_argument('--token-file', type=Path,
                        default=Path(os.environ.get('DISCORD_TOKEN_FILE') or
                                     (Path.home() / '.config/venator-vault/bot-token')))
    parser.add_argument('--stdin', action='store_true',
                        help='Explicitly read a token from stdin instead of a hidden terminal prompt.')
    args = parser.parse_args(argv)
    expected = args.application_id or ''
    if not expected.isascii() or not expected.isdecimal() or int(expected) <= 0:
        raise ValueError('Supply your expected bot ID using --application-id or DISCORD_APPLICATION_ID.')
    os.umask(0o077)
    destination = args.token_file.expanduser().absolute()
    root = destination.parent
    if any(path.is_symlink() for path in (destination, *destination.parents)):
        raise ValueError('Credential paths cannot contain symlinks.')
    if destination.exists():
        raise ValueError('A credential already exists; no replacement was attempted.')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('Credential directory must be owned and mode0700.')
    if args.stdin:
        token = sys.stdin.buffer.read(1025).strip().decode('ascii')
    else:
        if not sys.stdin.isatty():
            raise ValueError('Use an interactive terminal for the hidden prompt, or explicitly pass --stdin.')
        token = getpass.getpass('Discord bot token (hidden): ').strip()
    if not 30 <= len(token) <= 1024 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError('The supplied token format is invalid.')
    request = urllib.request.Request('https://discord.com/api/v10/users/@me', headers={
        'Authorization': 'Bot ' + token, 'User-Agent': 'KyberDex credential setup/1.0'})
    with urllib.request.urlopen(request, timeout=20) as response:
        identity = json.load(response)
    if identity.get('id') != expected or identity.get('bot') is not True:
        raise ValueError('This credential does not belong to the explicitly selected bot.')
    with tempfile.NamedTemporaryFile(dir=root, prefix='.bot-token-', delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(token.encode('ascii') + b'\n')
        handle.flush()
        os.fsync(handle.fileno())
    try:
        # A hard link atomically refuses a concurrently provisioned credential.
        os.link(temporary, destination)
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({'provisioned': True, 'identity_verified': True, 'application_id': identity['id'],
                      'mode': '0600', 'gateway_started': False}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, UnicodeError, urllib.error.URLError) as error:
        print(f'Credential setup did not finish ({type(error).__name__}). No credential value was logged.', file=sys.stderr)
        raise SystemExit(1)
