#!/bin/bash
# Europe Lite — the whole build, from a local Europe extract to the packaged graph.
#
#   tools/europe-lite/build.sh [--new-data] [--dry-run] [--until N] WORK [IMAGE]
#
#   WORK        a directory with enough disk (the source extract alone is 35 GB) that contains
#               europe.osm.pbf. The build never downloads the source; see README.
#   IMAGE       the Valhalla image built by valhalla/build-image.sh (default wedrive-valhalla:lite).
#
# Two modes, and which one ran is printed first and last:
#
#   (default)   REPRODUCE final2. europe.osm.pbf must have the size and sha256 in inputs.lock or the
#               build FAILS before doing anything. Every output inputs.lock names is trusted only
#               when its sha256 matches: a mismatching file on disk is rebuilt, and a rebuilt file
#               that still mismatches FAILS the build.
#   --new-data  REBUILD on another extract. Same pipeline, nothing is compared with inputs.lock,
#               and the result is a NEW routing artifact, not a reproduction of final2.
#
#   --dry-run   check the source, the image and every existing output; build nothing.
#   --until N   stop after step N (1..9).
#
# Every output is written under a temporary name and renamed into place only when the step that
# makes it has finished, so a file with its final name is never a half-written one. Downloads are
# additionally checked against their sha256 before the rename. A step is skipped when its output
# is ready: verified against inputs.lock (reproduce mode, locked files) or newer than everything it
# is built from (all other files).
#
# Nothing outside WORK is written. Valhalla runs in Docker with WORK mounted at /w; osmium and
# python3 run on the host.
set -euo pipefail

MODE=reproduce; DRY=0; UNTIL=9
while [ $# -gt 0 ]; do
  case "$1" in
    --new-data) MODE=new-data; shift ;;
    --dry-run)  DRY=1; shift ;;
    --until)    UNTIL="${2:?--until N}"; shift 2 ;;
    -h|--help)  sed -n 2,30p "$0"; exit 0 ;;
    -*)         echo "unknown option $1"; exit 2 ;;
    *)          break ;;
  esac
done
WORK="$(cd "${1:?usage: build.sh [--new-data] [--dry-run] [--until N] WORK [IMAGE]}" && pwd)"
IMAGE="${2:-wedrive-valhalla:lite}"
HERE="$(cd "$(dirname "$0")" && pwd)"
LOCK="$HERE/inputs.lock"
TZ_URL="https://github.com/slavonip/wedrive-maps/releases/download/maps-vendor/timezones.sqlite"
TZ_SHA256="5027ecd793c88036acae9d400daeb9be7b43627fe2430e19cf9b9d08e29907e4"
LITE_CLASSES='motorway,trunk,primary,secondary,tertiary,motorway_link,trunk_link,primary_link,secondary_link,tertiary_link'
SMALL_CLASSES='service,unclassified,residential,living_street,road'

cd "$WORK"
LOG="$WORK/build.log"
say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
die() { say "FAIL: $*"; exit 1; }
mb() { python3 -c "import os,sys;print('%.1f' % (os.path.getsize(sys.argv[1])/1048576))" "$1"; }
VAL() { docker run --rm -v "$WORK":/w --entrypoint "$1" "$IMAGE" "${@:2}"; }
sha() { sha256sum "$1" | cut -d' ' -f1; }

# inputs.lock: "lock NAME" prints "SIZE SHA" of a file listed under [intermediate] or [output],
# nothing if it lists none; "lockv SECTION KEY" prints a "key = value" entry.
lockpy() {
  python3 - "$LOCK" "$@" <<'PY'
import sys
path, args = sys.argv[1], sys.argv[2:]
sec = None
for line in open(path, encoding="utf-8"):
    s = line.split("#", 1)[0].rstrip()
    if s.startswith("["):
        sec = s.strip("[]").split()[0]; continue
    if len(args) == 2:
        if sec == args[0] and "=" in s:
            k, v = (x.strip() for x in s.split("=", 1))
            if k == args[1]:
                print(v); break
    elif sec in ("intermediate", "output"):
        t = s.split()
        if len(t) == 3 and t[0] == args[0]:
            print(t[1], t[2]); break
PY
}
lock() { lockpy "$1"; }
lockv() { lockpy "$1" "$2"; }

