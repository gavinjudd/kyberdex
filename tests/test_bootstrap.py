"""Exercise the public shell installer with isolated files and offline tool stubs."""
from __future__ import annotations

import errno
import io
import os
import pty
import select
import shutil
import signal
import subprocess
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def executable(path, content):
    path.write_text(content)
    path.chmod(0o700)


def terminal_run(script, env, *, pipe_script=False):
    """Give the child a controlling terminal, including the documented pipe invocation."""
    pid, descriptor = pty.fork()
    if pid == 0:
        try:
            command = ['/bin/sh', str(script)]
            if pipe_script:
                env = {**env, 'KYBERDEX_TEST_SCRIPT': str(script)}
                command = ['/bin/sh', '-c', 'cat "$KYBERDEX_TEST_SCRIPT" | /bin/sh']
            os.execve('/bin/sh', command, env)
        finally:
            os._exit(127)
    chunks = []
    deadline = time.monotonic() + 15
    status = None
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([descriptor], [], [], 0.1)
            if readable:
                try:
                    chunk = os.read(descriptor, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b''
                if chunk:
                    chunks.append(chunk)
            child, result = os.waitpid(pid, os.WNOHANG)
            if child:
                status = result
                break
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail('Offline bootstrap did not exit within 15 seconds.')
        return os.waitstatus_to_exitcode(status), b''.join(chunks).decode(errors='replace')
    finally:
        os.close(descriptor)


@pytest.fixture
def bootstrap(tmp_path):
    base = tmp_path.resolve()
    binaries = base / 'tools'
    binaries.mkdir()
    # GNU tar invokes gzip via PATH; macOS's libarchive handles it internally.
    for name in ('cat', 'cp', 'dirname', 'gzip', 'mkdir', 'mktemp', 'mv', 'rm', 'rmdir', 'sh', 'tar', 'uname'):
        target = shutil.which(name)
        assert target
        (binaries / name).symlink_to(target)
    python_stub = base / 'fake-python'
    executable(python_stub, '''#!/bin/sh
set -eu
printf '%s\\n' "$@" > "$KYBERDEX_TEST_PYTHON_ARGS"
printf '%s\\n' "${VAULT_ROOT-}" > "$KYBERDEX_TEST_ROOT"
if [ -t 0 ]; then printf 'yes\\n'; else printf 'no\\n'; fi > "$KYBERDEX_TEST_TERMINAL"
printf 'offline setup stub finished\\n'
''')
    uv_stub = base / 'fake-uv'
    executable(uv_stub, '''#!/bin/sh
set -eu
printf '%s\\n' "$@" >> "$KYBERDEX_TEST_UV_ARGS"
[ "${KYBERDEX_TEST_UV_FAIL:-0}" = 0 ] || exit 23
mkdir -p .venv/bin
cp "$KYBERDEX_TEST_PYTHON_STUB" .venv/bin/python
''')
    shutil.copy2(uv_stub, binaries / 'uv')
    uv_installer = base / 'fake-uv-installer'
    executable(uv_installer, '''#!/bin/sh
set -eu
printf '%s\\n' "$UV_UNMANAGED_INSTALL" > "$KYBERDEX_TEST_UV_DESTINATION"
mkdir -p "$UV_UNMANAGED_INSTALL"
cp "$KYBERDEX_TEST_UV_STUB" "$UV_UNMANAGED_INSTALL/uv"
''')
    executable(binaries / 'curl', '''#!/bin/sh
set -eu
printf 'fetch\\n' >> "$KYBERDEX_TEST_CURL_LOG"
[ "${KYBERDEX_TEST_CURL_FAIL:-0}" = 0 ] || exit 22
url= output=
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o) output=$2; shift ;;
        https://*) url=$1 ;;
    esac
    shift
done
case "$url" in
    https://github.com/gavinjudd/kyberdex/archive/refs/tags/v*.tar.gz)
        cp "$KYBERDEX_TEST_ARCHIVE" "$output" ;;
    https://astral.sh/uv/*/install.sh)
        cp "$KYBERDEX_TEST_UV_INSTALLER" "$output" ;;
    *) printf 'Unrecognized download URL\\n' >&2; exit 92 ;;
esac
''')
    archive = base / 'source.tar.gz'
    with tarfile.open(archive, 'w:gz') as bundle:
        files = {
            'kyberdex-release/pyproject.toml': b'[project]\nname="venator-vault"\n',
            'kyberdex-release/uv.lock': b'version = 1\n',
            'kyberdex-release/src/venator_vault/setup.py': b'"""Synthetic setup fixture."""\n',
        }
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            bundle.addfile(entry, io.BytesIO(content))
    destination = base / 'install with spaces'
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('VAULT_', 'DISCORD_', 'KYBERDEX_'))}
    env.update({
        'PATH': str(binaries),
        'KYBERDEX_INSTALL_DIR': str(destination),
        'KYBERDEX_TEST_ARCHIVE': str(archive),
        'KYBERDEX_TEST_CURL_LOG': str(base / 'curl.log'),
        'KYBERDEX_TEST_PYTHON_STUB': str(python_stub),
        'KYBERDEX_TEST_PYTHON_ARGS': str(base / 'python.args'),
        'KYBERDEX_TEST_ROOT': str(base / 'root.value'),
        'KYBERDEX_TEST_TERMINAL': str(base / 'terminal.value'),
        'KYBERDEX_TEST_UV_STUB': str(uv_stub),
        'KYBERDEX_TEST_UV_INSTALLER': str(uv_installer),
        'KYBERDEX_TEST_UV_ARGS': str(base / 'uv.args'),
        'KYBERDEX_TEST_UV_DESTINATION': str(base / 'uv.destination'),
    })
    return SimpleNamespace(base=base, binaries=binaries, directory=destination, archive=archive, env=env)


