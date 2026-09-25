#!/bin/bash
# The key of the Europe Lite engine recipe: sha256 over the sha256 of the Dockerfile and of both
# patches, in a fixed order, first 12 hex digits. Same recipe -> same key -> the image is not rebuilt.
set -euo pipefail
V="$(cd "$(dirname "$0")/../valhalla" && pwd)"
{ for f in Dockerfile valhalla-3.6.3-ferry-first-edge.patch valhalla-3.6.3-border-control-contract.patch; do
    sha256sum "$V/$f" | cut -d' ' -f1
  done; } | sha256sum | cut -c1-12
