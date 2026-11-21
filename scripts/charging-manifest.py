#!/usr/bin/env python3
"""Put the charging catalogue into the production manifest.

    charging-manifest.py <manifest.json> <charging.json> <url>

Sets manifest["charging"] = {format, build, url, bytes, sha256, sqlite_bytes, sqlite_sha256,
counts, sources} and nothing else: countries, portals, world, engine, tag and min_app are left
exactly as they are. The regional factory carries the entry forward (regional-manifest.py), so a
monthly regional run never drops it, and an app that predates it simply ignores the key.
"""
import json, sys

FORMAT = "wedrive-charging/1"
REQUIRED = ("format", "build", "bytes", "sha256", "sqlite_bytes", "sqlite_sha256", "counts", "sources")


def apply(manifest, charging, url):
    missing = [k for k in REQUIRED if k not in charging]
    if missing:
        raise SystemExit("charging.json lacks %s" % missing)
    if charging["format"] != FORMAT:
        raise SystemExit("charging.json format %r, expected %r" % (charging["format"], FORMAT))
    if manifest.get("kind") != "regional":
        raise SystemExit("not a regional manifest")
    if not url.startswith("https://"):
        raise SystemExit("charging url must be https: %r" % url)
    entry = {k: charging[k] for k in REQUIRED}
    entry["url"] = url
    manifest["charging"] = entry
    return manifest


if __name__ == "__main__":
    mpath, cpath, url = sys.argv[1], sys.argv[2], sys.argv[3]
    m = json.load(open(mpath, encoding="utf-8"))
    c = json.load(open(cpath, encoding="utf-8"))
    old = (m.get("charging") or {}).get("build")
    apply(m, c, url)
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    print("manifest charging: %s -> %s (%d bytes, %d stations)" % (old, c["build"], c["bytes"], c["counts"]["stations"]))
