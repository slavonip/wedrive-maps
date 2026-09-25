# Europe Lite — reproducible build

Europe Lite is one routing graph for the whole continent: the arterial network
(`motorway` … `tertiary` and their links), car ferries, turn restrictions, and the shortest
chains of small roads that connect each car-ferry end to that network. Why it looks like this,
and every defect found on the way, is in `docs/europe-lite.md` §5 of the (private) app repository.
This directory is how to build it again without anything from the machine it was first built on —
except the inputs listed in "What is still external" at the end, which are named rather than hidden.

**This is a copy.** It was taken from `slavonip/wedrive` `tools/europe-lite` at revision
`3eac2bc` so that the public factory depends on nothing private; `PIPELINE_REV` records that
revision and every change made here since (none of them touches routing). The factory is described
in "Factory" below.

## What you need

- Linux (or WSL2) with **Docker**, **osmium-tool** (1.14.0 was used), **python3** (3.10+),
  `curl`, `gzip`, `tar` (GNU, for `--sort`), `sha256sum`, `stat`.
- About **60 GB** free disk and **16 GB** RAM (the connector search peaks at ~8 GB, the graph
  build at a few GB).
- A Europe extract, which the build never downloads itself (see the two modes below).

## What reproducible means here

Three different things, kept apart on purpose:

| | what is the same | what the result is |
|---|---|---|
| **exact reproduction of final2** (`build.sh`, default mode) | the locked source PBF (size + sha256 in `inputs.lock`), the locked timezone database, Valhalla 3.6.3 at `e2f017b` + patches B2 and C, the scripts in this directory | final2 again |
| **rebuild on new OSM data** (`build.sh --new-data`) | the same pipeline and the same Valhalla source | a NEW routing artifact that must pass the gates on its own; it is not final2 |
| **the Docker image** | source, patches, base image digest | **not a release criterion.** apt is not pinned and image bytes may differ; the canonical recipe gave a different `valhalla_build_tiles` md5 than the image final2 was built with (`valhalla/Dockerfile` header) |

**The criterion for exact reproduction is identical content-bearing outputs**, and only those
that have been shown to be byte-reproducible are required:

| file | required identical? | evidence |
|---|---|---|
| `europe.osm.pbf` | yes — checked first, the build FAILS before any work otherwise | the lock |
| `europe_lite`, `europe_admin`, `small_roads`, `ferries` `.osm.pbf` | yes | osmium is deterministic: `ferries.osm.pbf` re-made on 2026-09-24 matched |
| `connector_ways.txt` | yes | the connector search re-run from scratch on 2026-09-24 matched byte for byte |
| `connectors.osm.pbf`, `europe_lite_final.osm.pbf` | yes | step 6 re-run on 2026-09-24 matched final2 byte for byte |
| graph tiles `t/`, `europe_lite.tar` | **no** — not proven | no two graphs have ever been built from one PBF with one binary, and Valhalla tile headers carry a build date. The graph is accepted by the gates, not by a hash |
| `europe_lite.tar.gz` | **no** | follows the tar |

The packaging is normalised now (`tar --sort=name --mtime=@0 --owner=0 --group=0`, `gzip -n`),
and that part IS proven: the same tiles, copied with new mtimes, give the same tar and the same
gzip. final2's own tar and tar.gz were made without that normalisation, so their hashes in
`inputs.lock` identify the published files and are not something a rebuild can match.

## Reproducing final2

```bash
# 0. the Valhalla image: 3.6.3 (e2f017b) + B2 + C on a pinned base, built and self-checked (~30-60 min)
tools/europe-lite/valhalla/build-image.sh wedrive-valhalla:lite

# 1. a work directory with the LOCKED source in it (a local copy: europe-latest has moved on)
mkdir -p /data/lite && cp /somewhere/europe.osm.pbf /data/lite/

# 2. the build. It FAILS at once if the source is not the locked one, and FAILS on any output
#    named in inputs.lock that does not come out identical.
tools/europe-lite/build.sh --dry-run /data/lite wedrive-valhalla:lite     # check, build nothing
tools/europe-lite/build.sh /data/lite wedrive-valhalla:lite

# 3. the gates, against the recorded baseline (see "Baseline for the gates")
export LITE_VALHALLA_IMAGE=wedrive-valhalla:lite
G=tools/europe-lite/gates; MAN=tools/europe-lite/baseline/final2-baseline.json
python3 $G/struct_gate.py  --baseline-manifest $MAN --new /data/lite \
                           --connectors /data/lite/connectors.osm.pbf --out /data/lite/gates
python3 $G/transit_gate.py --baseline-manifest $MAN --new /data/lite --gates /data/lite/gates
python3 $G/routes_gate.py  --new /data/lite --prod-manifest $MAN
```

