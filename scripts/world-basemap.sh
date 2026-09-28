#!/usr/bin/env bash
# world-basemap.sh <out-dir>
#
# The WORLD overview of WeDrive: one small world.pmtiles drawn under every installed country, so
# zooming out shows the whole planet and a country with no detailed map is never a black hole.
# VISUAL ONLY — no routing graph, no search, no addresses, no features. Built only here, on
# GitHub Actions (owner 2026-09-28), never on a PC.
#
#   1. the newest Protomaps planet build (builds.json), the same source as every country basemap;
#   2. `pmtiles extract --maxzoom=6` of the whole planet: z0-6 was measured at 45 MB with every
#      layer (z0-5 15 MB, z0-7 188 MB), and z6 is the owner's choice;
#   3. `tile-join -l earth -l water -l boundaries -l places`: only what an overview draws — no
#      roads, landuse, landcover, buildings, pois;
#   4. checks: PMTiles v3, maxzoom 6, EXACTLY those four layers, a tile at z0 and one at z6 over
#      Chișinău; then size + sha256 into world.json for the manifest.
#
# Needs: curl, python3, tile-join (tippecanoe) on PATH. Writes <out>/world.pmtiles, <out>/world.json.
set -euo pipefail
OUT="${1:?out dir}"
WORK="${WORK:-$(mktemp -d)}"
mkdir -p "$OUT" "$WORK/tools"
LAYERS="earth water boundaries places"
MAXZOOM=6

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

echo '==> newest Protomaps build'
BUILD="${BUILD:-$(curl -fsSL https://build-metadata.protomaps.dev/builds.json |
  python3 -c 'import json,sys; print(sorted(d["key"] for d in json.load(sys.stdin))[-1])')}"
# builds.json keys carry the extension ("20260927.pmtiles"); an input may be bare ("20260927").
case "$BUILD" in *.pmtiles) ;; *) BUILD="$BUILD.pmtiles" ;; esac
BUILD_ID="${BUILD%.pmtiles}"
echo "    $BUILD (id $BUILD_ID)"

# A remote extract is ranged reads against build.protomaps.com; one reset connection must not
# fail the build (the AL lesson of run 36367106205). A partial output is removed before a retry.
FULL="$WORK/world-all-layers.pmtiles"
for n in 1 2 3; do
  rm -f "$FULL"
  echo "==> extract z0-$MAXZOOM of the planet (attempt $n)"
  if "$PMTILES" extract "https://build.protomaps.com/$BUILD" "$FULL" --maxzoom="$MAXZOOM" --download-threads=8; then
    break
  fi
  [ "$n" = 3 ] && { echo "::error::extract failed three times"; exit 1; }
  sleep $((n * 45))
done
echo "    all layers: $(stat -c %s "$FULL") bytes"

echo "==> keep only: $LAYERS"
args=()
for l in $LAYERS; do args+=(-l "$l"); done
tile-join -f -pk -o "$OUT/world.pmtiles" "${args[@]}" "$FULL"

echo '==> checks'
"$PMTILES" show "$OUT/world.pmtiles" > "$WORK/show.txt"
cat "$WORK/show.txt"
"$PMTILES" show --metadata "$OUT/world.pmtiles" > "$WORK/meta.json"
# z0 tile, and a z6 tile over Chișinău (47.02 N, 28.83 E -> x=37, y=22 at z6)
"$PMTILES" tile "$OUT/world.pmtiles" 0 0 0 > "$WORK/z0.mvt"
"$PMTILES" tile "$OUT/world.pmtiles" 6 37 22 > "$WORK/z6.mvt"
python3 - "$OUT" "$WORK" "$BUILD_ID" "$MAXZOOM" "$LAYERS" "$(stat -c %s "$FULL")" <<'PY'
import hashlib, json, os, re, sys, datetime
out, work, build, maxzoom, layers = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5].split()
all_layers_size = int(sys.argv[6])
f = os.path.join(out, "world.pmtiles")
head = open(f, "rb").read(8)
assert head[:7] == b"PMTiles" and head[7] == 3, "not a PMTiles v3 archive"
show = open(os.path.join(work, "show.txt"), encoding="utf-8").read()
mz = re.search(r"max zoom[^\d]*(\d+)", show, re.I)
assert mz and int(mz.group(1)) == maxzoom, "max zoom is not %d: %s" % (maxzoom, mz and mz.group(1))
meta = json.load(open(os.path.join(work, "meta.json"), encoding="utf-8"))
got = sorted(l["id"] for l in meta.get("vector_layers", []))
assert got == sorted(layers), "layers %s, expected exactly %s" % (got, sorted(layers))
for t in ("z0.mvt", "z6.mvt"):
    assert os.path.getsize(os.path.join(work, t)) > 0, "empty tile " + t
size = os.path.getsize(f)
sha = hashlib.sha256(open(f, "rb").read()).hexdigest()
desc = {"file": "world.pmtiles", "size": size, "sha256": sha, "build": build,
        "date": "%s-%s-%s" % (build[:4], build[4:6], build[6:8]) if build[:8].isdigit() else build,
        "maxzoom": maxzoom, "layers": sorted(layers), "schema": "protomaps-v4",
        "all_layers_size": all_layers_size,
        "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
json.dump(desc, open(os.path.join(out, "world.json"), "w"), indent=1)
print("world.pmtiles %d bytes (%.1f MB; all layers z0-%d were %.1f MB), sha256 %s, layers %s"
      % (size, size / 1e6, maxzoom, all_layers_size / 1e6, sha, got))
PY
