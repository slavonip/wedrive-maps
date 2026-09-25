#!/bin/bash
# B1' control graph for the gates.
#
#   factory/baseline-graph.sh BASE STOCK_IMAGE
#
#   BASE must contain the build's europe_lite_final.osm.pbf, connectors.osm.pbf,
#   europe_admin.osm.pbf and timezones.sqlite.
#
# 1. europe_lite_baseline.osm.pbf = europe_lite_final.osm.pbf with promote.py's one tag change
#    undone on the connector ways (factory/baseline_pbf.py). For final2 this file is byte-identical
#    to the input of t_europe2, the baseline final2 was accepted against.
# 2. the graph of it, built by STOCK Valhalla 3.6.3 (no B2, no C) pinned by digest, with the same
#    commands as build.sh step 8.
# So the gates see what B2, C AND the promote.py rename change together — the lifted
# destination_only edges the transit gate exists to examine included.
#
# Writes BASE/europe_lite_baseline.osm.pbf, BASE/baseline-input.txt, BASE/config.json,
# BASE/admins.sqlite, BASE/t/. Nothing outside BASE. Production files are never modified.
set -euo pipefail
BASE="$(cd "${1:?usage: baseline-graph.sh BASE STOCK_IMAGE}" && pwd)"
IMAGE="${2:?stock image}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TZ_SHA256="5027ecd793c88036acae9d400daeb9be7b43627fe2430e19cf9b9d08e29907e4"
cd "$BASE"
say() { echo "[$(date +%H:%M:%S)] $*"; }
VAL() { docker run --rm -v "$BASE":/w --entrypoint "$1" "$IMAGE" "${@:2}"; }

for f in europe_lite_final.osm.pbf connectors.osm.pbf europe_admin.osm.pbf timezones.sqlite; do
  [ -s "$f" ] || { echo "BASE/$f missing"; exit 1; }
done
[ "$(sha256sum timezones.sqlite | cut -d' ' -f1)" = "$TZ_SHA256" ] || { echo "timezones.sqlite hash mismatch"; exit 1; }
# Stock means stock: none of Valhalla's sources may be modified in this image.
VAL bash -c 'test ! -d /src/valhalla || test -z "$(git -C /src/valhalla status --short 2>/dev/null)"' \
  || { echo "$IMAGE carries modified Valhalla sources: not a stock baseline"; exit 1; }

# --- 1. the baseline input ------------------------------------------------------------------------
say "B1' input: undo promote.py's service rename on the connectors"
osmium cat -f opl connectors.osm.pbf | awk '/^w/{print $1}' > connector_ids.txt
rm -f europe_lite_baseline.osm.pbf.part
osmium cat -f opl europe_lite_final.osm.pbf -o - \
  | python3 "$HERE/baseline_pbf.py" connector_ids.txt 2> baseline_pbf.log \
  | osmium cat -F opl -f pbf -o europe_lite_baseline.osm.pbf.part -
cat baseline_pbf.log
mv europe_lite_baseline.osm.pbf.part europe_lite_baseline.osm.pbf
{ echo "input   europe_lite_final.osm.pbf $(sha256sum europe_lite_final.osm.pbf | cut -d' ' -f1)"
  echo "output  europe_lite_baseline.osm.pbf $(stat -c %s europe_lite_baseline.osm.pbf) $(sha256sum europe_lite_baseline.osm.pbf | cut -d' ' -f1)"
  echo "undo    $(cat baseline_pbf.log)"
  echo "image   $IMAGE"; } > baseline-input.txt
cat baseline-input.txt

# --- 2. the graph ---------------------------------------------------------------------------------
say "B1' graph with $IMAGE"
VAL rm -rf /w/t; rm -f config.json admins.sqlite; mkdir -p t   # tiles are root-owned: remove them in the container, as build.sh does
VAL bash -c "valhalla_build_config --mjolnir-tile-dir /w/t --mjolnir-tile-extract '' \
    --mjolnir-admin /w/admins.sqlite --mjolnir-timezone /w/timezones.sqlite" > config.json.part
mv config.json.part config.json
VAL valhalla_build_admins -c /w/config.json /w/europe_admin.osm.pbf > admins.log 2>&1
t=$(date +%s)
VAL valhalla_build_tiles -c /w/config.json /w/europe_lite_baseline.osm.pbf > tiles.log 2>&1
say "B1' graph $(( $(date +%s)-t )) s, $(find t -name '*.gph' | wc -l) tiles, $(du -sm t | cut -f1) MB"
grep -E "Finished ReclassifyFerry|Finished with [0-9]+ shortcuts" tiles.log | sed 's/^.*Finished/  Finished/' || true
