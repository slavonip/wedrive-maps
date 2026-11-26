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
#
# WHERE THE BYTES COME FROM (2026-11-26). The IDENTITY of the source is always the dated Geofabrik
# file and its md5; only the transport varies. Measured 2026-11-03 (run 41694683517): one
# connection to Geofabrik averaged 1.5 MB/s, reached 91 % of 32.9 GiB in 5 h 50 min and the job
# was cancelled by its timeout; in October the same step ran at 18.7 MB/s. Measured 2026-11-26 from
# a desk: one connection 5.8–6.4 MB/s, four parallel ranges 14.2 MB/s, the GWDG mirror 14.7 MB/s
# with the same size and md5. So:
#   1. the GWDG mirror, but ONLY while the md5 it publishes beside its europe-latest equals the md5
#      of the pinned file — otherwise it holds another day's extract and is not asked at all;
#   2. Geofabrik itself, the bytes still missing cut into FETCH_RANGES ranges fetched in parallel.
# Every connection continues from the byte it reached, and one slower than its floor is dropped
# and reopened (the speed belongs to the connection, not to the file). The md5 of the whole file
# decides in the end, whatever the route was.
#
#   LITE_MIRROR        mirror URL of europe-latest.osm.pbf; empty = never use a mirror
#   MIRROR_MIN_BPS     floor of the mirror connection, bytes/s (default 3000000)
#   MIRROR_DEADLINE    seconds given to the mirror before Geofabrik takes over (default 3600)
#   FETCH_RANGES       parallel ranges at Geofabrik (default 4; 1 = one connection)
#   FETCH_MIN_BPS      floor of each Geofabrik connection, bytes/s (default 1000000)
#   FETCH_MIN_SECS     how long below a floor before the connection is dropped (default 60)
#   FETCH_DEADLINE     seconds given to Geofabrik (default 6300)
#   FETCH_PAUSE        seconds between connections (default 5)
#
# THE WORST CASE FITS THE STEP. The workflow gives this step 200 min. Nothing here starts over: the
# mirror gets at most 60 min, Geofabrik then continues from the mirror's last byte for at most
# 105 min, and the md5 / sha256 of the 33 GB file take a few minutes more — about 175 min, after
# which the script itself says FAIL (what arrived is kept) instead of being cut off by the step.
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

LITE_MIRROR="${LITE_MIRROR-https://ftp.gwdg.de/pub/misc/openstreetmap/download.geofabrik.de/europe-latest.osm.pbf}"
MIRROR_MIN_BPS="${MIRROR_MIN_BPS:-3000000}"; MIRROR_DEADLINE="${MIRROR_DEADLINE:-3600}"
FETCH_RANGES="${FETCH_RANGES:-4}"; FETCH_MIN_BPS="${FETCH_MIN_BPS:-1000000}"
FETCH_MIN_SECS="${FETCH_MIN_SECS:-60}"; FETCH_DEADLINE="${FETCH_DEADLINE:-6300}"
FETCH_PAUSE="${FETCH_PAUSE:-5}"
PART=europe.osm.pbf.part

MD5LINE="$(curl -fsSL --retry 3 --connect-timeout 30 --max-time 120 "$URL.md5")" || { echo "FAIL: no MD5 published at $URL.md5"; exit 1; }
MD5="$(awk '{print $1}' <<<"$MD5LINE")"; MD5NAME="$(awk '{print $2}' <<<"$MD5LINE")"
[[ "$MD5" =~ ^[0-9a-f]{32}$ ]] || { echo "FAIL: $URL.md5 holds no md5: $MD5LINE"; exit 1; }
[ "${MD5NAME#\*}" = "$NAME" ] || { echo "FAIL: $URL.md5 describes '$MD5NAME', not $NAME"; exit 1; }
[ -z "$WANT" ] || [ "$MD5" = "$WANT" ] || { echo "FAIL: $URL.md5 is $MD5, the precheck decided on $WANT"; exit 1; }
echo "expected md5 $MD5"

