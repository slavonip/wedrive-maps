#!/usr/bin/env bash
# Build ONE package's Valhalla graph from one or more OSM extracts.
#
# The multi-extract case is the point: two countries built separately are each cut at the
# frontier, so a car that crosses one needs a graph built from both PBFs in a SINGLE pass.
# Valhalla takes several inputs for exactly this.
#
#   usage: build-graph.sh <package-id> <geofabrik-path> [geofabrik-path ...]
#   e.g.   build-graph.sh md-ro europe/moldova europe/romania
#
# Runs inside the pinned Valhalla image (see the workflow); everything it touches is under /data.
set -euo pipefail

PACKAGE="${1:?package id, e.g. md-ro}"
shift
REGIONS=("$@")
[ ${#REGIONS[@]} -gt 0 ] || { echo "at least one geofabrik path is required" >&2; exit 2; }

WORK=/data
SRC="$WORK/src"
TILEDIR="$WORK/tiles_$PACKAGE"
EXTRACT="$WORK/out/$PACKAGE.tar"
CONF="$WORK/valhalla_$PACKAGE.json"

mkdir -p "$SRC" "$TILEDIR" "$WORK/out"

PBFS=()
for region in "${REGIONS[@]}"; do
  name="$(basename "$region")-latest.osm.pbf"
  pbf="$SRC/$name"
  if [ ! -f "$pbf" ]; then
    echo "==> downloading $region"
    curl -fsSL -o "$pbf" "https://download.geofabrik.de/${region}-latest.osm.pbf"
  fi
  PBFS+=("$pbf")
done
echo "==> sources: $(du -ch "${PBFS[@]}" | tail -1 | cut -f1)"

# Admins and timezones are NOT optional for a car that crosses borders (§1): without them
# Valhalla cannot apply per-country driving rules, and arrival times are wrong the moment you
# leave your own timezone.
#
# THE TIMEZONE DATA IS VENDORED, NOT FETCHED. `valhalla_build_timezones` downloads a shapefile
# from a third party's GitHub release at build time, and on 2026-09-17 that download simply
# failed — a monthly job must not depend on someone else's release being reachable at 3am. The
# image carries the file; this only rebuilds the database from it.
echo '==> config'
valhalla_build_config \
  --mjolnir-tile-dir "$TILEDIR" \
  --mjolnir-tile-extract "$EXTRACT" \
  --mjolnir-timezone "$TILEDIR/timezones.sqlite" \
  --mjolnir-admin "$TILEDIR/admins.sqlite" > "$CONF"

TIMEZONES=true
if [ -f /data/vendor/timezones.sqlite ]; then
  echo '==> timezones (vendored)'
  cp /data/vendor/timezones.sqlite "$TILEDIR/timezones.sqlite"
elif valhalla_build_timezones > "$TILEDIR/timezones.sqlite" 2>/dev/null && [ -s "$TILEDIR/timezones.sqlite" ]; then
  echo '==> timezones (built; this needed the network and may not next time)'
else
  # NOT fatal, and NOT silent. Without timezones the graph still routes correctly — the loss is
  # arrival times once the car crosses into another zone, which this car does. So the build goes
  # on, the flag goes into the metadata, and the manifest refuses to promote a package carrying
  # it. A missing timezone database must cost a release, never a wrong ETA nobody was told about.
  echo '==> timezones UNAVAILABLE — continuing; this package will not be promoted'
  rm -f "$TILEDIR/timezones.sqlite"
  TIMEZONES=false
fi

echo '==> admins'
valhalla_build_admins -c "$CONF" "${PBFS[@]}"

echo '==> tiles (the long step)'
valhalla_build_tiles -c "$CONF" "${PBFS[@]}"

echo '==> extract'
valhalla_build_extract -c "$CONF" -v

# Stamp what this is and what built it. The BUILDER's version is what a tile's own version string
# says, and the app has no version gate at all — so alignment is the factory's job to guarantee
# and to state, not the car's to discover.
cat > "$WORK/out/$PACKAGE-graph.json" <<META
{
  "package": "$PACKAGE",
  "artifact": "routing-graph",
  "engine": "valhalla",
  "engineVersion": "$(valhalla_build_tiles --version 2>&1 | awk '{print $2}')",
  "regions": [$(printf '"%s",' "${REGIONS[@]}" | sed 's/,$//')],
  "dataDate": "$(date -u +%Y-%m-%d)",
  "timezones": $TIMEZONES,
  "tzdataVersion": "$(dpkg-query -W -f='${Version}' tzdata 2>/dev/null || echo unknown)",
  "bytes": $(stat -c%s "$EXTRACT"),
  "sha256": "$(sha256sum "$EXTRACT" | cut -d' ' -f1)"
}
META

echo "==> done: $EXTRACT"
cat "$WORK/out/$PACKAGE-graph.json"
