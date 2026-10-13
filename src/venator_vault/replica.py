"""Verified Pi snapshots on the owner's Mac. Never copies credentials or live WALs."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import re
import selectors
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from . import __main__ as ops
from .config import Settings

LABEL = "local.kyberdex.pi-backup"
REMOTE_ROOT = "/opt/kyberdex"
FILES = frozenset(("manifest.json", "cards.json", "vault.sqlite3", "runtime.sqlite3"))
MAX_TRANSFER = 2 * 1024**3
TRANSFER_TIMEOUT = 180


def default_cache():
    return Path.home() / "Library/Application Support/KyberDex/pi-backups"


def private_directory(path):
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("Backup paths cannot contain symlinks.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Backup directory must be owned and mode0700.")
    return path


def validate_snapshot(path):
    settings = SimpleNamespace(database=Path("vault.sqlite3"), catalog=path / "cards.json")
    manifest = ops._validate_restore(settings, path)
    if {p.name for p in path.iterdir()} != set(manifest["files"]) | {"manifest.json"}:
        raise ValueError("Snapshot contains unexpected entries.")
    if type(manifest.get("created_ns")) is not int or manifest["created_ns"] <= 0:
        raise ValueError("Snapshot creation time is invalid.")
    return manifest


def export_snapshot(settings, output):
    """Export a consistent new snapshot, holding the backup/prune lock through send."""
    if settings.database.name != "vault.sqlite3":
        raise ValueError("Replica export requires the standard database filename.")
    with ops._maintenance_lock(settings), tempfile.TemporaryDirectory(
        prefix=".export-", dir=ops._backup_root(settings)
    ) as temporary:
        snapshot = Path(temporary) / "snapshot"
        manifest = ops._snapshot(settings, snapshot)
        names = ["manifest.json", *manifest["files"]]
        if sum((snapshot / name).stat().st_size for name in names) > MAX_TRANSFER // 2:
            raise ValueError("Snapshot exceeds the bounded replica size.")
        with tarfile.open(fileobj=output, mode="w|") as archive:
            for name in names:
                archive.add(snapshot / name, arcname=name, recursive=False)


def ssh_command(host, remote_root):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", host):
        raise ValueError("Invalid SSH host alias.")
    path = Path(remote_root)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Remote project path must be absolute.")
    command = f"cd {shlex.quote(str(path))} && .venv/bin/python -m venator_vault.replica export"
    return ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2", host, command]


def download(command, destination, *, timeout=TRANSFER_TIMEOUT):
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            with destination.open("xb") as target, selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                size = 0
                while True:
                    remaining = deadline-time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TimeoutError("Snapshot transfer timed out.")
                    chunk = os.read(process.stdout.fileno(), 64*1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_TRANSFER:
                        raise ValueError("Snapshot transfer exceeds its size limit.")
                    target.write(chunk)
                if process.wait(timeout=max(.01, deadline-time.monotonic())):
                    raise OSError("Pi snapshot transfer failed; previous copies retained.")
                target.flush()
                os.fsync(target.fileno())
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def unpack_snapshot(archive_path, destination):
    destination.mkdir(mode=0o700)
    seen, total = set(), 0
    with tarfile.open(archive_path, "r:") as archive:
        for member in archive:
            if member.name not in FILES or member.name in seen or not member.isfile():
                raise ValueError("Snapshot archive contains an unexpected entry.")
            total += member.size
            if member.size < 0 or total > MAX_TRANSFER:
                raise ValueError("Snapshot archive is too large.")
            seen.add(member.name)
            path = destination / member.name
            with archive.extractfile(member) as source, path.open("xb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            path.chmod(0o600)
    manifest = validate_snapshot(destination)
    ops._sync_directory(destination)
    return manifest


def save_status(cache, value):
    path = cache / "status.json"
    with tempfile.NamedTemporaryFile(mode="w", dir=cache, prefix=".status-", delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.chmod(0o600)
        os.replace(temporary, path)
        ops._sync_directory(cache)
    finally:
        temporary.unlink(missing_ok=True)


def status(cache):
    path = cache / "status.json"
    if not path.exists():
        return {"last_success_at": None, "fresh": False}
    if path.is_symlink():
        raise ValueError("Replica status cannot be a symlink.")
    result = json.loads(path.read_text())
    age = time.time()-result["last_success_at"] if result.get("last_success_at") else None
    return {**result, "age_seconds": age, "fresh": age is not None and 0 <= age <= 7200}


def prune(cache, keep, *, preserve):
    candidates = []
    for path in cache.iterdir():
        if not re.fullmatch(r"snapshot-[0-9]+", path.name) or path.is_symlink() or not path.is_dir():
            continue
        if path.stat().st_uid != os.getuid():
            continue
        try:
            manifest = validate_snapshot(path)
        except (OSError, ValueError, sqlite3.Error):
            continue
        candidates.append((manifest["created_ns"], path))
    # A Pi can report an older clock after power loss. The locally accepted
    # current receipt must survive even if its remote timestamp sorts earlier.
    retained = [path for _, path in sorted(candidates, reverse=True) if path != preserve]
    for path in retained[max(0, keep-1):]:
        shutil.rmtree(path)
    ops._sync_directory(cache)


def pull(cache, *, host="kyberdex-host", remote_root=REMOTE_ROOT, keep=168):
    if type(keep) is not int or not 1 <= keep <= 168:
        raise ValueError("Replica retention must be1–168 snapshots.")
    cache = private_directory(cache)
    lock_fd = os.open(cache / "replica.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Backup lock must be an owned regular file.")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        recorded = status(cache)
        try:
            with tempfile.TemporaryDirectory(prefix=".incoming-", dir=cache) as temporary:
                incoming = Path(temporary)
                download(ssh_command(host, remote_root), incoming / "transfer.tar")
                manifest = unpack_snapshot(incoming / "transfer.tar", incoming / "snapshot")
                target = cache / f"snapshot-{manifest['created_ns']}"
                if target.exists() or target.is_symlink():
                    if target.is_symlink() or validate_snapshot(target) != manifest:
                        raise ValueError("A different replica already uses this snapshot ID.")
                else:
                    os.rename(incoming / "snapshot", target)
                    ops._sync_directory(cache)
                result = {"last_attempt_at": time.time(), "last_success_at": time.time(),
                          "snapshot": target.name, "snapshot_created_at": manifest["created_at"],
                          "catalog_sha256": manifest["files"]["cards.json"],
                          "runtime_included": "runtime.sqlite3" in manifest["files"], "last_error": None}
                save_status(cache, result)
                recorded = result
                prune(cache, keep, preserve=target)
                return result
        except (OSError, ValueError, sqlite3.Error, tarfile.TarError, subprocess.SubprocessError) as error:
            save_status(cache, {**recorded, "last_attempt_at": time.time(), "last_error": type(error).__name__})
            raise


def bundled_runtime(cache):
    """Keep background backup execution outside macOS's protected Documents folder."""
    source = Path(__file__).resolve().parent
    names = ("__init__.py", "__main__.py", "config.py", "core.py", "economy.py", "runtime.py", "replica.py")
    contents = {name: (source/name).read_bytes() for name in names}
    identity = hashlib.sha256(b"".join(contents.values())).hexdigest()
    root = private_directory(cache.parent / "replica-runtime")
    version = root / identity
    if version.exists():
        if version.is_symlink() or any((version/"venator_vault"/name).is_symlink()
                                      or (version/"venator_vault"/name).read_bytes() != data
                                      for name, data in contents.items()):
            raise ValueError("Installed backup runtime differs from its expected bytes.")
        return version
    with tempfile.TemporaryDirectory(prefix=".runtime-", dir=root) as temporary:
        package = Path(temporary) / "venator_vault"
        package.mkdir(mode=0o700)
        for name, data in contents.items():
            path = package / name
            path.write_bytes(data)
            path.chmod(0o600)
            ops._sync(path)
        ops._sync_directory(package)
        ops._sync_directory(temporary)
        os.rename(temporary, version)
        ops._sync_directory(root)
    return version


