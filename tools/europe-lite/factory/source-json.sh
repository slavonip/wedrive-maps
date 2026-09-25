#!/bin/bash
# source.json for the manifest: what fetch-source.sh recorded plus the replication state written
# into the PBF header by Geofabrik. The replication date becomes the release version.
#
#   factory/source-json.sh WORK OUT
set -euo pipefail
WORK="${1:?WORK}"; OUT="${2:?OUT}"
S="$WORK/europe.osm.pbf.source"
[ -s "$S" ] || { echo "$S missing (made by fetch-source.sh)"; exit 1; }
get() { awk -F' *= *' -v k="$1" '$1==k{print $2}' "$S"; }
TS="$(osmium fileinfo -g header.option.osmosis_replication_timestamp "$WORK/europe.osm.pbf")"
SEQ="$(osmium fileinfo -g header.option.osmosis_replication_sequence_number "$WORK/europe.osm.pbf")"
[[ "$TS" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T ]] || { echo "no replication timestamp in the PBF header: '$TS'"; exit 1; }
python3 - "$OUT" "$(get url)" "$(get md5)" "$(get sha256)" "$(get size)" "$(get fetched)" "$TS" "$SEQ" <<'PY'
import json, sys
o, url, md5, sha, size, fetched, ts, seq = sys.argv[1:]
json.dump({"url": url, "md5": md5, "sha256": sha, "bytes": int(size), "fetched": fetched,
           "replication": ts, "sequence": int(seq) if seq.isdigit() else None},
          open(o, "w"), indent=1, sort_keys=True)
PY
cat "$OUT"
