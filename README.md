# KyberDex — open-source Star Wars Discord card bot

**Collect, trade, craft and battle Star Wars cards in your Discord server.** KyberDex is a self-hosted Python Discord bot with a transactional SQLite game engine, 851 card definitions, separate Ground and Fleet battles, cooperative raids and a searchable web catalog. Built for Venator Command; released for developers and community operators to inspect, learn from and adapt.

[Project website](https://gavinjudd.github.io/kyberdex/) · [Self-hosting guide](docs/SETUP.md) · [Gameplay & commands](docs/GAMEPLAY.md) · [Architecture](docs/ARCHITECTURE.md) · [Contribute](CONTRIBUTING.md)

[![CI](https://github.com/gavinjudd/kyberdex/actions/workflows/ci.yml/badge.svg)](https://github.com/gavinjudd/kyberdex/actions/workflows/ci.yml)
[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/Code-MIT-green.svg)](LICENSE)

> **One-command artwork setup:** `uv run venator-vault setup-art` downloads the catalog’s recorded source images and installs matching reviewed assets locally. You do not need to find hundreds of images yourself. Artwork stays out of Git; running your bot also needs your own Discord configuration. See [artwork setup](docs/ASSETS.md) and [source notices](NOTICE.md).

## Why KyberDex?

| System | What it does |
| --- | --- |
| Activity-aware encounters | Hidden randomized schedules; automatic drops wait for fresh activity in approved public channels. |
| Fair capture rules | Two collectors for Common–Epic, one for Legendary–Mythic; a persisted two-capture streak limit gives other members a turn. |
| Copy-level collections | Persistent attack/health rolls, favorites, wishlists, searchable collections and public card sharing. |
| Reviewed trades & crafting | Two-sided escrow, revision-specific confirmations, exact consumption previews and protected favorites/loadouts. |
| Ground & Fleet combat | Separate arenas, three-card squads, role abilities, faction bonuses and cooperative raid bosses. |
| Transparent economy | Published rarity odds, free daily credits, daily market, crafting costs and bounded seasonal supply. No real-money purchases or paid rolls. |
| Collectible presentation | Pillow card renderer with six rarity finishes, showcase layouts and limited-edition treatments. Install subject art locally with `setup-art`. |
| Durable operations | SQLite WAL, atomic ownership changes, persisted schedules, startup checks and integrity-checked backup/restore tools. |
| Public web catalog | Searchable card definitions and reviewed artwork, without player inventory or mutation endpoints. |

## Quick start

Use **Python 3.13+** and [uv](https://docs.astral.sh/uv/getting-started/installation/) on **Linux or macOS**. The operational CLI uses POSIX file locks; native Windows operation is not supported. Use a Linux environment such as WSL for Windows development.

```bash
git clone https://github.com/gavinjudd/kyberdex.git
cd kyberdex
uv sync --locked
uv run venator-vault setup-art
uv run venator-vault check --require-art
```

`setup-art` fetches the recorded artwork sources, applies the recorded image transforms and checks the result against expected pixels. The first setup downloads about 175 MB of source files. Downloads are cached and setup can be rerun after an interruption. The readiness check runs without connecting to Discord and creates local SQLite state when needed.

For offline code exploration, skip `setup-art` and run `uv run venator-vault check` without `--require-art`; the catalog remains browsable with unavailable-image placeholders. Downloading source artwork does not grant a license to reuse or redistribute it. [Artwork setup and source details →](docs/ASSETS.md)

Browse the local catalog:

```bash
uv run python -m venator_vault.web --root . --host 127.0.0.1 --port 8765
```

Open **http://127.0.0.1:8765**. Completed artwork setup adds the card illustrations; any artwork not installed is shown as unavailable. The project website is documentation, not a hosted game or a view of anyone's collection.

**Keep the checkout:** runtime code expects `data/`, `assets/` and `web/` alongside the source. Installing only a wheel does not provide a complete bot. The historical Python package and CLI names remain `venator_vault` and `venator-vault`.

## Self-host the Discord bot

The [full setup guide](docs/SETUP.md) covers Discord permissions, configuration and startup. In order:

1. Create your own dedicated Discord bot application for one server.
2. Configure your guild, activity channels and protected token file.
3. Run `uv run venator-vault setup-art` to install the recorded artwork locally.
4. Run `uv run python -m venator_vault check --require-art --require-token-file`.
5. Start `uv run python -m venator_vault run`, then use `/vault-admin setup` in your server.

Startup intentionally stops when required art, combat profiles, scarcity checks, presentation assets or storage are not ready. Seasons and special events are disabled by default. This is a **single-guild deployment**, not a public invite-and-forget bot hosting service.

## Discord slash commands

| Command | Purpose |
| --- | --- |
| `/vault help`, `/vault status` | Learn the rules and inspect encounter availability. |
| `/vault catalog`, `/vault collection`, `/vault card` | Browse definitions, inspect your copies or show an owned card. |
| `/vault favorite`, `/vault wishlist` | Protect a copy or track a card you want. |
| `/vault profile`, `/vault daily`, `/vault market` | View progress, collect free credits and review the daily market. |
| `/vault recipes`, `/vault craft`, `/vault salvage` | Inspect costs and confirm exactly which copies are consumed. |
| `/vault trade` | Open a two-sided trade table with escrow and mutual confirmation. |
| `/vault deck`, `/vault duel` | Save Ground/Fleet squads and challenge another collector. |
| `/vault raid` | Inspect a cooperative encounter, join a squad or attack. |
| `/vault complain` | Send a private report to the server's bot managers. |
| `/vault-admin setup`, `pause`, `spawn`, `event`, `raid`, `health`, `complaints` | Server management; requires **Manage Server**. |

Read [gameplay, odds and capture rules](docs/GAMEPLAY.md) for details. Administrative forced spawns and event starts are explicit exceptions to automatic activity scheduling.

## Catalog & game balance

The included catalog contains **851 definitions: 835 regular treatments covering 818 distinct subjects, plus 16 seasonal editions**. Treatments of one subject do not inflate distinct-subject completion. Ground and Fleet remain separate arenas. Source and continuity labels distinguish canon, approved alternate/game continuity and event-only material; inclusion does not reclassify Star Wars continuity.

The default rarity weights are **54% Common, 22% Uncommon, 17.8% Rare, 4.2% Epic, 1.6% Legendary and 0.4% Mythic** when every tier has eligible cards. Empty tiers are excluded and weights renormalize. Activated limited editions receive a separate lane within a rarity, with release windows and lifetime caps. See [the catalog and balance guide](docs/CATALOG.md).

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
