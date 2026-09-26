#!/usr/bin/env python3
"""Split a country's basemap over the release asset limit, recording the whole file's size and hash.

    split-map.py <out-dir> <low>

<out-dir>/<low>.pmtiles -> <low>.pmtiles (one asset) or <low>.pmtiles.partNNN, and
<out-dir>/map-parts.json {"bytes", "sha256", "parts": [...]} for regional-basemap-desc.py.
The same splitter as the graphs (split-assets.py): parts are transport only; the device joins
them and checks the WHOLE file's hash.
"""
import importlib.util
import json
import os
import pathlib
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("split_assets", os.path.join(HERE, "split-assets.py"))
sa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sa)


def main(argv):
    if len(argv) != 3:
        print(__doc__); return 2
    out = pathlib.Path(argv[1])
    info = sa.split(out / ("%s.pmtiles" % argv[2]))
    (out / "map-parts.json").write_text(json.dumps(info), encoding="utf-8")
    print("   map %d bytes, %d parts" % (info["bytes"], len(info["parts"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
