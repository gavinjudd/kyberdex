"""Operational entry points. Check/backup/restore never connect to Discord."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import sys
import time
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings
from .economy import audit_economy

BACKUP_FORMAT = "venator-vault-backup"
BACKUP_VERSION = 1
DEFAULT_RETENTION = 168
MINIMUM_REGULAR_SUBJECTS = 800
AUTO_NAME = re.compile(r"auto-\d{8}T\d{6}Z-\d+")
DIGEST = re.compile(r"[0-9a-f]{64}")
RESTORE_MARKER = "restore-in-progress.json"


def _file_lock(settings, filename, message):
    settings.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(settings.database.parent / filename, flags, 0o600)
    handle = os.fdopen(descriptor, "r+")
    try:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("The operations lock is not an owned regular file.")
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise SystemExit(message)
    except BaseException:
        handle.close()
        raise
    return handle


def worker_lock(settings, allow_interrupted_restore=False):
    handle = _file_lock(
        settings,
        "worker.lock",
        "The bot worker is already running. Stop it before starting or restoring another worker.",
    )
    marker = settings.database.parent / RESTORE_MARKER
    if not allow_interrupted_restore and (marker.exists() or marker.is_symlink()):
        handle.close()
        raise SystemExit(
            "An interrupted restore needs recovery before starting the worker. Restore a validated matching snapshot while stopped."
        )
    return handle


def _maintenance_lock(settings):
    return _file_lock(
        settings, "maintenance.lock", "Another backup or restore operation is running. Try again after it finishes."
    )


def token_permissions(path, required=False):
    """Check file metadata only: this helper never opens or prints a credential."""
    if path is None:
        if required:
            raise ValueError("DISCORD_TOKEN_FILE must name the dedicated bot's protected token file.")
        return {"configured": False, "present": False, "safe_permissions": False}
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if required:
            raise ValueError("The dedicated bot token file has not been provisioned.")
        return {"configured": True, "present": False, "safe_permissions": False}
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) not in (0o400, 0o600):
        raise ValueError(
            "The dedicated token must be an owned regular file with mode 0400 or 0600; symlinks are refused."
        )
    return {"configured": True, "present": True, "safe_permissions": True}


def _hash(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def art_required(card, enabled_events, now=None, *, enabled_releases=()):
    """Validate every enabled upcoming release before the worker can start."""
    now = now or datetime.now(UTC)
    event_id = card.get("event_id")
    if event_id and event_id not in enabled_events:
        return False
    release_id = card.get("release_id")
    if release_id and release_id not in enabled_releases:
        return False
    release = card.get("release")
    if release:
        start = datetime.fromisoformat(release["starts_at"])
        end = datetime.fromisoformat(release["ends_at"])
        return now < end if release_id or event_id else start <= now < end
    return not (card.get("category") in ("limited", "legends") or card.get("availability") == "event_only")


def _art_manifest(root):
    path = root / "assets/art/manifest.json"
    try:
        if (
            (root / "assets").is_symlink()
            or (root / "assets/art").is_symlink()
            or path.is_symlink()
            or path.stat().st_size > 8 * 1024 * 1024
        ):
            return {}
        manifest = json.loads(path.read_text())
        return manifest.get("assets", {}) if isinstance(manifest, dict) else {}
    except (OSError, ValueError):
        return {}


def verified_art_path(root, card, assets):
    """Bind the selected public file to the producer's reviewed asset manifest."""
    from .presentation import art_path

    entry = assets.get(card["id"]) if isinstance(assets, dict) else None
    if (
        not isinstance(entry, dict)
        or card.get("art_status") != "ready"
        or entry.get("status") != "ready"
        or entry.get("visual_qa") != "verified"
        or entry.get("path") != card.get("art")
        or (card.get("subject_id") and entry.get("subject_id") != card["subject_id"])
        or not isinstance(entry.get("sha256"), str)
        or not DIGEST.fullmatch(entry["sha256"])
    ):
        raise ValueError("Artwork does not have a matching reviewed manifest entry.")
    selected = root / card["art"]
    if selected.is_symlink() or (root / "assets").is_symlink() or (root / "assets/art").is_symlink():
        raise ValueError("Selected artwork must be a local regular asset, not a symlink.")
    path = art_path(root, card)
    with path.open("rb") as handle:
        header = handle.read(24)
    if (
        len(header) < 24
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or header[12:16] != b"IHDR"
        or int.from_bytes(header[16:20], "big") != entry.get("width")
        or int.from_bytes(header[20:24], "big") != entry.get("height")
        or entry.get("width", 0) <= 0
        or entry.get("height", 0) <= 0
        or entry["width"] * 3 != entry["height"] * 2
        or _hash(path) != entry["sha256"]
    ):
        raise ValueError("Selected artwork differs from its reviewed PNG dimensions or checksum.")
    return path


