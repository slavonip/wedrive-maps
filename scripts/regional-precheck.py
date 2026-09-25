#!/usr/bin/env python3
"""Which countries the regional factory must rebuild, and why — decided BEFORE anything is built.

    regional-precheck.py <regional.json> <regional-engine.lock> <manifest.json|-> \
        [--countries MD,RO] [--rebuild auto|all|MD,RO] [--md5-json file] [--github-output file]

A country is rebuilt when (auto):
    - it is not in the current manifest;
    - its Geofabrik extract changed: the MD5 Geofabrik publishes beside <path>-latest.osm.pbf
      differs from the one recorded in the manifest (or none is recorded);
    - the engine changed: the manifest records another engine_sha than regional-engine.lock;
    - the timezone database changed: another timezones sha than the lock.
Correctness over economy: an unknown provenance is a reason to rebuild, never to carry.

The rest of the chosen set is CARRIED: its published package is reused, byte for byte.
Nothing to rebuild -> go=false, and the run ends without a release.

--countries restricts the set (control runs: MD, then MD,RO). --rebuild all forces a full build;
a list forces exactly those. --md5-json replaces the Geofabrik lookup ({"MD": "<md5>"}) for tests.
"""
import argparse, json, os, sys, urllib.request


def keyvals(path):
    out = {}
    for line in open(path, encoding="utf-8"):
        s = line.split("#", 1)[0].strip()
        if "=" in s:
            k, v = (x.strip() for x in s.split("=", 1))
            out[k] = v
    return out


def geofabrik_md5(path):
    url = "https://download.geofabrik.de/%s-latest.osm.pbf.md5" % path
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode().split()[0]


def decide(cfg, lock, manifest, countries, rebuild_arg, md5s):
    """-> (rebuild list, carry list, {code: reason})"""
    regions = (manifest or {}).get("regions", {})
    reasons = {}
    if rebuild_arg == "all":
        reasons = {c: "forced (all)" for c in countries}
    elif rebuild_arg and rebuild_arg != "auto":
        asked = [c.strip().upper() for c in rebuild_arg.split(",") if c.strip()]
        bad = [c for c in asked if c not in countries]
        if bad:
            raise SystemExit("rebuild names countries outside the set: %s" % bad)
        reasons = {c: "forced" for c in asked}
    else:
        for c in countries:
            r = regions.get(c)
            src = (r or {}).get("source") or {}
            if not r:
                reasons[c] = "not in the current manifest"
            elif not src.get("md5"):
                reasons[c] = "no source MD5 recorded"
            elif src["md5"] != md5s.get(c):
                reasons[c] = "Geofabrik extract changed (%s -> %s)" % (src["md5"][:8], (md5s.get(c) or "?")[:8])
            elif r.get("engine_sha") != lock["engine_sha"]:
                reasons[c] = "engine changed"
            elif (r.get("timezones") or {}).get("sha256") != lock["timezones_sha256"]:
                reasons[c] = "timezone database changed"
    rebuild = [c for c in countries if c in reasons]
    carry = [c for c in countries if c not in reasons]
    missing = [c for c in carry if c not in regions]
    if missing:
        raise SystemExit("cannot carry %s: not in the current manifest" % missing)
    return rebuild, carry, reasons


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("config")
    ap.add_argument("lock")
    ap.add_argument("manifest", help="current manifest.json, or - for none")
    ap.add_argument("--countries", default="")
    ap.add_argument("--rebuild", default="auto")
    ap.add_argument("--md5-json")
    ap.add_argument("--github-output")
    a = ap.parse_args()

    cfg = json.load(open(a.config, encoding="utf-8"))
    lock = keyvals(a.lock)
    for k in ("engine_ref", "engine_sha", "image", "timezones_url", "timezones_sha256"):
        if not lock.get(k):
            raise SystemExit("regional-engine.lock: %s missing" % k)
    if "@sha256:" not in lock["image"]:
        raise SystemExit("regional-engine.lock: image is not pinned by digest: %s" % lock["image"])
    manifest = None
    if a.manifest != "-" and os.path.exists(a.manifest):
        manifest = json.load(open(a.manifest, encoding="utf-8"))
        if manifest.get("kind") != "regional":
            raise SystemExit("%s is not a regional manifest" % a.manifest)

    allc = list(cfg["countries"])
    countries = [c.strip().upper() for c in a.countries.split(",") if c.strip()] or allc
    bad = [c for c in countries if c not in cfg["countries"]]
    if bad:
        raise SystemExit("not in regional.json: %s" % bad)
    countries = [c for c in allc if c in countries]          # regional.json order

    if a.md5_json:
        md5s = json.load(open(a.md5_json, encoding="utf-8"))
    else:
        md5s = {c: geofabrik_md5(cfg["countries"][c]["geofabrik"]) for c in countries}

    rebuild, carry, reasons = decide(cfg, lock, manifest, countries, a.rebuild, md5s)
    for c in countries:
        print("%-3s %-8s %s" % (c, "REBUILD" if c in reasons else "carry", reasons.get(c, "unchanged")))
    out = {
        "countries": ",".join(countries),
        "rebuild": ",".join(rebuild),
        "carry": ",".join(carry),
        "complete": "true" if countries == allc else "false",
        "go": "true" if rebuild else "false",
        "image": lock["image"],
        "engine_ref": lock["engine_ref"],
        "engine_sha": lock["engine_sha"],
        "timezones_url": lock["timezones_url"],
        "timezones_sha256": lock["timezones_sha256"],
    }
    if a.github_output:
        with open(a.github_output, "a", encoding="utf-8") as f:
            for k, v in out.items():
                f.write("%s=%s\n" % (k, v))
    print(json.dumps(out, indent=1))
    if not rebuild:
        print("nothing to rebuild: every source, the engine and the timezones are unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
