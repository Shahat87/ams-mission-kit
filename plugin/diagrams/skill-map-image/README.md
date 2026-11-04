# skill-map-image

Generates the README skill-map picture (`plugin/docs/assets/skill-map.svg` light, `skill-map-dark.svg` dark electric-blue, and `skill-map.png`, plus the 960x600 Overview thumb `plugin/diagrams/assets/skill-map.png` with `--png`) from `plugin/diagrams/skill-map.json` and `plugin/VERSION`.

Run: `node plugin/diagrams/skill-map-image/gen.ts` (SVGs only) or add `--png` to also render the PNG with headless Chrome (override the binary with `CHROME`).

Each node's `author` GitHub avatar is fetched once from `https://github.com/<author>.png?size=96`, cached in `avatars/<author>.png` and embedded inline as a data URI. Pass `--refresh-avatars` to refetch; with no network and no cache the run exits non-zero.

Node positions and edge routes are hand-placed in `gen.ts` (`POS`, `ROUTE`); content comes from the JSON. No dependencies.
