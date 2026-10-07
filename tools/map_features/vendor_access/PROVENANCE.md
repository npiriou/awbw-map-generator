# Structural property access

These files are adapted from the existing local `awbw-xl` detectors that were
used by `scripts/audit_s_rank_islands.py` for the convoluted-map audit.
They use the bundled movement reference through `tools.map_features.common`,
and require no `awbw_native` extension or external checkout.

Original source SHA-256 (2026-10-04):

| Source | SHA-256 |
| --- | --- |
| `python/awbw_ai/property_access.py` | `fefaccc1230c5d70dbe0ccb4086edaf223bd04cf012a250357300f179b1f70ce` |
| `python/awbw_ai/island_transport.py` | `d95907838c447e6fa7a3bd3516a0a8e29184a0b23d7a35105c4723669b18fb26` |
| `scripts/audit_s_rank_islands.py` | `d4213f7d2dcacd8e23f885bd6b3f24d89ace3ef7a4565fc15ef3ba16cc5daba6` |

Adaptations:

- Imports use the local package and JSON movement reference.
- A known predeployed transport on impassable terrain can enter adjacent legal
  movement components, matching `crates/awbw-rules/src/movement.rs::path_cost`
  which validates entered destinations and skips the departure terrain. With
  no legal exit it is ignored as a known unusable blocker; it does not turn a
  complete inventory into unknown evidence.
- The wrapper combines per-player reports, preserving ownership of transports.
- Island geometry retains initial HQ/base origins with capturer fallback for
  players lacking those facilities. Infantry and mechs both qualify as capturers.
- Building accessibility also uses all predeployed capturers, so an already
  deployed infantry or mech can capture nearby island buildings.
- Capturers placed on impassable terrain supply adjacent legal foot origins
  when they can move off that terrain. Those without any escape are recorded
  as stranded and do not invalidate otherwise known origins.
- Missing player origins or an incomplete player roster cannot prove a neutral
  building unreachable. Such counts remain explicitly unknown.
