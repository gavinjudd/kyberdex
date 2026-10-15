# Set up KyberDex

## One-command installation

Run this in a terminal on **Linux, macOS or WSL**:

```sh
curl -fsSL https://raw.githubusercontent.com/gavinjudd/kyberdex/main/install.sh | sh
```

The installer prepares uv and Python 3.13 when needed, installs KyberDex into `~/kyberdex` and opens the setup wizard. It does not require sudo or edit your shell profile. Native Windows operation is not supported; use a WSL terminal.

The wizard handles configuration, channel selection, artwork downloads and readiness checks. **You still create a Discord bot and authorize its server invite in your browser.** Discord requires those actions from the account that controls the application and server; the installer cannot do them on your behalf.

## What the wizard asks

1. **Bot token:** create a dedicated bot in the [Discord Developer Portal](https://discord.com/developers/applications), then enter its token at the hidden local prompt. Never paste it into chat, an issue or a shell command. An existing saved token can be reused.
2. **Server ID:** choose a server you manage. If the bot has not joined, the wizard supplies an invite URL and lets you retry after authorizing it in your browser.
3. **Channels:** select an encounter channel from the public text channels where the bot has the required permissions. Select one activity channel, or accept the encounter channel as the default. Advanced environment configuration can specify more than one.
4. **Save:** confirm the configuration. The wizard saves protected settings and token files outside the checkout.
5. **Artwork and checks:** let the recorded artwork download and strict readiness checks finish. The first source download is about 175 MB; successful work is cached for retries.
6. **Start:** accept the foreground-start prompt to bring the bot online, or decline and start it later.

The wizard checks your token and server access. It does not create a Discord application, silently invite a bot or enable Administrator permission. Use one dedicated bot account and one server per deployment.

## Stop, restart or rerun setup

Keep the terminal open while the bot runs in the foreground. **Ctrl+C** stops it. Start it again with saved settings:

```sh
cd ~/kyberdex && ./start.sh
```

To change the saved setup, stop the bot and run from its installation directory:

```sh
.venv/bin/python -m venator_vault setup
```

Add `--no-start` to finish the wizard without offering foreground startup. This command uses the installed environment directly, even when uv is not on your shell’s PATH. Saved profiles live under `~/.config/kyberdex/profiles/<checkout-hash>/` (or `$XDG_CONFIG_HOME/kyberdex/profiles/<checkout-hash>/`), with a `settings.json` file and separate `bot-token-<suffix>` credential. Replacing a token keeps the previous profile in `settings.previous.json` and retains its previous token file so a failed save cannot destroy the working credential. Directories use mode `0700`; files use `0600`. Moving the checkout changes its profile identity, so rerun setup after a move.

The installer can resume a directory it previously installed. It refuses to overwrite an unrelated existing checkout. For an existing Git clone, update it deliberately, then open the same wizard with `uv run venator-vault setup`.

For a different install location, use an absolute path:

```sh
curl -fsSL https://raw.githubusercontent.com/gavinjudd/kyberdex/main/install.sh | KYBERDEX_INSTALL_DIR=/absolute/path/kyberdex sh
```

Use that directory for later start commands. The default installer starts no background service; unattended operation is covered under [managed services](#run-as-a-managed-service).

## Manual setup (advanced)

Use these individual steps if you prefer to manage the checkout, credentials and environment yourself. KyberDex requires Python 3.13+ and [uv](https://docs.astral.sh/uv/getting-started/installation/). A clone supports offline catalog exploration without a token or artwork download.

### 1. Install and verify offline

```bash
git clone https://github.com/gavinjudd/kyberdex.git
cd kyberdex
uv sync --locked
uv run python -m venator_vault check
```

Keep the repository directory: the package uses the adjacent `data`, `assets` and `web` directories. The check command initializes local state and reports artwork/presentation/catalog readiness. It does not connect to Discord. Until you run artwork setup, `launch_art_ready` remains false.

For a read-only catalog preview:

```bash
uv run python -m venator_vault.web --root . --host 127.0.0.1 --port 8765
```

Visit `http://127.0.0.1:8765`. Keep this development server bound to loopback. The built-in HTTP server is a small catalog server; public exposure needs an operator-managed HTTPS proxy and normal service hardening.

### 2. Create your own Discord application

Use the [Discord Developer Portal](https://discord.com/developers/applications) to create a dedicated bot application. Install that bot into the server you administer with the `bot` and `applications.commands` OAuth scopes.

Grant the bot **View Channel, Send Messages, Embed Links, Attach Files and Read Message History** in its configured channels. Administrator permission is not required. Server managers using `/vault-admin` need **Manage Server**.

The code enables the guild and guild-message intents. It does not need the privileged Message Content or Server Members intents. Activity eligibility uses author, channel, message type and timestamps rather than ordinary message text.

Do not use a Discord user token. Keep the bot token in a separate protected file, never in source, shell history, screenshots, issues or commits.

### 3. Configure the deployment

The application loads a saved wizard profile when one exists; explicit environment variables override it. A `.env` file is not automatically loaded by Python. For manual setup, export variables in the launching shell or use your service manager's environment-file support. See the repository's [`.env.example`](../.env.example) for a configuration template.

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

For a launching shell, replace the example IDs and export the configuration explicitly:

```bash
export VAULT_ROOT="$PWD"
export VAULT_GUILD_ID="YOUR_SERVER_ID"
export VAULT_ACTIVITY_CHANNEL_IDS="YOUR_PUBLIC_CHANNEL_ID,ANOTHER_PUBLIC_CHANNEL_ID"
export DISCORD_TOKEN_FILE="$HOME/.config/venator-vault/bot-token"
export VAULT_ENABLED_RELEASES=""
export VAULT_ENABLED_EVENTS=""
```

Use real IDs from your own server. The encounter channel is selected later with `/vault-admin setup`; automatic activity credit is restricted to the configured conversation channels.

Provision your token interactively after substituting your own bot application ID:

```bash
uv run python deploy/provision-token.py --application-id YOUR_BOT_USER_ID
```

The helper uses hidden input and defaults to `~/.config/venator-vault/bot-token`. Set `DISCORD_TOKEN_FILE` to that path, or supply a deliberate custom path with `--token-file`. `DISCORD_APPLICATION_ID` can supply the application ID instead of the CLI argument.

The token must be entered locally and kept private. `check --require-token-file` checks credential metadata without displaying its contents. The run command reads the token only after readiness gates pass.

### 4. Install artwork automatically

```bash
uv run venator-vault setup-art
uv run venator-vault check --require-art
```

The setup command downloads recorded sources, reproduces the recorded image transforms and installs assets only when they match the expected reviewed pixels. The first setup downloads about 175 MB of source files; it uses a local cache and can be rerun after an interrupted download. Source images and generated PNGs remain local and ignored by Git.

The catalog contains 871 definitions backed by 847 distinct PNG files; some treatments share an asset. See [artwork setup](ASSETS.md) for retry behavior, source notices and replacing images with your own. The downloader does not grant copyright permission.

The default startup contract also requires at least 800 distinct regular subjects and reviewed combat profiles. Reducing the catalog to a few cards requires a deliberate engine/test change; it is not a supported configuration shortcut. Enabled future releases are checked before their release window begins.

### 5. Pass preflight and connect

```bash
uv run python -m venator_vault check --require-art --require-token-file
uv run python -m venator_vault run
```

All launch gates must pass. Do not edit readiness checks just to hide missing images or unresolved reviews.

In your server, use `/vault-admin setup` to choose the encounter channel, then inspect `/vault-admin health` and `/vault status`. Test a capture, reviewed trade, craft confirmation and raid in your own test server before inviting real players. A passing offline suite does not confirm Discord permissions, message delivery or successful live gameplay.

## Run as a managed service

The [`deploy/`](../deploy/) directory contains Linux systemd tooling and examples. The supplied user-service units expect the checkout at `~/kyberdex`; choose that location for the helper workflow, or deliberately adapt every referenced path. Read their path and environment requirements before installation. Keep the bot and backups under the same unprivileged account, with private state and configuration directories. Run one gateway worker against the database; the operational CLI uses an exclusive worker lock.

Enable and verify backups before collecting player state. Follow [OPERATIONS.md](OPERATIONS.md) for snapshots, restore validation and recovery limits. A configured service is not proof of a current successful backup or restart; verify both on your host.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `launch_art_ready: false` | Run or rerun `uv run venator-vault setup-art`, then strict readiness. Inspect failed downloads and `missing_art_sample`. |
| Artwork setup reports an unavailable or changed source | Keep the reported failure; retry after a temporary network problem. A changed image needs an updated reviewed source record, not a bypassed hash check. |
| Token permissions rejected | Use an owned regular file with mode `0400`/`0600`, not a symlink or shared file. |
| Bot cannot start without guild configuration | Set `VAULT_GUILD_ID` to your own server ID. |
| Bot online, no automatic drops | Check setup/pause state, approved activity channels and fresh eligible human activity. Quiet periods intentionally suppress automatic drops. |
| Commands or images fail | Check bot installation, guild scope and channel permissions, especially Attach Files and Embed Links. |
| Worker already running | Stop the existing worker before setup-art/check/restore/start operations that acquire the worker lock. |
| Catalog image unavailable | Confirm both card and manifest status, `visual_qa`, path, subject, exact SHA-256 and 2:3 PNG dimensions. |
| Windows import error for `fcntl` | Use Linux/WSL; the operations CLI needs POSIX locks. |
