#!/usr/bin/env python3
"""The map and search blocks of ONE country, for the regional manifest.

    regional-basemap-desc.py <out-dir> <CODE>

Reads what build-basemap.sh + index-to-sqlite.py + split-assets.py left in <out-dir> for the
country (<low>-basemap.json, map-parts.json, <low>.search.sqlite, <low>-index-meta.json) and
writes <out-dir>/<low>.basemap-desc.json:

    {"code": "MD",
     "map":    {"format": "pmtiles", "file": "md.pmtiles", "bytes", "sha256", ["parts": [...]],
                "schema", "build", "dataDate", "maxzoom", "bbox"},
     "search": {"format": "wedrive-search/sqlite-fts4", "file": "md.search.sqlite", "bytes",
                "sha256", "counts"}}

The factory job that builds graphs gets only these small descriptors (the manifest needs sizes
and hashes, not gigabytes); the files themselves go straight to the publish job.
"""
import hashlib
import json
import os
import sqlite3
import sys

SEARCH_FORMAT = "wedrive-search/sqlite-fts4"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv):
    if len(argv) != 3:
        print(__doc__); return 2
    out, code = argv[1], argv[2].upper()
    low = code.lower()
    meta = json.load(open(os.path.join(out, "%s-basemap.json" % low), encoding="utf-8"))
    parts = json.load(open(os.path.join(out, "map-parts.json"), encoding="utf-8"))
    m = {"format": "pmtiles", "file": "%s.pmtiles" % low, "bytes": parts["bytes"], "sha256": parts["sha256"],
         "schema": meta["schema"], "build": meta["build"], "dataDate": meta["dataDate"],
         "maxzoom": meta["maxzoom"], "bbox": meta["bbox"]}
    if parts["parts"]:
        m["parts"] = parts["parts"]
    if parts["sha256"] != meta["sha256"] or parts["bytes"] != meta["bytes"]:
        raise SystemExit("%s: the split map is not the extracted one (%s vs %s)" % (code, parts["sha256"][:12], meta["sha256"][:12]))
    sq = os.path.join(out, "%s.search.sqlite" % low)
    im = json.load(open(os.path.join(out, "%s-index-meta.json" % low), encoding="utf-8"))
    s = {"format": SEARCH_FORMAT, "file": os.path.basename(sq), "bytes": os.path.getsize(sq),
         "sha256": sha256(sq), "counts": im.get("counts")}
    # house numbers inside the same file (search_addresses.py): counted, with the PBF they came from
    c = sqlite3.connect(sq)
    if c.execute("SELECT count(*) FROM sqlite_master WHERE name = 'addr_meta'").fetchone()[0]:
        am = dict(c.execute("SELECT k, v FROM addr_meta"))
        s["addresses"] = {"format": am.get("format"), "streets": int(am.get("streets", 0)),
                          "houses": int(am.get("houses", 0)), "source_md5": am.get("source_md5")}
    c.close()
    desc = {"code": code, "map": m, "search": s}
    path = os.path.join(out, "%s.basemap-desc.json" % low)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(desc, f, indent=2)
    print(json.dumps(desc, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
