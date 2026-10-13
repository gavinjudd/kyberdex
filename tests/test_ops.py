import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from venator_vault import __main__ as ops


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "state").mkdir()
    catalog = tmp_path / "data/cards.json"
    catalog.write_text(json.dumps({"schema_version": 1, "cards": [{"id": "clone"}]}))
    result = SimpleNamespace(root=tmp_path, database=tmp_path / "state/vault.sqlite3", catalog=catalog, token_file=None)
    for name in ["vault.sqlite3", "runtime.sqlite3"]:
        with closing(sqlite3.connect(tmp_path / "state" / name)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE sample(value INTEGER)")
            db.execute("INSERT INTO sample VALUES(10)")
            db.commit()
    return result


@pytest.fixture
def presentation_assets(settings):
    """Use the actual reviewed fonts/finish; unrelated ops fixtures stay small."""
    source = Path(__file__).resolve().parents[1] / "assets"
    for directory in ("fonts", "finishes"):
        shutil.copytree(source / directory, settings.root / "assets" / directory)


def value(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT value FROM sample").fetchone()[0]


def update(path, number):
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE sample SET value=?", (number,))
        db.commit()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_snapshot_pair_includes_catalog_and_verified_permissions(settings):
    destination = settings.root / "manual"
    result = ops.backup(settings, destination)
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["format"] == ops.BACKUP_FORMAT
    assert manifest["managed"] is False
    assert set(manifest["files"]) == {"vault.sqlite3", "runtime.sqlite3", "cards.json"}
    assert result["catalog_sha256"] == digest(settings.catalog)
    for name, expected in manifest["files"].items():
        assert digest(destination / name) == expected
        assert (destination / name).stat().st_mode & 0o777 == 0o600
    assert not list(destination.glob("*-wal"))
    assert ops.restore(settings, destination)["applied"] is False


def test_catalog_mismatch_never_changes_state(settings):
    destination = settings.root / "manual"
    ops.backup(settings, destination)
    update(settings.database, 20)
    settings.catalog.write_text(json.dumps({"schema_version": 1, "cards": [{"id": "different"}]}))
    with pytest.raises(ValueError, match="Catalog checksum mismatch"):
        ops.restore(settings, destination, apply=True)
    assert value(settings.database) == 20
    assert not (settings.root / "backups").exists()


def test_bad_checksum_and_corrupt_sqlite_refused(settings):
    destination = settings.root / "manual"
    ops.backup(settings, destination)
    (destination / "vault.sqlite3").write_bytes(b"not a sqlite database")
    with pytest.raises(ValueError, match="checksum mismatch"):
        ops.restore(settings, destination, apply=True)
    manifest = json.loads((destination / "manifest.json").read_text())
    manifest["files"]["vault.sqlite3"] = digest(destination / "vault.sqlite3")
    (destination / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(sqlite3.DatabaseError):
        ops.restore(settings, destination, apply=True)
    assert value(settings.database) == 10


def test_restore_pair_replaces_stale_wal_and_keeps_rollback(settings):
    destination = settings.root / "manual"
    ops.backup(settings, destination)
    live = sqlite3.connect(settings.database)
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.execute("UPDATE sample SET value=30")
    live.commit()
    old_wal = settings.database.with_name(settings.database.name + "-wal").read_bytes()
    live.close()
    settings.database.with_name(settings.database.name + "-wal").write_bytes(old_wal)
    update(settings.database.parent / "runtime.sqlite3", 40)
    result = ops.restore(settings, destination, apply=True)
    assert value(settings.database) == 10
    assert value(settings.database.parent / "runtime.sqlite3") == 10
    rollback = Path(result["rollback"])
    assert value(rollback / "vault.sqlite3") == 30
    assert value(rollback / "runtime.sqlite3") == 40
    assert json.loads((rollback / "manifest.json").read_text())["managed"] is False
    for name in ["vault.sqlite3", "runtime.sqlite3"]:
        assert not (settings.database.parent / (name + "-wal")).exists()
        with closing(sqlite3.connect(settings.database.parent / name)) as db:
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_restore_failure_rolls_back_touched_pair(settings):
    destination = settings.root / "manual"
    ops.backup(settings, destination)
    update(settings.database, 30)
    update(settings.database.parent / "runtime.sqlite3", 40)
    replace = ops.os.replace

    def fail_runtime(source, target):
        if Path(target).name == "runtime.sqlite3":
            raise OSError("Simulated disk failure")
        return replace(source, target)

    with (
        patch.object(ops.os, "replace", side_effect=fail_runtime),
        pytest.raises(OSError, match="Simulated disk failure"),
    ):
        ops.restore(settings, destination, apply=True)
    assert value(settings.database) == 30
    assert value(settings.database.parent / "runtime.sqlite3") == 40
    assert not list(settings.database.parent.glob(".*.restore-*"))


def test_restore_without_runtime_cannot_replace_existing_pair(settings):
    runtime = settings.database.parent / "runtime.sqlite3"
    runtime.unlink()
    destination = settings.root / "manual"
    ops.backup(settings, destination)
    with closing(sqlite3.connect(runtime)) as db:
        db.execute("CREATE TABLE sample(value INTEGER)")
        db.execute("INSERT INTO sample VALUES(99)")
        db.commit()
    with pytest.raises(ValueError, match="complete database pair"):
        ops.restore(settings, destination, apply=True)
    assert value(runtime) == 99


def test_retention_only_prunes_managed_automatic_snapshots(settings):
    explicit = settings.root / "backups/auto-20000101T000000Z-1"
    ops.backup(settings, explicit)
    before = settings.root / "backups/before-restore-1"
    ops.backup(settings, before)
    unknown = settings.root / "backups/auto-20000101T000000Z-2"
    unknown.mkdir()
    (unknown / "user-note.txt").write_text("Preserve this user content")
    created = []
    for _ in range(4):
        created.append(Path(ops.backup(settings, keep=2)["backup"]))
    assert not created[0].exists() and not created[1].exists()
    assert created[2].exists() and created[3].exists()
    assert explicit.exists() and before.exists() and unknown.exists()
    assert (unknown / "user-note.txt").read_text() == "Preserve this user content"


def test_worker_lock_excludes_second_process_and_restore(settings):
    destination = settings.root / "manual"
    ops.backup(settings, destination)
    with ops.worker_lock(settings):
        script = 'from pathlib import Path; from types import SimpleNamespace; from venator_vault.__main__ import worker_lock; worker_lock(SimpleNamespace(database=Path(__import__("sys").argv[1])))'
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        child = subprocess.run(
            [sys.executable, "-c", script, str(settings.database)],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert child.returncode != 0
        assert "already running" in child.stderr
        with pytest.raises(SystemExit, match="already running"):
            ops.restore(settings, destination, apply=True)
    with ops.worker_lock(settings):
        pass


def test_symlink_lock_and_backup_entries_refused(settings):
    outside = settings.root / "outside"
    outside.write_text("preserve")
    (settings.database.parent / "worker.lock").symlink_to(outside)
    with pytest.raises(OSError):
        ops.worker_lock(settings)
    assert outside.read_text() == "preserve"
    destination = settings.root / "manual"
    ops.backup(settings, destination)
    (destination / "cards.json").unlink()
    (destination / "cards.json").symlink_to(settings.catalog)
    with pytest.raises(ValueError, match="regular files"):
        ops.restore(settings, destination)


def test_token_metadata_does_not_read_contents_and_refuses_unsafe_files(settings):
    path = settings.root / "dedicated-token"
    path.write_text("test fixture, never a credential")
    path.chmod(0o600)
    with patch.object(Path, "read_text", side_effect=AssertionError("Credential contents must not be read")):
        assert ops.token_permissions(path, required=True)["safe_permissions"] is True
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0400 or 0600"):
        ops.token_permissions(path, required=True)
    path.chmod(0o600)
    link = settings.root / "token-link"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symlinks"):
        ops.token_permissions(link, required=True)
    assert ops.token_permissions(settings.root / "absent")["present"] is False
    with pytest.raises(ValueError, match="not been provisioned"):
        ops.token_permissions(settings.root / "absent", required=True)


def test_art_gate_only_requires_regular_and_current_enabled_events():
    from datetime import datetime

    now = datetime(2026, 10, 8, tzinfo=UTC)
    release = {"starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-11-01T00:00:00Z"}
    assert ops.art_required({"category": "canon"}, (), now)
    assert not ops.art_required({"category": "legends", "event_id": "archive", "release": release}, (), now)
    assert ops.art_required({"category": "legends", "event_id": "archive", "release": release}, ("archive",), now)
    assert not ops.art_required(
        {"category": "limited", "release": {"starts_at": "2027-01-01T00:00:00Z", "ends_at": "2027-02-01T00:00:00Z"}},
        (),
        now,
    )
    assert not ops.art_required(
        {"category": "limited", "release": {"starts_at": "2026-01-01T00:00:00Z", "ends_at": "2026-02-01T00:00:00Z"}},
        (),
        now,
    )


def test_launch_gate_requires_800_distinct_subjects_and_complete_metadata(settings, presentation_assets, monkeypatch):
    import types

    health = {"regular_subject_count": 799, "regular_subject_metadata_complete": True,
              "integrity": "ok", "journal_mode": "wal", "synchronous": 2,
              "foreign_keys": True, "foreign_key_violations": 0}

    class OfflineGame:
        def __init__(self, *args, **kwargs):
            self.catalog = {}

        def health(self):
            return health

    monkeypatch.setattr(
        ops.Settings, "from_env", classmethod(lambda cls: SimpleNamespace(**vars(settings), enabled_events=()))
    )
    # This test isolates subject metadata; scarcity and profile gates have their
    # own production-catalog integration tests.
    monkeypatch.setattr(ops, "audit_economy", lambda *a, **kw: {"ready": True})
    monkeypatch.setitem(sys.modules, "venator_vault.core", types.SimpleNamespace(Game=OfflineGame))
    assert ops.main(["check", "--require-art"]) == 1
    health["regular_subject_count"] = 800
    assert ops.main(["check", "--require-art"]) == 0
    health["regular_subject_metadata_complete"] = False
    assert ops.main(["check", "--require-art"]) == 1


def test_snapshot_freezes_both_database_writers(settings):
    original = ops._copy_snapshot
    checked = []

    def observe_guard(*args, **kwargs):
        for path in [settings.database, settings.database.parent / "runtime.sqlite3"]:
            with (
                closing(sqlite3.connect(path, timeout=0.01)) as contender,
                pytest.raises(sqlite3.OperationalError, match="locked"),
            ):
                contender.execute("UPDATE sample SET value=99")
            checked.append(path.name)
        return original(*args, **kwargs)

    with patch.object(ops, "_copy_snapshot", side_effect=observe_guard):
        ops.backup(settings, settings.root / "manual")
    assert set(checked) == {"vault.sqlite3", "runtime.sqlite3"}
    update(settings.database, 20)
    update(settings.database.parent / "runtime.sqlite3", 30)
    assert value(settings.database) == 20 and value(settings.database.parent / "runtime.sqlite3") == 30


def test_interrupted_restore_blocks_worker_until_validated_repair(settings):
    destination = settings.root / "manual"
    ops.backup(settings, destination)
    marker = settings.database.parent / ops.RESTORE_MARKER
    marker.write_text("{}")
    with pytest.raises(SystemExit, match="interrupted restore"):
        ops.worker_lock(settings)
    ops.restore(settings, destination, apply=True)
    assert not marker.exists()
    with ops.worker_lock(settings):
        pass


def test_failed_repair_keeps_existing_interruption_marker(settings):
    destination = settings.root / "manual"
    ops.backup(settings, destination)
    marker = settings.database.parent / ops.RESTORE_MARKER
    marker.write_text('{"rollback":"original-good-snapshot"}')
    with (
        patch.object(ops.os, "replace", side_effect=OSError("disk failure")),
        pytest.raises(OSError, match="disk failure"),
    ):
        ops.restore(settings, destination, apply=True)
    assert json.loads(marker.read_text())["rollback"] == "original-good-snapshot"
    with pytest.raises(SystemExit, match="interrupted restore"):
        ops.worker_lock(settings)


def test_discord_art_path_withholds_revoked_file(settings):
    from venator_vault.presentation import art_path

    path = settings.root / "assets/art/clone.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"old previously approved image")
    card = {"id": "clone", "art": "assets/art/clone.png", "art_status": "ready"}
    assert art_path(settings.root, card) == path
    for status in (None, "pending", "visual_qa_rejected", "generation_failed"):
        card["art_status"] = status
        with pytest.raises(ValueError, match="Artwork is not ready"):
            art_path(settings.root, card)


def test_art_gate_binds_reviewed_bytes_dimensions_and_subject(settings):
    art = settings.root / "assets/art"
    art.mkdir(parents=True)
    path = art / "clone.png"
    header = (
        b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + (1024).to_bytes(4, "big") + (1536).to_bytes(4, "big")
    )
    path.write_bytes(header + b"controlled test fixture")
    card = {"id": "clone", "subject_id": "clone-subject", "art": "assets/art/clone.png", "art_status": "ready"}
    entry = {
        "path": card["art"],
        "subject_id": card["subject_id"],
        "status": "ready",
        "visual_qa": "verified",
        "width": 1024,
        "height": 1536,
        "sha256": digest(path),
    }
    assert ops.verified_art_path(settings.root, card, {"clone": entry}) == path
    path.write_bytes(header + b"changed bytes")
    with pytest.raises(ValueError, match="checksum"):
        ops.verified_art_path(settings.root, card, {"clone": entry})
    entry["sha256"] = digest(path)
    entry["subject_id"] = "wrong-subject"
    with pytest.raises(ValueError, match="manifest entry"):
        ops.verified_art_path(settings.root, card, {"clone": entry})
    entry["subject_id"] = card["subject_id"]
    entry["visual_qa"] = "pending"
    with pytest.raises(ValueError, match="manifest entry"):
        ops.verified_art_path(settings.root, card, {"clone": entry})


@pytest.mark.parametrize("subject_count,missing_art", [(799, False), (800, True)])
def test_direct_run_refuses_launch_before_token_read_or_gateway(
    settings, presentation_assets, monkeypatch, subject_count, missing_art
):
    import types

    token = settings.root / "dedicated-token"
    token.write_text("This is only a controlled noncredential test fixture")
    token.chmod(0o600)
    supplied = SimpleNamespace(**vars(settings), enabled_events=(), guild_id=42)
    supplied.require_discord_configuration = lambda: ops.Settings.require_discord_configuration(supplied)
    supplied.token_file = token
    supplied.read_token = lambda: pytest.fail("No credential should be read before failed readiness")

    class OfflineGame:
        def __init__(self, *args, **kwargs):
            self.catalog = (
                {"clone": {"id": "clone", "category": "canon", "art_status": "pending", "art": "assets/art/clone.png"}}
                if missing_art
                else {}
            )

        def health(self):
            return {"regular_subject_count": subject_count, "regular_subject_metadata_complete": True}

    monkeypatch.setattr(ops.Settings, "from_env", classmethod(lambda cls: supplied))
    monkeypatch.setitem(sys.modules, "venator_vault.core", types.SimpleNamespace(Game=OfflineGame))
    monkeypatch.setitem(
        sys.modules,
        "venator_vault.bot",
        types.SimpleNamespace(VaultBot=lambda settings: pytest.fail("No gateway before readiness")),
    )
    assert ops.main(["run"]) == 1


@pytest.mark.parametrize("fault", ["missing_font", "tampered_foil", "malformed_finish_manifest"])
@pytest.mark.parametrize("command", [["check", "--require-art"], ["run"]])
def test_launch_rejects_broken_presentation_before_token_read_or_gateway(
    settings, presentation_assets, monkeypatch, capsys, fault, command
):
    import types

    from PIL import Image

    # Make both other launch gates valid, including a real reviewed source PNG,
    # so this failure can only come from the presentation asset boundary.
    art_root = settings.root / "assets/art"
    art_root.mkdir()
    image_path = art_root / "clone.png"
    Image.new("RGB", (200, 300), "navy").save(image_path)
    card = {"id": "clone", "category": "canon", "art_status": "ready", "art": "assets/art/clone.png"}
    (art_root / "manifest.json").write_text(json.dumps({"assets": {"clone": {
        "status": "ready", "visual_qa": "verified", "path": card["art"],
        "width": 200, "height": 300, "sha256": digest(image_path),
    }}}))
    supplied = SimpleNamespace(**vars(settings), enabled_events=(), guild_id=42)
    supplied.require_discord_configuration = lambda: ops.Settings.require_discord_configuration(supplied)
    token = settings.root / "dedicated-token"
    token.write_text("This is only a controlled noncredential test fixture")
    token.chmod(0o600)
    supplied.token_file = token
    supplied.read_token = lambda: pytest.fail("Presentation must pass before reading a credential")

    class OfflineGame:
        def __init__(self, *args, **kwargs):
            self.catalog = {card["id"]: card}

        def health(self):
            return {"regular_subject_count": 800, "regular_subject_metadata_complete": True}

    monkeypatch.setattr(ops.Settings, "from_env", classmethod(lambda cls: supplied))
    monkeypatch.setitem(sys.modules, "venator_vault.core", types.SimpleNamespace(Game=OfflineGame))
    monkeypatch.setitem(sys.modules, "venator_vault.bot", types.SimpleNamespace(
        VaultBot=lambda settings: pytest.fail("Presentation must pass before creating the gateway")
    ))
    healthy = ops.readiness(supplied)
    assert healthy["launch_catalog_ready"] and healthy["launch_art_ready"]
    assert healthy["launch_presentation_ready"] and healthy["presentation_error"] is None
    if fault == "missing_font":
        manifest = json.loads((settings.root / "assets/fonts/manifest.json").read_text())
        (settings.root / "assets/fonts" / manifest["fonts"][0]["file"]).unlink()
    elif fault == "tampered_foil":
        manifest = json.loads((settings.root / "assets/finishes/manifest.json").read_text())
        (settings.root / "assets/finishes" / manifest["file"]).write_bytes(b"tampered")
    else:
        (settings.root / "assets/finishes/manifest.json").write_text('{"private-diagnostic":')
    assert ops.main(command) == 1
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["launch_catalog_ready"] and report["launch_art_ready"]
    assert report["launch_presentation_ready"] is False
    assert report["presentation_error"] in ("ValueError", "JSONDecodeError")
    assert str(settings.root) not in output.out + output.err
    assert "private-diagnostic" not in output.out + output.err
