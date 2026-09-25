#!/bin/bash
# Prepare final2 — the graph accepted on 2026-09-24, NOT rebuilt — as the first Europe Lite
# release, europe-lite-2026-09-21. This script only assembles and checks DIR; it publishes nothing.
#
#   factory/seed-final2.sh FINAL2_DIR SOURCE_PBF DIR
#
#   FINAL2_DIR  ~/eu/final2 (europe_lite_final.tar.gz, europe_lite_final.osm.pbf)
#   SOURCE_PBF  the locked source extract, for its MD5 (its sha256 and size are checked first)
#   DIR         empty directory; receives the five assets under their contract names
#
# Then, and only on the owner's command:
#   factory/publish.sh DIR slavonip/wedrive-maps <checkout of wedrive-maps main>
#
# The gates recorded for final2 are the ones its acceptance was judged by: the three gates run
# against t_europe2 (pre-fix promote.py + stock Valhalla 3.6.3), kept in factory/fixtures.
set -euo pipefail
F2="$(cd "${1:?FINAL2_DIR}" && pwd)"; SRC="${2:?SOURCE_PBF}"; DIR="${3:?DIR}"
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$DIR"; DIR="$(cd "$DIR" && pwd)"
check() { [ "$(stat -c %s "$1")" = "$2" ] && [ "$(sha256sum "$1" | cut -d' ' -f1)" = "$3" ] \
          || { echo "$1 is not the final2 file ($2 bytes, $3)"; exit 1; }; echo "ok: $1"; }

check "$F2/europe_lite_final.tar.gz" 1165688788 f6c129c9b32a4bfef75a98379d4b6f64b4db4a6a0e85045f5a081fefe18f0a76
check "$F2/europe_lite_final.osm.pbf" 1418946230 a9ebf5252a39b7df953614ef3993151cd4b3cc91eba2a056203323e2f962a713
check "$SRC" 35020804941 61975e09c65f8b3d3f5b0b19caa1701fce77f6f7af47a7f92084ff7bd9dcb64f

# Same bytes under the contract names (hard links when possible: nothing is rewritten).
ln -f "$F2/europe_lite_final.tar.gz" "$DIR/europe_lite.tar.gz" 2>/dev/null || cp "$F2/europe_lite_final.tar.gz" "$DIR/europe_lite.tar.gz"
ln -f "$F2/europe_lite_final.osm.pbf" "$DIR/europe_lite.osm.pbf" 2>/dev/null || cp "$F2/europe_lite_final.osm.pbf" "$DIR/europe_lite.osm.pbf"

python3 "$HERE/gates_json.py" \
  --struct "$HERE/fixtures/struct.txt" --struct-exit 0 \
  --transit "$HERE/fixtures/transit.txt" --transit-exit 0 \
  --routes "$HERE/fixtures/routes.txt" --routes-exit 0 \
  --baseline "t_europe2: the same connector set, promote.py before 00f843a, stock Valhalla 3.6.3 (the final2 acceptance baseline)" \
  --baseline-image "ghcr.io/valhalla/valhalla@sha256:2b19ea46551a9687b245022551183829d817fdee9b58c5e7b2adb6e422749c43" \
  --out "$DIR/gates.json"

echo "md5 of the source (35 GB)"
MD5="$(md5sum "$SRC" | cut -d' ' -f1)"
python3 - "$DIR/source.json" "$MD5" <<'PY'
import json, sys
json.dump({"url": "https://download.geofabrik.de/europe-latest.osm.pbf",
           "fetched": "2026-09-22", "replication": "2026-09-21T20:21:51Z", "sequence": 4920,
           "bytes": 35020804941, "md5": sys.argv[2],
           "sha256": "61975e09c65f8b3d3f5b0b19caa1701fce77f6f7af47a7f92084ff7bd9dcb64f"},
          open(sys.argv[1], "w"), indent=1, sort_keys=True)
PY
python3 "$HERE/lite_manifest.py" make --repo slavonip/wedrive-maps --dir "$DIR" \
  --source "$DIR/source.json" --gates "$DIR/gates.json" \
  --lite-image "wedrive-valhalla:lite-prod2 (local image, not published; functionally equivalent to valhalla/Dockerfile)" \
  --tiles 23746 --tar-bytes 2710016000 \
  --run-note "final2: built locally on 2026-09-24 and accepted by the owner; not built by the factory" \
  --factory-commit "$(git -C "$HERE" rev-parse HEAD 2>/dev/null || echo unknown)" \
  --out "$DIR/europe-lite.json"
rm -f "$DIR/source.json"
ls -l "$DIR"
echo "ready: publish only on the owner's command (factory/publish.sh)"