size_of() { stat -c %s "$1" 2>/dev/null || echo 0; }
say() { echo "[$(date -u +%H:%M:%S)] $*"; }

# stream <label> <url> <file> <floor-bytes/s> <deadline-epoch> [<first-byte> <last-byte>]
# Without a range: continue <file> to the end of <url> (curl -C -). With one: <file> holds the
# bytes first..last of <url>, and what is already in it is not asked for again. 0 = complete.
stream() {
  local label="$1" url="$2" file="$3" floor="$4" deadline="$5" first="${6:-}" last="${7:-}"
  local rc have n=0 stuck=0 before left
  while :; do
    n=$((n + 1)); rc=0; before=$(size_of "$file")
    # the deadline also ends a connection that is merely steady (above its floor, yet too slow)
    left=$(( deadline - $(date +%s) )); [ "$left" -lt 1 ] && left=1
    if [ -z "$first" ]; then
      curl -fsSL -C - --connect-timeout 30 --max-time "$left" --speed-limit "$floor" --speed-time "$FETCH_MIN_SECS" \
           -o "$file" "$url" || rc=$?
    else
      if [ $(( first + before )) -gt "$last" ]; then return 0; fi
      curl -fsSL --connect-timeout 30 --max-time "$left" --speed-limit "$floor" --speed-time "$FETCH_MIN_SECS" \
           -r "$(( first + before ))-$last" "$url" >> "$file" || rc=$?
    fi
    have=$(size_of "$file")
    if [ "$rc" = 0 ]; then
      [ -z "$first" ] && return 0
      [ $(( first + have )) -gt "$last" ] && return 0
      rc=18   # the server closed a range early without an error: go on from where it stopped
    fi
    # 33/36: the server will not continue this file. A whole file starts over; a range cannot.
    if [ "$rc" = 33 ] || [ "$rc" = 36 ]; then
      [ -z "$first" ] || { say "$label: the server refused a range (curl $rc)"; return 1; }
      rm -f "$file"; have=0
    fi
    # Three connections in a row that brought nothing: this route is not going to deliver.
    if [ "$(date +%s)" -ge "$deadline" ]; then
      say "$label: not complete at its deadline ($have bytes, $n connections)"; return 1
    fi
    if [ "$have" -le "$before" ]; then stuck=$((stuck + 1)); else stuck=0; fi
    if [ "$stuck" -ge 3 ]; then say "$label: three connections brought no bytes (last curl exit $rc) — giving up on this route"; return 1; fi
    if [ "$rc" = 28 ]; then
      say "$label: connection $n below $floor B/s for ${FETCH_MIN_SECS} s (or timed out) at $have bytes — reconnecting"
    else
      say "$label: connection $n ended with curl exit $rc at $have bytes — continuing from there"
    fi
    sleep "$FETCH_PAUSE"
  done
}

VIA=""
# --- 1: the mirror, only while it holds this very extract ---------------------------------------------
case "$URL" in
  https://download.geofabrik.de/*)
    if [ -n "$LITE_MIRROR" ]; then
      MMD5="$(curl -fsSL --retry 2 --connect-timeout 20 --max-time 60 "$LITE_MIRROR.md5" 2>/dev/null | awk '{print $1}')" || MMD5=""
      if [ "$MMD5" = "$MD5" ]; then
        say "mirror holds this extract (md5 $MMD5): $LITE_MIRROR"
        if stream mirror "$LITE_MIRROR" "$PART" "$MIRROR_MIN_BPS" "$(( $(date +%s) + MIRROR_DEADLINE ))"; then
          if [ "$(md5sum "$PART" | cut -d' ' -f1)" = "$MD5" ]; then
            VIA="$LITE_MIRROR"
          else
            say "the mirror's file does not match $MD5 — removed, Geofabrik instead"; rm -f "$PART"
          fi
        else
          say "the mirror did not finish; Geofabrik continues from $(size_of "$PART") bytes"
        fi
      else
        say "mirror not used: its md5 is '${MMD5:-unreadable}', the pinned file is $MD5"
      fi
    fi ;;
