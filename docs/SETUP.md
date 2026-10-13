# Self-hosting KyberDex

KyberDex runs from a source checkout on Linux or macOS with Python 3.13+. Plan for one dedicated Discord bot application, one guild and one active game worker per deployment. A clean clone supports offline development and catalog browsing; Discord gameplay also requires an operator-provided, reviewed artwork pack.

## 1. Install and verify offline

```bash
git clone https://github.com/gavinjudd/kyberdex.git
cd kyberdex
uv sync --locked --extra dev
uv run pytest
uv run python -m venator_vault check
```

Keep the repository directory: the package uses the adjacent `data`, `assets` and `web` directories. The check command initializes local state and reports artwork/presentation/catalog readiness. It does not connect to Discord. Without artwork, `launch_art_ready` remains false.

For a read-only catalog preview:

```bash
uv run python -m venator_vault.web --root . --host 127.0.0.1 --port 8765
```

Visit `http://127.0.0.1:8765`. Keep this development server bound to loopback. The built-in HTTP server is a small catalog server; public exposure needs an operator-managed HTTPS proxy and normal service hardening.

## 2. Create your own Discord application

Use the [Discord Developer Portal](https://discord.com/developers/applications) to create a dedicated bot application. Install that bot into the server you administer with the `bot` and `applications.commands` OAuth scopes.

Grant the bot **View Channel, Send Messages, Embed Links, Attach Files and Read Message History** in its configured channels. Administrator permission is not required. Server managers using `/vault-admin` need **Manage Server**.

The code enables the guild and guild-message intents. It does not need the privileged Message Content or Server Members intents. Activity eligibility uses author, channel, message type and timestamps rather than ordinary message text.

Do not use a Discord user token. Keep the bot token in a separate protected file, never in source, shell history, screenshots, issues or commits.

## 3. Configure the deployment

The application reads environment variables. A `.env` file is not automatically loaded by Python; export variables in the launching shell or use your service manager's environment-file support. See the repository's [`.env.example`](../.env.example) for a configuration template.

| Variable | Purpose |
| --- | --- |
| `VAULT_ROOT` | Absolute path to this checkout; defaults to the source checkout location. |
| `VAULT_DATABASE` | Game SQLite path; defaults to `state/vault.sqlite3` under the root. |
| `VAULT_GUILD_ID` | Your Discord server ID. Configure explicitly before running the bot. |
| `VAULT_ACTIVITY_CHANNEL_IDS` | Comma-separated IDs of approved public conversation channels. An empty list gives no automatic activity credit. |
| `DISCORD_TOKEN_FILE` | Path to an owned regular token file with mode `0400` or `0600`. |
| `VAULT_SPAWN_INTERVAL` | Nominal scheduling interval in seconds; default `900`, accepted range `300`–`86400`. |
| `VAULT_DROP_DURATION` | Capture window in seconds; default `180`, accepted range `60`–`900`, no greater than the nominal interval. |
| `VAULT_PUBLIC_URL` | Optional public URL for your separate catalog server. Leave empty until you have one. |
| `VAULT_ENABLED_RELEASES` | Comma-separated explicitly enabled release keys; empty by default. |
| `VAULT_ENABLED_EVENTS` | Comma-separated explicitly enabled exclusive-event keys; empty by default. |

Use real IDs from your own server. The encounter channel is selected later with `/vault-admin setup`; automatic activity credit is restricted to the configured conversation channels.

Provision your token interactively after substituting your own bot application ID:

```bash
uv run python deploy/provision-token.py --application-id YOUR_BOT_USER_ID
```

The helper uses hidden input and defaults to `~/.config/venator-vault/bot-token`. Set `DISCORD_TOKEN_FILE` to that path, or supply a deliberate custom path with `--token-file`. `DISCORD_APPLICATION_ID` can supply the application ID instead of the CLI argument.

The token must be entered locally and kept private. `check --require-token-file` checks credential metadata without displaying its contents. The run command reads the token only after readiness gates pass.

## 4. Supply and review artwork

Follow [ASSETS.md](ASSETS.md). Every card required for the enabled catalog must have matching local PNG bytes, dimensions, hash, subject identity and review metadata. Enabled future releases are checked before their release window begins.

The included 851 definitions are not a ready-to-launch artwork pack. The default startup contract also requires at least 800 distinct regular subjects and reviewed combat profiles. Reducing the catalog to a few cards requires a deliberate engine/test change; it is not a supported configuration shortcut.

## 5. Pass preflight and connect

```bash
uv run python -m venator_vault check --require-art --require-token-file
uv run python -m venator_vault run
```

All launch gates must pass. Do not edit readiness checks just to hide missing images or unresolved reviews.

In your server, use `/vault-admin setup` to choose the encounter channel, then inspect `/vault-admin health` and `/vault status`. Test a capture, reviewed trade, craft confirmation and raid in your own test server before inviting real players. A passing offline suite does not confirm Discord permissions, message delivery or successful live gameplay.

## 6. Run as a managed service

The [`deploy/`](../deploy/) directory contains Linux systemd tooling and examples. The supplied user-service units expect the checkout at `~/kyberdex`; choose that location for the helper workflow, or deliberately adapt every referenced path. Read their path and environment requirements before installation. Keep the bot and backups under the same unprivileged account, with private state and configuration directories. Run one gateway worker against the database; the operational CLI uses an exclusive worker lock.

Enable and verify backups before collecting player state. Follow [OPERATIONS.md](OPERATIONS.md) for snapshots, restore validation and recovery limits. A configured service is not proof of a current successful backup or restart; verify both on your host.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `launch_art_ready: false` | Expected in a fresh clone. Complete the artwork pack and manifest; inspect `missing_art_sample`. |
| Token permissions rejected | Use an owned regular file with mode `0400`/`0600`, not a symlink or shared file. |
| Bot cannot start without guild configuration | Set `VAULT_GUILD_ID` to your own server ID. |
| Bot online, no automatic drops | Check setup/pause state, approved activity channels and fresh eligible human activity. Quiet periods intentionally suppress automatic drops. |
| Commands or images fail | Check bot installation, guild scope and channel permissions, especially Attach Files and Embed Links. |
| Worker already running | Stop the existing worker before check/restore/start operations that acquire the worker lock. |
| Catalog image unavailable | Confirm both card and manifest status, `visual_qa`, path, subject, exact SHA-256 and 2:3 PNG dimensions. |
| Windows import error for `fcntl` | Use Linux/WSL; the operations CLI needs POSIX locks. |
