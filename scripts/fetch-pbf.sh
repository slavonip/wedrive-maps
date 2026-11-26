#!/usr/bin/env bash
# fetch-pbf.sh <geofabrik-path> <out.osm.pbf>
# The country PBF, with one md5 discipline for everybody who needs it: the `source` step of the
# graph job, the `source` step of the basemap job (address index), and regional-build.sh when it
# has no extract yet. Used only once its md5 matches the one Geofabrik publishes beside it; a
# mismatch while Geofabrik rolls over waits and retries.
# The md5 lands in <out>.md5 and is recorded in the search asset (addr_meta.source_md5).
#
# THE DOWNLOAD IS RESUMABLE, AND NOTHING THAT ARRIVED IS THROWN AWAY. Measured 2026-11-04 (run
# 41818389322): Geofabrik gave FR 0.53 MB/s, NL 0.75, BE 0.85 while RU got 17.8 and IT 10.9 in the
# same hour — and FR's 5.12 GB did arrive at 0.53 MB/s. The speed belongs to the CONNECTION, not to
# the file. So:
#   - every connection continues from the byte the file has reached (curl -C -), also after a
#     stall, a reset or a pause: the server is never asked again for bytes it already sent;
#   - a connection slower than FETCH_MIN_BPS for FETCH_MIN_SECS is dropped and reopened. The floor
#     is LOW on purpose (256 kB/s): it removes a dead or crawling connection, it does not refuse a
#     slow day on which the file would still arrive;
#   - the partial file is deleted only when the transfer is complete and its md5 is wrong (or the
#     server refuses to continue a file at all — curl 33/36 — and there is nothing to continue);
#   - there is ONE deadline for the whole script, FETCH_DEADLINE, whatever number of connections
#     and md5 attempts it took (the job's own timeout is 330 min).
# The two ways to fail are named apart: "TRANSFER FAILED" (deadline, or a server that sends
# nothing — the bytes so far are kept) and "MD5 MISMATCH" (a complete file that is not the one).
#
#   FETCH_MIN_BPS      speed floor, bytes/s (default 256000)
#   FETCH_MIN_SECS     how long below the floor before the connection is dropped (default 60)
#   FETCH_DEADLINE     seconds for the whole script (default 14400 = 240 min)
#   FETCH_PAUSE        seconds between connections (default 5)
#   FETCH_STALLS       rounds of three connections that brought nothing, in a row, before giving
#                      up (default 5), FETCH_STALL_WAIT seconds apart (default 60)
set -euo pipefail
GEO="$1"; OUT="$2"
URL="https://download.geofabrik.de/${GEO}-latest.osm.pbf"
MD5_ATTEMPTS="${MD5_ATTEMPTS:-7}"; MD5_WAIT="${MD5_WAIT:-600}"
FETCH_MIN_BPS="${FETCH_MIN_BPS:-256000}"; FETCH_MIN_SECS="${FETCH_MIN_SECS:-60}"
FETCH_DEADLINE="${FETCH_DEADLINE:-14400}"; FETCH_PAUSE="${FETCH_PAUSE:-5}"
FETCH_STALLS="${FETCH_STALLS:-5}"; FETCH_STALL_WAIT="${FETCH_STALL_WAIT:-60}"
DEADLINE_AT=$(( $(date +%s) + FETCH_DEADLINE ))

size_of() { stat -c %s "$1" 2>/dev/null || echo 0; }

# fetch <url> <file>: continue <file> until the server has no more bytes for it.
#   0 = the transfer is complete    1 = three connections in a row brought nothing    2 = deadline
fetch() {
  local url="$1" out="$2" rc code have before left n=0 stuck=0
  while :; do
    left=$(( DEADLINE_AT - $(date +%s) ))
    if [ "$left" -lt 1 ]; then return 2; fi
    n=$((n + 1)); rc=0; before=$(size_of "$out")
    # --max-time: the deadline also ends a connection that is merely steady, yet too slow
    code=$(curl -fsSL -C - --connect-timeout 30 --max-time "$left" \
                --speed-limit "$FETCH_MIN_BPS" --speed-time "$FETCH_MIN_SECS" \
                -w '%{http_code}' -o "$out" "$url") || rc=$?
    [ "$rc" = 0 ] && return 0
    have=$(size_of "$out")
    # 416 to a continued request: the server has nothing beyond what is on disk — it is all here
    # (the connection before this one ended at the last byte). The md5 says whether it is right.
    if [ "$code" = 416 ] && [ "$have" -gt 0 ]; then return 0; fi
    # 33/36: the server refused to continue, so what is on disk cannot be continued — start over.
    if [ "$rc" = 33 ] || [ "$rc" = 36 ]; then rm -f "$out"; have=0; fi
    if [ "$(date +%s)" -ge "$DEADLINE_AT" ]; then return 2; fi
    if [ "$have" -le "$before" ]; then stuck=$((stuck + 1)); else stuck=0; fi
    if [ "$stuck" -ge 3 ]; then return 1; fi
    if [ "$rc" = 28 ]; then
      echo "fetch: connection $n slower than $FETCH_MIN_BPS B/s for ${FETCH_MIN_SECS} s (or timed out) at $have bytes — reconnecting from there" >&2
    else
      echo "fetch: connection $n ended with curl exit $rc at $have bytes — continuing from there" >&2
    fi
    sleep "$FETCH_PAUSE"
  done
}

