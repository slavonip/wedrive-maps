"""The Geofabrik paths one package is built from, space-separated.

Used by the workflow to turn a package id into build-graph.sh's arguments, so the mapping lives
in regions.yml alone rather than being repeated in YAML the shell has to parse. An id that names
no package in the file is derived from its country codes — see packages.py.
"""
import sys

import packages

config = packages.load()
try:
    print(" ".join(packages.regions_of(config, sys.argv[1])))
except ValueError as error:
    print(error, file=sys.stderr)
    raise SystemExit(2)
