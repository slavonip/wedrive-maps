#!/bin/bash
# Publish one Europe Lite release atomically, then move the Android pointer.
#
#   factory/publish.sh DIR REPO POINTER_REPO_DIR
#
#   DIR               europe_lite.tar.gz, europe_lite.osm.pbf, gates.json, europe-lite.json
#   REPO              owner/name of the repository that holds the releases
#   POINTER_REPO_DIR  a checkout of REPO's default branch; europe-lite.json there is the pointer
#
# Order, and why it is this order:
#   1. check the manifest against the files, write SHA256SUMS, refuse an existing tag
#   2. create the release as a DRAFT (invisible to Android)
#   3. upload every asset (never --clobber: a tag is immutable)
#   4. download them back from the draft and check every sha256
#   5. publish the release, then check every public URL answers with the stated size
#   6. only now commit europe-lite.json at the repository root: that commit IS the switch
# A failure before step 5 deletes the draft and leaves nothing behind. A failure after it leaves a
# published release that nothing points to, and the pointer unchanged. The pointer never moves to a
# release that did not pass steps 1-5.
set -euo pipefail
DIR="$(cd "${1:?DIR}" && pwd)"; REPO="${2:?REPO}"; PTR="$(cd "${3:?POINTER_REPO_DIR}" && pwd)"
HERE="$(cd "$(dirname "$0")" && pwd)"
say() { echo "[$(date +%H:%M:%S)] $*"; }
cd "$DIR"

# --- 1 --------------------------------------------------------------------------------------------
# The release's files come from the manifest: the contract five, plus europe_lite.features when the
# manifest describes it (releases before 2026-09-27 have none).
[ -s europe-lite.json ] || { echo "missing europe-lite.json"; exit 1; }
mapfile -t ALL < <(python3 "$HERE/lite_manifest.py" files europe-lite.json)
SUMMED=(); for f in "${ALL[@]}"; do [ "$f" = SHA256SUMS ] || SUMMED+=("$f"); done
for f in "${SUMMED[@]}"; do
  [ -s "$f" ] || { echo "missing $f"; exit 1; }
done
python3 "$HERE/lite_manifest.py" check europe-lite.json --dir "$DIR"
TAG="$(python3 -c 'import json;print(json.load(open("europe-lite.json"))["tag"])')"
VERSION="$(python3 -c 'import json;print(json.load(open("europe-lite.json"))["version"])')"
[ "$(python3 -c 'import json;print(json.load(open("gates.json"))["verdict"])')" = PASS ] || { echo "gates.json is not PASS"; exit 1; }
sha256sum "${SUMMED[@]}" > SHA256SUMS
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  echo "release $TAG already exists: tags are immutable, nothing published"; exit 1
fi

# --- 2-4: a draft, removed again if anything fails before it is published ---------------------------
PUBLISHED=0
cleanup() { [ "$PUBLISHED" = 1 ] || { say "removing draft $TAG"; gh release delete "$TAG" --repo "$REPO" --yes >/dev/null 2>&1 || true; }; }
trap cleanup EXIT
python3 - > notes.md <<'PY'
import json
m = json.load(open("europe-lite.json")); g = m["gates"]
print("Europe Lite %s — the routing graph of the whole continent's arterial network.\n" % m["version"])
print("- source: Geofabrik Europe, replication %s, sha256 `%s`" % (m["source"]["replication"], m["source"]["sha256"]))
print("- engine: Valhalla %s `%s` + B2 + C, image `%s`" % (m["engine"]["valhalla"], m["engine"]["commit"][:7], m["engine"]["image"]))
print("- pipeline: `%s`" % m["pipeline"]["revision"])
print("- gates: struct %s, transit %s (%s pairs, %s new transits), routes %s (pass %s, known %s, accepted %s, fail %s)" % (
    g["struct"]["verdict"], g["transit"]["verdict"], g["transit"].get("pairs"), g["transit"].get("new_transits"),
    g["routes"]["verdict"], g["routes"].get("pass"), g["routes"].get("known"), g["routes"].get("accepted"), g["routes"].get("fail")))
print("- run: %s" % (m["run"].get("url") or m["run"].get("note") or "-"))
print("\nThe manifest is europe-lite.json; SHA256SUMS covers every other asset.")
PY
say "draft $TAG"
gh release create "$TAG" --repo "$REPO" --draft --title "Europe Lite $VERSION" --notes-file notes.md
say "upload"
gh release upload "$TAG" --repo "$REPO" "${ALL[@]}"
say "verify the draft's assets by downloading them back"
CHK="$(mktemp -d)"
gh release download "$TAG" --repo "$REPO" --dir "$CHK"
( cd "$CHK" && sha256sum -c SHA256SUMS && cmp SHA256SUMS "$DIR/SHA256SUMS" )
n="$(ls "$CHK" | wc -l)"; [ "$n" = "${#ALL[@]}" ] || { echo "the draft has $n assets, expected ${#ALL[@]}"; exit 1; }
rm -rf "$CHK"

# --- 5 --------------------------------------------------------------------------------------------
gh release edit "$TAG" --repo "$REPO" --draft=false --latest=false
PUBLISHED=1
say "published $TAG; checking the public URLs"
for f in "${ALL[@]}"; do
  want="$(stat -c %s "$f")"
  got="$(curl -sfIL "https://github.com/$REPO/releases/download/$TAG/$f" | awk 'tolower($1)=="content-length:"{v=$2} END{gsub("\r","",v); print v}')"
  [ "$got" = "$want" ] || { echo "public $f: size $got, expected $want — pointer NOT moved"; exit 1; }
done

# --- 6: the switch ----------------------------------------------------------------------------------
cp europe-lite.json "$PTR/europe-lite.json"
cd "$PTR"
git add europe-lite.json
git -c user.name="europe-lite-factory" -c user.email="actions@users.noreply.github.com" \
    commit -q -m "Europe Lite: current -> $TAG"
for i in 1 2 3; do
  git pull -q --rebase --autostash && git push -q && { say "pointer -> $TAG"; exit 0; }
  sleep 5
done
echo "pointer push failed three times: $TAG is published, the pointer is unchanged"; exit 1
