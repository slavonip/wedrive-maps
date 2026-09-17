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

# ── WHAT THIS BUILD COSTS, measured rather than estimated ────────────────────────────────────
# The factory's standing estimate was "roughly half an hour per gigabyte", extrapolated from a
# handful of whole-JOB wall times that also contained PBF downloads and a basemap extract. The
# four-country master then built its tiles in 357 s at 1.5 GB, which that rule cannot explain —
# so the rule was measuring something other than what it claimed.
#
# Every build now records its own stages and its own peaks. Nothing here is for a scaling
# experiment in particular: the point is that the curve accumulates from ORDINARY builds, so the
# question "how big a master fits a runner" is answered by history rather than by one probe.
METRICS="$OUT/build-metrics.json"
STAGES="$WORK/stages.csv"
SAMPLES="$WORK/samples.csv"
echo "stage,seconds" > "$STAGES"
BUILD_STARTED=$(date +%s)

# Invoked THROUGH bash rather than executed. The exec bit does not survive this repository:
# it is developed on Windows with `core.filemode=false`, so `chmod +x` is never recorded and git
# stores 0644 — the container then refuses the script. Because the sampler is backgrounded, the
# refusal could not be caught by `set -e` either, so the first scale probe ran to completion and
# reported `samples: 0` having measured none of what it exists to measure.
bash "$(dirname "${BASH_SOURCE[0]}")/sample-resources.sh" "$SAMPLES" "$WORK" &
SAMPLER=$!

# A background process that dies is invisible by construction, so check rather than assume: the
# sampler writes its header immediately and a row within five seconds.
sleep 6
if ! [ -s "$SAMPLES" ] || [ "$(wc -l < "$SAMPLES")" -lt 2 ]; then
  # A warning, not a failure: a production build must not be lost because its instrumentation
  # was. The scale probe treats the same condition as fatal, because there the measurement IS
  # the deliverable.
  echo "::warning::the resource sampler produced nothing; this build records no peaks"
fi
# Killed however this script leaves, including the abort we spent a week chasing — a build that
# dies is exactly the build whose peak memory we want to know.
trap 'kill $SAMPLER 2>/dev/null || true' EXIT

stage() {
  local name="$1"
  local now
  now=$(date +%s)
  if [ -n "${STAGE_NAME:-}" ]; then
    echo "$STAGE_NAME,$((now - STAGE_STARTED))" >> "$STAGES"
  fi
  STAGE_NAME="$name"
  STAGE_STARTED=$now
}

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── osmium, for the merge ───────────────────────────────────────────────────────────────────
# From Ubuntu's own archive rather than a third party's release, which is a different risk class
# from the timezone shapefile that failed us — but it is still a fetch at 02:17, and if it ever
# becomes a problem the answer is to bake it into a pinned image of our own.
if ! command -v osmium >/dev/null; then
  echo '==> osmium-tool'
  apt-get update -qq && apt-get install -y -qq osmium-tool >/dev/null
fi
osmium --version | head -1

stage download
PBFS=()
CUT_ARGS=()
FRESHNESS_ARGS=()
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
  FRESHNESS_ARGS+=(--pbf "$code=$pbf")

  # The BOUNDARY, not the bounding box. Cutting md-ro by bounding box put 100% of the build in
  # Romania's set, because Moldova sits inside Romania's rectangle — and past two countries that
  # is the normal case, not the exception.
  poly="$POLY/$name.poly"
  [ -f "$poly" ] || curl -fsSL -o "$poly" "https://download.geofabrik.de/${region}.poly"
  CUT_ARGS+=("$code:$poly")
done

# ── IS THE OSM DATA AN IMPROVEMENT? ──────────────────────────────────────────────────────────
# Not "did the download succeed" — that was never in doubt. A mirror can serve a months-old file,
# or roll back to one older than what we already published, and both produce a release that looks
# newer by its date while containing older roads. The PBF header carries the replication
# timestamp of the OSM state it was cut from, so the real age can be read rather than assumed.
#
# The backwards case is the one an age limit cannot see, and it is the one that would replace a
# good graph with a worse one.
OSM_VERDICT="$WORK/osm.verdict.json"
rm -f "$OSM_VERDICT"
python3 "$HERE/check-osm-freshness.py" "${FRESHNESS_ARGS[@]}" \
  --index "${OSM_INDEX:-$WORK/index-tiles.json}" --record "$OSM_VERDICT"

SOURCE_BYTES=$(du -cb "${PBFS[@]}" | tail -1 | cut -f1)
echo "==> sources: $(du -ch "${PBFS[@]}" | tail -1 | cut -f1) across ${#PBFS[@]} countries"

stage merge
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
stage config
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

# The gate writes its own verdict here and the manifest embeds it verbatim, so what a graph
# claims about its timezone data is what the gate actually observed — not a shell variable
# reassembled alongside it.
TZ_VERDICT="$WORK/timezones.verdict.json"
rm -f "$TZ_VERDICT"
TZ_DATASET=unknown
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
  python3 "$HERE/check-timezones.py" /data/vendor/timezones.sqlite \
    --expect-dataset "$TZ_DATASET" --record "$TZ_VERDICT"

  cp /data/vendor/timezones.sqlite "$TILEDIR/timezones.sqlite"