def _storage_ready(health):
    return (health.get("integrity") == "ok" and health.get("journal_mode") == "wal"
            and health.get("synchronous") == 2 and health.get("foreign_keys") is True
            and health.get("foreign_key_violations") == 0)


def readiness(settings, require_token=False):
    """One offline gate used by direct CLI launch and systemd preflight alike."""
    from .card_render import CardRenderer
    from .core import Game
    from .runtime import Runtime

    permissions = token_permissions(settings.token_file, required=require_token)
    enabled_releases = getattr(settings, "enabled_releases", ())
    game = Game(settings.database, settings.catalog, enabled_events=settings.enabled_events,
                enabled_releases=enabled_releases)
    health = game.health()
    runtime_path = settings.database.with_name("runtime.sqlite3")
    runtime_health = Runtime(runtime_path).health() if runtime_path.exists() else None
    storage_ready = _storage_ready(health) and (runtime_health is None or _storage_ready(runtime_health))
    catalog_ready = (
        health.get("regular_subject_metadata_complete") is True
        and health.get("regular_subject_count", 0) >= MINIMUM_REGULAR_SUBJECTS
    )
    assets = _art_manifest(settings.root)
    missing, provisional = [], []
    for card in game.catalog.values():
        if not art_required(card, settings.enabled_events, enabled_releases=enabled_releases):
            continue
        if card.get("power_profile", {}).get("basis") not in ("curated_design", "depiction_override"):
            provisional.append(card["id"])
        try:
            verified_art_path(settings.root, card, assets)
        except (ValueError, OSError):
            missing.append(card["id"])
    presentation_error = None
    try:
        CardRenderer(settings.root).verify_assets()
    except (ValueError, OSError, KeyError, TypeError) as error:
        # The report can be logged or shown publicly; asset paths and malformed
        # manifest contents are not useful diagnostics outside the host.
        presentation_error = type(error).__name__
    economy = audit_economy(game.catalog.values(), tier_weights=getattr(game, "spawn_weights", None),
                            limited_share_percent=getattr(game, "limited_share_percent", 25),
                            interval=getattr(settings, "spawn_interval", 900),
                            minimum_delay=getattr(settings, "drop_duration", 180))
    return {
        "catalog_cards": len(game.catalog),
        "missing_art_count": len(missing),
        "missing_art_sample": missing[:12],
        "storage": health,
        "runtime_storage": runtime_health,
        "launch_storage_ready": storage_ready,
        "launch_catalog_ready": catalog_ready,
        "launch_art_ready": not missing,
        "launch_presentation_ready": presentation_error is None,
        "launch_balance_ready": not provisional,
        "provisional_profile_count": len(provisional),
        "provisional_profile_sample": provisional[:12],
        "launch_economy_ready": economy["ready"],
        "economy": economy,
        "presentation_error": presentation_error,
        "minimum_regular_subjects": MINIMUM_REGULAR_SUBJECTS,
        "token_file": permissions,
    }


def _regular(path):
    return stat.S_ISREG(Path(path).lstat().st_mode)


