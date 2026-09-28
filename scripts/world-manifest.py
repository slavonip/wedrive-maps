#!/usr/bin/env python3
"""Put the WORLD overview into the production manifest.

    world-manifest.py <manifest.json> <world.json> <url>

Sets manifest["world"] = {url, size, sha256, build, date, maxzoom, layers, schema} and nothing else:
countries, portals, engine and tag are left exactly as they are. The regional factory carries the
entry forward (regional-manifest.py), so a monthly run never drops it.
"""
import json, sys

REQUIRED = ("size", "sha256", "build", "maxzoom", "layers")


def apply(manifest, world, url):
    missing = [k for k in REQUIRED if k not in world]
    if missing:
        raise SystemExit("world.json lacks %s" % missing)
    if manifest.get("kind") != "regional":
        raise SystemExit("not a regional manifest")
    entry = {k: world[k] for k in ("size", "sha256", "build", "date", "maxzoom", "layers", "schema") if k in world}
    entry["url"] = url
    manifest["world"] = entry
    return manifest


if __name__ == "__main__":
    mpath, wpath, url = sys.argv[1], sys.argv[2], sys.argv[3]
    m = json.load(open(mpath, encoding="utf-8"))
    w = json.load(open(wpath, encoding="utf-8"))
    old = (m.get("world") or {}).get("build")
    apply(m, w, url)
    json.dump(m, open(mpath, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("manifest world: %s -> %s (%d bytes)" % (old, w["build"], w["size"]))
