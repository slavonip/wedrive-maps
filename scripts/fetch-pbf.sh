#!/usr/bin/env bash
# fetch-pbf.sh <geofabrik-path> <out.osm.pbf>
# The country PBF for the ADDRESS step of the basemap job, with the same md5 discipline as
# regional-build.sh (which fetches it for the graph): used only once its md5 matches the one
# Geofabrik publishes beside it; a mismatch while Geofabrik rolls over waits and retries.
# The md5 lands in <out>.md5 and is recorded in the search asset (addr_meta.source_md5).
set -euo pipefail
GEO="$1"; OUT="$2"
URL="https://download.geofabrik.de/${GEO}-latest.osm.pbf"
MD5_ATTEMPTS="${MD5_ATTEMPTS:-7}"; MD5_WAIT="${MD5_WAIT:-600}"
for attempt in $(seq 1 "$MD5_ATTEMPTS"); do
  MD5_WANT="$(curl -fsSL "$URL.md5" | awk '{print $1}')"
  [ "${#MD5_WANT}" = 32 ] || { echo "no MD5 published at $URL.md5" >&2; exit 1; }
  echo "==> $GEO (md5 $MD5_WANT)"
  curl -fsSL -o "$OUT.part" "$URL"
  if [ "$(md5sum "$OUT.part" | cut -d' ' -f1)" = "$MD5_WANT" ]; then
    mv "$OUT.part" "$OUT"; echo "$MD5_WANT" > "$OUT.md5"; exit 0
  fi
  rm -f "$OUT.part"; echo "md5 mismatch (attempt $attempt of $MD5_ATTEMPTS)" >&2
  [ "$attempt" = "$MD5_ATTEMPTS" ] || sleep "$MD5_WAIT"
done
echo "Geofabrik never gave a consistent $GEO" >&2; exit 1
