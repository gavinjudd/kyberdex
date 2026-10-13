# Install artwork locally

From the repository root:

```bash
uv run venator-vault setup-art
uv run venator-vault check --require-art
```

The installer fetches recorded sources and reproduces the expected reviewed
pixels for the catalog. Downloads are cached and setup can be rerun after an
interruption. The 851 card definitions use 847 distinct PNG files; you do not
need to find the images individually.

Downloaded images and the generated manifest stay local. This directory is
ignored by Git except for this README. Downloading does not grant a reuse or
redistribution license; see [source notices](../../NOTICE.md).

The [artwork guide](../../docs/ASSETS.md) explains installation and custom-image
review. Offline tests use their own synthetic fixtures and do not require the
downloaded collection.
