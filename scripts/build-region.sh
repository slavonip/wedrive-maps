#!/usr/bin/env bash
# Build a SUPER-REGION in one pass, then cut it into per-country tile sets.
#
#   usage: build-region.sh <region-id> <country-code:geofabrik-path> [...]
#   e.g.   build-region.sh eu-core AT:europe/austria HU:europe/hungary MD:europe/moldova \
#                                  RO:europe/romania UA:europe/ukraine
#
# THE ORDER IS THE WHOLE POINT and it is the opposite of build-graph.sh. That script builds a
# package from its own PBFs and ships it whole. This one builds EVERY country together, so the
# frontier has edges, and only then cuts along national boundaries — which is how a car can hold
# Moldova and Romania as separate downloads and still drive between them.
#
# Measured before this existed (scripts/cut-by-country.py): both cuts assembled give Chișinău →
# Bucharest 456.5 km, identical to the whole build, while Moldova's cut alone correctly answers
# NO ROUTE. Merging two SEPARATE builds does not work at all: 35 shared tile paths, 0 of them
# byte-identical.
#
# Runs inside the pinned Valhalla image; everything it touches is under /data.
set -euo pipefail

REGION="${1:?region id, e.g. eu-core}"
shift
MEMBERS=("$@")
[ ${#MEMBERS[@]} -gt 0 ] || { echo "at least one CC:geofabrik-path is required" >&2; exit 2; }

WORK=/data
SRC="$WORK/src"
TILEDIR="$WORK/tiles_$REGION"
CUTS="$WORK/cuts_$REGION"
OUT="$WORK/out"
CONF="$WORK/valhalla_$REGION.json"
POLY="$WORK/poly"

mkdir -p "$SRC" "$TILEDIR" "$CUTS" "$OUT" "$POLY"

PBFS=()
CUT_ARGS=()
for member in "${MEMBERS[@]}"; do
  code="${member%%:*}"
  region="${member#*:}"
  name="$(basename "$region")"

  pbf="$SRC/$name-latest.osm.pbf"
  if [ ! -f "$pbf" ]; then
    echo "==> downloading $region"
    curl -fsSL -o "$pbf" "https://download.geofabrik.de/${region}-latest.osm.pbf"
  fi
  PBFS+=("$pbf")

  # The BOUNDARY, not the bounding box. Cutting md-ro by bounding box put 100% of the build in
  # Romania's set, because Moldova sits entirely inside Romania's rectangle — and once the list
  # grows past two countries that is the normal case, not the exception.
  poly="$POLY/$name.poly"
  if [ ! -f "$poly" ]; then
    curl -fsSL -o "$poly" "https://download.geofabrik.de/${region}.poly"
  fi
  CUT_ARGS+=("$code:$poly")
done

echo "==> sources: $(du -ch "${PBFS[@]}" | tail -1 | cut -f1) across ${#PBFS[@]} countries"

# ── config: a DIRECTORY, not an extract ─────────────────────────────────────────────────────
# A car assembles its graph from several per-country downloads, so it reads a tile_dir. Verified
# 2026-09-17 that this loses nothing: routes, one-ways and per-node timezones all behave exactly
# as they do from a single .tar.
echo '==> config'
valhalla_build_config \
  --mjolnir-tile-dir "$TILEDIR" \
  --mjolnir-timezone "$TILEDIR/timezones.sqlite" \
  --mjolnir-admin "$TILEDIR/admins.sqlite" > "$CONF"
python3 - "$CONF" <<'PY'
import json, sys
path = sys.argv[1]
config = json.load(open(path))
# POP IT, do not blank it. `--mjolnir-tile-extract ""` leaves the key present and Valhalla then
# looks for a file called "" instead of reading the directory, producing a graph with no tiles
# and no error — measured 2026-09-17.
config["mjolnir"].pop("tile_extract", None)
json.dump(config, open(path, "w"), indent=2)
PY

TIMEZONES=true
TZ_DATASET=unknown
if [ -f /data/vendor/timezones.sqlite ]; then
  TZ_DATASET="$(cat /data/vendor/timezones.version 2>/dev/null || echo vendored)"
  echo "==> timezones (vendored, $TZ_DATASET)"
  cp /data/vendor/timezones.sqlite "$TILEDIR/timezones.sqlite"
else
  echo '==> timezones UNAVAILABLE — continuing; this region will not be promoted'
  TIMEZONES=false
fi

echo '==> admins'
valhalla_build_admins -c "$CONF" "${PBFS[@]}"

echo '==> tiles (the long step, and the only one that has to see every country at once)'
valhalla_build_tiles -c "$CONF" "${PBFS[@]}"

echo '==> cutting along national boundaries'
python3 "$WORK/scripts/cut-by-country.py" "$TILEDIR" "$CUTS" "${CUT_ARGS[@]}"

# ── one archive per country ─────────────────────────────────────────────────────────────────
# THE BUILD ID IS THE CONTRACT. A GraphId carries an index assigned during the build, so an edge
# in one country's tile refers to a node in another's by a number that only means anything within
# ONE build. Two countries from different builds therefore produce edges pointing at the wrong
# nodes — silently, with no error and no crash, just wrong routes. Every cut carries the id and
# the car refuses to mix them.
BUILD_ID="$REGION-$(date -u +%Y-%m-%d)"
ENGINE="$(valhalla_build_tiles --version 2>&1 | awk '{print $2}')"

for member in "${MEMBERS[@]}"; do
  code="${member%%:*}"
  [ -d "$CUTS/$code" ] || { echo "no tiles cut for $code" >&2; exit 1; }
  archive="$OUT/$code-tiles.tar"
  ( cd "$CUTS/$code" && tar -cf "$archive" . )
  cat > "$OUT/$code-tiles.json" <<META
{
  "country": "$code",
  "artifact": "country-tiles",
  "engine": "valhalla",
  "engineVersion": "$ENGINE",
  "buildId": "$BUILD_ID",
  "region": "$REGION",
  "members": [$(printf '"%s",' "${MEMBERS[@]%%:*}" | sed 's/,$//')],
  "dataDate": "$(date -u +%Y-%m-%d)",
  "timezones": $TIMEZONES,
  "tiles": $(find "$CUTS/$code" -name '*.gph' | wc -l),
  "bytes": $(stat -c%s "$archive"),
  "sha256": "$(sha256sum "$archive" | cut -d' ' -f1)"
}
META
  echo "    $code: $(find "$CUTS/$code" -name '*.gph' | wc -l) tiles, \
$(( $(stat -c%s "$archive") / 1048576 )) MB"
done

echo "==> done: $BUILD_ID"
