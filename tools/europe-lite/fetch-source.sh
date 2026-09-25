#!/bin/bash
# Download a NEW Europe extract for `build.sh --new-data`. Never needed to reproduce final2: that
# needs the exact file in inputs.lock, which Geofabrik no longer serves (europe-latest moves daily).
#
#   tools/europe-lite/fetch-source.sh WORK [URL]      (default: Geofabrik europe-latest)
#
# The file is written to WORK/europe.osm.pbf.part (an interrupted download is continued), checked
# against the MD5 Geofabrik publishes beside it, and only then renamed to WORK/europe.osm.pbf.
# WORK/europe.osm.pbf.source records the URL, the MD5, the sha256 and the download time; put its
# values into the build notes, since this extract is what the new artifact was built from.
set -euo pipefail
WORK="$(cd "${1:?usage: fetch-source.sh WORK [URL]}" && pwd)"
URL="${2:-https://download.geofabrik.de/europe-latest.osm.pbf}"
cd "$WORK"
[ -e europe.osm.pbf ] && { echo "WORK/europe.osm.pbf exists; move it away first"; exit 1; }

MD5="$(curl -fsSL "$URL.md5" | awk '{print $1}')"
[ "${#MD5}" = 32 ] || { echo "no MD5 published at $URL.md5"; exit 1; }
echo "expected md5 $MD5"
curl -fL -C - -o europe.osm.pbf.part "$URL"
if [ "$(md5sum europe.osm.pbf.part | cut -d' ' -f1)" != "$MD5" ]; then
  # The file changed on the server during a continued download, or the transfer is damaged.
  rm -f europe.osm.pbf.part
  echo "FAIL: md5 does not match $MD5; the partial file was removed, run again"; exit 1
fi
SHA="$(sha256sum europe.osm.pbf.part | cut -d' ' -f1)"
mv -f europe.osm.pbf.part europe.osm.pbf
printf 'url      = %s\nmd5      = %s\nsha256   = %s\nsize     = %s\nfetched  = %s\n' \
  "$URL" "$MD5" "$SHA" "$(stat -c %s europe.osm.pbf)" "$(date -u +%FT%TZ)" > europe.osm.pbf.source
cat europe.osm.pbf.source
echo "now: build.sh --new-data $WORK"
