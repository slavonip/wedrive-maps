#!/usr/bin/env bash
# ONE graph over many countries, from ONE merged PBF, then cut back into countries.
#
#   usage: build-master.sh <region-id> <CC:geofabrik-path> [...]
#   e.g.   build-master.sh eu-core AT:europe/austria HU:europe/hungary \
#                                  MD:europe/moldova RO:europe/romania
#
# THE MERGE IS THE POINT, and it is doing two jobs at once.
#
# First, it is the shape the design wants. A country is the unit of DISTRIBUTION and never the
# unit of build (docs/map-factory.md §12): build one coherent graph, cut it along national
# boundaries afterwards, and a frontier tile becomes the SAME FILE in both countries' packages.
# Measured: two separate builds share 35 tile paths with 0 byte-identical; one master build cut
# afterwards shares 36 with 36 identical.
#
# Second, it may sidestep a defect we cannot fix. `valhalla_build_tiles` given four PBFs aborts
# with `vector::_M_range_check` against an empty vector on tile 2/776984, identically on 3.5.1
# and 3.6.3, while every country alone and every pair builds cleanly. Eight bisecting runs
# established that the variable was the builder, not any country. A master build hands Valhalla a
# SINGLE file, so the multi-input path that fails is never taken. Whether that is the cause is
# not yet known — this run is how we find out.
#
# Runs inside the pinned Valhalla image; everything it touches is under /data.
set -euo pipefail

