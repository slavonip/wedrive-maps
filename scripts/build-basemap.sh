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

python3 - "$BASEMAP" "$PACKAGE" "$BUILD" "$MAXZOOM" "$W" "$S" "$E" "$N" <<'META' > "$OUT_DIR/$PACKAGE-basemap.json"
import hashlib
import json
import os
import subprocess
import sys

path, package, build, maxzoom, w, s, e, n = sys.argv[1:9]

# The schema name lives in the archive's own metadata; take it from there rather than from what
# we believe we asked for.
meta = {}
try:
    raw = subprocess.run([os.environ.get("PMTILES", "pmtiles"), "show", "--metadata", path],
                         capture_output=True, text=True).stdout
    meta = json.loads(raw) if raw.strip().startswith("{") else {}
except Exception:
    meta = {}

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
    "schema": meta.get("name") or meta.get("type") or "protomaps-basemap",
    "schemaVersion": meta.get("version"),
    "vectorLayers": sorted(layer["id"] for layer in meta.get("vector_layers", [])) or None,
    "maxzoom": int(maxzoom),
    "bbox": [float(w), float(s), float(e), float(n)],
    "dataDate": meta.get("planetTime", "")[:10] or build[:4] + "-" + build[4:6] + "-" + build[6:8],
    "bytes": os.path.getsize(path),
    "sha256": digest.hexdigest(),
}, indent=2))
META

echo "==> done: $BASEMAP"
cat "$OUT_DIR/$PACKAGE-basemap.json"
