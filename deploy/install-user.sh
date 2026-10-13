#!/bin/sh
# Stage KyberDex on Linux with systemd; review service.env before launching.
set -eu
umask 077
vault_project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$vault_project_root"
[ "$(uname -s)" = Linux ] || { echo 'This installer requires the Linux with systemd.' >&2; exit 1; }
[ "$vault_project_root" = "$HOME/kyberdex" ] || { echo 'Clone or copy KyberDex to ~/kyberdex before running this installer.' >&2; exit 1; }
if systemctl --user is-active --quiet venator-vault.service; then
  echo 'The gateway is running. Stop the owned service before staging changes.' >&2
  exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3,13), "Python 3.13+ is required"'
[ -d .venv ] || python3 -m venv .venv
# uv-created environments omit pip; use the interpreter's bundled bootstrap.
if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  .venv/bin/python -m ensurepip --upgrade
fi
PIP_CONFIG_FILE=/dev/null .venv/bin/python -m pip --isolated install --index-url https://pypi.org/simple --require-hashes --only-binary=:all: -r deploy/requirements-test.txt
.venv/bin/python - <<'PY'
import sysconfig
from pathlib import Path
root=Path.cwd()
(Path(sysconfig.get_paths()['purelib'])/'venator_vault_source.pth').write_text(str(root/'src')+'\n')
PY
.venv/bin/python -m pip check
.venv/bin/python -W error::ResourceWarning -m pytest -q -W error::pytest.PytestUnraisableExceptionWarning
vault_config_dir="$HOME/.config/venator-vault"
vault_unit_dir="$HOME/.config/systemd/user"
[ ! -L "$vault_config_dir" ] && [ ! -L "$vault_unit_dir" ] || { echo 'Configuration symlinks are refused.' >&2; exit 1; }
mkdir -p "$vault_config_dir" "$vault_unit_dir"
chmod 700 "$vault_config_dir"
if [ ! -e "$vault_config_dir/service.env" ]; then
  cat > "$vault_config_dir/service.env" <<EOF
VAULT_ROOT=$vault_project_root
VAULT_DATABASE=$vault_project_root/state/vault.sqlite3
DISCORD_TOKEN_FILE=$vault_config_dir/bot-token
VAULT_GUILD_ID=
VAULT_ACTIVITY_CHANNEL_IDS=
VAULT_SPAWN_INTERVAL=900
VAULT_DROP_DURATION=180
VAULT_ENABLED_EVENTS=
VAULT_ENABLED_RELEASES=
VAULT_PUBLIC_URL=
EOF
  chmod 600 "$vault_config_dir/service.env"
fi
python3 - <<'PY'
import os, stat
from pathlib import Path
p=Path.home()/'.config/venator-vault/service.env'
i=p.lstat()
assert stat.S_ISREG(i.st_mode) and i.st_uid==os.getuid() and stat.S_IMODE(i.st_mode)==0o600, 'service.env must be an owned mode0600 regular file'
PY
for vault_unit in venator-vault.service venator-vault-backup.service venator-vault-backup.timer; do
  if [ -e "$vault_unit_dir/$vault_unit" ]; then
    [ ! -L "$vault_unit_dir/$vault_unit" ] && grep -Eq '^Description=.*(Venator Vault|KyberDex)' "$vault_unit_dir/$vault_unit" || { echo 'An unexpected service file already exists; refusing to overwrite it.' >&2; exit 1; }
  fi
  cp "deploy/$vault_unit" "$vault_unit_dir/$vault_unit"
  chmod 600 "$vault_unit_dir/$vault_unit"
done
VAULT_ROOT="$vault_project_root" DISCORD_TOKEN_FILE="$vault_config_dir/bot-token" .venv/bin/python -m venator_vault check
systemd-analyze --user verify "$vault_unit_dir/venator-vault.service" "$vault_unit_dir/venator-vault-backup.service" "$vault_unit_dir/venator-vault-backup.timer"
systemctl --user daemon-reload
systemctl --user disable venator-vault.service
systemctl --user enable --now venator-vault-backup.timer
systemctl --user start venator-vault-backup.service
systemctl --user show venator-vault.service -p ActiveState -p UnitFileState -p MemoryMax -p MemorySwapMax -p CPUQuotaPerSecUSec -p TasksMax -p NoNewPrivileges
systemctl --user show venator-vault-backup.timer -p ActiveState -p UnitFileState -p NextElapseUSecRealtime
echo 'Gateway remains disabled and stopped. After launch readiness, deploy/enable-boot.sh --check validates boot prerequisites; --enable-boot enables future boot startup without starting it now.'
