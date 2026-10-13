# Architecture and operating limits

KyberDex separates Discord interactions, transactional game rules, persistent scheduling and a read-only web catalog. It is a Python 3.13+ application using discord.py, SQLite and Pillow, deployed from a source checkout.

## Modules

| Module | Responsibility |
| --- | --- |
| [`core.py`](../src/venator_vault/core.py) | Catalog validation, SQLite schema/migrations, supply reservations, captures, inventories, crafting, escrow, decks, duels and raids. |
| [`runtime.py`](../src/venator_vault/runtime.py) | Durable scheduling, activity contributions, administrator audit records and interaction-message bindings. |
| [`bot.py`](../src/venator_vault/bot.py) | Discord lifecycle, gateway recovery, base commands, admin commands and encounter delivery. |
| [`features.py`](../src/venator_vault/features.py) | Market, trades, crafting, decks, duel and raid interactions. |
| [`presentation.py`](../src/venator_vault/presentation.py) | Embeds, safe artwork path selection and bounded Discord request helpers. |
| [`card_render.py`](../src/venator_vault/card_render.py), [`showcase_render.py`](../src/venator_vault/showcase_render.py) | Verified presentation assets, Pillow card layout and rarity finishes. |
| [`economy.py`](../src/venator_vault/economy.py) | Catalog scarcity and economy checks. |
| [`web.py`](../src/venator_vault/web.py) | Public catalog JSON, static browser UI and allowlisted reviewed art. |
| [`__main__.py`](../src/venator_vault/__main__.py) | Run/check/backup/restore operations, readiness gates and POSIX worker locks. |
| [`config.py`](../src/venator_vault/config.py) | Explicit environment settings and protected token access. |

## Transactions and persistence

The game database stores player copies, balances, trades, drops, supply reservations and combat state. The runtime database sits beside it and stores scheduling and delivery context. SQLite uses WAL, foreign keys and full synchronous durability settings.

Capture capacity, replay protection, the guild-wide capture streak and stock changes are enforced inside database transactions. Trade offers are versioned; a changed offer invalidates both confirmations. Final ownership transfer happens atomically. Scheduling deadlines are sampled and persisted rather than rerolled on startup.

Operational snapshots preserve the game/runtime pair consistently. Restore validates checksums, catalog compatibility and SQLite integrity before replacing state. See [operations](OPERATIONS.md) for use and limits.

## Discord I/O and rendering

Discord commands and components adapt requests into game operations. Successful database changes are not undone merely because a later image delivery fails. Gateway/network requests have bounded handling. Rendering runs outside the gateway event loop, through a single rendering lane with a cache bounded to 48 entries and 32 MiB by default.

The renderer binds fonts/finishes to manifests and composes locally installed subject art. The startup path checks matching source-art metadata and hashes before the bot connects. Rendering success alone does not imply full launch readiness.

## Public and private data

The web catalog reads public definitions and allowlisted assets. It does not open player databases and provides no inventory or game-mutation endpoint. The bot uses a dedicated Discord bot token stored outside tracked source.

Automatic activity credit uses message metadata without reading or storing ordinary chat content. The game still stores Discord identifiers and gameplay records. Player-submitted complaints are intentionally stored for server managers, so “no chat content” does not mean “no user-submitted text anywhere.” Operators are responsible for their own access, retention, backups and server disclosures.

## Deployment boundaries

- **One configured guild and one active game worker.** This release is not a multi-tenant hosted bot platform.
- **Linux/macOS operations.** POSIX locks and filesystem permission checks are part of the CLI. Linux systemd examples are supplied separately.
- **Checkout-based runtime.** A wheel alone does not contain the full catalog, fonts, finishes and web UI.
- **Local durable storage.** Keep databases on a reliable filesystem. SQLite is not a distributed storage or active-active replication design.
- **Local artwork setup.** `setup-art` fetches recorded sources and installs assets that reproduce the expected reviewed pixels. Images remain outside Git. Live encounters require successful strict readiness and explicit Discord configuration.
- **Small public catalog server.** The built-in HTTP server is not a general application hosting or authentication layer.
- **No cross-bot integration.** KyberDex does not import inventories or trade through another collection bot.

The test suite covers engine and interaction behavior using isolated synthetic state. Passing tests and historical design intent do not prove current uptime, successful Discord delivery or recovery on a different host. Verify those behaviors on the deployment you operate.
