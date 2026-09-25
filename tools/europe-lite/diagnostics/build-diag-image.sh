#!/bin/bash
# DIAGNOSTICS ONLY — never used by build.sh or the gates.
#
# Builds valhalla_service_tw: the production image's Thor with thor-trace.patch, which logs
# bidirectional A* for chosen GraphIds (VH_WATCH, VH_WATCH_BOX) and decomposes the cost of the
# chosen connection against FormPath/recost_forward (VH_FP). This is the tool that found the
# three Dover–Calais causes; see docs/europe-lite.md §5.
#
# Usage: build-diag-image.sh [BASE_IMAGE] [DIAG_IMAGE]
#        (defaults wedrive-valhalla:lite and wedrive-valhalla:lite-diag)
set -euo pipefail
BASE="${1:-wedrive-valhalla:lite}"
DIAG="${2:-wedrive-valhalla:lite-diag}"
HERE="$(cd "$(dirname "$0")" && pwd)"
docker rm -f lite-diag-build >/dev/null 2>&1 || true
docker run --name lite-diag-build -v "$HERE":/diag --entrypoint bash "$BASE" -c '
set -e
cd /src/valhalla
git apply --check /diag/thor-trace.patch && git apply /diag/thor-trace.patch
make -C build valhalla_service -j"$(nproc)" 2>&1 | grep -E " error |Built target valhalla_service" | tail -3
install -m 755 build/valhalla_service /usr/local/bin/valhalla_service_tw'
docker commit lite-diag-build "$DIAG" >/dev/null
docker rm lite-diag-build >/dev/null
echo "diagnostic image $DIAG: valhalla_service_tw"