# ready NAME [DEP...] — 0 when NAME can be used as it is.
#   reproduce mode, NAME in inputs.lock: size and sha256 must match; a mismatch is removed.
#   otherwise: NAME exists and is newer than every DEP (renamed into place only when complete).
ready() {
  local name="$1"; shift
  # a .part is an interrupted write: never used, removed before the step runs (not in a dry run)
  [ "$DRY" = 1 ] || rm -f "$name.part"
  [ -e "$name" ] || return 1
  local want=""; [ "$MODE" = reproduce ] && want="$(lock "$name")"
  if [ -n "$want" ]; then
    local size="${want% *}" hash="${want#* }"
    if [ "$(stat -c %s "$name")" = "$size" ] && [ "$(sha "$name")" = "$hash" ]; then
      say "ok:   $name (sha256 matches inputs.lock)"; return 0
    fi
    say "BAD:  $name does not match inputs.lock"
    [ "$DRY" = 1 ] && return 1
    rm -f "$name"; return 1
  fi
  local d; for d in "$@"; do
    if [ "$d" -nt "$name" ]; then say "old:  $name is older than $d"; return 1; fi
  done
  say "ok:   $name"; return 0
}

# need NAME [DEP...] — 0 when the step must run now. Never in a dry run: that only reports.
need() {
  ready "$@" && return 1
  if [ "$DRY" = 1 ]; then say "todo: $1"; return 1; fi
  say "make: $1"; return 0
}

# done_ NAME — rename NAME.part into place; in reproduce mode a locked NAME must match.
done_() {
  mv -f "$1.part" "$1"
  local want=""; [ "$MODE" = reproduce ] && want="$(lock "$1")"
  if [ -n "$want" ]; then
    [ "$(sha "$1")" = "${want#* }" ] || die "$1 was rebuilt and does not match inputs.lock: this is not final2"
    say "ok:   $1 rebuilt, sha256 matches inputs.lock"
  fi
}

# fetch URL NAME SHA256 — download to NAME.part, check, rename. An existing NAME is used only if
# its sha256 matches; anything else is fetched again.
fetch() {
  local url="$1" name="$2" hash="$3"
  if [ -e "$name" ] && [ "$(sha "$name")" = "$hash" ]; then say "ok:   $name (sha256 checked)"; return 0; fi
  if [ "$DRY" = 1 ]; then say "todo: $name (download)"; return 0; fi
  rm -f "$name" "$name.part"
  say "get:  $url"
  curl -fsSL -o "$name.part" "$url"
  [ "$(sha "$name.part")" = "$hash" ] || { rm -f "$name.part"; die "$name: downloaded file does not match its sha256"; }
  mv -f "$name.part" "$name"
}

stop_after() { [ "$1" -ge "$UNTIL" ] && { say "=== stopped after step $1 (--until $UNTIL)"; exit 0; }; return 0; }
T0=$(date +%s)

# --- 0. tools, mode, source, image ------------------------------------------------------------
for tool in osmium python3 docker gzip tar sha256sum curl stat; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool"; exit 1; }
done
[ -s europe.osm.pbf ] || { echo "WORK/europe.osm.pbf not found (the build does not download it; see README)"; exit 1; }
docker image inspect "$IMAGE" >/dev/null || { echo "image $IMAGE not found: run valhalla/build-image.sh"; exit 1; }
OSMIUM="$(osmium --version | head -1 | awk '{print $NF}')"
say "=== START: mode=$MODE$([ $DRY = 1 ] && echo ' (dry run)') WORK=$WORK IMAGE=$IMAGE osmium $OSMIUM"

if [ "$MODE" = reproduce ]; then
  want_size="$(lockv source size)"; want_sha="$(lockv source sha256)"
  [ -n "$want_size" ] && [ -n "$want_sha" ] || die "inputs.lock has no [source] size/sha256"
  have_size="$(stat -c %s europe.osm.pbf)"
  [ "$have_size" = "$want_size" ] || die "europe.osm.pbf is $have_size bytes, inputs.lock says $want_size: not the final2 source (use --new-data for a new extract)"
  say "checking europe.osm.pbf sha256 (35 GB, a few minutes)"
  [ "$(sha europe.osm.pbf)" = "$want_sha" ] || die "europe.osm.pbf sha256 differs from inputs.lock: not the final2 source (use --new-data for a new extract)"
  say "ok:   europe.osm.pbf is the final2 source"
  SRC_SHA="$want_sha"
  want_osmium="$(lockv tools osmium-tool)"
  [ -z "$want_osmium" ] || [ "$want_osmium" = "$OSMIUM" ] || say "WARNING: osmium $OSMIUM, final2 was built with $want_osmium"
  want_commit="$(lockv valhalla commit)"
  have_commit="$(VAL bash -c 'git -C /src/valhalla rev-parse HEAD' 2>/dev/null || true)"
  [ "$have_commit" = "$want_commit" ] || die "image $IMAGE carries Valhalla $have_commit, inputs.lock says $want_commit"
  say "ok:   image $IMAGE is Valhalla $want_commit"