REGION="${1:?region id, e.g. eu-core}"
shift
MEMBERS=("$@")
[ ${#MEMBERS[@]} -gt 0 ] || { echo "at least one CC:geofabrik-path is required" >&2; exit 2; }

WORK=/data
SRC="$WORK/src"
POLY="$WORK/poly"
TILEDIR="$WORK/tiles_$REGION"
CUTS="$WORK/cuts_$REGION"
OUT="$WORK/out"
CONF="$WORK/valhalla_$REGION.json"
MASTER="$WORK/master_$REGION.osm.pbf"

mkdir -p "$SRC" "$POLY" "$TILEDIR" "$CUTS" "$OUT"

# ── osmium, for the merge ───────────────────────────────────────────────────────────────────
# From Ubuntu's own archive rather than a third party's release, which is a different risk class
# from the timezone shapefile that failed us — but it is still a fetch at 02:17, and if it ever
# becomes a problem the answer is to bake it into a pinned image of our own.
if ! command -v osmium >/dev/null; then
  echo '==> osmium-tool'
  apt-get update -qq && apt-get install -y -qq osmium-tool >/dev/null
fi
osmium --version | head -1

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
  # Romania's set, because Moldova sits inside Romania's rectangle — and past two countries that
  # is the normal case, not the exception.
  poly="$POLY/$name.poly"
  [ -f "$poly" ] || curl -fsSL -o "$poly" "https://download.geofabrik.de/${region}.poly"
  CUT_ARGS+=("$code:$poly")
done

echo "==> sources: $(du -ch "${PBFS[@]}" | tail -1 | cut -f1) across ${#PBFS[@]} countries"

echo '==> merging into one coherent extract'
# Geofabrik extracts overlap at frontiers, so the same way appears in two files. `osmium merge`
# resolves that properly: it merges sorted inputs and keeps one copy of each object, which is
# exactly what concatenating the files would NOT do.
rm -f "$MASTER"
osmium merge "${PBFS[@]}" -o "$MASTER" --overwrite
echo "    master: $(du -h "$MASTER" | cut -f1)"

# ── config: a DIRECTORY, not an extract ─────────────────────────────────────────────────────
# A car assembles its graph from several per-country downloads, so it reads a tile_dir. Measured
# 2026-09-17 with the engine held open: 175 ms more to open, and nothing measurable per route.
echo '==> config'
valhalla_build_config \
  --mjolnir-tile-dir "$TILEDIR" \
  --mjolnir-timezone "$TILEDIR/timezones.sqlite" \
  --mjolnir-admin "$TILEDIR/admins.sqlite" > "$CONF"
python3 - "$CONF" <<'PY'
import json, sys
path = sys.argv[1]
config = json.load(open(path))
# POP IT, never blank it: an empty string sends Valhalla looking for a file called "" and yields
# a graph with no tiles and no error.
config["mjolnir"].pop("tile_extract", None)
json.dump(config, open(path, "w"), indent=2)
PY

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TIMEZONES=true
TZ_DATASET=unknown
TZ_RUNTIME=unknown
if [ -f /data/vendor/timezones.sqlite ]; then
  TZ_DATASET="$(cat /data/vendor/timezones.version 2>/dev/null || echo vendored)"
  echo "==> timezones (vendored, $TZ_DATASET)"

  # THE TIMEZONE DATASET IS A VERSIONED INPUT, CHECKED BEFORE USE — not a file vendored once and
  # trusted forever. The one we trusted for weeks was built from timezone-boundary-builder 2024a
  # and still carried five Balkan zones that tzdata has since merged into Europe/Belgrade; the
  # runtime refuses such an identifier outright, half an hour into tile building, and it does so
  # with two unrelated-looking messages depending on which path reaches it first.
  #
  # `set -e` is on, so a refusal stops the build here. That is the entire point: the alternative
  # is an abort with a core dump and nothing to bisect.
  rm -f "$WORK/tzdata.runtime"
  python3 "$HERE/check-timezones.py" /data/vendor/timezones.sqlite \
    --expect-dataset "$TZ_DATASET" --record "$WORK/tzdata.runtime"
  if [ -f "$WORK/tzdata.runtime" ]; then TZ_RUNTIME="$(cat "$WORK/tzdata.runtime")"; fi

  cp /data/vendor/timezones.sqlite "$TILEDIR/timezones.sqlite"
else
  echo '::warning::no timezone database; this region cannot be promoted'
  TIMEZONES=false
fi

echo '==> admins'
valhalla_build_admins -c "$CONF" "$MASTER"

echo '==> tiles: ONE build over ONE file'
valhalla_build_tiles -c "$CONF" "$MASTER"

# The master extract is gigabytes of intermediate on a runner with a finite disk, and nothing
# downstream needs it.
rm -f "$MASTER"

echo '==> cutting along national boundaries'
python3 "$HERE/cut-by-country.py" "$TILEDIR" "$CUTS" "${CUT_ARGS[@]}"

# ── THE PROPERTY THE WHOLE DESIGN RESTS ON ──────────────────────────────────────────────────
# A tile shared by two countries must be byte-identical in both, or the second install silently
# replaces the first with a different graph. Checked here rather than assumed, because it is the
# single assumption that would fail invisibly.
echo '==> verifying that shared tiles are identical'
python3 - "$CUTS" <<'PY'
import hashlib
import itertools
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
countries = {d.name: {str(p.relative_to(d)): p for p in d.rglob("*.gph")}
             for d in sorted(root.iterdir()) if d.is_dir()}

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

worst = 0
for a, b in itertools.combinations(sorted(countries), 2):
    shared = sorted(set(countries[a]) & set(countries[b]))
    if not shared:
        continue
    differing = [n for n in shared
                 if digest(countries[a][n]) != digest(countries[b][n])]
    print(f"   {a}/{b}: {len(shared)} shared, {len(shared) - len(differing)} identical")
    worst = max(worst, len(differing))
    for name in differing[:3]:
        print(f"      DIFFERS: {name}")

if worst:
    print("   a shared tile differs between countries: installing one would replace the other")
    raise SystemExit(1)
print("   every shared tile is byte-identical")
PY

# ── one archive per country ─────────────────────────────────────────────────────────────────
BUILD_ID="$REGION-$(date -u +%Y-%m-%d)"
ENGINE="$(valhalla_build_tiles --version 2>&1 | awk '{print $2}')"

for member in "${MEMBERS[@]}"; do
  code="${member%%:*}"
  [ -d "$CUTS/$code" ] || { echo "no tiles cut for $code" >&2; exit 1; }
  archive="$OUT/$code-tiles.tar"
  ( cd "$CUTS/$code" && tar -cf "$archive" . )

  # THE MANIFEST NAMES EVERY TILE AND ITS HASH, which is what lets a car install and remove
  # countries independently: a tile is deleted only when no installed country still claims it,
  # and a month later only the tiles whose hash changed are fetched. Content addressing turns
  # both reference counting and delta updates into the same one mechanism.
  python3 - "$CUTS/$code" "$code" "$BUILD_ID" "$REGION" "$ENGINE" "$TIMEZONES" "$TZ_DATASET" \
    "$archive" <<'META' > "$OUT/$code-tiles.json"
import hashlib
import json
import os
import pathlib
import sys

cut, code, build_id, region, engine, timezones, tzdata, archive, tzruntime = sys.argv[1:10]
root = pathlib.Path(cut)
tiles = []
for path in sorted(root.rglob("*.gph")):
    data = path.read_bytes()
    tiles.append({
        "path": str(path.relative_to(root)).replace("\\", "/"),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    })

whole = hashlib.sha256()
with open(archive, "rb") as handle:
    for chunk in iter(lambda: handle.read(1 << 20), b""):
        whole.update(chunk)

print(json.dumps({
    "country": code,
    "artifact": "country-tiles",
    "engine": "valhalla",
    "engineVersion": engine,
    # EVERY INSTALLED COUNTRY MUST SHARE THIS. A GraphId carries an index assigned during the
    # build, so an edge in one country refers to a node in another by a number that means
    # something only within one build. Mixing vintages gives edges pointing at the wrong nodes,
    # silently.
    "buildId": build_id,
    "region": region,
    "dataDate": build_id.rsplit("-", 3)[-3] + "-" + build_id.rsplit("-", 2)[-2] + "-"
                + build_id.rsplit("-", 1)[-1],
    "timezones": timezones == "true",
    # WHICH RULES THIS GRAPH WAS BUILT UNDER, on both sides of the question. The zone is written
    # into every node at build time and can never be corrected afterwards, so in six months the
    # only way to know whether a graph predates a zone merge is to have written it down here:
    # `tzdata` is the polygon dataset (timezone-boundary-builder), `tzdataRuntime` the tzdata of
    # the image whose library accepted those identifiers.
    "tzdata": tzdata,
    "tzdataRuntime": tzruntime,
    "tileCount": len(tiles),
    "bytes": os.path.getsize(archive),
    "sha256": whole.hexdigest(),
    "tiles": tiles,
}, indent=2))
META
  echo "    $code: $(python3 -c "
import json,sys
m = json.load(open('$OUT/$code-tiles.json'))
print(f\"{m['tileCount']} tiles, {m['bytes'] // 1048576} MB\")")"
done

echo "==> done: $BUILD_ID"
