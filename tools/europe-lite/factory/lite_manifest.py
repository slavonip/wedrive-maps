# -*- coding: utf-8 -*-
"""europe-lite.json: the manifest of one Europe Lite release, and the current pointer for Android.

    lite_manifest.py make  --repo OWNER/REPO --dir DIR --source source.json --gates gates.json \
                           --lite-image REF --tiles N --tar-bytes N [--run-id ID --run-url URL] \
                           [--factory-commit SHA] --out europe-lite.json
    lite_manifest.py check europe-lite.json [--dir DIR]

`make` hashes the two published files in DIR (europe_lite.tar.gz, europe_lite.osm.pbf), which are
already under their stable names; the version is the UTC date of the source's replication
timestamp and the release tag is europe-lite-<version>. `check` validates a manifest and, with
--dir, that every file it names in DIR has the bytes and sha256 it states. Exit code 0 / 1.

Asset names are a CONTRACT and never carry the version: every release has europe_lite.tar.gz,
europe_lite.osm.pbf, SHA256SUMS, europe-lite.json and gates.json. The version lives in the tag
and in this file; a URL is unique through its tag.
"""
import argparse, datetime, hashlib, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
LITE = os.path.dirname(HERE)
GRAPH, PBF = "europe_lite.tar.gz", "europe_lite.osm.pbf"
FEATURES = "europe_lite.features"
ASSETS = [GRAPH, PBF, "gates.json", "europe-lite.json", "SHA256SUMS"]
PATCHES = {"B2": "valhalla-3.6.3-ferry-first-edge.patch",
           "C": "valhalla-3.6.3-border-control-contract.patch"}
VALHALLA = {"version": "3.6.3", "commit": "e2f017b16080f49203de245a211b09efab09cf72"}
SHA = re.compile(r"^[0-9a-f]{64}$")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def keyvals(path):
    """'key = value' files (PIPELINE_REV, engine.lock); repeated keys become lists."""
    out = {}
    for line in open(path, encoding="utf-8"):
        s = line.split("#", 1)[0].rstrip()
        if "=" in s and not s[:1].isspace():
            k, v = (x.strip() for x in s.split("=", 1))
            out.setdefault(k, []).append(v)
    return {k: v[0] if len(v) == 1 else v for k, v in out.items()}


def features_counts(path):
    """The counts line of a wedrive-features file (the header is the contract, see
    scripts/regional-features.py)."""
    with open(path, encoding="ascii") as f:
        for line in f:
            if line.startswith("counts "):
                return {k: int(v) for k, v in (kv.split("=") for kv in line.split(" ", 1)[1].strip().split(","))}
    raise SystemExit("%s has no counts line" % path)


def release_files(m):
    """The files of a release: the contract five, plus the features when the manifest has them."""
    return [GRAPH, PBF] + ([FEATURES] if m.get("features") else []) + ["gates.json", "europe-lite.json", "SHA256SUMS"]


def url(repo, tag, name):
    return "https://github.com/%s/releases/download/%s/%s" % (repo, tag, name)


def entry(repo, tag, d, name):
    p = os.path.join(d, name)
    return {"file": name, "url": url(repo, tag, name), "bytes": os.path.getsize(p), "sha256": sha256(p)}


def make(a):
    src = json.load(open(a.source, encoding="utf-8"))
    gates = json.load(open(a.gates, encoding="utf-8"))
    if gates.get("verdict") != "PASS":
        print("gates verdict is %r: a manifest is only made for a PASS" % gates.get("verdict")); return 1
    version = src["replication"][:10]
    tag = "europe-lite-" + version
    rev = keyvals(os.path.join(LITE, "PIPELINE_REV"))
    m = {
        "schema": 1,
        "kind": "europe-lite",
        "version": version,
        "tag": tag,
        "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": src,
        "engine": {
            "valhalla": VALHALLA["version"], "commit": VALHALLA["commit"],
            "patches": {k: {"file": f, "sha256": sha256(os.path.join(LITE, "valhalla", f))}
                        for k, f in PATCHES.items()},
            "image": a.lite_image,
        },
        "pipeline": {"origin": rev["origin"], "revision": rev["revision"],
                     "factory_commit": a.factory_commit},
        "graph": dict(entry(a.repo, tag, a.dir, GRAPH), compression="gzip",
                      tar_bytes=a.tar_bytes, tiles=a.tiles),
        "pbf": entry(a.repo, tag, a.dir, PBF),
        "gates": {
            "verdict": gates["verdict"],
            "baseline": gates["baseline"],
            "file": "gates.json",
            **{g: {"verdict": gates[g]["verdict"], **gates[g]["counts"]}
               for g in ("struct", "transit", "routes")},
        },
        "run": {"id": a.run_id, "url": a.run_url} if a.run_id else {"id": None, "url": None,
                                                                       "note": a.run_note},
    }
    # The lowest app versionCode that reads this release (app-compat.json, shared with the regional
    # factory). Optional in validate(): older releases have none and stay valid for a rollback.
    m["min_app"] = int(json.load(open(os.path.join(os.path.dirname(os.path.dirname(LITE)), "app-compat.json"),
                                      encoding="utf-8"))["min_app"])
    # Navigation features of the Lite roads (optional: releases before 2026-09-27 have none, and
    # they must stay valid for a rollback).
    if os.path.exists(os.path.join(a.dir, FEATURES)):
        m["features"] = dict(entry(a.repo, tag, a.dir, FEATURES), format="wedrive-features/1",
                             counts=features_counts(os.path.join(a.dir, FEATURES)))
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1, sort_keys=True)
        f.write("\n")
    problems = validate(m, a.dir)
    for p in problems:
        print("PROBLEM:", p)
    print("%s -> %s (%s)" % (a.out, tag, "OK" if not problems else "INVALID"))
    return 1 if problems else 0


