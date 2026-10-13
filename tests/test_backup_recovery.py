"""Private backup transfer, fail-closed restore and real interrupted-process recovery."""
import io
import json
import os
import plistlib
import signal
import sqlite3
import subprocess
import sys
import tarfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from venator_vault import __main__ as ops
from venator_vault import replica


@pytest.fixture
def settings(tmp_path):
    root = tmp_path / "pi"
    (root/"state").mkdir(parents=True)
    (root/"data").mkdir()
    catalog = root/"data/cards.json"
    catalog.write_text(json.dumps({"schema_version": 1, "cards": []}))
    for name in ("vault.sqlite3", "runtime.sqlite3"):
        with closing(sqlite3.connect(root/"state"/name)) as db:
            db.executescript("PRAGMA journal_mode=WAL; CREATE TABLE sample(value INTEGER); INSERT INTO sample VALUES(10);")
    return SimpleNamespace(root=root, database=root/"state/vault.sqlite3", catalog=catalog)


def archive(settings):
    output = io.BytesIO()
    replica.export_snapshot(settings, output)
    return output.getvalue()


def mock_download(monkeypatch, data):
    monkeypatch.setattr(replica, "download", lambda command, destination: destination.write_bytes(data))


def value(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT value FROM sample").fetchone()[0]


def rewrite_tar(data, mutate):
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(data)) as original, tarfile.open(fileobj=output, mode="w") as result:
        for member in original:
            contents = original.extractfile(member).read()
            contents = mutate(member.name, contents)
            member.size = len(contents)
            result.addfile(member, io.BytesIO(contents))
    return output.getvalue()


def test_export_only_contains_consistent_state_catalog_and_manifest(settings):
    (settings.root/"private-token").write_text("controlled noncredential marker")
    data = archive(settings)
    with tarfile.open(fileobj=io.BytesIO(data)) as exported:
        assert set(exported.getnames()) == replica.FILES
    assert b"controlled noncredential marker" not in data
    assert not list((settings.root/"backups").glob(".export-*"))


def test_pull_verifies_and_promotes_private_complete_snapshot(settings, tmp_path, monkeypatch):
    mock_download(monkeypatch, archive(settings))
    cache = tmp_path/"cache"
    result = replica.pull(cache)
    selected = cache/result["snapshot"]
    manifest = replica.validate_snapshot(selected)
    assert manifest["files"]["cards.json"] == ops._hash(settings.catalog)
    assert value(selected/"vault.sqlite3") == value(selected/"runtime.sqlite3") == 10
    assert selected.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in selected.iterdir())
    assert replica.status(cache)["fresh"]
    assert not list(cache.glob(".incoming-*"))
    # A replica is a normal validated restore source, including both databases.
    assert ops.restore(settings, selected)["valid"]