def assert_clean_staging(case):
    assert not Path(str(case.directory) + '.setup-lock').exists()
    assert not list(case.base.glob('.kyberdex-install.*'))


def test_bootstrap_refuses_noninteractive_execution_before_downloading(bootstrap):
    result = subprocess.run(['/bin/sh', str(ROOT / 'install.sh')], env=bootstrap.env,
                            input='', capture_output=True, text=True, start_new_session=True, timeout=10, check=False)
    assert result.returncode != 0
    assert 'interactive terminal' in result.stderr
    assert not bootstrap.directory.exists()
    assert not (bootstrap.base / 'curl.log').exists()


def test_bootstrap_pipe_invocation_installs_to_spaced_path_and_restores_terminal(bootstrap):
    code, output = terminal_run(ROOT / 'install.sh', bootstrap.env, pipe_script=True)
    assert code == 0, output
    assert (bootstrap.base / 'python.args').read_text().splitlines() == ['-m', 'venator_vault', 'setup']
    assert (bootstrap.base / 'uv.args').read_text().splitlines() == ['sync', '--locked', '--no-dev', '--python', '3.13']
    assert (bootstrap.base / 'terminal.value').read_text() == 'yes\n'
    assert (bootstrap.directory / '.kyberdex-install').is_file()
    assert (bootstrap.directory / '.bootstrap/runtime-ready').is_file()
    assert_clean_staging(bootstrap)


def test_bootstrap_resumes_same_release_without_replacing_existing_state(bootstrap):
    assert terminal_run(ROOT / 'install.sh', bootstrap.env)[0] == 0
    original_uv_calls = (bootstrap.base / 'uv.args').read_bytes()
    state = bootstrap.directory / 'state'
    state.mkdir()
    sentinel = state / 'preserved-state'
    sentinel.write_bytes(b'local state must remain unchanged')
    code, output = terminal_run(ROOT / 'install.sh', {**bootstrap.env, 'KYBERDEX_TEST_UV_FAIL': '1'})
    assert code == 0, output
    assert sentinel.read_bytes() == b'local state must remain unchanged'
    assert (bootstrap.base / 'uv.args').read_bytes() == original_uv_calls
    assert (bootstrap.base / 'curl.log').read_text().splitlines() == ['fetch']
    assert_clean_staging(bootstrap)


@pytest.mark.parametrize('kind', ['unrelated', 'older-release'])
def test_bootstrap_refuses_existing_directory_and_preserves_contents(bootstrap, kind):
    bootstrap.directory.mkdir()
    sentinel = bootstrap.directory / 'important-file'
    sentinel.write_text('preserve')
    if kind == 'older-release':
        (bootstrap.directory / '.kyberdex-install').write_text('kyberdex-bootstrap-v1:0.0.1\n')
    code, output = terminal_run(ROOT / 'install.sh', bootstrap.env)
    assert code != 0, output
    expected = 'Destination already exists' if kind == 'unrelated' else 'different installer version'
    assert expected in output
    assert sentinel.read_text() == 'preserve'
    assert not (bootstrap.base / 'curl.log').exists()
    assert_clean_staging(bootstrap)