def validate(m, d=None):
    p = []
    need = ["schema", "kind", "version", "tag", "created", "source", "engine", "pipeline", "graph",
            "pbf", "gates", "run"]
    p += ["missing %s" % k for k in need if k not in m]
    if p:
        return p
    if m["schema"] != 1: p.append("schema is %r" % m["schema"])
    if m["kind"] != "europe-lite": p.append("kind is %r" % m["kind"])
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(m["version"])): p.append("version %r is not YYYY-MM-DD" % m["version"])
    if m["tag"] != "europe-lite-" + str(m["version"]): p.append("tag %r does not match version" % m["tag"])
    s = m["source"]
    for k in ("url", "replication", "sha256", "bytes"):
        if k not in s: p.append("source.%s missing" % k)
    if "sha256" in s and not SHA.match(s["sha256"]): p.append("source.sha256 malformed")
    if str(s.get("replication", ""))[:10] != m["version"]: p.append("version is not the replication date")
    e = m["engine"]
    if e.get("valhalla") != VALHALLA["version"] or e.get("commit") != VALHALLA["commit"]:
        p.append("engine is not Valhalla %s %s" % (VALHALLA["version"], VALHALLA["commit"]))
    for k in PATCHES:
        if not SHA.match(str(e.get("patches", {}).get(k, {}).get("sha256", ""))): p.append("patch %s hash missing" % k)
    if not e.get("image"): p.append("engine.image missing")
    if not m["pipeline"].get("revision"): p.append("pipeline.revision missing")
    for key, name in (("graph", GRAPH), ("pbf", PBF)) + ((("features", FEATURES),) if "features" in m else ()):
        x = m[key]
        if x.get("file") != name: p.append("%s.file is %r, the contract says %r" % (key, x.get("file"), name))
        if not str(x.get("url", "")).endswith("/releases/download/%s/%s" % (m["tag"], name)):
            p.append("%s.url does not point at %s/%s" % (key, m["tag"], name))
        if not isinstance(x.get("bytes"), int) or x["bytes"] <= 0: p.append("%s.bytes invalid" % key)
        if not SHA.match(str(x.get("sha256", ""))): p.append("%s.sha256 malformed" % key)
        if d is not None and "sha256" in x:
            f = os.path.join(d, name)
            if not os.path.exists(f): p.append("%s missing in %s" % (name, d))
            elif os.path.getsize(f) != x["bytes"] or sha256(f) != x["sha256"]:
                p.append("%s in %s does not match the manifest" % (name, d))
    if "features" in m:
        fe = m["features"]
        if fe.get("format") != "wedrive-features/1": p.append("features.format is %r" % fe.get("format"))
        if not isinstance(fe.get("counts"), dict) or not fe["counts"]: p.append("features.counts missing")
    g = m["gates"]
    if g.get("verdict") != "PASS": p.append("gates verdict is %r" % g.get("verdict"))
    for k in ("struct", "transit", "routes"):
        if g.get(k, {}).get("verdict") != "PASS": p.append("gate %s is not PASS" % k)
    if "id" not in m["run"] or "url" not in m["run"]: p.append("run.id/url missing")
    return p


def check(a):
    m = json.load(open(a.manifest, encoding="utf-8"))
    problems = validate(m, a.dir)
    for x in problems:
        print("PROBLEM:", x)
    print("%s: %s" % (a.manifest, "OK" if not problems else "INVALID"))
    return 1 if problems else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    mk = sub.add_parser("make")
    mk.add_argument("--repo", required=True)
    mk.add_argument("--dir", required=True, help="directory holding the files under their stable names")
    mk.add_argument("--source", required=True, help="source.json: url, md5, sha256, bytes, replication, sequence, fetched")
    mk.add_argument("--gates", required=True)
    mk.add_argument("--lite-image", required=True)
    mk.add_argument("--tiles", type=int, required=True)
    mk.add_argument("--tar-bytes", type=int, required=True)
    mk.add_argument("--run-id", type=int)
    mk.add_argument("--run-url")
    mk.add_argument("--run-note", default="")
    mk.add_argument("--factory-commit", default="")
    mk.add_argument("--out", required=True)
    ck = sub.add_parser("check")
    ck.add_argument("manifest")
    ck.add_argument("--dir")
    fl = sub.add_parser("files", help="the file names of the release this manifest describes, one per line")
    fl.add_argument("manifest")
    a = ap.parse_args()
    if a.cmd == "files":
        print("\n".join(release_files(json.load(open(a.manifest, encoding="utf-8")))))
        return 0
    return make(a) if a.cmd == "make" else check(a)


if __name__ == "__main__":
    sys.exit(main())
