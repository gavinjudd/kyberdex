# KyberDex contributor instructions

- Python 3.13+, `src/venator_vault` is the engine; the historical module and CLI name stays compatible.
- Keep game ownership changes transactional. Preserve escrow, supply caps, replay protection, collector limits, capture streaks, persisted schedules and restart recovery.
- Run `uv sync --locked --extra dev`, `uv run ruff check .` and `uv run pytest -q` before submitting behavior changes. Add tests for new rules.
- Use synthetic player state and artwork in tests. Never contact a live Discord guild in automated tests.
- Never commit credentials, player inventories, private messages, database files, host receipts or third-party subject artwork. Keep credentials in an owned mode 0600/0400 file outside this checkout.
- The public archive serves catalog metadata and reviewed artwork only. Never expose inventories or mutation endpoints without authentication.
- Launch readiness is fail-closed. Do not mark placeholders as reviewed assets or weaken the art, storage or economy gates to make a demo start.
- Read `CONTRIBUTING.md`, `NOTICE.md` and the relevant docs before editing.