def test_bootstrap_refuses_symlinked_install_parent(bootstrap):
    actual = bootstrap.base / 'actual'
    actual.mkdir()
    link = bootstrap.base / 'linked'
    link.symlink_to(actual, target_is_directory=True)
    env = {**bootstrap.env, 'KYBERDEX_INSTALL_DIR': str(link / 'app')}
    code, output = terminal_run(ROOT / 'install.sh', env)
    assert code != 0, output
    assert 'without symlinks' in output
    assert not list(actual.iterdir())
    assert not (bootstrap.base / 'curl.log').exists()


def test_bootstrap_download_failure_leaves_no_partial_install(bootstrap):
    code, output = terminal_run(ROOT / 'install.sh', {**bootstrap.env, 'KYBERDEX_TEST_CURL_FAIL': '1'})
    assert code != 0, output
    assert (bootstrap.base / 'curl.log').read_text().splitlines() == ['fetch']
    assert not bootstrap.directory.exists()
    assert not (bootstrap.base / 'python.args').exists()
    assert_clean_staging(bootstrap)


def test_bootstrap_incomplete_release_is_rejected_before_install(bootstrap):
    with tarfile.open(bootstrap.archive, 'w:gz'):
        pass
    code, output = terminal_run(ROOT / 'install.sh', bootstrap.env)
    assert code != 0, output
    assert 'downloaded release is incomplete' in output
    assert not bootstrap.directory.exists()
    assert not (bootstrap.base / 'python.args').exists()
    assert_clean_staging(bootstrap)


def test_bootstrap_dependency_failure_can_resume_without_redownloading(bootstrap):
    code, _ = terminal_run(ROOT / 'install.sh', {**bootstrap.env, 'KYBERDEX_TEST_UV_FAIL': '1'})
    assert code != 0
    assert (bootstrap.directory / '.kyberdex-install').is_file()
    assert not (bootstrap.directory / '.bootstrap/runtime-ready').exists()
    assert not (bootstrap.base / 'python.args').exists()
    assert_clean_staging(bootstrap)
    code, output = terminal_run(ROOT / 'install.sh', bootstrap.env)
    assert code == 0, output
    assert (bootstrap.base / 'curl.log').read_text().splitlines() == ['fetch']
    assert (bootstrap.directory / '.bootstrap/runtime-ready').is_file()


def test_bootstrap_prepared_runtime_resumes_without_uv_or_downloads(bootstrap):
    assert terminal_run(ROOT / 'install.sh', bootstrap.env)[0] == 0
    original_uv_calls = (bootstrap.base / 'uv.args').read_bytes()
    (bootstrap.binaries / 'uv').unlink()
    code, output = terminal_run(ROOT / 'install.sh', {**bootstrap.env, 'KYBERDEX_TEST_CURL_FAIL': '1'})
    assert code == 0, output
    assert (bootstrap.base / 'uv.args').read_bytes() == original_uv_calls
    assert (bootstrap.base / 'curl.log').read_text().splitlines() == ['fetch']
    assert not (bootstrap.base / 'uv.destination').exists()
    assert not (bootstrap.directory / '.bootstrap/bin/uv').exists()
    assert_clean_staging(bootstrap)


def test_bootstrap_missing_uv_uses_only_install_local_unmanaged_destination(bootstrap):
    (bootstrap.binaries / 'uv').unlink()
    code, output = terminal_run(ROOT / 'install.sh', bootstrap.env)
    assert code == 0, output
    expected = bootstrap.directory / '.bootstrap/bin'
    assert (bootstrap.base / 'uv.destination').read_text().strip() == str(expected)
    assert (expected / 'uv').is_file()
    assert (bootstrap.base / 'curl.log').read_text().splitlines() == ['fetch', 'fetch']
    assert_clean_staging(bootstrap)


def test_start_script_reports_missing_environment(bootstrap):
    bootstrap.directory.mkdir()
    script = bootstrap.directory / 'start.sh'
    shutil.copy2(ROOT / 'start.sh', script)
    result = subprocess.run(['/bin/sh', str(script)], env=bootstrap.env,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode != 0
    assert 'setup first' in result.stderr
    assert not (bootstrap.base / 'python.args').exists()


def test_start_script_uses_its_own_root_and_prepared_python(bootstrap):
    python = bootstrap.directory / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    shutil.copy2(bootstrap.base / 'fake-python', python)
    script = bootstrap.directory / 'start.sh'
    shutil.copy2(ROOT / 'start.sh', script)
    result = subprocess.run(['/bin/sh', str(script)], env=bootstrap.env,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert (bootstrap.base / 'python.args').read_text().splitlines() == ['-m', 'venator_vault', 'run']
    assert (bootstrap.base / 'root.value').read_text().strip() == str(bootstrap.directory)
