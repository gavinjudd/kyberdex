#!/bin/sh
# Download this script to inspect it, or run the one-line installer from README.
set -eu
umask 077

kyberdex_version=0.3.0
kyberdex_uv_version=0.12.0
kyberdex_stage=
kyberdex_lock=

fail() { printf '\nKyberDex setup: %s\n' "$*" >&2; exit 1; }
cleanup() {
    if [ -n "$kyberdex_stage" ]; then rm -rf -- "$kyberdex_stage"; fi
    if [ -n "$kyberdex_lock" ]; then rmdir -- "$kyberdex_lock" 2>/dev/null || :; fi
}
no_symlinks() {
    kyberdex_check=$1
    while [ "$kyberdex_check" != / ]; do
        [ ! -L "$kyberdex_check" ] || fail 'Choose an install directory without symlinks.'
        kyberdex_check=$(dirname -- "$kyberdex_check")
    done
}

main() {
    case $(uname -s) in
        Linux|Darwin) ;;
        *) fail 'Use Linux, macOS, or a Linux terminal in WSL.' ;;
    esac
    for kyberdex_tool in curl tar mktemp; do
        command -v "$kyberdex_tool" >/dev/null 2>&1 || fail "Install $kyberdex_tool and rerun this command."
    done
    # stdin is the script when invoked with curl | sh. Prompts must use the terminal.
    if ! ( : </dev/tty ) 2>/dev/null; then
        fail 'Open an interactive terminal and rerun the installer. Bot tokens are never read from a pipe.'
    fi
    exec 3<>/dev/tty
    kyberdex_dir=${KYBERDEX_INSTALL_DIR:-"$HOME/kyberdex"}
    case "$kyberdex_dir" in
        /*) ;;
        *) fail 'KYBERDEX_INSTALL_DIR must be an absolute path.' ;;
    esac
    case "$kyberdex_dir" in /|*/../*|*/./*|*/..|*/.) fail 'Choose a normal, dedicated install directory.' ;; esac
    kyberdex_dir=${kyberdex_dir%/}
    no_symlinks "$kyberdex_dir"
    kyberdex_parent=$(dirname -- "$kyberdex_dir")
    mkdir -p -- "$kyberdex_parent"
    kyberdex_pending_lock="$kyberdex_dir.setup-lock"
    if ! mkdir -- "$kyberdex_pending_lock" 2>/dev/null; then
        fail "Another installer may be running. Inspect $kyberdex_pending_lock before retrying."
    fi
    kyberdex_lock=$kyberdex_pending_lock
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM HUP
    kyberdex_stage=$(mktemp -d "$kyberdex_parent/.kyberdex-install.XXXXXX")

    if [ -e "$kyberdex_dir" ]; then
        [ -d "$kyberdex_dir" ] || fail 'The install destination exists and is not a directory.'
        [ ! -L "$kyberdex_dir/.kyberdex-install" ] || fail 'The install marker must not be a symlink.'
        [ -f "$kyberdex_dir/.kyberdex-install" ] || fail "Destination already exists; nothing was overwritten. In an updated KyberDex clone, run: uv run venator-vault setup. Or choose a new KYBERDEX_INSTALL_DIR."
        [ "$(cat "$kyberdex_dir/.kyberdex-install")" = "kyberdex-bootstrap-v1:$kyberdex_version" ] || fail 'This directory belongs to a different installer version. Preserve its state and use a new directory or the documented manual update path.'
        printf 'Resuming KyberDex setup in %s\n' "$kyberdex_dir"
    else
        printf 'Downloading KyberDex %s to %s\n' "$kyberdex_version" "$kyberdex_dir"
        curl --fail --show-error --silent --location --proto '=https' --proto-redir '=https' --retry 2 \
            --connect-timeout 20 --max-time 180 \
            "https://github.com/gavinjudd/kyberdex/archive/refs/tags/v$kyberdex_version.tar.gz" \
            -o "$kyberdex_stage/source.tar.gz"
        mkdir "$kyberdex_stage/source"
        tar -xzf "$kyberdex_stage/source.tar.gz" -C "$kyberdex_stage/source" --strip-components=1
        [ -f "$kyberdex_stage/source/pyproject.toml" ] && [ -f "$kyberdex_stage/source/src/venator_vault/setup.py" ] && \
            [ -f "$kyberdex_stage/source/uv.lock" ] || fail 'The downloaded release is incomplete; no installation was started.'
        printf 'kyberdex-bootstrap-v1:%s\n' "$kyberdex_version" > "$kyberdex_stage/source/.kyberdex-install"
        [ ! -e "$kyberdex_dir" ] || fail 'The destination appeared during installation; refusing to overwrite it.'
        mv -- "$kyberdex_stage/source" "$kyberdex_dir"
    fi
    [ -f "$kyberdex_dir/src/venator_vault/setup.py" ] && [ -f "$kyberdex_dir/uv.lock" ] || fail 'Installation is incomplete. Preserve any state before choosing a fresh directory.'
    no_symlinks "$kyberdex_dir/.bootstrap/runtime-ready"
    if [ -x "$kyberdex_dir/.venv/bin/python" ] && [ -f "$kyberdex_dir/.bootstrap/runtime-ready" ] && \
        [ "$(cat "$kyberdex_dir/.bootstrap/runtime-ready")" = "$kyberdex_version" ]; then
        # Re-entry reaches the wizard's worker lock without touching a live venv.
        printf 'Reusing the prepared runtime.\n'
    else
        if command -v uv >/dev/null 2>&1; then
            kyberdex_uv=$(command -v uv)
        else
            kyberdex_uv="$kyberdex_dir/.bootstrap/bin/uv"
            no_symlinks "$kyberdex_dir/.bootstrap/bin"
            if [ ! -x "$kyberdex_uv" ]; then
                printf 'Installing uv locally (no administrator access or shell-profile changes).\n'
                curl --fail --show-error --silent --location --proto '=https' --proto-redir '=https' --retry 2 \
                    --connect-timeout 20 --max-time 120 \
                    "https://astral.sh/uv/$kyberdex_uv_version/install.sh" -o "$kyberdex_stage/uv-installer.sh"
                UV_UNMANAGED_INSTALL="$kyberdex_dir/.bootstrap/bin" sh "$kyberdex_stage/uv-installer.sh"
            fi
        fi
        printf 'Preparing Python 3.13 and the locked dependencies...\n'
        cd -- "$kyberdex_dir"
        "$kyberdex_uv" sync --locked --no-dev --python 3.13
        mkdir -p "$kyberdex_dir/.bootstrap"
        printf '%s\n' "$kyberdex_version" > "$kyberdex_dir/.bootstrap/runtime-ready"
    fi
    cd -- "$kyberdex_dir"
    printf '\nStarting guided setup. Enter your token only at the hidden prompt.\n'
    cleanup
    kyberdex_stage=
    kyberdex_lock=
    trap - EXIT INT TERM HUP
    exec "$kyberdex_dir/.venv/bin/python" -m venator_vault setup <&3
}

# Keep all actions inside a function so a truncated network transfer cannot
# execute a half-downloaded installer.
main "$@"