## Rebuilding on new OSM data

```bash
tools/europe-lite/fetch-source.sh /data/new           # europe-latest -> .part -> MD5 check -> rename
tools/europe-lite/build.sh --new-data /data/new wedrive-valhalla:lite
```

The gates then need a baseline GRAPH, because the recorded manifest only answers questions about
final2's connector set; with a different set it stops with exit code 2 rather than guess. The
baseline is the same new data built WITHOUT the three fixes: `promote.py` without the `service`
rename, and the stock `ghcr.io/valhalla/valhalla:3.6.3` (digest in `inputs.lock`) instead of this
image. Run the gates with `--baseline DIR[:CONFIG]` and `routes_gate.py --prod DIR[:CONFIG]`.

Each gate exits 0 on PASS, 1 on FAIL, 2 when a manifest does not cover the question.

### The steps inside build.sh

| step | tool | output |
|---|---|---|
| 0 checks | size + sha256 of the source (reproduce mode), Valhalla commit inside the image | — |
| 1 base Lite | `osmium tags-filter` highway classes + `route=ferry` + `type=restriction` | `europe_lite_base.osm.pbf` |
| 2 admin boundaries | `osmium tags-filter r/boundary=administrative` | `europe_admin.osm.pbf` |
| 3 small roads | `osmium tags-filter` service/unclassified/residential/living_street/road | `small_roads.osm.pbf` |
| 4 ferry ends | `osmium`, `connectors/ferry_ends.py` | `ferries.osm.pbf`, `ferry_boxes.geojson` |
| 5 connectors | `connectors/extract_batched.sh` → `connectors_dir.py` → `widen.sh` → `connectors_dir.py` | `connector_ways.txt` |
| 6 promote + merge | `osmium getid`, `promote.py` (tested by `test_promote.py`), `osmium merge` | `connectors.osm.pbf`, `europe_lite_final.osm.pbf` |
| 7 timezones | release `maps-vendor` of wedrive-maps | `timezones.sqlite` |
| 8 graph | `valhalla_build_config`, `valhalla_build_admins`, `valhalla_build_tiles` (one PBF) | `t/`, `europe_lite.tar` |
| 9 package | `gzip -n -6`, `sha256sum` | `europe_lite.tar.gz`, `SHA256SUMS` |

**No output is ever half-written under its final name.** Every step writes `NAME.part` and renames
it only when it has finished; a leftover `.part` is an interrupted write and is deleted, never
used. A download (`timezones.sqlite`, and the source in `fetch-source.sh`) is also checked against
its hash before the rename, and an existing file is used only after that check. A step is skipped
when its output is ready: in reproduce mode a file named in `inputs.lock` must match its sha256
(a mismatch is rebuilt, and a rebuilt mismatch FAILS); any other file must be newer than
everything it is built from. `--until N` stops after step N.

## Baseline for the gates

`struct_gate` and `transit_gate` compare the new graph with a **baseline**: the same connectors
built without the fixes, so that "which connector lost destination_only, and did that open a
through-route" can be answered. `routes_gate` needs the old production graph to tell a
pre-existing route (KNOWN) from a regression; without it those pairs FAIL. For final2 both were
`t_europe2` (described in `inputs.lock`, `[baseline]`).

`baseline/final2-baseline.json` is the record of every answer `t_europe2` gave to those gates about
final2 — per connector way the edges `locate` found at the probe point, per lifted way the 12
baseline routes, per acceptance pair the old production route — each stored with the exact
question it answers (probe point, pair coordinates). A gate reading it asks the new graph as usual
and takes the baseline side from the record; any question not in the record stops the gate with
exit code 2.

