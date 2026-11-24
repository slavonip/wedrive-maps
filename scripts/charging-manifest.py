#!/usr/bin/env python3
"""Put the charging catalogue into the production manifest.

    charging-manifest.py <manifest.json> <charging.json> <url> [<url of charging-v2.sqlite.gz>]

Sets manifest["charging"] = {format, build, url, bytes, sha256, sqlite_bytes, sqlite_sha256,
counts, sources} and nothing else: countries, portals, world, engine, tag and min_app are left
exactly as they are. The regional factory carries the entry forward (regional-manifest.py), so a
monthly regional run never drops it, and an app that predates it simply ignores the key.

wedrive-charging/2 (2026-11-24) rides INSIDE that entry as charging["v2"] = the same fields for
the /2 file. Each `format` names exactly the file its block points at: the top level stays the /1
file every app reads, and an app that knows /2 takes the nested block. Being nested, the regional
factory's carry-forward of `charging` carries it too.
"""
import json, sys

FORMAT = "wedrive-charging/1"
FORMAT2 = "wedrive-charging/2"
REQUIRED = ("format", "build", "bytes", "sha256", "sqlite_bytes", "sqlite_sha256", "counts", "sources")


def _https(url):
    if not url.startswith("https://"):
        raise SystemExit("charging url must be https: %r" % url)


def apply(manifest, charging, url, url_v2=None):
    missing = [k for k in REQUIRED if k not in charging]
    if missing:
        raise SystemExit("charging.json lacks %s" % missing)
    if charging["format"] != FORMAT:
        raise SystemExit("charging.json format %r, expected %r" % (charging["format"], FORMAT))
    if manifest.get("kind") != "regional":
        raise SystemExit("not a regional manifest")
    _https(url)
    entry = {k: charging[k] for k in REQUIRED}
    entry["url"] = url
    v2 = charging.get("v2")
    if v2 is not None or url_v2 is not None:
        if v2 is None or url_v2 is None:
            raise SystemExit("charging.json v2 and its url go together (v2 %s, url %s)" % (v2 is not None, url_v2))
        missing = [k for k in REQUIRED if k not in v2]
        if missing:
            raise SystemExit("charging.json v2 lacks %s" % missing)
        if v2["format"] != FORMAT2:
            raise SystemExit("charging.json v2 format %r, expected %r" % (v2["format"], FORMAT2))
        if v2["build"] != charging["build"]:
            raise SystemExit("charging.json v2 build %r is not the /1 build %r" % (v2["build"], charging["build"]))
        _https(url_v2)
        entry["v2"] = dict({k: v2[k] for k in REQUIRED}, url=url_v2)
    manifest["charging"] = entry
    return manifest


if __name__ == "__main__":
    mpath, cpath, url = sys.argv[1], sys.argv[2], sys.argv[3]
    url_v2 = sys.argv[4] if len(sys.argv) > 4 else None
    m = json.load(open(mpath, encoding="utf-8"))
    c = json.load(open(cpath, encoding="utf-8"))
    old = (m.get("charging") or {}).get("build")
    apply(m, c, url, url_v2)
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    print("manifest charging: %s -> %s (%d bytes, %d stations)" % (old, c["build"], c["bytes"], c["counts"]["stations"]))
    if url_v2:
        print("manifest charging.v2: %s (%d bytes, %d stations, %d EVSE ids)"
              % (c["v2"]["format"], c["v2"]["bytes"], c["v2"]["counts"]["stations"], c["v2"]["counts"]["evse_ids"]))
