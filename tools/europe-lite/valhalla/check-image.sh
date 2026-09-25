#!/bin/bash
# Prove what is inside a Europe Lite Valhalla image — the one built here, or one pulled by digest.
#
# Usage: check-image.sh IMAGE
#
#   - the source is Valhalla 3.6.3, commit e2f017b;
#   - exactly the files of B2 and C differ from it (ferry_connections.cc + its gurka test, nodeinfo.h);
#   - the patched lines are really there;
#   - the tile builder carries none of the diagnostic hooks used during the investigation.
set -euo pipefail
IMAGE="${1:?usage: check-image.sh IMAGE}"
docker run --rm --entrypoint bash "$IMAGE" -c '
set -e
cd /src/valhalla
head=$(git rev-parse HEAD)
changed=$(git status --short | awk "{print \$2}" | sort | tr "\n" " ")
echo "commit:   $head"
echo "changed:  $changed"
[ "$head" = "e2f017b16080f49203de245a211b09efab09cf72" ] || { echo "WRONG COMMIT"; exit 1; }
[ "$changed" = "src/mjolnir/ferry_connections.cc test/gurka/test_ferry_connections.cc valhalla/baldr/nodeinfo.h " ] \
  || { echo "UNEXPECTED CHANGED FILES"; exit 1; }
grep -q "auto_inbound || auto_outbound" src/mjolnir/ferry_connections.cc || { echo "B2 MISSING"; exit 1; }
grep -q "type() != NodeType::kBorderControl" valhalla/baldr/nodeinfo.h || { echo "C MISSING"; exit 1; }
for s in VH_FX VH_FT VH_AUDIT VH_WATCH VH_FP "FT| " "FX| " "AUD|"; do
  if grep -q "$s" /usr/local/bin/valhalla_build_tiles; then echo "DIAGNOSTIC STRING $s IN BINARY"; exit 1; fi
done
echo "B2 + C present, no diagnostics"
# For the record only: the md5 is NOT a release criterion (apt is not pinned; see README).
md5sum /usr/local/bin/valhalla_build_tiles
'
echo "image $IMAGE OK"
