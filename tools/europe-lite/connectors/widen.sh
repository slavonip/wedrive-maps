#!/bin/bash
# A wider window for the ferry ends the first connector pass did not connect.
#
# "Not found within 0.025°" is not "unreachable": Holyhead gave a 2.4 km chain in a 2.8 km window,
# so the margin was minimal. For the ends listed in unreached.txt the window is 0.07° (~7.8 km),
# cut in batches of 800 for the same osmium reason as extract_batched.sh, and whatever it finds is
# APPENDED to cand.opl for the second connector pass.
#
# Usage: widen.sh WORK
#   reads  WORK/unreached.txt WORK/small_roads.osm.pbf WORK/europe_lite_base.osm.pbf
#   writes WORK/cand_wide.osm.pbf, appends to WORK/cand.opl
set -e
WORK="${1:?usage: widen.sh WORK}"
cd "$WORK"

python3 - <<'PY'
import json, math
pts = []
for line in open("unreached.txt"):
    a = line.split()
    if len(a) == 3 and (float(a[1]) or float(a[2])):
        pts.append((float(a[1]), float(a[2])))
BOX = 0.070
grid = {}
for lat, lon in pts:
    grid.setdefault((round(lat/BOX), round(lon/BOX)), (lat, lon))
polys = []
for lat, lon in grid.values():
    dlon = BOX / max(0.2, abs(math.cos(math.radians(lat))))
    polys.append([[[lon-dlon, lat-BOX], [lon+dlon, lat-BOX],
                   [lon+dlon, lat+BOX], [lon-dlon, lat+BOX],
                   [lon-dlon, lat-BOX]]])
print("unreached points %d -> wide boxes %d" % (len(pts), len(polys)))
for i in range(0, len(polys), 800):
    json.dump({"type": "Feature", "properties": {},
               "geometry": {"type": "MultiPolygon", "coordinates": polys[i:i+800]}},
              open("wide_%02d.geojson" % (i//800), "w"))
print("batches %d" % ((len(polys)+799)//800))
PY

rm -f wpart_*.osm.pbf
for f in wide_*.geojson; do
  i="${f#wide_}"; i="${i%.geojson}"
  echo "  wide batch $i"
  osmium extract -p "$f" -s simple -o "wpart_s_$i.osm.pbf" small_roads.osm.pbf --overwrite 2>/dev/null
  osmium extract -p "$f" -s simple -o "wpart_l_$i.osm.pbf" europe_lite_base.osm.pbf --overwrite 2>/dev/null
done

osmium merge wpart_*.osm.pbf -o cand_wide.osm.pbf --overwrite 2>/dev/null
rm -f wpart_*.osm.pbf wide_*.geojson
osmium cat -f opl -o cand_wide.opl --overwrite cand_wide.osm.pbf
cat cand_wide.opl >> cand.opl
rm -f cand_wide.opl
ls -l --block-size=1M cand_wide.osm.pbf cand.opl | awk '{print $5" MB  "$9}'
