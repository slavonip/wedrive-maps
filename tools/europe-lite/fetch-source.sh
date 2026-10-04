#!/bin/bash
# Download a NEW Europe extract for `build.sh --new-data`. Never needed to reproduce final2: that
# needs the exact file in inputs.lock, which Geofabrik no longer serves (europe-latest moves daily).
#
#   tools/europe-lite/fetch-source.sh WORK [URL MD5]
#
# Without URL the source is resolved by scripts/regional-precheck.py `source europe` — the same
# rule the regional factory uses: the DATED file europe-latest stands for, with its own .md5, and
# when -latest is sent to an external mirror (2026-10-03: ftp5.gwdg.de, whose -latest.md5 is 404)
# the date comes from Geofabrik's europe-updates/state.txt. An undated URL is refused. The
# workflow passes URL and MD5 from its precheck so the build reads exactly what was decided on.
#
# The file is written to WORK/europe.osm.pbf.part (an interrupted download is continued), checked
# against the MD5 Geofabrik publishes beside it — which must name this very file and, when MD5 is
# given, equal it — and only then renamed to WORK/europe.osm.pbf.
# WORK/europe.osm.pbf.source records the URL, the MD5, the sha256 and the download time; put its
# values into the build notes, since this extract is what the new artifact was built from.
set -euo pipefail
WORK="$(cd "${1:?usage: fetch-source.sh WORK [URL MD5]}" && pwd)"
URL="${2:-}"; WANT="${3:-}"
if [ -z "$URL" ]; then
  SRC="$(python3 "$(dirname "$0")/../../scripts/regional-precheck.py" source europe)"
  URL="$(python3 -c 'import json,sys;print(json.loads(sys.argv[1])["url"])' "$SRC")"
  WANT="$(python3 -c 'import json,sys;print(json.loads(sys.argv[1])["md5"])' "$SRC")"
fi
NAME="${URL##*/}"
[[ "$NAME" =~ ^europe-[0-9]{6}\.osm\.pbf$ ]] || { echo "FAIL: $URL is not a dated Geofabrik extract (europe-YYMMDD.osm.pbf)"; exit 1; }
[ -z "$WANT" ] || [[ "$WANT" =~ ^[0-9a-f]{32}$ ]] || { echo "FAIL: expected md5 '$WANT' is not an md5"; exit 1; }
cd "$WORK"
[ -e europe.osm.pbf ] && { echo "WORK/europe.osm.pbf exists; move it away first"; exit 1; }

MD5LINE="$(curl -fsSL "$URL.md5")" || { echo "FAIL: no MD5 published at $URL.md5"; exit 1; }
MD5="$(awk '{print $1}' <<<"$MD5LINE")"; MD5NAME="$(awk '{print $2}' <<<"$MD5LINE")"
[[ "$MD5" =~ ^[0-9a-f]{32}$ ]] || { echo "FAIL: $URL.md5 holds no md5: $MD5LINE"; exit 1; }
[ "${MD5NAME#\*}" = "$NAME" ] || { echo "FAIL: $URL.md5 describes '$MD5NAME', not $NAME"; exit 1; }
[ -z "$WANT" ] || [ "$MD5" = "$WANT" ] || { echo "FAIL: $URL.md5 is $MD5, the precheck decided on $WANT"; exit 1; }
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
