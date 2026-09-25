#!/bin/bash
# Run the three existing gates of a new Europe Lite graph against its B1' control graph and write
# gates.json. Exit code 0 only when all three PASS and they agree with each other (gates_json.py).
#
#   factory/run-gates.sh NEW BASE CONNECTORS OUT IMAGE BASELINE_IMAGE
#
#   NEW, BASE   graph directories (config.json with tile_dir /w/t, and t/), as the gates expect;
#               BASE/baseline-input.txt (written by baseline-graph.sh) goes into gates.json
#   CONNECTORS  connectors.osm.pbf of the build
#   OUT         directory for the reports, lifted.json and gates.json
#   IMAGE       image that runs valhalla_service for the gates (the lite engine)
#   BASELINE_IMAGE  recorded in gates.json as what built BASE
#
# The gates run one after another, never in parallel (each routes through Docker on the whole
# continent). A gate that fails still leaves its report; the next ones run anyway so that one
# report shows everything, and gates.json says FAIL.
set -uo pipefail
NEW="${1:?NEW}"; BASE="${2:?BASE}"; CON="${3:?CONNECTORS}"; OUT="${4:?OUT}"; IMAGE="${5:?IMAGE}"; BIMG="${6:?BASELINE_IMAGE}"
G="$(cd "$(dirname "$0")/../gates" && pwd)"
F="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$OUT"
export LITE_VALHALLA_IMAGE="$IMAGE"
BASEDIR="${BASE%%:*}"
if [ -s "$BASEDIR/baseline-input.txt" ]; then INPUT="$(tr '\n' ';' < "$BASEDIR/baseline-input.txt")"
else INPUT="baseline-input.txt missing"; fi

t=$(date +%s)
python3 "$G/struct_gate.py" --baseline "$BASE" --new "$NEW" --connectors "$CON" --out "$OUT" > "$OUT/struct.txt" 2>&1
se=$?; echo "struct  exit $se  $(( $(date +%s)-t )) s"; t=$(date +%s)
if [ -s "$OUT/lifted.json" ]; then
  python3 "$G/transit_gate.py" --baseline "$BASE" --new "$NEW" --gates "$OUT" > "$OUT/transit.txt" 2>&1
  te=$?
else
  echo "struct_gate wrote no lifted.json; transit gate not run" > "$OUT/transit.txt"; te=1
fi
echo "transit exit $te  $(( $(date +%s)-t )) s"; t=$(date +%s)
python3 "$G/routes_gate.py" --new "$NEW" --prod "$BASE" > "$OUT/routes.txt" 2>&1
re=$?; echo "routes  exit $re  $(( $(date +%s)-t )) s"

python3 "$F/gates_json.py" \
  --struct "$OUT/struct.txt" --struct-exit $se \
  --transit "$OUT/transit.txt" --transit-exit $te \
  --routes "$OUT/routes.txt" --routes-exit $re \
  --baseline "B1': europe_lite_final.osm.pbf with promote.py's service rename undone on the connectors, built by stock Valhalla 3.6.3 (no B2, no C). $INPUT" \
  --baseline-image "$BIMG" --lifted "$OUT/lifted.json" --out "$OUT/gates.json"
