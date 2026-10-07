# Third-party notices

## Advance Wars artwork

Terrain, building and unit sprites depict Advance Wars artwork. Advance Wars
belongs to Nintendo and Intelligent Systems. This is an unofficial fan project
and is not affiliated with or endorsed by Nintendo, Intelligent Systems or AWBW.
The application's MIT license does not grant rights to third-party artwork.

The original sprite pack and rendering conventions were adapted from the local
`awbw-xl-ui` project. Source metadata and file hashes are preserved in
`frontend/assets/provenance.json`. Additional editor themes and army sprites
were extracted from the public AWBW sprite atlas and sprite files; their sources
and hashes are in `frontend/assets/editor-themes.json`. The favicon reuses the
included Classic mountain tile without modification.

## Pixelify Sans

Copyright 2021 The Pixelify Sans Project Authors. Distributed under the SIL Open
Font License 1.1. The full license is included at
`frontend/assets/fonts/OFL.txt`.

Source: [Google Fonts](https://github.com/google/fonts/tree/main/ofl/pixelifysans).

## Map references and access analysis

Frozen terrain, unit and movement references were extracted from `awbw-xl`.
Source paths and hashes are in `tools/map_features/references/provenance.json`.
The property and island access detectors were adapted from that project's
`property_access.py` and `island_transport.py` modules. Their source hashes and
adaptations are recorded in `tools/map_features/vendor_access/PROVENANCE.md`.

## Runtime

[PyTorch](https://pytorch.org/) is installed separately under its own license.
Its package includes the relevant license and third-party notices.