@pytest.mark.parametrize("fault", ["transport", "checksum", "invalid_sqlite", "promotion"])
def test_failed_replacement_preserves_last_verified_copy_and_success_time(settings, tmp_path, monkeypatch, fault):
    cache = tmp_path/"cache"
    mock_download(monkeypatch, archive(settings))
    first = replica.pull(cache)
    before = {p.name: ops._hash(p) for p in (cache/first["snapshot"]).iterdir()}
    data = archive(settings)
    if fault == "transport":
        def fail(command, destination):
            destination.write_bytes(b"truncated transfer")
            raise OSError("controlled transport outage")
        monkeypatch.setattr(replica, "download", fail)
    elif fault == "checksum":
        mock_download(monkeypatch, rewrite_tar(data, lambda name, content: content+b"tamper" if name=="cards.json" else content))
    elif fault == "invalid_sqlite":
        # Keep hashes self-consistent to ensure SQLite integrity, not just hashes,
        # is an independent acceptance condition.
        broken = b"not a database"
        import hashlib

        def corrupt(name, content):
            if name == "vault.sqlite3":
                return broken
            if name == "manifest.json":
                manifest = json.loads(content)
                manifest["files"]["vault.sqlite3"] = hashlib.sha256(broken).hexdigest()
                return json.dumps(manifest).encode()
            return content
        mock_download(monkeypatch, rewrite_tar(data, corrupt))
    else:
        mock_download(monkeypatch, data)
        monkeypatch.setattr(replica.os, "rename", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises((OSError, ValueError, sqlite3.Error)):
        replica.pull(cache, keep=1)
    assert replica.status(cache)["last_success_at"] == first["last_success_at"]
    assert replica.status(cache)["last_error"]
    assert {p.name: ops._hash(p) for p in (cache/first["snapshot"]).iterdir()} == before
    assert not list(cache.glob(".incoming-*"))


@pytest.mark.parametrize("name,kind", [("../escape", "regular"), ("cards.json", "symlink"),
                                      ("secrets", "directory"), ("unknown", "regular")])
def test_archive_cannot_escape_or_install_links(tmp_path, name, kind):
    path = tmp_path/"input.tar"
    with tarfile.open(path, "w") as stream:
        entry = tarfile.TarInfo(name)
        if kind == "symlink":
            entry.type = tarfile.SYMTYPE
            entry.linkname = "/etc/passwd"
        elif kind == "directory":
            entry.type = tarfile.DIRTYPE
        stream.addfile(entry)
    with pytest.raises(ValueError, match="unexpected entry"):
        replica.unpack_snapshot(path, tmp_path/"unpacked")
    assert not (tmp_path/"escape").exists()


def test_retention_only_removes_old_verified_snapshots(settings, tmp_path, monkeypatch):
    cache = tmp_path/"cache"
    mock_download(monkeypatch, archive(settings))
    first = replica.pull(cache)
    unknown = cache/"snapshot-1"
    unknown.mkdir()
    (unknown/"important-note").write_text("preserve")
    mock_download(monkeypatch, archive(settings))
    second = replica.pull(cache, keep=1)
    assert not (cache/first["snapshot"]).exists()
    assert (cache/second["snapshot"]).exists()
    assert (unknown/"important-note").read_text() == "preserve"


def test_backward_pi_clock_cannot_prune_newly_accepted_replica(settings, tmp_path, monkeypatch):
    cache = tmp_path/"cache"
    mock_download(monkeypatch, archive(settings))
    first = replica.pull(cache)
    def earlier(name, contents):
        if name == "manifest.json":
            manifest = json.loads(contents)
            manifest["created_ns"] -= 1_000_000_000_000
            return json.dumps(manifest).encode()
        return contents
    mock_download(monkeypatch, rewrite_tar(archive(settings), earlier))
    result = replica.pull(cache, keep=1)
    assert result["snapshot"] != first["snapshot"]
    assert replica.validate_snapshot(cache/replica.status(cache)["snapshot"])
    assert not (cache/first["snapshot"]).exists()


def test_transfer_timeout_and_size_limit_terminate_owned_process(tmp_path, monkeypatch):
    with pytest.raises(TimeoutError):
        replica.download([sys.executable, "-c", "import time; time.sleep(20)"], tmp_path/"timeout", timeout=.1)
    monkeypatch.setattr(replica, "MAX_TRANSFER", 10)
    with pytest.raises(ValueError, match="size limit"):
        replica.download([sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'x'*1000)"], tmp_path/"oversized")


def test_ssh_keeps_host_verification_and_quotes_remote_path():
    command = replica.ssh_command("kyberdex-host", "/srv/kyberdex/path with spaces")
    assert "BatchMode=yes" in command and "StrictHostKeyChecking=yes" in command
    assert "cd '/srv/kyberdex/path with spaces'" in command[-1]
    for host in ("-oProxyCommand=bad", "host; command", "user@host"):
        with pytest.raises(ValueError):
            replica.ssh_command(host, replica.REMOTE_ROOT)


def test_bundled_backup_runtime_runs_without_project_or_optional_dependencies(tmp_path):
    cache = replica.private_directory(tmp_path/"cache")
    installed = replica.bundled_runtime(cache)
    assert replica.bundled_runtime(cache) == installed
    env = {**os.environ, "PYTHONPATH": str(installed)}
    result = subprocess.run([str(Path(sys.executable).resolve()), "-S", "-m", "venator_vault.replica", "status",
                             "--cache", str(cache)], cwd=tmp_path, env=env, text=True, capture_output=True, check=True)
    assert json.loads(result.stdout)["last_success_at"] is None


def test_installed_launch_agent_retries_failures_and_uses_private_bundled_code(tmp_path, monkeypatch):
    monkeypatch.setattr(replica.sys, "platform", "darwin")
    monkeypatch.setattr(replica.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path/"Library").mkdir()
    calls = []
    monkeypatch.setattr(replica.subprocess, "run", lambda args, **kw: calls.append(args))
    cache = tmp_path/"Library/Application Support/KyberDex/pi-backups"
    replica.install_agent(cache)
    path = tmp_path/f"Library/LaunchAgents/{replica.LABEL}.plist"
    config = plistlib.loads(path.read_bytes())
    assert config["StartInterval"] == 3600
    assert config["KeepAlive"] == {"SuccessfulExit": False}
    assert config["ThrottleInterval"] == 300
    assert str(cache.parent/"replica-runtime") in config["WorkingDirectory"]
    assert "Documents" not in config["WorkingDirectory"]
    assert config["ProgramArguments"][0] == str(Path(sys.executable).resolve())
    assert calls[-1][1] == "bootstrap"


def run_killed(settings, code, *arguments):
    prefix = """
import os,signal,sys
from pathlib import Path
from types import SimpleNamespace
from venator_vault import __main__ as ops
root=Path(sys.argv[1])
s=SimpleNamespace(root=root,database=root/'state/vault.sqlite3',catalog=root/'data/cards.json')
"""
    result = subprocess.run([sys.executable, "-c", prefix+code, str(settings.root), *map(str, arguments)],
                            capture_output=True, timeout=10, check=False)
    assert result.returncode == -signal.SIGKILL, result.stderr.decode()


def test_sigkill_mid_backup_never_accepts_partial_snapshot(settings):
    previous = Path(ops.backup(settings)["backup"])
    run_killed(settings, """
original=ops._sync
def interrupted(path):
    original(path)
    if Path(path).name=='vault.sqlite3':os.kill(os.getpid(),signal.SIGKILL)
ops._sync=interrupted
ops.backup(s)
""")
    partial = next(p for p in (settings.root/"backups").glob("auto-*") if p != previous)
    assert not (partial/"manifest.json").exists()
    ops._prune(settings, 1)
    assert previous.exists() and partial.exists()
    assert ops.restore(settings, previous)["valid"]


def test_pi_retention_preserves_current_snapshot_after_clock_correction(settings, monkeypatch):
    previous = Path(ops.backup(settings)["backup"])
    manifest_path = previous/"manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["created_ns"] += 1_000_000_000_000
    manifest_path.write_text(json.dumps(manifest))
    current = Path(ops.backup(settings, keep=1)["backup"])
    assert current.exists() and not previous.exists()
    assert ops.restore(settings, current)["valid"]


@pytest.mark.parametrize("field,bad", [("integrity", "corrupt"), ("journal_mode", "delete"),
                                       ("synchronous", 1), ("foreign_keys", False),
                                       ("foreign_key_violations", 1)])
def test_storage_gate_checks_durability_and_integrity_independently(field, bad):
    healthy = {"integrity": "ok", "journal_mode": "wal", "synchronous": 2,
               "foreign_keys": True, "foreign_key_violations": 0}
    assert ops._storage_ready(healthy)
    healthy[field] = bad
    assert not ops._storage_ready(healthy)


def test_plain_check_returns_failure_for_unhealthy_storage(settings, monkeypatch):
    monkeypatch.setattr(ops.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(ops, "readiness", lambda *a, **kw: {"launch_storage_ready": False})
    assert ops.main(["check"]) == 1


def test_sigkill_between_restore_files_keeps_gate_closed_until_full_repair(settings):
    snapshot = Path(ops.backup(settings)["backup"])
    for name in ("vault.sqlite3", "runtime.sqlite3"):
        with closing(sqlite3.connect(settings.database.parent/name)) as db:
            db.execute("UPDATE sample SET value=99")
            db.commit()
    run_killed(settings, """
original=ops.os.replace
def interrupted(source,target):
    original(source,target)
    if Path(target).name=='vault.sqlite3':os.kill(os.getpid(),signal.SIGKILL)
ops.os.replace=interrupted
ops.restore(s,Path(sys.argv[2]),apply=True)
""", snapshot)
    assert (settings.database.parent/ops.RESTORE_MARKER).exists()
    with pytest.raises(SystemExit, match="interrupted restore"):
        ops.worker_lock(settings)
    assert value(settings.database) == 10
    assert value(settings.database.parent/"runtime.sqlite3") == 99
    ops.restore(settings, snapshot, apply=True)
    assert value(settings.database) == value(settings.database.parent/"runtime.sqlite3") == 10
    with ops.worker_lock(settings):
        pass