else
  say "NEW DATA: europe.osm.pbf is not compared with inputs.lock; the result is a new artifact, NOT final2"
fi

# --- 1. base Lite: the arterial network, ferries and turn restrictions -------------------------
# route=ferry is on WAYS, not highway=*, so it has to be asked for explicitly.
if need europe_lite_base.osm.pbf europe.osm.pbf; then
  osmium tags-filter -O -f pbf -o europe_lite_base.osm.pbf.part europe.osm.pbf \
      "w/highway=$LITE_CLASSES" "w/route=ferry" "r/type=restriction"
  done_ europe_lite_base.osm.pbf; say "[1] europe_lite_base.osm.pbf $(mb europe_lite_base.osm.pbf) MB"
fi
stop_after 1

# --- 2. administrative boundaries (left-hand traffic, countries) -------------------------------
if need europe_admin.osm.pbf europe.osm.pbf; then
  osmium tags-filter -O -f pbf -o europe_admin.osm.pbf.part europe.osm.pbf "r/boundary=administrative"
  done_ europe_admin.osm.pbf; say "[2] europe_admin.osm.pbf $(mb europe_admin.osm.pbf) MB"
fi
stop_after 2

# --- 3. small roads: where the ferry connectors are searched -----------------------------------
if need small_roads.osm.pbf europe.osm.pbf; then
  osmium tags-filter -O -f pbf -o small_roads.osm.pbf.part europe.osm.pbf "w/highway=$SMALL_CLASSES"
  done_ small_roads.osm.pbf; say "[3] small_roads.osm.pbf $(mb small_roads.osm.pbf) MB"
fi
stop_after 3

# --- 4. ferry ends -> candidate boxes ----------------------------------------------------------
if need ferries.osm.pbf europe_lite_base.osm.pbf; then
  osmium tags-filter -O -f pbf -o ferries.osm.pbf.part europe_lite_base.osm.pbf w/route=ferry
  done_ ferries.osm.pbf
fi
if need ferries.opl ferries.osm.pbf; then
  osmium cat -f opl -o ferries.opl.part --overwrite ferries.osm.pbf
  done_ ferries.opl
fi
if need ferry_boxes.geojson ferries.opl; then
  python3 "$HERE/connectors/ferry_ends.py" ferries.opl ferry_boxes.geojson.part ferry_end_nodes.txt | tee -a "$LOG"
  done_ ferry_boxes.geojson
fi
stop_after 4

# --- 5. connector search: candidates, first pass, wider window, second pass --------------------
if need connector_ways.txt ferry_boxes.geojson ferries.opl small_roads.osm.pbf europe_lite_base.osm.pbf; then
  bash "$HERE/connectors/extract_batched.sh" "$WORK" | tee -a "$LOG"
  python3 "$HERE/connectors/test_access.py" > /dev/null
  python3 "$HERE/connectors/connectors_dir.py" cand.opl ferries.opl connector_ways.pass1.txt unreached.txt | tee -a "$LOG"
  bash "$HERE/connectors/widen.sh" "$WORK" | tee -a "$LOG"
  python3 "$HERE/connectors/connectors_dir.py" cand.opl ferries.opl connector_ways.txt.part unreached.pass2.txt | tee -a "$LOG"
  done_ connector_ways.txt
fi
[ -e connector_ways.txt ] && say "[5] connector ways $(wc -l < connector_ways.txt)"
stop_after 5