# obtain <url> <md5>: <url> into $OUT.part, complete and matching <md5>.
#   0 = it is there and it is the right file
#   2 = MD5 MISMATCH: a complete file that is not <md5> (deleted — the only case that deletes)
#   exits 1 = TRANSFER FAILED: the deadline, or a server that sends nothing (the bytes are kept)
obtain() {
  local url="$1" want="$2" rc stalls=0 before have got
  while :; do
    before=$(size_of "$OUT.part"); rc=0
    fetch "$url" "$OUT.part" || rc=$?
    have=$(size_of "$OUT.part")
    if [ "$rc" = 0 ]; then
      got=$(md5sum "$OUT.part" | cut -d' ' -f1)
      [ "$got" = "$want" ] && return 0
      echo "MD5 MISMATCH: $url arrived whole ($have bytes) with md5 $got, expected $want — deleted" >&2
      rm -f "$OUT.part"
      return 2
    fi
    if [ "$rc" = 2 ]; then
      echo "TRANSFER FAILED: $url is not complete after ${FETCH_DEADLINE} s, the deadline of this script ($have bytes arrived and are kept)" >&2
      exit 1
    fi
    if [ "$have" -gt "$before" ]; then stalls=1; else stalls=$((stalls + 1)); fi
    if [ "$stalls" -ge "$FETCH_STALLS" ]; then
      echo "TRANSFER FAILED: $url sent nothing in $stalls rounds of three connections ($have bytes arrived and are kept)" >&2
      exit 1
    fi
    echo "fetch: three connections to $url brought no bytes (round $stalls of $FETCH_STALLS, $have bytes so far) — waiting ${FETCH_STALL_WAIT} s, then continuing from there" >&2
    sleep "$FETCH_STALL_WAIT"
  done
}

# PINNED BY THE PRECHECK (SRC_URL + SRC_MD5): the same dated file the graph of this country is
# built from. A dated file never changes, so a mismatch is a broken download, retried, not waited.
if [ -n "${SRC_URL:-}" ] && [ -n "${SRC_MD5:-}" ]; then
  for attempt in 1 2 3; do
    echo "==> $SRC_URL (md5 $SRC_MD5)"
    if obtain "$SRC_URL" "$SRC_MD5"; then
      mv "$OUT.part" "$OUT"; echo "$SRC_MD5" > "$OUT.md5"; exit 0
    fi
    echo "$SRC_URL did not match its md5 (attempt $attempt of 3)" >&2
    [ "$attempt" = 3 ] || sleep "${MD5_RETRY_WAIT:-30}"
  done
  echo "MD5 MISMATCH: $SRC_URL never matched $SRC_MD5 in 3 complete downloads" >&2; exit 1
fi
for attempt in $(seq 1 "$MD5_ATTEMPTS"); do
  MD5_WANT="$(curl -fsSL --retry 3 --connect-timeout 30 --max-time 120 "$URL.md5" | awk '{print $1}')"
  [ "${#MD5_WANT}" = 32 ] || { echo "no MD5 published at $URL.md5" >&2; exit 1; }
  echo "==> $GEO (md5 $MD5_WANT)"
  # -latest can roll over between two connections; a continued file is then two extracts spliced
  # together. It arrives "whole", fails the md5 below, is deleted there and fetched again.
  if obtain "$URL" "$MD5_WANT"; then
    mv "$OUT.part" "$OUT"; echo "$MD5_WANT" > "$OUT.md5"; exit 0
  fi
  echo "md5 mismatch (attempt $attempt of $MD5_ATTEMPTS)" >&2
  [ "$attempt" = "$MD5_ATTEMPTS" ] || sleep "$MD5_WAIT"
done
echo "MD5 MISMATCH: Geofabrik never gave a consistent $GEO" >&2; exit 1