def _integrity(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        if (
            db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
            or db.execute("PRAGMA foreign_key_check").fetchone()
        ):
            raise ValueError("Database integrity or foreign-key check failed; nothing restored.")


def _sync(path):
    with Path(path).open("rb") as handle:
        os.fsync(handle.fileno())


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _activate_wal(path):
    with closing(sqlite3.connect(path)) as db:
        if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            raise ValueError("The restored database could not activate WAL mode.")
        if (
            db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
            or db.execute("PRAGMA foreign_key_check").fetchone()
        ):
            raise ValueError("The restored database failed validation after activating WAL.")


def _restore_marker(settings, source, rollback):
    marker = settings.database.parent / RESTORE_MARKER
    temporary = marker.with_name(f".{marker.name}.{time.time_ns()}")
    try:
        with temporary.open("x") as handle:
            json.dump(
                {
                    "format": "venator-vault-restore",
                    "source": str(source),
                    "rollback": str(rollback) if rollback else None,
                },
                handle,
            )
        temporary.chmod(0o600)
        _sync(temporary)
        os.replace(temporary, marker)
        _sync_directory(marker.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_snapshot(settings, destination, managed=False):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    hashes = {}
    for name in (settings.database.name, "runtime.sqlite3"):
        source = settings.database.parent / name
        if not source.exists():
            continue
        if not _regular(source):
            raise ValueError("A database snapshot source must be a regular file, not a symlink.")
        target = destination / name
        with (
            closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original,
            closing(sqlite3.connect(target)) as copy,
        ):
            original.backup(copy)
            copy.execute("PRAGMA journal_mode=DELETE")
        target.chmod(0o600)
        _integrity(target)
        _sync(target)
        hashes[name] = _hash(target)
    if settings.database.name not in hashes:
        raise ValueError("There is no game database to back up.")
    shutil.copyfile(settings.catalog, destination / "cards.json")
    (destination / "cards.json").chmod(0o600)
    json.loads((destination / "cards.json").read_text())
    _sync(destination / "cards.json")
    hashes["cards.json"] = _hash(destination / "cards.json")
    manifest = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "managed": managed,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "created_ns": time.time_ns(),
        "files": hashes,
        "game_database": settings.database.name,
    }
    marker = destination / "manifest.json"
    marker.write_text(json.dumps(manifest, indent=2))
    marker.chmod(0o600)
    _sync(marker)
    _sync_directory(destination)
    _sync_directory(destination.parent)
    return manifest


def _snapshot(settings, destination, managed=False):
    """Reserve both SQLite writers while copying a consistent database pair."""
    if not settings.database.exists():
        raise ValueError("There is no game database to back up.")
    runtime = settings.database.parent / "runtime.sqlite3"
    for source in (settings.database, runtime):
        if source.exists() and not _regular(source):
            raise ValueError("Database snapshot sources must be regular files, not symlinks.")
    with closing(sqlite3.connect(settings.database, timeout=30)) as guard:
        if runtime.exists():
            guard.execute("ATTACH DATABASE ? AS runtime_guard", (str(runtime),))
        guard.execute("BEGIN IMMEDIATE")
        try:
            return _copy_snapshot(settings, destination, managed)
        finally:
            guard.rollback()


def _backup_root(settings):
    directory = settings.root / "backups"
    if directory.is_symlink():
        raise ValueError("The managed backup directory must not be a symlink.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def _manifest(settings, source):
    source = Path(source)
    if source.is_symlink() or not source.is_dir() or not _regular(source / "manifest.json"):
        raise ValueError("The backup must be a regular snapshot directory with an owned-format manifest.")
    manifest = json.loads((source / "manifest.json").read_text())
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != BACKUP_FORMAT
        or manifest.get("version") != BACKUP_VERSION
    ):
        raise ValueError("Unsupported backup format; nothing restored.")
    allowed = {settings.database.name, "runtime.sqlite3", "cards.json"}
    files = manifest.get("files")
    if (
        not isinstance(files, dict)
        or set(files) - allowed
        or not {settings.database.name, "cards.json"} <= set(files)
        or manifest.get("game_database") != settings.database.name
        or any(not isinstance(value, str) or not DIGEST.fullmatch(value) for value in files.values())
    ):
        raise ValueError("The backup manifest contains unexpected files or invalid checksums.")
    for name in files:
        if not _regular(source / name):
            raise ValueError("Snapshot entries must be regular files, not symlinks.")
    return manifest


def _prune(settings, keep, *, preserve=None):
    if not 1 <= keep <= 168:
        raise ValueError("Backup retention must be between 1 and 168 automatic snapshots.")
    directory = _backup_root(settings)
    candidates = []
    for path in directory.iterdir():
        if (
            not AUTO_NAME.fullmatch(path.name)
            or path.is_symlink()
            or not path.is_dir()
            or path.stat().st_uid != os.getuid()
        ):
            continue
        try:
            manifest = _manifest(settings, path)
            if (
                manifest.get("managed") is not True
                or not isinstance(manifest.get("created_ns"), int)
                or {entry.name for entry in path.iterdir()} != set(manifest["files"]) | {"manifest.json"}
            ):
                continue
            candidates.append((manifest["created_ns"], path))
        except (OSError, ValueError, TypeError):
            continue
    candidates.sort(key=lambda row: row[0], reverse=True)
    if preserve is not None:
        # Clock correction after reboot must not delete the just-created backup.
        candidates.sort(key=lambda row: row[1] != Path(preserve))
    removed = []
    for _, path in candidates[keep:]:
        shutil.rmtree(path)
        removed.append(path.name)
    if removed:
        _sync_directory(directory)
    return removed


def backup(settings, destination=None, keep=DEFAULT_RETENTION):
    if not 1 <= keep <= 168:
        raise ValueError("Backup retention must be between 1 and 168 automatic snapshots.")
    with _maintenance_lock(settings):
        managed = destination is None
        if managed:
            directory = _backup_root(settings)
            destination = directory / (time.strftime("auto-%Y%m%dT%H%M%SZ-", time.gmtime()) + str(time.time_ns()))
        manifest = _snapshot(settings, destination, managed)
        removed = _prune(settings, keep, preserve=destination) if managed else []
    result = {
        "backup": str(destination),
        "files": list(manifest["files"]),
        "integrity": "ok",
        "catalog_sha256": manifest["files"]["cards.json"],
        "automatic": managed,
        "pruned_count": len(removed),
    }
    print(json.dumps(result))
    return result


def _validate_restore(settings, source):
    manifest = _manifest(settings, source)
    for name, expected in manifest["files"].items():
        path = Path(source) / name
        if _hash(path) != expected:
            raise ValueError("Backup checksum mismatch; nothing restored.")
        if name != "cards.json":
            _integrity(path)
    if _hash(settings.catalog) != manifest["files"]["cards.json"]:
        raise ValueError(
            "Catalog checksum mismatch. Restore requires the exact matching catalog snapshot; no state was changed."
        )
    return manifest


def _remove_wal(target):
    for suffix in ("-wal", "-shm"):
        target.with_name(target.name + suffix).unlink(missing_ok=True)


def restore(settings, source, apply=False):
    source = Path(source)
    manifest = _validate_restore(settings, source)
    files = manifest["files"]
    if not apply:
        result = {"valid": True, "applied": False, "files": list(files), "catalog_match": True}
        print(json.dumps(result))
        return result
    with ExitStack() as locks:
        locks.enter_context(worker_lock(settings, allow_interrupted_restore=True))
        locks.enter_context(_maintenance_lock(settings))
        _validate_restore(settings, source)  # Recheck after exclusive operations authority.
        if (settings.database.parent / "runtime.sqlite3").exists() and "runtime.sqlite3" not in files:
            raise ValueError("This snapshot lacks the existing runtime database; restore a complete database pair.")
        rollback = None
        if settings.database.exists():
            rollback = _backup_root(settings) / f"before-restore-{time.time_ns()}"
            _snapshot(settings, rollback, managed=False)
        staged = {}
        applied = []
        marker = settings.database.parent / RESTORE_MARKER
        marker_written = False
        try:
            for name in files:
                if name == "cards.json":
                    continue
                target = settings.database.parent / name
                temporary = target.with_name(f".{target.name}.restore-{time.time_ns()}")
                with (source / name).open("rb") as original, temporary.open("xb") as copy:
                    shutil.copyfileobj(original, copy)
                temporary.chmod(0o600)
                _integrity(temporary)
                _sync(temporary)
                staged[name] = temporary
            if not marker.exists():
                _restore_marker(settings, source, rollback)
                marker_written = True
            for name, temporary in staged.items():
                target = settings.database.parent / name
                applied.append(name)  # WAL removal itself changes recoverable state.
                _remove_wal(target)
                os.replace(temporary, target)
                _integrity(target)
                _activate_wal(target)
                _sync(target)
            _sync_directory(settings.database.parent)
            marker.unlink(missing_ok=True)
            _sync_directory(settings.database.parent)
            result = {
                "restored": True,
                "source": str(source),
                "catalog_match": True,
                "rollback": str(rollback) if rollback else None,
            }
            print(json.dumps(result))
            return result
        except BaseException:
            for name in applied:
                target = settings.database.parent / name
                _remove_wal(target)
                if rollback and (rollback / name).exists():
                    shutil.copyfile(rollback / name, target)
                    target.chmod(0o600)
                    _integrity(target)
                    _activate_wal(target)
                    _sync(target)
                else:
                    target.unlink(missing_ok=True)
            _sync_directory(settings.database.parent)
            if marker_written:
                marker.unlink(missing_ok=True)
                _sync_directory(settings.database.parent)
            raise
        finally:
            for temporary in staged.values():
                temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="KyberDex bot and operations")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="Connect the dedicated bot to Discord")
    setup = commands.add_parser("setup", help="Interactive token, server, channel and artwork setup")
    setup.add_argument("--no-start", action="store_true", help="Save and verify setup without offering to start")
    art = commands.add_parser("setup-art", help="Download pinned artwork sources and reproduce reviewed images")
    art.add_argument("--sources", type=Path, help="Recipe registry (default: data/art-sources.json)")
    art.add_argument("--workers", type=int, default=4, help="Concurrent downloads/transforms, 1–4 (default: 4)")
    check = commands.add_parser("check", help="Validate storage, catalog, artwork and card presentation without connecting")
    check.add_argument("--require-art", action="store_true")
    check.add_argument(
        "--require-token-file", action="store_true", help="Require safe credential metadata without reading it"
    )
    back = commands.add_parser("backup", help="Create an integrity-checked database snapshot")
    back.add_argument("--destination", type=Path, help="Explicit snapshots are never automatically pruned")
    back.add_argument("--keep", type=int, default=DEFAULT_RETENTION)
    recover = commands.add_parser("restore", help="Verify a backup; --apply replaces state while stopped")
    recover.add_argument("source", type=Path)
    recover.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        settings = Settings.from_env()
        if args.command == "setup":
            from .setup import run_setup
            with worker_lock(settings):
                result = run_setup(settings, no_start=args.no_start)
            if result["start"]:
                return main(["run"])
            return 0 if result["ready"] else 1
        if args.command == "setup-art":
            from .art_setup import setup_art
            with worker_lock(settings):
                report = setup_art(settings.root, sources=args.sources, workers=args.workers,
                                   progress=lambda message: print(message, file=sys.stderr, flush=True))
            print(json.dumps(report, indent=2))
            return 0 if report["complete"] else 1
        if args.command == "backup":
            backup(settings, args.destination, args.keep)
        elif args.command == "restore":
            restore(settings, args.source, args.apply)
        elif args.command == "check":
            with worker_lock(settings):
                report = readiness(settings, args.require_token_file)
                print(json.dumps(report, indent=2))
                if not report["launch_storage_ready"] or (args.require_art and not all(report[key] for key in (
                    "launch_art_ready", "launch_catalog_ready", "launch_presentation_ready",
                    "launch_balance_ready", "launch_economy_ready"
                ))):
                    return 1
        else:
            settings.require_discord_configuration()
            with worker_lock(settings):
                report = readiness(settings, require_token=True)
                print(json.dumps(report, indent=2))
                if not all(report[key] for key in (
                    "launch_art_ready", "launch_catalog_ready", "launch_presentation_ready",
                    "launch_balance_ready", "launch_economy_ready", "launch_storage_ready"
                )):
                    raise ValueError(
                        "Launch requires healthy durable storage, 800 distinct regular subjects, reviewed artwork and combat profiles, verified card fonts and finishes, and passing catalog scarcity checks."
                    )
                from .bot import VaultBot

                bot = VaultBot(settings)
                bot.run(settings.read_token(), log_level=logging.INFO)
    except (EOFError, KeyboardInterrupt):
        print("Setup interrupted; completed downloads and previously saved settings are preserved.", file=sys.stderr)
        return 1
    except (ValueError, OSError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
