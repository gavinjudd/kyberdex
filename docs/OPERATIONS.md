# Operations, backups and recovery

Operate one KyberDex worker per deployment. Keep the checkout, token, databases and backup paths under a dedicated unprivileged account. Backups contain player identifiers and game state; keep them private and outside public repositories.

## Readiness

```bash
uv run python -m venator_vault check
uv run python -m venator_vault check --require-art --require-token-file
```

Checks do not connect to Discord. The first command reports readiness and validates storage; it can succeed while artwork is missing. The strict check requires the artwork, catalog, presentation, balance and economy launch gates. Credential validation inspects the token file's metadata without printing its contents.

The check command acquires the exclusive worker lock. Stop your owned worker before using a check that needs this lock; use `/vault-admin health` for a running bot's health view. Startup runs the same readiness gates before connecting.

## Create snapshots

```bash
uv run python -m venator_vault backup
```

The default managed backup directory is `backups/`, retaining up to 168 managed snapshots. This is a count limit, not a guaranteed number of days. A timer must actually run successfully to create them.

For a named snapshot that is not automatically pruned:

```bash
uv run python -m venator_vault backup --destination /private/backup/path/kyberdex-snapshot
```

Choose a new destination directory. The backup uses SQLite snapshots, validates integrity, copies the matching catalog and records SHA-256 hashes in a manifest. It coordinates the game and runtime database pair. It does not back up the token or all artwork/source files; preserve needed configuration and reviewed assets separately using appropriate access controls.

Copy backups to a separate failure domain using your own secure transfer configuration. A snapshot remaining on the same disk does not protect against loss of that disk. Periodically validate a copy and rehearse restoration in an isolated environment.

## Verify and restore

First validate a snapshot without applying it:

```bash
uv run python -m venator_vault restore /private/backup/path/kyberdex-snapshot
```

The snapshot's catalog must match the current catalog; preserve versioned source/catalog copies to make restoration reproducible. Validation checks allowed files, hashes and SQLite integrity.

To restore state, stop the game worker first, retain a separate copy of the existing state and then deliberately apply the validated snapshot:

```bash
uv run python -m venator_vault restore /private/backup/path/kyberdex-snapshot --apply
```

Applying a restore replaces state. The CLI takes exclusive locks, creates a rollback snapshot when there is existing state and records an in-progress marker. An interrupted restore blocks ordinary worker startup until recovered with a validated matching snapshot. Do not simply delete a recovery marker to hide an unfinished restore.

After restoration, run strict readiness checks, restart the worker and verify a controlled interaction. Do not run two workers or run tests against your real player database.

## Service and restart checks

The [`deploy/`](../deploy/) examples include systemd service/watchdog settings, backup timers and clock checking. Inspect the scripts and configure paths for your host. Verify service state, timer execution, snapshot integrity and restart behavior on the actual installation.

Persisted scheduling prevents rerolling deadlines on restart. Quiet periods suppress automatic drops even if a timer is overdue. Restore can only recover state captured by the chosen snapshot; it cannot recreate gameplay after that snapshot. Hardware, filesystem and operator failures remain possible even when application checks pass.
