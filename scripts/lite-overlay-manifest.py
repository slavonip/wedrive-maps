# -*- coding: utf-8 -*-
"""Put the Europe Lite OVERLAY (overview map + settlements catalogue) into europe-lite.json.

    lite-overlay-manifest.py <europe-lite.json> <overview.json> <places.json> <repo> <tag>

Sets europe-lite.json["overview"] and ["places"] and nothing else: the graph, its features, its
gates and its tag are left exactly as they are. The two files live in their OWN release
(`lite-overlay-<date>`), so each section names its release, and the Lite factory carries both
sections forward when it publishes a new graph (tools/europe-lite/factory/lite_manifest.py), so a
monthly Lite run never drops them.

The sections, as the app reads them (data/RegionalInstaller.kt, LiteOffer):

    "overview": {"file": "overview.pmtiles", "url", "bytes", "sha256", "release", "build", "date",
                 "maxzoom", "layers", "schema"}
    "places":   {"format": "wedrive-lite-places/1", "file": "places.sqlite.gz", "url", "bytes",
                 "sha256" (of the .gz), "sqlite_bytes", "sqlite_sha256" (of the unpacked file),
                 "release", "rows", "counts" {CC: rows}, "kinds", "excluded"}
"""
import json, re, sys

SHA = re.compile(r"^[0-9a-f]{64}$")
OVERVIEW_FILE = "overview.pmtiles"
PLACES_FILE = "places.sqlite.gz"
PLACES_FORMAT = "wedrive-lite-places/1"


def url(repo, tag, name):
    return "https://github.com/%s/releases/download/%s/%s" % (repo, tag, name)


def sections(overview, places, repo, tag):
    if not re.match(r"^lite-overlay-\d{4}-\d{2}-\d{2}(-\d+)?$", tag):
        raise SystemExit("tag %r is not lite-overlay-YYYY-MM-DD[-N]" % tag)
    ov = {"file": OVERVIEW_FILE, "url": url(repo, tag, OVERVIEW_FILE), "bytes": overview["size"],
          "sha256": overview["sha256"], "release": tag}
    for k in ("build", "date", "maxzoom", "layers", "schema"):
        if k in overview:
            ov[k] = overview[k]
    pl = {"format": PLACES_FORMAT, "file": PLACES_FILE, "url": url(repo, tag, PLACES_FILE), "bytes": places["bytes"],
          "sha256": places["sha256"], "sqlite_bytes": places["sqlite_bytes"], "sqlite_sha256": places["sqlite_sha256"],
          "release": tag, "rows": places["rows"], "counts": places["counts"], "kinds": places["kinds"],
          "excluded": places["excluded"]}
    return ov, pl


def validate(m):
    """Problems of the overlay sections of a manifest (absent sections are no problem)."""
    p = []
    ov, pl = m.get("overview"), m.get("places")
    for key, sec, name in (("overview", ov, OVERVIEW_FILE), ("places", pl, PLACES_FILE)):
        if sec is None:
            continue
        if sec.get("file") != name:
            p.append("%s.file is %r, the contract says %r" % (key, sec.get("file"), name))
        rel = str(sec.get("release", ""))
        if not rel.startswith("lite-overlay-"):
            p.append("%s.release %r is not a lite-overlay release" % (key, rel))
        if not str(sec.get("url", "")).endswith("/releases/download/%s/%s" % (rel, name)):
            p.append("%s.url does not point at %s/%s" % (key, rel, name))
        if not isinstance(sec.get("bytes"), int) or sec["bytes"] <= 0:
            p.append("%s.bytes invalid" % key)
        if not SHA.match(str(sec.get("sha256", ""))):
            p.append("%s.sha256 malformed" % key)
    if ov is not None and ov.get("maxzoom") is not None and not isinstance(ov["maxzoom"], int):
        p.append("overview.maxzoom invalid")
    if pl is not None:
        if pl.get("format") != PLACES_FORMAT:
            p.append("places.format is %r" % pl.get("format"))
        if not SHA.match(str(pl.get("sqlite_sha256", ""))):
            p.append("places.sqlite_sha256 malformed")
        if not isinstance(pl.get("sqlite_bytes"), int) or pl["sqlite_bytes"] <= 0:
            p.append("places.sqlite_bytes invalid")
        if not isinstance(pl.get("counts"), dict) or not pl["counts"]:
            p.append("places.counts missing")
    return p


def main(argv):
    if len(argv) != 6:
        print(__doc__)
        return 2
    mpath, opath, ppath, repo, tag = argv[1:]
    m = json.load(open(mpath, encoding="utf-8"))
    if m.get("kind") != "europe-lite":
        raise SystemExit("%s is not a europe-lite manifest" % mpath)
    ov, pl = sections(json.load(open(opath, encoding="utf-8")), json.load(open(ppath, encoding="utf-8")), repo, tag)
    old = (m.get("overview") or {}).get("release")
    m["overview"], m["places"] = ov, pl
    problems = validate(m)
    for x in problems:
        print("PROBLEM:", x)
    if problems:
        return 1
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1, sort_keys=True)
        f.write("\n")
    print("europe-lite.json overlay: %s -> %s (overview %d bytes, places %d rows, %d bytes)"
          % (old, tag, ov["bytes"], pl["rows"], pl["bytes"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