def install_agent(cache, *, host="kyberdex-host", remote_root=REMOTE_ROOT):
    if sys.platform != "darwin":
        raise ValueError("The replica launch agent is for the owner's Mac.")
    ssh_command(host, remote_root)  # Validate before installing a scheduled transfer.
    cache = private_directory(cache)
    directory = Path.home() / "Library/LaunchAgents"
    if directory.is_symlink():
        raise ValueError("LaunchAgents directory cannot be a symlink.")
    directory.mkdir(exist_ok=True, mode=0o700)
    path = directory / f"{LABEL}.plist"
    runtime = bundled_runtime(cache)
    arguments = [str(Path(sys.executable).resolve()), "-m", "venator_vault.replica", "pull", "--cache", str(cache),
                 "--host", host, "--remote-root", remote_root]
    if path.exists() or path.is_symlink():
        if path.is_symlink() or plistlib.loads(path.read_bytes()).get("Label") != LABEL:
            raise ValueError("An unrelated launch agent occupies the backup path.")
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    definition = {"Label": LABEL, "ProgramArguments": arguments, "StartInterval": 3600, "RunAtLoad": True,
                  "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 300,
                  "ProcessType": "Background", "LowPriorityIO": True, "Nice": 10,
                  "StandardOutPath": str(cache / "agent.log"), "StandardErrorPath": str(cache / "agent.log"),
                  "WorkingDirectory": str(runtime),
                  "EnvironmentVariables": {"PYTHONPATH": str(runtime)}}
    # plistlib preserves spaces in absolute paths as argument boundaries.
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".kyberdex-", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(plistlib.dumps(definition))
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.chmod(0o600)
        os.replace(temporary, path)
        ops._sync_directory(directory)
    finally:
        temporary.unlink(missing_ok=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"installed": True, "label": LABEL, "interval_seconds": 3600, "cache": str(cache)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Private verified Pi backup copies on the owner's Mac")
    parser.add_argument("command", choices=("export", "pull", "install", "status"))
    parser.add_argument("--cache", type=Path, default=default_cache())
    parser.add_argument("--host", default=os.environ.get("VAULT_REPLICA_HOST", "kyberdex-host"))
    parser.add_argument("--remote-root", default=os.environ.get("VAULT_REPLICA_ROOT", REMOTE_ROOT))
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == "export":
            # The systemd configuration contains paths/settings, not token bytes.
            allowed = {"VAULT_ROOT", "VAULT_DATABASE"}
            path = Path.home() / ".config/venator-vault/service.env"
            if path.exists():
                for line in path.read_text().splitlines():
                    key, sep, value = line.partition("=")
                    if sep and key in allowed:
                        os.environ[key] = value
            export_snapshot(Settings.from_env(), sys.stdout.buffer)
            return 0
        result = (pull(args.cache, host=args.host, remote_root=args.remote_root) if args.command == "pull" else install_agent(args.cache, host=args.host, remote_root=args.remote_root)
                  if args.command == "install" else status(args.cache))
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, sqlite3.Error, tarfile.TarError, subprocess.SubprocessError) as error:
        print(f"Backup replica failed ({type(error).__name__}); existing verified copies retained.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
