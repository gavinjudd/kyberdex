# Bring your own reviewed artwork

Subject images and the private deployment's asset manifest are intentionally
not distributed. This directory is ignored except for this README.

Use artwork you have permission to use. Place the images at the paths in
`data/cards.json` and create a reviewed `manifest.json` as documented in
[the artwork guide](../../docs/ASSETS.md). The bot validates the files,
PNG dimensions, SHA-256 hashes and review records before it can start.

Offline tests create their own synthetic fixtures. Passing tests does not
approve artwork for gameplay. The font and decorative foil assets included
elsewhere in this repository have separate notices in [NOTICE.md](../../NOTICE.md).
