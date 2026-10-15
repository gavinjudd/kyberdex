# KyberDex — open-source Star Wars Discord card bot

**Collect, trade, craft and battle Star Wars cards in your Discord server.** KyberDex is a self-hosted Python Discord bot with a transactional SQLite game engine, 871 card definitions, separate Ground and Fleet battles, cooperative raids and a searchable web catalog. Built for Venator Command; released for developers and community operators to inspect, learn from and adapt.

[Project website](https://gavinjudd.github.io/kyberdex/) · [Self-hosting guide](docs/SETUP.md) · [Gameplay & commands](docs/GAMEPLAY.md) · [Architecture](docs/ARCHITECTURE.md) · [Contribute](CONTRIBUTING.md)

[![CI](https://github.com/gavinjudd/kyberdex/actions/workflows/ci.yml/badge.svg)](https://github.com/gavinjudd/kyberdex/actions/workflows/ci.yml)
[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/Code-MIT-green.svg)](LICENSE)

## One-command setup

Run this in a terminal on **Linux, macOS or WSL**:

```sh
curl -fsSL https://raw.githubusercontent.com/gavinjudd/kyberdex/main/install.sh | sh
```

The installer prepares uv and Python when needed, installs KyberDex in `~/kyberdex` and opens an interactive setup wizard. It needs no sudo and does not edit your shell profile. The wizard verifies your bot token, lets you choose your Discord channels, saves protected configuration, downloads the artwork, runs readiness checks and offers to start the bot in the foreground.

**Discord still needs your browser:** create a bot application and authorize its invite into a server you manage. The wizard guides you through those steps. Enter the bot token only into its hidden local prompt—not chat, an issue or a shell command.

The first artwork setup downloads about **175 MB** of source files and is cached for retries. You do not need to find hundreds of card images or build a manifest yourself. [Full setup guide](docs/SETUP.md) · [Read the installer](install.sh) · [Artwork sources](docs/ASSETS.md)

To start it again later with your saved settings:

```sh
cd ~/kyberdex && ./start.sh
```

This is a **single-guild deployment** running on your own computer or server. Keep the terminal open in foreground mode; **Ctrl+C** stops the bot. [Managed-service setup](docs/SETUP.md#run-as-a-managed-service) is separate. Seasons and special events are disabled by default.

## Why KyberDex?

| System | What it does |
| --- | --- |
| Activity-aware encounters | Hidden randomized schedules; automatic drops wait for fresh activity in approved public channels. |
| Fair capture rules | Two collectors for Common–Epic, one for Legendary–Mythic; a persisted two-capture streak limit gives other members a turn. |
| Copy-level collections | Persistent attack/health rolls, favorites, wishlists, searchable collections and public card sharing. |
| Reviewed trades & crafting | Two-sided escrow, revision-specific confirmations, exact consumption previews and protected favorites/loadouts. |
| Ground & Fleet combat | Separate arenas, three-card squads, role abilities, faction bonuses and cooperative raid bosses. |
| Transparent economy | Published rarity odds, free daily credits, daily market, crafting costs and bounded seasonal supply. No real-money purchases or paid rolls. |
| Collectible presentation | Full-bleed Pillow card faces with a distinct material per rarity, from matte Common to animated holographic Mythic and Celestial GIFs. Install subject art locally with `setup-art`. |
| Durable operations | SQLite WAL, atomic ownership changes, persisted schedules, startup checks and integrity-checked backup/restore tools. |
| Public web catalog | Searchable card definitions and reviewed artwork, without player inventory or mutation endpoints. |

## Manual installation and development

Already have an up-to-date checkout? Run `uv run venator-vault setup` there to open the same wizard.

Prefer to clone the repository and configure it yourself? The [manual setup guide](docs/SETUP.md#manual-setup-advanced) covers the individual commands, environment variables, token provisioning and catalog preview. [CONTRIBUTING.md](CONTRIBUTING.md) covers the development environment and offline tests.

**Keep the checkout:** runtime code expects `data/`, `assets/` and `web/` alongside the source. Installing only a wheel does not provide a complete bot. The historical Python package and CLI names remain `venator_vault` and `venator-vault`.

## Discord slash commands

| Command | Purpose |
| --- | --- |
| `/vault help`, `/vault status` | Learn the rules and inspect encounter availability. |
| `/vault catalog`, `/vault collection`, `/vault card` | Browse definitions, inspect your copies or show an owned card. |
| `/vault favorite`, `/vault wishlist` | Protect a copy or track a card you want. |
| `/vault profile`, `/vault daily`, `/vault market` | View progress, collect free credits and review the daily market. |
| `/vault recipes`, `/vault craft`, `/vault salvage` | Inspect costs and confirm exactly which copies are consumed. |
| `/vault trade`, `/vault give` | Swap cards (one-sided offers fine; your partner is pinged) or gift them outright. |
| `/vault gamble` | Coin flips, Kessel Run slots and member wagers, using credits only. |
| `/vault deck`, `/vault duel` | Save Ground/Fleet squads and challenge another collector. |
| `/vault raid` | Inspect a cooperative encounter, join a squad or attack. |
| `/vault complain` | Send a private report to the server's bot managers. |
| `/vault-admin setup`, `pause`, `spawn`, `event`, `raid`, `health`, `complaints`, `failed-names`, `give`, `clear-inventory`, `gambling` | Server management; requires **Manage Server**. |

Read [gameplay, odds and capture rules](docs/GAMEPLAY.md) for details. Administrative forced spawns and event starts are explicit exceptions to automatic activity scheduling.

## Catalog & game balance

The included catalog contains **871 definitions: 835 regular treatments covering 818 distinct subjects, 16 seasonal editions and 20 staff-only Celestial cards**. Treatments of one subject do not inflate distinct-subject completion. Ground and Fleet remain separate arenas. Source and continuity labels distinguish canon, approved alternate/game continuity and event-only material; inclusion does not reclassify Star Wars continuity.

The default rarity weights are **54% Common, 22% Uncommon, 17.8% Rare, 4.2% Epic, 1.6% Legendary and 0.4% Mythic** when every tier has eligible cards; within a tier, well-known subjects are weighted up. Celestial cards above Mythic never drop and are granted only by server staff. Empty tiers are excluded and weights renormalize. Activated limited editions receive a separate lane within a rarity, with release windows and lifetime caps. See [the catalog and balance guide](docs/CATALOG.md).

## Architecture

```text
Discord commands & components      Public catalog (read-only)
          │                                  │
   bot.py / features.py                    web.py
          │                                  │
   core.py + runtime.py            data/cards.json + reviewed assets
          │                                  │
   SQLite game + runtime state           card_render.py
```

- **Game engine:** transactional captures, supply reservations, escrow, crafting and combat outcomes.
- **Runtime:** persistent schedules, activity contributions, interaction bindings and administrator audit records.
- **Rendering:** Pillow compositions outside the gateway event loop; a bounded render cache.
- **Privacy:** the web catalog never opens player databases. Automatic activity uses message metadata, without reading or storing ordinary chat text. Player-submitted complaints are stored for administrators.
- **Operations:** offline readiness checks and paired database snapshots; deployment examples under [`deploy/`](deploy/).

See [architecture and operating limits](docs/ARCHITECTURE.md) and [backup/restore](docs/OPERATIONS.md).

## Contributing

Start with [`CONTRIBUTING.md`](CONTRIBUTING.md). Useful contributions include behavioral tests, accessible catalog improvements, documented balance fixes and portability work. Keep changes focused and include a reproducible check. Do not submit credentials, player databases, production receipts or copyrighted artwork without redistribution permission.

[Report a bug](https://github.com/gavinjudd/kyberdex/issues/new?template=bug_report.yml) · [Suggest an improvement](https://github.com/gavinjudd/kyberdex/issues/new?template=feature_request.yml) · [Security policy](SECURITY.md)

## License & attribution

Original software is available under the **[MIT License](LICENSE)**. Bundled fonts have their own OFL notices. The software license does not grant rights to Star Wars names, characters, trademarks or third-party artwork. This is an independent fan project, not affiliated with or endorsed by Lucasfilm, Disney or Discord. The full boundary is documented in [`NOTICE.md`](NOTICE.md).
