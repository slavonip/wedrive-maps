#!/usr/bin/env bash
# Cut ONE package's basemap out of the Protomaps planet.
#
#   usage: build-basemap.sh <package-id> <W> <S> <E> <N>
#
# A package is two artifacts and this is the other one. The graph is what the car routes on; the
# basemap is what the driver sees. They are built from DIFFERENT upstreams — Geofabrik OSM
# extracts and Protomaps planet builds — which is why §13 insists both carry a dataDate and why
# the Maps screen has to show them: a road drawn but not routable, or routable but not drawn,
# reads as "the app is broken" rather than as "these two files are six weeks apart".
#
# No planet download. `pmtiles extract` pulls only the tiles inside the box over HTTP range
# requests — Moldova came out at 151 MB from a 137 GB planet that was never fetched.
set -euo pipefail

PACKAGE="${1:?package id}"
W="${2:?west}"; S="${3:?south}"; E="${4:?east}"; N="${5:?north}"

OUT_DIR="${OUT_DIR:-/data/out}"
WORK="${WORK:-/data}"
MAXZOOM="${MAXZOOM:-14}"

# 14 NAVIGATES; 15 additionally brings POIs including parking, at a real size cost (§13), and
# Protomaps basemaps stop at 15 — asking for more silently yields nothing. Overridable rather
# than hardcoded because §8b's charging search may yet want 15.

mkdir -p "$OUT_DIR" "$WORK/tools"

# ── the tool ────────────────────────────────────────────────────────────────────────────────
# Pinned, like everything else the factory depends on (§17). A floating version of a downloader
# is a monthly job that changes behaviour under you.
PMTILES_VERSION="${PMTILES_VERSION:-1.31.2}"
if [ ! -x "$WORK/tools/pmtiles" ]; then
  echo "==> go-pmtiles $PMTILES_VERSION"
  curl -fsSL -o /tmp/pmtiles.tar.gz \
    "https://github.com/protomaps/go-pmtiles/releases/download/v${PMTILES_VERSION}/go-pmtiles_${PMTILES_VERSION}_Linux_x86_64.tar.gz"
  tar -xzf /tmp/pmtiles.tar.gz -C "$WORK/tools" pmtiles
  chmod +x "$WORK/tools/pmtiles"
fi
PMTILES="$WORK/tools/pmtiles"

# ── which planet build ──────────────────────────────────────────────────────────────────────
# The builds PAGE is a JS app and lists nothing a script can read; the manifest behind it does.
# Taking the newest rather than pinning, because the whole point of a monthly job is fresh data
# — and the key is recorded in the manifest, so any package can be traced to the build it came
# from and rebuilt from that exact one.
echo '==> newest Protomaps build'
BUILD=$(curl -fsSL https://build-metadata.protomaps.dev/builds.json |
  python3 -c 'import json,sys; print(sorted(d["key"] for d in json.load(sys.stdin))[-1])')
echo "    $BUILD"

BASEMAP="$OUT_DIR/$PACKAGE.pmtiles"
echo "==> extract $W,$S,$E,$N at z0-$MAXZOOM"
"$PMTILES" extract "https://build.protomaps.com/$BUILD" "$BASEMAP" \
  --bbox="$W,$S,$E,$N" --maxzoom="$MAXZOOM" --download-threads=8

# ── what it is, stamped beside it ───────────────────────────────────────────────────────────
# `pmtiles show` reads the header and the metadata the archive carries about itself, including
# the vector_layers a style has to match. SCHEMA IS LOAD-BEARING: a style pointed at the wrong
# one renders NOTHING, silently, and that is the single most confusing failure in §13. So it is
# recorded here and checked by probe-basemap.py rather than assumed.
SHOW=$("$PMTILES" show "$BASEMAP")
echo "$SHOW"

python3 - "$BASEMAP" "$PACKAGE" "$BUILD" "$MAXZOOM" "$W" "$S" "$E" "$N" "$PMTILES" \
  <<'META' > "$OUT_DIR/$PACKAGE-basemap.json"
