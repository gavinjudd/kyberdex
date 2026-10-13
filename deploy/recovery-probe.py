"""Opt-in, disposable user-systemd recovery drill; no gateway, credentials, or game data.

Uses accelerated durations to exercise clean exits, six successive restarts, a
stalled watchdog with forced termination, and a deliberate stop that stays stopped.
This does not simulate lost power, filesystem damage, or Discord availability.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path


def events(path):
    try:
        return [json.loads(line) for line in path.read_text().splitlines()]
    except FileNotFoundError:
        return []


def record(path, event, **fields):
    with path.open("a") as stream:
        stream.write(json.dumps({"event": event, **fields}) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def notify(message):
    address = os.environ["NOTIFY_SOCKET"]
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        channel.settimeout(1)
        channel.sendto(message.encode(), address)


def child(path):
    attempt = sum(item["event"] == "start" for item in events(path)) + 1
    record(path, "start", attempt=attempt, pid=os.getpid())
    if attempt <= 6:
        # The first clean exit also must recover under Restart=always.
        notify("READY=1")
        time.sleep(0.1)
        return 0 if attempt == 1 else 71
    if attempt == 7:
        # Ignore graceful termination so TimeoutAbortSec must ultimately kill it.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        notify("READY=1")
        record(path, "intentional_watchdog_stall", attempt=attempt)
        while True:
            time.sleep(1)
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    notify("READY=1\nWATCHDOG=1")
    record(path, "healthy", attempt=attempt)
    while not stopped:
        notify("WATCHDOG=1")
        time.sleep(0.1)
    record(path, "graceful_stop", attempt=attempt)
    return 0


def command(*args, check=True):
    return subprocess.run(args, capture_output=True, text=True, check=check, timeout=12)


def show(unit, *properties):
    result = command("systemctl", "--user", "show", unit, *(f"--property={key}" for key in properties), check=False)
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def run_probe():
    if sys.platform != "linux":
        raise SystemExit("This explicit drill requires Linux user systemd.")
    unit = f"kyberdex-recovery-probe-{os.getpid()}-{uuid.uuid4().hex[:8]}.service"
    # Safety boundary: only this freshly created exact unit may be stopped/reset.
    before = show("venator-vault.service", "ActiveState", "UnitFileState", "MainPID")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    with tempfile.TemporaryDirectory(prefix="venator-vault-probe-", dir=runtime) as directory:
        event_path = Path(directory) / "events.jsonl"
        properties = {
            "Type": "notify", "NotifyAccess": "main", "Restart": "always", "RestartSec": "0.2s",
            "StartLimitIntervalSec": "0", "WatchdogSec": "2s", "WatchdogSignal": "SIGTERM",
            "TimeoutStartSec": "5s", "TimeoutStopSec": "1s", "TimeoutAbortSec": "1s",
            "KillSignal": "SIGTERM", "KillMode": "control-group", "SendSIGKILL": "yes", "LimitCORE": "0",
            "UMask": "0077", "NoNewPrivileges": "yes", "RestrictAddressFamilies": "AF_UNIX",
            "MemoryMax": "64M", "TasksMax": "8", "CPUQuota": "25%",
        }
        observed = {}
        try:
            command("systemd-run", "--user", "--quiet", "--no-block", "--collect", f"--unit={unit}",
                    *(f"--property={key}={value}" for key, value in properties.items()),
                    sys.executable, str(Path(__file__).resolve()), "--child", str(event_path))
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if any(item["event"] == "healthy" for item in events(event_path)):
                    observed = show(unit, "ActiveState", "NRestarts", "Result", "WatchdogUSec", "RestartUSec")
                    if observed.get("ActiveState") == "active":
                        break
                time.sleep(0.1)
            else:
                raise RuntimeError("Recovery probe did not become healthy within 25 seconds.")
            if observed.get("ActiveState") != "active" or int(observed.get("NRestarts", "0")) != 7:
                raise RuntimeError(f"Unexpected isolated restart result: {observed}")
            command("systemctl", "--user", "stop", unit)
            # Longer than the accelerated restart delay: a deliberate stop must stay stopped.
            time.sleep(0.6)
            history = events(event_path)
            if sum(item["event"] == "start" for item in history) != 8:
                raise RuntimeError("The disposable unit restarted after an explicit stop.")
            if not any(item["event"] == "graceful_stop" and item["attempt"] == 8 for item in history):
                raise RuntimeError("The healthy disposable process did not shut down gracefully.")
        finally:
            command("systemctl", "--user", "stop", unit, check=False)
            command("systemctl", "--user", "reset-failed", unit, check=False)
        after = show("venator-vault.service", "ActiveState", "UnitFileState", "MainPID")
        if before != after:
            raise RuntimeError("Gateway state changed during the isolated drill; investigate independently.")
        return {
            "status": "isolated_systemd_recovery_pass", "at": datetime.now(UTC).isoformat(),
            "unit": unit, "accelerated_properties": properties, "observed_before_stop": observed,
            "events": history, "gateway_before": before, "gateway_after": after,
            "gateway_started_by_probe": False, "credentials_or_game_data_accessed": False,
            "limits": ["Accelerated synthetic process, not the real Discord gateway.",
                       "No reboot, power cut, network cut, or storage failure was performed."],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run", action="store_true", help="Run only the disposable accelerated systemd drill")
    group.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        return child(args.child)
    print(json.dumps(run_probe(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
