"""Exercise boot activation boundaries without changing the host's service manager."""

import configparser
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def unit(name):
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.read(DEPLOY / name)
    return config


def test_gateway_recovers_after_outages_without_bypassing_launch_gates():
    config = unit("venator-vault.service")
    service = config["Service"]
    assert config["Unit"]["StartLimitIntervalSec"] == "0"
    assert "StartLimitBurst" not in config["Unit"]
    assert "network-online.target" not in config["Unit"].get("Wants", "")
    assert service["Restart"] == "always"
    assert service["RestartSec"] == "60"
    assert service["Type"] == "notify"
    assert service["NotifyAccess"] == "main"
    assert service["WatchdogSec"] == "120"
    assert service["WatchdogSignal"] == "SIGTERM"
    assert service["TimeoutAbortSec"] == service["TimeoutStopSec"] == "45"
    assert service["TimeoutStartSec"] == "300"
    assert service["KillMode"] == "control-group"
    assert service["KillSignal"] == "SIGTERM"
    assert service["SendSIGKILL"] == "yes"
    assert service["LimitCORE"] == "0"
    preflight = service["ExecStartPre"]
    assert not preflight.startswith("-")
    assert preflight.endswith("check --require-art --require-token-file")
    assert service["ExecStart"].endswith("-m venator_vault run")
    preflights = [line for line in (DEPLOY / "venator-vault.service").read_text().splitlines()
                  if line.startswith("ExecStartPre=")]
    assert len(preflights) == 2
    assert preflights[0].endswith("deploy/check-clock.py")
    assert preflights[1].endswith("check --require-art --require-token-file")


def test_backup_catches_up_hourly_and_retains_one_week():
    backup = unit("venator-vault-backup.service")["Service"]
    timer = unit("venator-vault-backup.timer")["Timer"]
    assert backup["ExecStart"].endswith("backup --keep 168")
    assert backup["TimeoutStartSec"] == "300"
    assert timer["OnCalendar"] == "hourly"
    assert timer.getboolean("Persistent")
    assert int(timer["RandomizedDelaySec"]) <= 120
    assert timer["Unit"] == "venator-vault-backup.service"


@pytest.fixture
def activation(tmp_path):
    home = tmp_path / "home"
    root = home / "kyberdex"
    root.mkdir(parents=True)
    shutil.copytree(DEPLOY, root / "deploy")
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    installed = unit_dir / "venator-vault.service"
    shutil.copyfile(root / "deploy" / installed.name, installed)
    installed.chmod(0o600)
    config = home / ".config" / "venator-vault" / "service.env"
    config.parent.mkdir()
    # EnvironmentFile data is not shell code. A shell-source regression must fail.
    sentinel = tmp_path / "shell-was-executed"
    config.write_text(f"VAULT_ROOT={root}\nDONT_EXECUTE=$(touch {sentinel})\n")
    config.chmod(0o600)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    command = binaries / "fake-command"
    command.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['VAULT_TEST_LOG'], 'a') as stream:
    stream.write(json.dumps([name, *args]) + '\\n')
if name == 'uname':
    print('Linux' if args == ['-s'] else 'aarch64')
elif name == 'loginctl':
    print(os.environ.get('VAULT_TEST_LINGER', 'yes'))
elif name == 'systemctl':
    if 'show' in args:
        prop = next(value.split('=', 1)[1] for value in args if value.startswith('--property='))
        values = {
            'ActiveState': os.environ.get('VAULT_TEST_STATE', 'inactive'),
            'FragmentPath': str(pathlib.Path.home() / '.config/systemd/user/venator-vault.service'),
            'DropInPaths': os.environ.get('VAULT_TEST_DROPINS', ''),
            'NeedDaemonReload': os.environ.get('VAULT_TEST_RELOAD', 'no'),
        }
        print(values[prop])
elif name == 'systemd-run':
    sys.exit(int(os.environ.get('VAULT_TEST_GATE_EXIT', '0')))
else:
    raise SystemExit('Unexpected mock command')