**Equivalence was shown, not assumed** (2026-09-24): each gate was run against the `t_europe2`
graph, recording, and then against the manifest; the outputs are identical line for line, and
`lifted.json` too. The gate is therefore exactly as strong with the manifest as with the graph —
for final2's connector set, and only for it.

To record a manifest for another baseline graph: add `--record-baseline FILE` to the struct and
transit gates and `--record-prod FILE` to the routes gate while running them with `--baseline` /
`--prod`.

## What is where

| | examples | where |
|---|---|---|
| **source-controlled** | `build.sh`, `fetch-source.sh`, `connectors/`, `promote.py`, `valhalla/` (Dockerfile + the two patches), `gates/`, `diagnostics/`, `inputs.lock`, `baseline/final2-baseline.json`, `factory/`, `engine.lock`, `PIPELINE_REV` | this directory |
| **generated, small** | `connector_ways.txt` (64 KB), `config.json`, gate reports, `SHA256SUMS` | WORK; identified by sha256 in `inputs.lock` where they are criteria |
| **generated, too large for Git** | `europe.osm.pbf` 35 GB, `small_roads.osm.pbf` 3.6 GB, `europe_lite_final.osm.pbf` 1.4 GB, `cand.opl` 1.9 GB, graph `t/` 2.6 GB, `europe_lite.tar.gz` 1.1 GB | WORK only; the release artifact is `europe_lite.tar.gz` |
| **built, not stored** | Docker image `wedrive-valhalla:lite` | rebuilt from `valhalla/` |

## What is still external

Named here so that none of it is mistaken for being in the repository:

1. **The final2 source extract** (35 GB, sha256 in `inputs.lock`). Not downloadable any more; exact
   reproduction exists only as long as a copy of this file does.
2. **The `t_europe2` baseline graph** (`g_europe2.tar`, 2.7 GB, sha256 in `inputs.lock`). Not needed
   to reproduce final2's acceptance (the manifest replaces it), but needed to record a manifest
   again or to judge a rebuild whose connector set differs. Its planned place is a
   `wedrive-maps` release asset; it is **not published**.
3. **The pre-fix `promote.py`** that `t_europe2` was built with: not in Git (the fix, `00f843a`,
   replaced it in place). For new data, the baseline recipe above describes it by its one
   difference.
4. **apt packages** of the Docker image: whatever the Ubuntu archive serves on the day. Pinning them
   (snapshot.ubuntu.com, or `pkg=version`) would fix the environment, not the old image's bytes.
5. **Network sources**: the Valhalla repository on GitHub, the Ubuntu archive, the `maps-vendor`
   release (hash-checked), Geofabrik for new data (MD5-checked).

## Factory

`.github/workflows/europe-lite-factory.yml` builds a new Europe Lite from the current Geofabrik
extract with exactly this pipeline, gates it, and publishes it.

```
precheck   engine.lock pinned? Geofabrik MD5 differs from the current pointer (or force)?
build      lite engine by digest + check-image.sh; fetch-source.sh; build.sh --new-data
baseline   B1': factory/baseline-graph.sh — the same europe_lite_final.osm.pbf with promote.py's
           service rename undone on the connectors (factory/baseline_pbf.py), built by stock
           Valhalla 3.6.3 by digest (engine.lock stock_image), same commands as build.sh step 8
gates      factory/run-gates.sh: struct, transit, routes (--prod = the B1' graph) -> gates.json,
           including the transit coverage rule (below)
package    the five assets under their contract names + europe-lite.json (lite_manifest.py)
publish    only with publish=true: factory/publish.sh
report     on any failure: an issue; nothing is published and the pointer does not move
```

**The release contract.** Tag `europe-lite-YYYY-MM-DD`, the UTC date of the source's replication
timestamp. Every release has the same five assets, and the version is never in a file name:

