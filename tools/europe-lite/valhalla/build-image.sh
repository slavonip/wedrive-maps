#!/bin/bash
# Build the Europe Lite Valhalla image and prove what is in it (check-image.sh).
#
# Usage: build-image.sh [IMAGE]        (default wedrive-valhalla:lite)
set -euo pipefail
IMAGE="${1:-wedrive-valhalla:lite}"
HERE="$(cd "$(dirname "$0")" && pwd)"

docker build -t "$IMAGE" "$HERE"
bash "$HERE/check-image.sh" "$IMAGE"
