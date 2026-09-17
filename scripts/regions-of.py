"""The Geofabrik paths one package is built from, space-separated.

Used by the workflow to turn a package id into build-graph.sh's arguments, so the mapping lives
in regions.yml alone rather than being repeated in YAML the shell has to parse.
"""
import sys

import yaml

package = sys.argv[1]
config = yaml.safe_load(open("regions.yml", encoding="utf-8"))
regions = config["packages"][package]["regions"]
print(" ".join(regions))
