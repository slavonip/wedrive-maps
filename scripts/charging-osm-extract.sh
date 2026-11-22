#!/bin/bash
# OSM half of the charging catalogue (wedrive-charging/1): every charging station in the
# regional.json countries, as GeoJSON sequence, with its provenance.
#
#   scripts/charging-osm-extract.sh OUT                 only the charging stations, one Overpass query
#                                                       per country (charging-osm-overpass.py; a country
#                                                       Overpass will not serve falls back to its dated,
#                                                       md5-checked Geofabrik extract; both fail: exit 1)
#   scripts/charging-osm-extract.sh OUT FILE.osm.pbf…   use local extracts (tests, local measurements)
#
# OUT/osm.geojsonseq   one feature per line, id "n123"/"w45"/"r6" (osmium --add-unique-id=type_id),
#                      ways and multipolygons as their geometry: charging-build.py takes the centroid
# OUT/osm-source.json  [{name, url, md5, sha256, bytes, replication, sequence, stations}] — the
#                      provenance written into the manifest (as regional-build.sh does per country)
#
# What is kept: amenity=charging_station, and the lifecycle forms of it — disused:, construction:,
# proposed:, planned: (they become NOT_OPERATIONAL / PLANNED) and abandoned:, demolished:,
# removed:, razed: (dropped by the build, but counted, so a gone station is seen to be gone).
# Which country a station belongs to is decided by charging-build.py, not here.
#
# Country bboxes overlap, so one object can arrive in several country files. They come with
# `out meta` (versions and timestamps), and after `osmium merge` — which keeps EVERY version it
# sees — `osmium time-filter` keeps the newest version of each object.
set -euo pipefail
OUT="${1:?usage: charging-osm-extract.sh OUT [FILE.osm.pbf ...]}"; shift
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
WORK="${WORK:-$OUT/work}"
mkdir -p "$WORK"

FILTER=(nwr/amenity=charging_station
        nwr/disused:amenity=charging_station nwr/construction:amenity=charging_station
        nwr/proposed:amenity=charging_station nwr/planned:amenity=charging_station
        nwr/abandoned:amenity=charging_station nwr/demolished:amenity=charging_station
        nwr/removed:amenity=charging_station nwr/razed:amenity=charging_station)

PROV="$WORK/provenance.jsonl"
: > "$PROV"
FILTERED=()

# filter_one NAME PBF URL MD5 — filter one extract and record where it came from
filter_one() {
  local name="$1" pbf="$2" url="$3" md5="$4"
  local rep seq sha bytes out="$WORK/$name-charging.osm.pbf"
  rep="$(osmium fileinfo -g header.option.osmosis_replication_timestamp "$pbf")"
  seq="$(osmium fileinfo -g header.option.osmosis_replication_sequence_number "$pbf" || true)"
  [ -n "$rep" ] || { echo "FAIL: $pbf has no replication timestamp — its data date cannot be named" >&2; exit 1; }
  sha="$(sha256sum "$pbf" | cut -d' ' -f1)"
  bytes="$(stat -c %s "$pbf")"
  osmium tags-filter --overwrite -o "$out" "$pbf" "${FILTER[@]}"
  # the charging objects THEMSELVES (-R: without the nodes their ways reference) — the count the
  # build must reproduce, the same number a plain `osmium tags-filter` gives on this extract
  local tagged="$WORK/$name-tagged.osm.pbf" n
  osmium tags-filter --overwrite -R -o "$tagged" "$out" "${FILTER[@]}"
  n=$(( $(osmium fileinfo -e -g data.count.nodes "$tagged") + $(osmium fileinfo -e -g data.count.ways "$tagged") + $(osmium fileinfo -e -g data.count.relations "$tagged") ))
  rm -f "$tagged"
  python3 - "$PROV" "$name" "$url" "$md5" "$sha" "$bytes" "$rep" "$seq" "$n" <<'PY'
import json, sys
p, name, url, md5, sha, size, rep, seq, n = sys.argv[1:]
json.dump({"name": name, "url": url, "md5": md5, "sha256": sha, "bytes": int(size),
           "replication": rep, "sequence": int(seq) if seq.strip().isdigit() else None,
           "stations": int(n)}, open(p, "a", encoding="utf-8"), sort_keys=True)
open(p, "a", encoding="utf-8").write("\n")
PY
  echo "==> $name: $n charging objects, data $rep"
  FILTERED+=("$out")
}

if [ $# -gt 0 ]; then
  for pbf in "$@"; do
    name="$(basename "$pbf")"; name="${name%%.*}"
    filter_one "$name" "$pbf" "local:$(basename "$pbf")" "$(md5sum "$pbf" | cut -d' ' -f1)"
  done
else
  # one country at a time, sequential; it writes <CC>.osm (Overpass) or <CC>.osm.pbf (Geofabrik)
  # and appends each country's provenance row to $PROV itself
  OV="$WORK/overpass"
  rm -rf "$OV"
  python3 "$HERE/charging-osm-overpass.py" "$HERE/../regional.json" "$OV" "$PROV"
  for f in "$OV"/*.osm "$OV"/*.osm.pbf; do
    [ -e "$f" ] || continue
    name="$(basename "$f")"; name="${name%%.*}"
    out="$WORK/$name-charging.osm.pbf"
    osmium sort --overwrite -o "$out" "$f"
    FILTERED+=("$out")
  done
  [ "${#FILTERED[@]}" -gt 0 ] || { echo "FAIL: charging-osm-overpass.py wrote no country file" >&2; exit 1; }
fi

if [ "${#FILTERED[@]}" -gt 1 ]; then
  osmium merge -H --overwrite -o "$WORK/merged.osm.pbf" "${FILTERED[@]}"
  osmium time-filter --overwrite -o "$WORK/charging.osm.pbf" "$WORK/merged.osm.pbf"
  rm -f "$WORK/merged.osm.pbf"
else
  cp -f "${FILTERED[0]}" "$WORK/charging.osm.pbf"
fi
osmium export --overwrite -f geojsonseq -x print_record_separator=false \
  --add-unique-id=type_id --geometry-types=point,linestring,polygon \
  -o "$OUT/osm.geojsonseq" "$WORK/charging.osm.pbf"
python3 -c 'import json,sys; rows=[json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]; json.dump(sorted(rows, key=lambda r: r["name"]), open(sys.argv[2], "w", encoding="utf-8"), indent=2, sort_keys=True)' \
  "$PROV" "$OUT/osm-source.json"
echo "==> $(wc -l < "$OUT/osm.geojsonseq") features -> $OUT/osm.geojsonseq"
cat "$OUT/osm-source.json"
