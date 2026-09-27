#!/usr/bin/env python3
"""Move ONE country's graph outputs into the directory its graph job uploads.

    regional-graph-out.py <cc.package.json> <work-dir> <out-dir>

The package (or its parts, after regional-package.py), the frontier, the features and the package
description itself — exactly what the factory job needs from a graph job, and nothing of the
extract or the tile directory it was built in.
"""
import json, os, shutil, sys


def main():
    if len(sys.argv) != 4:
        print(__doc__); return 2
    desc, work, out = sys.argv[1:]
    d = json.load(open(desc, encoding="utf-8"))
    names = [p["name"] for p in d.get("parts") or []] or [d["package"]]
    names += [d["frontier"]["file"], d["features"]["file"]]
    os.makedirs(out, exist_ok=True)
    for n in names:
        shutil.move(os.path.join(work, n), os.path.join(out, n))
    shutil.move(desc, os.path.join(out, os.path.basename(desc)))
    for n in sorted(os.listdir(out)):
        print("   %12d  %s" % (os.path.getsize(os.path.join(out, n)), n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
