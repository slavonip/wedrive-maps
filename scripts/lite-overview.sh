#!/usr/bin/env bash
# lite-overview.sh <out-dir>
#
# The Europe Lite OVERVIEW map: one overview.pmtiles that the app draws under the installed
# countries, so a country the car drives through on Europe Lite alone (no full package) shows its
# roads and its towns instead of the WORLD's blank z0-6 (owner, 2026-11-25: "motorways down to
# secondary + labels of cities AND villages, up to zoom 9" — variant A1 of
# docs/research/2026-11-25-lite-overview-places.md in the app repository).
# VISUAL ONLY — no graph, no search, no features. Built only on GitHub Actions (lite-overlay.yml).
#
#   1. the newest Protomaps planet build (builds.json), the source of every basemap we ship;
#   2. `pmtiles extract --region polygons/europe-lite.geojson --maxzoom 9`: the Lite coverage
#      (Geofabrik europe.poly, the polygon the Lite graph is cut from; RU is not in it);
#   3. `tile-join -l earth -l water -l boundaries -l places -l roads`: no landuse (436 of 555 MB at
#      z0-9, measured 2026-11-25), no landcover, buildings, pois;
#   4. checks: PMTiles v3, maxzoom 9, EXACTLY those five layers, tiles at z0 and over Thessaloniki
#      at z9, size <= MAX_MB; then size + sha256 into overview.json for the manifest.
#
# Needs: curl, python3, tile-join (tippecanoe) on PATH. Writes <out>/overview.pmtiles, overview.json.
set -euo pipefail
OUT="${1:?out dir}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REGION="${REGION:-$HERE/../polygons/europe-lite.geojson}"
WORK="${WORK:-$(mktemp -d)}"
mkdir -p "$OUT" "$WORK/tools"
LAYERS="earth water boundaries places roads"
MAXZOOM=9
# AC-1 of the plan: the overview must stay at or under 120 MB (98.4 MB measured for z0-9, 5 layers)
MAX_MB="${MAX_MB:-120}"

PMTILES_VERSION="${PMTILES_VERSION:-1.31.2}"
if [ ! -x "$WORK/tools/pmtiles" ]; then
  echo "==> go-pmtiles $PMTILES_VERSION"
  curl -fsSL -o "$WORK/pmtiles.tar.gz" \
    "https://github.com/protomaps/go-pmtiles/releases/download/v${PMTILES_VERSION}/go-pmtiles_${PMTILES_VERSION}_Linux_x86_64.tar.gz"
  tar -xzf "$WORK/pmtiles.tar.gz" -C "$WORK/tools" pmtiles
  chmod +x "$WORK/tools/pmtiles"
fi
PMTILES="$WORK/tools/pmtiles"
command -v tile-join >/dev/null || { echo "::error::tile-join (tippecanoe) not on PATH"; exit 1; }
[ -s "$REGION" ] || { echo "::error::region polygon $REGION missing"; exit 1; }

echo '==> newest Protomaps build'
BUILD="${BUILD:-$(curl -fsSL https://build-metadata.protomaps.dev/builds.json |
  python3 -c 'import json,sys; print(sorted(d["key"] for d in json.load(sys.stdin))[-1])')}"
case "$BUILD" in *.pmtiles) ;; *) BUILD="$BUILD.pmtiles" ;; esac
BUILD_ID="${BUILD%.pmtiles}"
echo "    $BUILD (id $BUILD_ID)"

# Ranged reads against build.protomaps.com: one reset connection must not fail the build (the AL
# lesson of run 36367106205). A partial output is removed before a retry.
FULL="$WORK/overview-all-layers.pmtiles"
for n in 1 2 3; do
  rm -f "$FULL"
  echo "==> extract z0-$MAXZOOM of the Lite polygon (attempt $n)"
  if "$PMTILES" extract "https://build.protomaps.com/$BUILD" "$FULL" --region="$REGION" \
      --maxzoom="$MAXZOOM" --download-threads=8; then
    break
  fi
  [ "$n" = 3 ] && { echo "::error::extract failed three times"; exit 1; }
  sleep $((n * 45))
done
echo "    all layers: $(stat -c %s "$FULL") bytes"

echo "==> keep only: $LAYERS"
args=()
for l in $LAYERS; do args+=(-l "$l"); done
tile-join -f -pk -o "$OUT/overview.pmtiles" "${args[@]}" "$FULL"

echo '==> checks'
"$PMTILES" show "$OUT/overview.pmtiles" > "$WORK/show.txt"
cat "$WORK/show.txt"
"$PMTILES" show --metadata "$OUT/overview.pmtiles" > "$WORK/meta.json"
# z0, and z9 over Thessaloniki (40.64 N, 22.94 E -> x=288, y=192 at z9): a Lite-only country
"$PMTILES" tile "$OUT/overview.pmtiles" 0 0 0 > "$WORK/z0.mvt"
"$PMTILES" tile "$OUT/overview.pmtiles" 9 288 192 > "$WORK/z9.mvt"
python3 - "$OUT" "$WORK" "$BUILD_ID" "$MAXZOOM" "$LAYERS" "$(stat -c %s "$FULL")" "$MAX_MB" <<'PY'
import hashlib, json, os, re, sys, datetime
out, work, build, maxzoom, layers = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5].split()
all_layers_size, max_mb = int(sys.argv[6]), float(sys.argv[7])
f = os.path.join(out, "overview.pmtiles")
head = open(f, "rb").read(8)
assert head[:7] == b"PMTiles" and head[7] == 3, "not a PMTiles v3 archive"
show = open(os.path.join(work, "show.txt"), encoding="utf-8").read()
mz = re.search(r"max zoom[^\d]*(\d+)", show, re.I)
assert mz and int(mz.group(1)) == maxzoom, "max zoom is not %d: %s" % (maxzoom, mz and mz.group(1))
meta = json.load(open(os.path.join(work, "meta.json"), encoding="utf-8"))
got = sorted(l["id"] for l in meta.get("vector_layers", []))
assert got == sorted(layers), "layers %s, expected exactly %s" % (got, sorted(layers))
for t in ("z0.mvt", "z9.mvt"):
    assert os.path.getsize(os.path.join(work, t)) > 0, "empty tile " + t
size = os.path.getsize(f)
assert size <= max_mb * 1e6, "overview.pmtiles is %.1f MB, the gate is %.0f MB" % (size / 1e6, max_mb)
h = hashlib.sha256()
with open(f, "rb") as fh:
    for b in iter(lambda: fh.read(1 << 20), b""):
        h.update(b)
desc = {"file": "overview.pmtiles", "size": size, "sha256": h.hexdigest(), "build": build,
        "date": "%s-%s-%s" % (build[:4], build[4:6], build[6:8]) if build[:8].isdigit() else build,
        "maxzoom": maxzoom, "layers": sorted(layers), "schema": "protomaps-v4",
        "region": "polygons/europe-lite.geojson", "all_layers_size": all_layers_size,
        "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
json.dump(desc, open(os.path.join(out, "overview.json"), "w"), indent=1)
print("overview.pmtiles %d bytes (%.1f MB; all layers z0-%d were %.1f MB), sha256 %s, layers %s"
      % (size, size / 1e6, maxzoom, all_layers_size / 1e6, desc["sha256"], got))
PY