| asset | what |
|---|---|
| `europe_lite.tar.gz` | the graph (`build.sh`'s `europe_lite.tar.gz`) |
| `europe_lite.osm.pbf` | the PBF the graph was built from (`build.sh`'s `europe_lite_final.osm.pbf`, bytes unchanged) |
| `gates.json` | the three gate reports, parsed, with their full text |
| `europe-lite.json` | the manifest (below) |
| `SHA256SUMS` | sha256 of the other four |

`europe-lite.json` at the root of the default branch is the **current pointer** for Android: a
copy of the manifest of the release in use. Rollback is a commit that puts an older release's
manifest there; nothing is rebuilt. Old releases are not deleted.

**Publication is atomic** (`factory/publish.sh`): draft release → upload all five → download them
back and check every sha256 → publish → check each public URL answers with the right size → commit
the pointer. A failure before publishing deletes the draft; a failure after it leaves a release
nothing points to. The pointer only ever names a release that passed every step.

**The manifest** (`factory/lite_manifest.py make`, validated by `check`): `schema`, `kind =
"europe-lite"`, `version`, `tag`, `source` (URL, replication timestamp and sequence, bytes, MD5,
sha256), `engine` (Valhalla version and commit, sha256 of B2 and C, image digest), `pipeline`
(origin, revision, factory commit), `graph` and `pbf` (file, URL, bytes, sha256; the graph also
`tar_bytes` and `tiles`), `gates` (verdict and counts of each gate, the baseline), `run` (GitHub run
id and URL).

**B1', the control graph.** On new data the recorded `baseline/final2-baseline.json` cannot answer
(it holds final2's connector set only), so every run builds its own control graph: the release's
PBF with the one tag change `promote.py` makes — `service=<v>` → `wedrive:service=<v>` on connector
ways, for the three values Valhalla infers destination_only from — undone on the baseline side
only, built by stock Valhalla. The gates therefore see what B2, C and the rename change together,
which is what final2 was accepted on: for final2 the baseline PBF is byte-identical to the input
of `t_europe2` (`europe_lite2.osm.pbf`, sha256 `77c51876…`).

*Why not simply the same PBF on stock Valhalla (B1).* Measured on final2 on 2026-09-25: every gate
PASSed and the transit gate examined **0 pairs instead of 216**. The 18 ferry-access edges whose
destination_only is lifted lose it because of the rename (it lets the stock ferry reclassification
reach them), not because of B2 or C; with the rename on both sides the structural gate saw no
lifting and the transit gate had nothing to check. A PASS over nothing.

**Transit coverage rule** (`factory/gates_json.py`). The transit gate examines exactly 12 pairs
around each way the structural gate found with destination_only lifted (`lifted.json`). If it
examined any other number — in particular 0 while there were lifted ways — the transit verdict is
FAIL whatever it printed. No lifted ways and 0 pairs is the legitimate empty case and stays PASS.

**The engine** is built once per recipe by `.github/workflows/lite-engine-image.yml` and pushed
to GHCR as `wedrive-valhalla-lite:3.6.3-e2f017b-<key>`, where the key (`factory/lite-key.sh`) hashes
the Dockerfile and both patches. The factory runs only the digest in `engine.lock`, set by a
reviewed commit.

**Order of switching it on:** engine image → commit its digest to `engine.lock` → publish final2 as
`europe-lite-2026-09-21` (`factory/seed-final2.sh`, then `factory/publish.sh`) → a manual run with
`publish=false` (measurement: `lite-build-logs` holds the resource samples) → a manual run with
`publish=true` → only then the monthly schedule (`17 3 3 * *`; enabled on 2026-09-25 after runs 36111034562 and 36124922573 — a scheduled run publishes, and precheck skips it when Geofabrik has nothing newer). If a
job does not fit GitHub-hosted limits (6 h, ~86 GB free disk, 16 GB RAM), the factory moves to the
existing Hetzner self-hosted runner; the pipeline is not changed to make it fit.

## Diagnostics (not part of the build)

`diagnostics/` holds the instrumented Thor (`thor-trace.patch`, `build-diag-image.sh`) and
`invariant.py`, which compares the cost a route was chosen by with the cost of that route. The
production build and the gates never use them.

## Tests

```bash
python3 tools/europe-lite/test_promote.py tools/europe-lite     # promote rule, 6 cases
python3 tools/europe-lite/connectors/test_access.py             # OSM access hierarchy, 14 cases
# Valhalla side: the gurka tests travel inside the B2 patch (test/gurka/test_ferry_connections.cc)
```
