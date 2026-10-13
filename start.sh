#!/bin/sh
# Restart a configured installation without shell exports or an uv PATH entry.
set -eu
kyberdex_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
if [ ! -x "$kyberdex_root/.venv/bin/python" ]; then
    printf '%s\n' 'Run the KyberDex installer or uv run venator-vault setup first.' >&2
    exit 1
fi
cd -- "$kyberdex_root"
export VAULT_ROOT="$kyberdex_root"
exec "$kyberdex_root/.venv/bin/python" -m venator_vault run
