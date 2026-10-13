# Contributing to KyberDex

Thanks for improving the game. Focused, explainable changes are easiest to review: reproduce a problem, change the smallest relevant surface and show how you checked the result.

## Get started

Use Python 3.13+ on Linux or macOS and work from a repository checkout:

```bash
uv sync --locked --extra dev
uv run pytest
uv run python -m venator_vault check
```

The public checkout excludes third-party subject art from Git. `uv run venator-vault setup-art` installs it locally from recorded sources when needed. Offline tests use isolated/synthetic assets and state; the catalog can be explored without a live Discord token or an artwork download. See [setup](docs/SETUP.md) before attempting a live bot deployment.

## A good pull request

1. Describe the concrete problem and expected result. Open an issue first for a broad redesign or compatibility change.
2. Preserve existing patterns and keep game rules in the transactional engine rather than Discord handlers.
3. Add behavioral regression tests for changed rules or shared logic. Include concurrent/replay cases when changing ownership, capacity, reservations or credits.
4. Run the relevant tests, then the full suite if the change touches core rules or persistence.
5. Document user-visible changes, configuration and operating limits. For browser changes, check desktop and mobile layout and keyboard access.
6. Explain validation and any remaining limitation in the pull request.

Do not claim live Discord validation based only on unit tests. Test integrations in your own test guild with a dedicated bot account and synthetic inventories. Do not interact with someone else's live collection bot or player assets.

## Areas that welcome contributions

- Reproducible bug fixes and meaningful tests for scheduling, concurrency and restart behavior.
- Keyboard/screen-reader improvements to the public catalog.
- Better self-hosting documentation and explicit platform support.
- Catalog corrections backed by a source, with honest continuity and subject identity.
- Clearer game feedback and confirmation flows that preserve transaction protections.

## Data and asset boundaries

Never submit bot tokens, private configuration, player databases, backups, report messages, production identifiers, private logs, operational receipts or screenshots containing player information. Redact identifiers in bug reports and use minimal synthetic examples.

Do not add Star Wars artwork unless you can establish redistribution permission and provide the required attribution/license evidence. A source citation or visual review is not permission. Preserve all third-party notices. See [NOTICE.md](NOTICE.md) and [artwork review](docs/ASSETS.md).

Catalog changes must retain deterministic formulas, accurate provenance and distinct subject counts. Do not label a provisional combat or artwork review as complete just to pass a launch gate.

Original contributions are provided under this repository's MIT software license, subject to the third-party boundaries in `NOTICE.md`.

## Report bugs or security issues

For ordinary bugs, use the [bug report form](https://github.com/gavinjudd/kyberdex/issues/new?template=bug_report.yml), including your revision, environment, reproduction and sanitized output. For security-sensitive findings, follow [SECURITY.md](SECURITY.md) instead of publishing exploit details or credentials in an issue.