# --- 6. connectors -> promote -> merge ---------------------------------------------------------
# connectors.osm.pbf is also an input of the structural gate (README).
if need connectors.osm.pbf connector_ways.txt small_roads.osm.pbf; then
  osmium getid -r -i connector_ways.txt small_roads.osm.pbf -f pbf -o connectors.osm.pbf.part --overwrite
  done_ connectors.osm.pbf
fi
if need europe_lite_final.osm.pbf connectors.osm.pbf europe_lite_base.osm.pbf "$HERE/promote.py"; then
  python3 "$HERE/test_promote.py" "$HERE" > /dev/null 2>&1 || die "test_promote FAILED"
  osmium cat -f opl -o connectors.opl --overwrite connectors.osm.pbf
  python3 "$HERE/promote.py" connectors.opl connectors_promoted.opl | tee -a "$LOG"
  osmium cat -f pbf -o connectors_promoted.osm.pbf --overwrite connectors_promoted.opl
  rm -f connectors.opl connectors_promoted.opl
  osmium merge europe_lite_base.osm.pbf connectors_promoted.osm.pbf -f pbf -o europe_lite_final.osm.pbf.part --overwrite
  done_ europe_lite_final.osm.pbf; say "[6] europe_lite_final.osm.pbf $(mb europe_lite_final.osm.pbf) MB"
fi
stop_after 6

# --- 7. timezone database (versioned input, checked by hash) -----------------------------------
fetch "$TZ_URL" timezones.sqlite "$TZ_SHA256"
stop_after 7

# --- 8. config, admins, graph ------------------------------------------------------------------
# One PBF argument only: several PBFs abort valhalla_build_tiles with vector::_M_range_check.
if need config.json; then
  VAL bash -c "valhalla_build_config --mjolnir-tile-dir /w/t --mjolnir-tile-extract '' \
      --mjolnir-admin /w/admins.sqlite --mjolnir-timezone /w/timezones.sqlite" > config.json.part
  done_ config.json
fi
if need admins.sqlite europe_admin.osm.pbf config.json; then
  # built under a temporary name through a copy of the config that points there
  python3 - <<'PY'
import json
c = json.load(open("config.json"))
c["mjolnir"]["admin"] = "/w/admins.sqlite.part"
json.dump(c, open("config.admins.json", "w"), indent=2)
PY
  VAL valhalla_build_admins -c /w/config.admins.json /w/europe_admin.osm.pbf >> "$LOG" 2>&1
  rm -f config.admins.json
  done_ admins.sqlite
fi
if need europe_lite.tar europe_lite_final.osm.pbf admins.sqlite timezones.sqlite config.json; then
  VAL rm -rf /w/t; mkdir -p t
  t=$(date +%s)
  VAL valhalla_build_tiles -c /w/config.json /w/europe_lite_final.osm.pbf > tiles.log 2>&1
  say "[8] graph $(( $(date +%s)-t )) s, $(find t -name '*.gph' | wc -l) tiles, $(du -sm t | cut -f1) MB"
  grep -E "Finished ReclassifyFerry|Finished with [0-9]+ shortcuts" tiles.log | sed 's/^.*Finished/  Finished/' | tee -a "$LOG"
  # Normalised archive: name order, fixed mtime and owner, so the same tiles give the same tar.
  # The tiles themselves are not proven byte-reproducible (README).
  tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner --format=gnu \
      -C t -cf europe_lite.tar.part .
  done_ europe_lite.tar
fi
stop_after 8

# --- 9. package --------------------------------------------------------------------------------
if need europe_lite.tar.gz europe_lite.tar; then
  gzip -n -6 -c europe_lite.tar > europe_lite.tar.gz.part   # -n: no name, no timestamp in the header
  done_ europe_lite.tar.gz
fi
if [ "$DRY" = 0 ]; then
  # the source was hashed at the start in reproduce mode; 35 GB is not hashed twice
  { echo "${SRC_SHA:-$(sha europe.osm.pbf)}  europe.osm.pbf"
    sha256sum europe_lite_final.osm.pbf connectors.osm.pbf connector_ways.txt \
        europe_lite.tar europe_lite.tar.gz; } > SHA256SUMS.part
  mv -f SHA256SUMS.part SHA256SUMS
  cat SHA256SUMS | tee -a "$LOG"
fi
say "=== DONE in $(( $(date +%s)-T0 )) s, mode=$MODE$([ $DRY = 1 ] && echo ' (dry run)'). Gates: see README (gates/)"