else
  echo '::warning::no timezone database; this region cannot be promoted'
  printf '{"dataset": null, "runtime": null, "compatibilityGate": "ABSENT", "zoneCount": 0}\n' \
    > "$TZ_VERDICT"
fi

stage admins
echo '==> admins'
valhalla_build_admins -c "$CONF" "$MASTER" 2>&1 | tee "$WORK/admins.log"

# ── DID THE ADMIN BUILD DROP ANYTHING WE ARE BUILDING? ──────────────────────────────────────
# It drops records routinely — 30 on the four-country build — and upstream says to ignore that on
# an extract. That is probably right, and "probably right, according to someone else, about a
# message we have never read" is the exact shape in which the timezone abort survived eight runs.
# So it is counted rather than silenced: every dropped record naming a country ABSENT from this
# extract is expected; one naming a country we are building is refused.
#
# Admin records carry driving side, access defaults and country-crossing costs. A graph that lost
# them still returns routes, which is why no routing probe would ever notice.
ADMIN_VERDICT="$WORK/admins.verdict.json"
MEMBER_CODES=()
for member in "${MEMBERS[@]}"; do MEMBER_CODES+=("${member%%:*}"); done
python3 "$HERE/check-admins.py" "$WORK/admins.log" --record "$ADMIN_VERDICT"   --expect "${MEMBER_CODES[@]}"

stage tiles
echo '==> tiles: ONE build over ONE file'
# The output is KEPT, because the builder already counts the quantities that predict what a build
# costs and we have been discarding them. PBF gigabytes are a weak proxy — Austria showed road
# density costing more than bytes do — and these are the real ones:
#
#   Finished with 4938978 routable ways containing 52541965 nodes
#   Finished with 45701150 nodes contained in routable ways
#   Finished with 9418454 graph edges
#   Directed Edge Count = 18836908
#   Building 1007 tiles with 4 threads
#
# `pipefail` is set, so teeing cannot hide a failing build.
valhalla_build_tiles -c "$CONF" "$MASTER" 2>&1 | tee "$WORK/tiles.log"

# The master extract is gigabytes of intermediate on a runner with a finite disk, and nothing
# downstream needs it.
rm -f "$MASTER"

stage cut
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
stage archives
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
  python3 - "$CUTS/$code" "$code" "$BUILD_ID" "$REGION" "$ENGINE" "$archive" \
    "$TZ_VERDICT" "$OSM_VERDICT" "$ADMIN_VERDICT" <<'META' > "$OUT/$code-tiles.json"
import hashlib
import json
import os
import pathlib
import sys

cut, code, build_id, region, engine, archive, tz_path, osm_path, admin_path = sys.argv[1:10]
timezones = json.load(open(tz_path))
try:
    osm = json.load(open(osm_path))
    # Per country, because each extract has its own replication state: a mirror can be stale for
    # one country and current for another.
    osm = {"snapshot": osm.get("snapshots", {}).get(code),
           "freshnessGate": osm.get("freshnessGate"),
           "maxAgeDays": osm.get("maxAgeDays")}
except Exception:                                  # noqa: BLE001
    osm = {"snapshot": None, "freshnessGate": "UNCHECKED"}

try:
    verdict = json.load(open(admin_path))
    admins = {"adminsGate": verdict.get("adminsGate"),
              "droppedCount": verdict.get("droppedCount")}
except Exception:                                  # noqa: BLE001
    admins = {"adminsGate": "UNCHECKED", "droppedCount": None}
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
    # WHICH RULES THIS GRAPH WAS BUILT UNDER. The zone is written into every node at build time
    # and can never be corrected afterwards, so in six months the only way to know whether a graph
    # predates a zone merge is to have written it down here — and BOTH halves are needed, because
    # a deprecated identifier is a disagreement between the polygon dataset and the runtime's
    # tzdata rather than a property of either alone.
    #
    #   dataset            the timezone-boundary-builder release the polygons came from
    #   runtime            the tzdata of the image whose library accepted those identifiers
    #   compatibilityGate  what check-timezones.py concluded before the build was allowed to start
    #   zoneCount          NOT a check — a forensic fingerprint. A release that suddenly carries
    #                      444 zones where every previous one carried 304 shows up here at a
    #                      glance, before anyone opens a CI log.
    "timezones": timezones,
    # WHICH OSM STATE THIS GRAPH IS, which is not the same fact as when it was built. A build
    # that ran this morning can be routing on data from three months ago, and the download time
    # cannot tell you which.
    "osm": osm,
    # How many admin records the build dropped, and the verdict on whether any of them mattered.
    # Kept because "30 dropped, none of them ours" is a property worth being able to check has
    # not changed, rather than a warning nobody reads.
    "admins": admins,
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

stage end           # closes "archives"; "end" is a sentinel and is never reported

# ── the measurement, written beside the artifacts ────────────────────────────────────────────
kill $SAMPLER 2>/dev/null || true
python3 "$HERE/summarise-build.py" "$STAGES" "$SAMPLES" "$TILEDIR" "$CUTS" --log "$WORK/tiles.log" \
  --region "$REGION" --build-id "$BUILD_ID" --engine "$ENGINE" \
  --sources "$SOURCE_BYTES" --members "${#MEMBERS[@]}" \
  --elapsed "$(($(date +%s) - BUILD_STARTED))" > "$METRICS"
cat "$METRICS"

echo "==> done: $BUILD_ID"
