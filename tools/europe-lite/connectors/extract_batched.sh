#!/bin/bash
# Candidate small roads around the car-ferry ends, cut IN BATCHES.
#
# osmium extract -p with a multipolygon of more than ~1000 parts SILENTLY loses whole areas:
# one polygon finds Dover, 1000 do, 2000 do not, 4438 do not. So the boxes are cut into batches
# of 800 and the parts merged. The check at the end looks for two known Dover ways and two
# Messina ways; if they are missing, the extract lost areas again.
#
# Usage: extract_batched.sh WORK
#   reads  WORK/ferry_boxes.geojson WORK/small_roads.osm.pbf WORK/europe_lite_base.osm.pbf
#   writes WORK/cand_small.osm.pbf WORK/cand_lite.osm.pbf WORK/cand.opl
set -e
WORK="${1:?usage: extract_batched.sh WORK}"
cd "$WORK"
rm -f batch_*.geojson part_small_*.osm.pbf part_lite_*.osm.pbf

python3 - <<'PY'
import json
g = json.load(open("ferry_boxes.geojson"))
p = g["geometry"]["coordinates"]
B = 800
n = 0
for i in range(0, len(p), B):
    sub = p[i:i+B]
    json.dump({"type": "Feature", "properties": {},
               "geometry": {"type": "MultiPolygon", "coordinates": sub}},
              open("batch_%02d.geojson" % (i // B), "w"))
    n += 1
print("batches %d, polygons %d" % (n, len(p)))
PY

for f in batch_*.geojson; do
  i="${f#batch_}"; i="${i%.geojson}"
  echo "  batch $i"
  osmium extract -p "$f" -s simple -o "part_small_$i.osm.pbf" small_roads.osm.pbf --overwrite 2>/dev/null
  osmium extract -p "$f" -s simple -o "part_lite_$i.osm.pbf" europe_lite_base.osm.pbf --overwrite 2>/dev/null
done

echo "merge"
osmium merge part_small_*.osm.pbf -o cand_small.osm.pbf --overwrite 2>/dev/null
osmium merge part_lite_*.osm.pbf  -o cand_lite.osm.pbf  --overwrite 2>/dev/null
rm -f part_small_*.osm.pbf part_lite_*.osm.pbf

echo "check on known ways"
osmium getid cand_small.osm.pbf w98812998 w914030942 -o chk_dover.osm.pbf --overwrite 2>/dev/null
d=$(osmium fileinfo -e -g data.count.ways chk_dover.osm.pbf)
osmium getid cand_small.osm.pbf w369367679 w50591010 -o chk_messina.osm.pbf --overwrite 2>/dev/null
m=$(osmium fileinfo -e -g data.count.ways chk_messina.osm.pbf)
rm -f chk_dover.osm.pbf chk_messina.osm.pbf
echo "  Dover ways in candidates: $d (must be 2), Messina: $m (must be 2)"
[ "$d" = 2 ] && [ "$m" = 2 ] || { echo "extract lost areas - stopping"; exit 1; }

echo "opl"
osmium cat -f opl -o cand_a.opl --overwrite cand_small.osm.pbf
osmium cat -f opl -o cand_b.opl --overwrite cand_lite.osm.pbf
cat cand_a.opl cand_b.opl > cand.opl
rm -f cand_a.opl cand_b.opl batch_*.geojson
ls -l --block-size=1M cand_small.osm.pbf cand_lite.osm.pbf cand.opl | awk '{print $5" MB  "$9}'
