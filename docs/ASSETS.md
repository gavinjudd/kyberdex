# Automatic artwork setup

The [guided setup](SETUP.md#one-command-installation) installs artwork automatically. To install or repair only the collection's artwork, stop an existing bot worker and run:

```bash
uv run venator-vault setup-art
uv run venator-vault check --require-art
```

The catalog has 871 card definitions backed by 848 distinct PNG files, including seasonal treatments. Installing seasonal artwork does not enable those releases. You do not need to locate or manually configure each image. The installer fetches the recorded source images (official Star Wars sites, plus Wookieepedia article images for cards with no usable official frame), applies the recorded crop, sizing, cutout-flattening and brightness transforms, and compares the result with expected reviewed pixel hashes before installing it.

Setup reads the tracked source recipes in [`data/art-sources.json`](../data/art-sources.json). It verifies the downloaded file's SHA-256 and the transformed image's reviewed RGB pixel hash, then writes the local PNG and its actual file hash into the runtime manifest. Pixel verification allows the installer to reproduce the reviewed image without requiring identical PNG compression bytes.

The first run downloads about 175 MB of source files and generates the local PNG collection. Allow about 1 GB of free disk space for the artwork and download cache. Downloads are cached under `.cache/kyberdex-art/raw/`. Setup skips verified installed outputs and resumes incomplete work when you rerun it. If a source cannot be fetched or no longer matches, setup exits unsuccessfully with counts and per-asset error details; successful files and cached downloads remain available for the next attempt. Changed source content is not silently accepted.

The follow-up readiness check confirms the installed catalog's launch gates. Artwork setup does not connect to Discord or start gameplay. Supply your own guild and protected token configuration before running the bot.

## If a download fails

Rerun `uv run venator-vault setup-art` after a temporary network or source-host failure. If the error reports changed bytes or pixels, keep the failure and report the card/source identifier with sanitized output. A maintainer must review a replacement source recipe; deleting the expected hash is not a valid repair.

## Source and licensing notice

Image files are downloaded to your machine, not distributed in this Git repository. The source records describe where images came from and how to reproduce them; hashes establish integrity, not copyright ownership. Downloading an image does not grant a reuse or redistribution license. Use the images only as permitted by their owners and your intended deployment. See [NOTICE.md](../NOTICE.md).

The normal setup reuses the recorded review only when the reproduced pixels match. You do not need to re-review every unchanged image. Replacing a source or changing its pixels requires a new review, described below.

## What startup verifies

The launch check uses `verified_art_path()` in [`src/venator_vault/__main__.py`](../src/venator_vault/__main__.py). For each card required by the enabled catalog, it verifies:

1. The card has `art_status: "ready"` and a local `art` path.
2. `assets/art/manifest.json` contains an `assets` mapping keyed by the exact card ID.
3. That manifest entry has `status: "ready"` and `visual_qa: "verified"`.
4. Its `path` exactly matches the card's `art`; its `subject_id` matches when the card defines one.
5. Its `sha256` is a lowercase 64-character SHA-256 and matches the selected file's actual bytes.
6. The file is a PNG with positive dimensions matching `width` and `height`, at a **2:3 aspect ratio** (`width * 3 == height * 2`).
7. The selected file and artwork directories are local, allowed paths, not symlinks.

The image is source art. The renderer later composes it into a 5:7 collectible card; do not substitute an already labeled finished card as capture artwork, which would disclose the answer.

## Advanced: replace images or build your own pack

For custom artwork, follow this process instead of marking an unverified replacement ready:

1. Choose a source you can use and record its origin, license or permission and the depicted subject/phase.
2. Crop to a 2:3 PNG, keeping important features clear of the finished card's title, stats and edition elements.
3. Match the catalog's card ID, path and `subject_id` exactly. Several depictions can share a subject identity while requiring different image files.
4. Review both the source image and the actual composed card. Confirm identity, depicted phase, legibility and composition.
5. Only after review, record the final bytes' dimensions and SHA-256 and mark the entry verified. Re-review after any pixel change.
6. Run the full launch artwork check. An excluded, pending or rejected card is not made ready by renaming a status field.

This intentionally non-ready manifest example illustrates the schema. Replace placeholders with actual reviewed evidence; copying it does not approve artwork:

```json
{
  "assets": {
    "card-id-from-catalog": {
      "path": "assets/art/card-id-from-catalog.png",
      "subject_id": "subject-id-from-catalog",
      "status": "pending",
      "visual_qa": "pending",
      "width": 800,
      "height": 1200,
      "sha256": "REPLACE_AFTER_REVIEW_WITH_ACTUAL_LOWERCASE_SHA256"
    }
  }
}
```

After the rights review and visual review are complete, set the manifest `status` to `ready`, `visual_qa` to `verified`, and the matching catalog `art_status` to `ready`. These are operator attestations bound to a specific image hash; the validator cannot decide copyright ownership or recognize an incorrectly depicted character for you.

Use Pillow from the project environment to inspect a particular image without changing it:

```bash
uv run python - <<'PY'
from hashlib import sha256
from pathlib import Path
from PIL import Image

path = Path("assets/art/your-card-id.png")
with Image.open(path) as image:
    print({"format": image.format, "width": image.width, "height": image.height})
print({"sha256": sha256(path.read_bytes()).hexdigest()})
PY
```

## Rendering a review image

Once your selected local card and source asset are prepared, render a single candidate into an ignored local output directory:

```bash
uv run python - <<'PY'
import json
from pathlib import Path
from venator_vault.card_render import CardRenderer

root = Path.cwd()
cards = json.loads((root / "data/cards.json").read_text())["cards"]
card = next(card for card in cards if card["id"] == "your-card-id")
output = root / "work" / "card-review.png"
output.parent.mkdir(exist_ok=True)
# Preview only: this in-memory flag does not approve or modify the catalog.
output.write_bytes(CardRenderer(root).render({**card, "art_status": "ready"}))
print(output)
PY
```

Inspect the resulting file visually. Rendering a candidate is only a preview; it does not update a manifest or certify readiness. The [contribution guide](../CONTRIBUTING.md) explains what may be submitted publicly.

## Verify the complete enabled catalog

```bash
uv run python -m venator_vault check --require-art
```

The readiness report includes `missing_art_count`, a bounded sample of missing card IDs, presentation verification and other launch gates. Disabled seasonal/exclusive content has different eligibility rules; enabling a future release can make its artwork required before its start date. Do not infer readiness for disabled content from a passing check.

Bundled fonts and finish assets have separate manifests checked by `CardRenderer.verify_assets()`. Preserve the font license notices and update a manifest only when you intentionally review and replace its corresponding bytes.
