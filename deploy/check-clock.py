"""Refuse gateway startup until the Pi confirms synchronized system time."""

import subprocess
import sys


def main():
    try:
        result = subprocess.run(
            ["/usr/bin/timedatectl", "show", "--property=NTPSynchronized", "--value"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is None or result.returncode != 0 or result.stdout.strip().lower() not in {"yes", "true"}:
        print("Clock synchronization is not confirmed; gateway startup is blocked until NTP synchronization succeeds.",
              file=sys.stderr)
        return 1
    print("Clock synchronization confirmed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