esac

# --- 2: Geofabrik, the missing bytes in parallel ranges ---------------------------------------------
if [ -z "$VIA" ]; then
  DEADLINE=$(( $(date +%s) + FETCH_DEADLINE ))
  SIZE="$(curl -sIL --connect-timeout 30 --max-time 60 "$URL" | awk 'tolower($1)=="content-length:"{v=$2} END{gsub("\r","",v); print v}')" || SIZE=""
  RANGED=no
  if [[ "$SIZE" =~ ^[0-9]+$ ]] && [ "$FETCH_RANGES" -gt 1 ]; then
    [ "$(curl -s -o /dev/null -L -r 0-0 --connect-timeout 30 --max-time 60 -w '%{http_code}' "$URL" || true)" = 206 ] && RANGED=yes
  fi
  HAVE=$(size_of "$PART")
  if [ "$RANGED" = yes ] && [ "$HAVE" -lt "$SIZE" ]; then
    REST=$(( SIZE - HAVE ))
    N=$FETCH_RANGES; [ "$REST" -lt $(( N * 16777216 )) ] && N=1
    CHUNK=$(( (REST + N - 1) / N ))
    # The layout of the ranges belongs to one prefix length; pieces of another layout are useless.
    LAYOUT="$HAVE $SIZE $N"
    if [ "$(cat "$PART.ranges" 2>/dev/null || true)" != "$LAYOUT" ]; then
      rm -f "$PART".r[0-9]*; echo "$LAYOUT" > "$PART.ranges"
    fi
    say "Geofabrik: $REST bytes missing of $SIZE, in $N ranges"
    pids=()
    for i in $(seq 0 $(( N - 1 ))); do
      first=$(( HAVE + i * CHUNK )); last=$(( first + CHUNK - 1 )); [ "$last" -ge "$SIZE" ] && last=$(( SIZE - 1 ))
      stream "range $((i + 1))/$N" "$URL" "$PART.r$i" "$FETCH_MIN_BPS" "$DEADLINE" "$first" "$last" &
      pids+=($!)
    done
    ok=1
    for p in "${pids[@]}"; do wait "$p" || ok=0; done
    [ "$ok" = 1 ] || { echo "FAIL: Geofabrik did not deliver every range; what arrived is kept, run again to continue"; exit 1; }
    # Joined in order, each piece removed as soon as it is in: the peak is the file plus one piece.
    for i in $(seq 0 $(( N - 1 ))); do cat "$PART.r$i" >> "$PART"; rm -f "$PART.r$i"; done
    rm -f "$PART.ranges"
  elif [ "$RANGED" != yes ] || [ "$HAVE" -lt "$SIZE" ]; then
    say "Geofabrik: one connection (size '${SIZE:-unknown}', ranges $RANGED)"
    stream geofabrik "$URL" "$PART" "$FETCH_MIN_BPS" "$DEADLINE" \
      || { echo "FAIL: $URL did not arrive; what arrived is kept, run again to continue"; exit 1; }
  fi
  VIA="$URL"
fi

if [ "$(md5sum "$PART" | cut -d' ' -f1)" != "$MD5" ]; then
  # The file changed on the server during a continued download, or the transfer is damaged.
  rm -f "$PART" "$PART".r[0-9]* "$PART.ranges"
  echo "FAIL: md5 does not match $MD5; the partial file was removed, run again"; exit 1
fi
SHA="$(sha256sum "$PART" | cut -d' ' -f1)"
mv -f "$PART" europe.osm.pbf
printf 'url      = %s\nmd5      = %s\nsha256   = %s\nsize     = %s\nfetched  = %s\nvia      = %s\n' \
  "$URL" "$MD5" "$SHA" "$(stat -c %s europe.osm.pbf)" "$(date -u +%FT%TZ)" "$VIA" > europe.osm.pbf.source
cat europe.osm.pbf.source
echo "now: build.sh --new-data $WORK"
