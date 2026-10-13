#!/bin/sh
# Validate launch readiness, or explicitly enable the already-staged gateway at boot.
# This never starts the gateway. Do not source service.env as shell code.
set -eu
umask 077
vault_mode=${1:---check}
[ "$#" -le 1 ] && { [ "$vault_mode" = --check ] || [ "$vault_mode" = --enable-boot ]; } || {
  echo 'Usage: deploy/enable-boot.sh [--check|--enable-boot]' >&2
  exit 2
}
vault_project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$vault_project_root"
[ "$(uname -s)" = Linux ] || { echo 'This command requires the Linux with systemd.' >&2; exit 1; }
[ "$vault_project_root" = "$HOME/kyberdex" ] || { echo 'This helper expects the staged checkout at ~/kyberdex.' >&2; exit 1; }
vault_unit=venator-vault.service
vault_config="$HOME/.config/venator-vault/service.env"
vault_installed_unit="$HOME/.config/systemd/user/$vault_unit"
vault_require_stopped() {
  vault_state=$(systemctl --user show "$vault_unit" --property=ActiveState --value)
  case "$vault_state" in
    inactive|failed) ;;
    *) echo 'Gateway must be stopped before boot activation.' >&2; exit 1 ;;
  esac
}
vault_require_stopped
[ "$(loginctl show-user "$(id -u)" --property=Linger --value)" = yes ] || {
  echo 'User linger is not enabled; boot activation refused. An administrator must enable it first.' >&2
  exit 1
}
python3 - "$vault_config" "$vault_installed_unit" <<'PY'
import os
import stat
import sys
from pathlib import Path

for name in sys.argv[1:]:
    path = Path(name)
    info = path.lstat()
    if not (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600):
        raise SystemExit(f'{path.name} must be an owned mode0600 regular file')
PY
cmp -s "deploy/$vault_unit" "$vault_installed_unit" || { echo 'Installed gateway unit differs; stage and verify it first.' >&2; exit 1; }
[ "$(systemctl --user show "$vault_unit" --property=FragmentPath --value)" = "$vault_installed_unit" ] || {
  echo 'Unexpected loaded gateway unit; activation refused.' >&2; exit 1;
}
[ -z "$(systemctl --user show "$vault_unit" --property=DropInPaths --value)" ] || {
  echo 'Gateway unit overrides need explicit review before activation.' >&2; exit 1;
}
[ "$(systemctl --user show "$vault_unit" --property=NeedDaemonReload --value)" = no ] || {
  echo 'Staged units need daemon-reload before activation.' >&2; exit 1;
}
# systemd parses the same protected EnvironmentFile as the production service.
# The one-off process only checks launch gates and credential metadata.
systemd-run --user --wait --pipe --collect --quiet \
  "--unit=venator-vault-activation-check-$$" \
  --property=Type=exec \
  "--property=WorkingDirectory=$vault_project_root" \
  "--property=EnvironmentFile=$vault_config" \
  "--property=ExecStartPre=$vault_project_root/.venv/bin/python $vault_project_root/deploy/check-clock.py" \
  --property=UMask=0077 --property=NoNewPrivileges=yes \
  --property=RuntimeMaxSec=300 --property=TimeoutStartSec=30 --property=TimeoutStopSec=30 \
  --property=LimitCORE=0 \
  "$vault_project_root/.venv/bin/python" -m venator_vault check --require-art --require-token-file
vault_require_stopped
if [ "$vault_mode" = --enable-boot ]; then
  systemctl --user enable "$vault_unit"
  echo 'Launch checks passed. Boot activation enabled; the gateway remains stopped until an explicit start or the next boot.'
else
  echo 'Launch checks passed. Validation only: gateway activation is unchanged.'
fi
