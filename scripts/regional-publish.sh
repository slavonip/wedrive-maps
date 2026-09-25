#!/usr/bin/env bash
# Publish one regional release atomically, then move manifest.json — or only prove it (verify).
#
#   scripts/regional-publish.sh DIST REPO POINTER_REPO_DIR MODE
#
#   DIST              manifest.json + the files of THIS run's release (packages or parts built in
#                     this run, and every portal table). Carried countries are not here: the
#                     manifest points at the immutable assets of the release they came from.
#   MODE  verify      draft -> upload -> download back -> check -> DELETE the draft. The pointer is
#                     never touched. Used by control runs on part of the set.
#         publish     the same, then publish -> check every URL of the manifest (carried included)
#                     answers with its size -> commit manifest.json. Refused for an incomplete set.
#
# A failure before publishing deletes the draft. A failure after it leaves a release that nothing
# points to, and manifest.json unchanged: the pointer only ever names a release that passed all.
set -euo pipefail
DIST="$(cd "${1:?DIST}" && pwd)"; REPO="${2:?REPO}"; PTR="$(cd "${3:?POINTER_REPO_DIR}" && pwd)"; MODE="${4:?verify|publish}"
HERE="$(cd "$(dirname "$0")" && pwd)"
M="$DIST/manifest.json"
say() { echo "[$(date +%H:%M:%S)] $*"; }
[ "$MODE" = verify ] || [ "$MODE" = publish ] || { echo "MODE must be verify or publish"; exit 2; }

# --- 1: what goes out, and that it is what the manifest says --------------------------------------
TAG="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["tag"])' "$M")"
[ -n "$TAG" ] || { echo "manifest has no tag"; exit 1; }
mapfile -t ASSETS < <(python3 "$HERE/regional-manifest.py" assets "$M")
say "release $TAG: ${#ASSETS[@]} assets + manifest.json"
python3 "$HERE/regional-manifest.py" verify-assets "$M" "$DIST"
if [ "$MODE" = publish ]; then
  python3 "$HERE/regional-manifest.py" complete "$M" "$PTR/regional.json"
fi
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  echo "release $TAG already exists: tags are immutable, nothing published"; exit 1
fi

# --- 2-4: a draft, removed again unless it gets published --------------------------------------------
PUBLISHED=0
cleanup() { [ "$PUBLISHED" = 1 ] || { say "removing draft $TAG"; gh release delete "$TAG" --repo "$REPO" --yes >/dev/null 2>&1 || true; }; }
trap cleanup EXIT
say "draft $TAG"
gh release create "$TAG" --repo "$REPO" --draft --title "Региональные пакеты $TAG" \
  --notes "Региональные пакеты стран и таблицы порталов. manifest.json внутри; пакеты неизменившихся стран остаются в релизах, на которые он указывает."
say "upload"
( cd "$DIST" && gh release upload "$TAG" --repo "$REPO" "${ASSETS[@]}" manifest.json )
say "verify the draft by downloading it back"
CHK="$(mktemp -d)"
gh release download "$TAG" --repo "$REPO" --dir "$CHK"
cmp "$CHK/manifest.json" "$M"
python3 "$HERE/regional-manifest.py" verify-assets "$M" "$CHK"
n="$(ls "$CHK" | wc -l)"; [ "$n" = "$(( ${#ASSETS[@]} + 1 ))" ] || { echo "the draft has $n assets, expected $(( ${#ASSETS[@]} + 1 ))"; exit 1; }
rm -rf "$CHK"
if [ "$MODE" = verify ]; then
  say "verify mode: every asset came back intact; the draft is deleted, manifest.json untouched"
  exit 0
fi

# --- 5 --------------------------------------------------------------------------------------------
gh release edit "$TAG" --repo "$REPO" --draft=false --latest=false
PUBLISHED=1
say "published $TAG; checking every URL of the manifest, carried countries included"
python3 "$HERE/regional-manifest.py" urls "$M" | while read -r url size; do
  got="$(curl -sfIL "$url" | awk 'tolower($1)=="content-length:"{v=$2} END{gsub("\r","",v); print v}')"
  [ "$got" = "$size" ] || { echo "$url: size $got, expected $size — manifest.json NOT moved"; exit 1; }
done

# --- 6: the switch ----------------------------------------------------------------------------------
cp "$M" "$PTR/manifest.json"
cd "$PTR"
git add manifest.json
git -c user.name="wedrive-factory" -c user.email="factory@users.noreply.github.com" \
    commit -q -m "Региональный манифест -> $TAG"
for i in 1 2 3; do
  git pull -q --rebase --autostash && git push -q && { say "manifest.json -> $TAG"; exit 0; }
  sleep 5
done
echo "manifest push failed three times: $TAG is published, manifest.json is unchanged"; exit 1