import hashlib
import json
import os
import subprocess
import sys

path, package, build, maxzoom, w, s, e, n = sys.argv[1:9]

# The schema name lives in the archive's own metadata; take it from there rather than from what
# we believe we asked for.
# THE BINARY IS PASSED IN, not looked up on PATH. It is downloaded into $WORK/tools and is
# not on PATH at all; the first version of this used `os.environ.get("PMTILES", "pmtiles")`,
# found nothing, swallowed the exception and wrote the FALLBACK schema string into the
# manifest. That fabricated value then matched the app's allowlist by coincidence while the
# real schema did not — so the gate would have passed a placeholder and refused a correctly
# labelled package. Found 2026-09-17 by opening the app and reading the row.
meta = {}
raw = subprocess.run([sys.argv[9], "show", "--metadata", path],
                     capture_output=True, text=True, check=True).stdout
meta = json.loads(raw)

digest = hashlib.sha256()
with open(path, "rb") as handle:
    for chunk in iter(lambda: handle.read(1 << 20), b""):
        digest.update(chunk)

print(json.dumps({
    "package": package,
    "artifact": "basemap",
    "format": "pmtiles",
    "upstream": "protomaps",
    "build": build,
    # `protomaps-v4`, matching the convention `MapRegion.schema` has used since the first
    # sideloaded region. The archive itself says `name: "Protomaps Basemap"` and
    # `version: "4.15.2"`; the MAJOR is what decides whether a style can read it, because that
    # is what changes layer names, and a style pointed at the wrong schema draws nothing and
    # reports nothing (§13).
    "schema": "protomaps-v" + str(meta["version"]).split(".")[0],
    "schemaName": meta.get("name"),
    "schemaVersion": meta.get("version"),
    "vectorLayers": sorted(layer["id"] for layer in meta.get("vector_layers", [])) or None,
    "attribution": meta.get("attribution"),
    "maxzoom": int(maxzoom),
    "bbox": [float(w), float(s), float(e), float(n)],
    "dataDate": meta.get("planetTime", "")[:10] or build[:4] + "-" + build[4:6] + "-" + build[6:8],
    "bytes": os.path.getsize(path),
    "sha256": digest.hexdigest(),
}, indent=2))
META

# ── the third artifact: a searchable index of place names ───────────────────────────────────
#
# The names are already inside the basemap, and until now nothing could look them up: MapLibre
# queries only what is currently rendered, so a destination off-screen was unfindable and the
# search screen had to say "not built" (§8a level 3).
#
# DECODED HERE rather than on the head unit. A PMTiles and MVT reader in Kotlin is a few hundred
# lines to own forever, running on the slowest computer in the arrangement every time someone
# types a letter. One pass on a runner produces a flat file the car scans instantly — measured
# on Moldova: 5040 places, 353 KB, small villages included, and typing "Ia" puts Iași first
# because the index carries population.
echo '==> place index'
python3 "$WORK/scripts/build-places.py" "$BASEMAP" "$W,$S,$E,$N"   "$OUT_DIR/$PACKAGE-places.json" --pmtiles "$PMTILES" --zoom 10

python3 - "$OUT_DIR/$PACKAGE-places.json" "$PACKAGE" <<'PLACES' > "$OUT_DIR/$PACKAGE-places-meta.json"
import hashlib
import json
import os
import sys

path, package = sys.argv[1], sys.argv[2]
digest = hashlib.sha256()
with open(path, "rb") as handle:
    for chunk in iter(lambda: handle.read(1 << 20), b""):
        digest.update(chunk)
index = json.load(open(path, encoding="utf-8"))
print(json.dumps({
    "package": package,
    "artifact": "places",
    "count": index["count"],
    "zoom": index["zoom"],
    "bytes": os.path.getsize(path),
    "sha256": digest.hexdigest(),
}, indent=2))
PLACES

echo "==> done: $BASEMAP"
cat "$OUT_DIR/$PACKAGE-basemap.json"
cat "$OUT_DIR/$PACKAGE-places-meta.json"
