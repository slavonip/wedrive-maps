#!/usr/bin/env python3
"""What this run really rebuilt, once the matrix jobs are back — and what it carries instead.

    regional-effective.py <work-dir> <basemaps-dir> <manifest.json|-> --rebuild MD,RO --carry HU \
        [--github-env FILE]

The factory fans out: every country's graph is its own job, and so is its map + search (the
basemap job). Any of them may fail without the others noticing, which is the point — one broken
country must not stop fifty. This step turns the planned lists into the real ones:

    rebuilt   the graph job left <cc>.package.json AND the basemap job left <cc>.basemap-desc.json.
              Only BOTH count: graph, frontier, features and search addresses of a country must come
              from one Geofabrik PBF (the source-identity gate), so a country is never published
              with a new graph and an old search, or the other way round.
    carried   planned carry, plus every planned rebuild that failed but WAS published before: its
              previous release is reused whole, byte for byte (regional-carry.py).
    absent    planned rebuild that failed and was NEVER published: it is simply not in this release
              (manifest "absent", with the reason), and nothing it would have replaced is lost.

The half of a failed country that did succeed is deleted here, so nothing downstream can pick it up.
"""
import argparse, glob, json, os, sys


def effective(work, basemaps, manifest, rebuild, carry):
    prev = (manifest or {}).get("regions", {})
    ok, carried, absent = [], list(carry), {}
    for code in rebuild:
        low = code.lower()
        graph = os.path.isfile(os.path.join(work, "%s.package.json" % low))
        base = os.path.isfile(os.path.join(basemaps, "%s.basemap-desc.json" % low))
        if graph and base:
            ok.append(code)
            continue
        why = "graph job failed" if not graph else "map/search job failed"
        if not base and not graph:
            why = "graph and map/search jobs failed"
        # drop the half that succeeded: it must not be mistaken for this run's country
        if graph:
            d = json.load(open(os.path.join(work, "%s.package.json" % low), encoding="utf-8"))
            names = [d.get("package")] + [p["name"] for p in d.get("parts") or []] + \
                    [(d.get("frontier") or {}).get("file"), (d.get("features") or {}).get("file")]
            for n in filter(None, names):
                p = os.path.join(work, n)
                if os.path.isfile(p):
                    os.remove(p)
            os.remove(os.path.join(work, "%s.package.json" % low))
        if base:
            os.remove(os.path.join(basemaps, "%s.basemap-desc.json" % low))
        if code in prev:
            carried.append(code)
            print("%-3s %s -> CARRY the published %s" % (code, why, prev[code].get("graph_version")))
        else:
            absent[code] = why
            print("%-3s %s -> ABSENT (never published)" % (code, why))
    for code in ok:
        print("%-3s rebuilt" % code)
    return ok, carried, absent


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("work"); ap.add_argument("basemaps"); ap.add_argument("manifest")
    ap.add_argument("--rebuild", default=""); ap.add_argument("--carry", default="")
    ap.add_argument("--github-env")
    a = ap.parse_args()
    manifest = None
    if a.manifest != "-" and os.path.isfile(a.manifest):
        manifest = json.load(open(a.manifest, encoding="utf-8"))
    split = lambda s: [c.strip().upper() for c in s.split(",") if c.strip()]
    ok, carried, absent = effective(a.work, a.basemaps, manifest, split(a.rebuild), split(a.carry))
    with open(os.path.join(a.work, "absent.json"), "w", encoding="utf-8") as f:
        json.dump(absent, f, indent=1, sort_keys=True)
    if a.github_env:
        with open(a.github_env, "a", encoding="utf-8") as f:
            # new names: a job-level env of the same name would not be overridden by GITHUB_ENV
            f.write("EFF_REBUILD=%s\nEFF_CARRY=%s\n" % (",".join(ok), ",".join(carried)))
    print("rebuilt %d, carried %d, absent %d" % (len(ok), len(carried), len(absent)))
    if not ok and not carried:
        print("nothing to publish: every country failed and none was published before")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