''')
    command.chmod(0o755)
    for name in ("uname", "loginctl", "systemctl", "systemd-run"):
        (binaries / name).symlink_to(command)
    log = tmp_path / "commands.jsonl"
    env = dict(os.environ, HOME=str(home), PATH=f"{binaries}:{os.environ['PATH']}", VAULT_TEST_LOG=str(log))

    def run(*args, **overrides):
        log.write_text("")
        result = subprocess.run(
            ["sh", str(root / "deploy/enable-boot.sh"), *args],
            env=dict(env, **overrides), capture_output=True, text=True, timeout=10, check=False,
        )
        commands = [json.loads(line) for line in log.read_text().splitlines()]
        assert not sentinel.exists()
        assert not any(command[0] == "systemctl" and ("start" in command or "--now" in command) for command in commands)
        return result, commands

    return run, installed, config


def test_boot_validation_uses_service_environment_and_never_enables(activation):
    run, _, config = activation
    result, commands = run()
    assert result.returncode == 0, result.stderr
    gate = next(command for command in commands if command[0] == "systemd-run")
    assert f"--property=EnvironmentFile={config}" in gate
    assert "--wait" in gate and "--collect" in gate
    assert "--property=RuntimeMaxSec=300" in gate
    assert any(value.startswith("--property=ExecStartPre=") and value.endswith("deploy/check-clock.py") for value in gate)
    assert gate[-4:] == ["venator_vault", "check", "--require-art", "--require-token-file"]
    assert not any("enable" in command for command in commands)


def test_explicit_enable_occurs_after_successful_launch_check(activation):
    run, _, _ = activation
    result, commands = run("--enable-boot")
    assert result.returncode == 0, result.stderr
    enabled = commands.index(["systemctl", "--user", "enable", "venator-vault.service"])
    checked = next(i for i, command in enumerate(commands) if command[0] == "systemd-run")
    assert checked < enabled
    assert "remains stopped" in result.stdout


@pytest.mark.parametrize("overrides,expected", [
    ({"VAULT_TEST_GATE_EXIT": "1"}, "gate"),
    ({"VAULT_TEST_LINGER": "no"}, "linger"),
    ({"VAULT_TEST_STATE": "active"}, "stopped"),
    ({"VAULT_TEST_STATE": "activating"}, "stopped"),
    ({"VAULT_TEST_DROPINS": "/tmp/override.conf"}, "overrides"),
    ({"VAULT_TEST_RELOAD": "yes"}, "daemon-reload"),
])
def test_activation_refuses_missing_readiness_or_unsafe_service_state(activation, overrides, expected):
    run, _, _ = activation
    result, commands = run("--enable-boot", **overrides)
    assert result.returncode != 0
    assert not any("enable" in command for command in commands)
    if expected == "gate":
        assert any(command[0] == "systemd-run" for command in commands)
    else:
        assert expected in result.stderr
        assert not any(command[0] == "systemd-run" for command in commands)


@pytest.mark.parametrize("defect", ["stale", "symlink", "public_config"])
def test_activation_refuses_stale_units_or_unprotected_configuration(activation, defect):
    run, installed, config = activation
    if defect == "stale":
        installed.write_text(installed.read_text().replace("RestartSec=60", "RestartSec=15"))
    elif defect == "symlink":
        original = installed.with_suffix(".original")
        installed.rename(original)
        installed.symlink_to(original)
    else:
        config.chmod(0o644)
    result, commands = run("--enable-boot")
    assert result.returncode != 0
    assert not any(command[0] == "systemd-run" or "enable" in command for command in commands)


def test_unknown_boot_option_does_not_contact_service_manager(activation):
    run, _, _ = activation
    result, commands = run("--now")
    assert result.returncode == 2
    assert commands == []


def test_staging_never_enables_gateway():
    installer = (DEPLOY / "install-user.sh").read_text()
    assert "systemctl --user disable venator-vault.service" in installer
    assert "systemctl --user enable --now venator-vault-backup.timer" in installer
    assert "systemctl --user start venator-vault.service" not in installer
    assert "systemctl --user enable venator-vault.service" not in installer


def test_deploy_shell_syntax():
    for path in DEPLOY.glob("*.sh"):
        subprocess.run(["sh", "-n", str(path)], check=True, capture_output=True)


@pytest.mark.parametrize("value,returncode,expected", [
    ("yes\n", 0, 0), ("true\n", 0, 0), ("no\n", 0, 1), ("false\n", 0, 1),
    ("", 0, 1), ("unknown", 0, 1), ("yes\n", 1, 1),
])
def test_clock_gate_requires_positive_current_sync(monkeypatch, capsys, value, returncode, expected):
    spec = importlib.util.spec_from_file_location("vault_clock_check", DEPLOY / "check-clock.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def query(args, **kwargs):
        assert args == ["/usr/bin/timedatectl", "show", "--property=NTPSynchronized", "--value"]
        assert kwargs["timeout"] == 5
        return subprocess.CompletedProcess(args, returncode, value, "")

    monkeypatch.setattr(module.subprocess, "run", query)
    assert module.main() == expected
    if expected:
        assert "startup is blocked" in capsys.readouterr().err


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.TimeoutExpired("timedatectl", 5)])
def test_clock_gate_fails_closed_if_query_unavailable(monkeypatch, error):
    spec = importlib.util.spec_from_file_location("vault_clock_check", DEPLOY / "check-clock.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def query(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(module.subprocess, "run", query)
    assert module.main() == 1
