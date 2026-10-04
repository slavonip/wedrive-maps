#!/usr/bin/env python3
"""Which countries the regional factory must rebuild, and why — decided BEFORE anything is built.

    regional-precheck.py <regional.json> <regional-engine.lock> <manifest.json|-> \
        [--countries MD,RO] [--rebuild auto|all|MD,RO] [--md5-json file] [--github-output file]
    regional-precheck.py source <geofabrik path>      {url, md5} of one dated extract (Europe Lite)

A country is rebuilt when (auto):
    - it is not in the current manifest;
    - its Geofabrik extract changed: the MD5 of the DATED file <path>-latest.osm.pbf redirects to
      differs from the one recorded in the manifest (or none is recorded);
    - the engine changed: the manifest records another engine_sha than regional-engine.lock;
    - the timezone database changed: another timezones sha than the lock.
Correctness over economy: an unknown provenance is a reason to rebuild, never to carry.

The rest of the chosen set is CARRIED: its published package is reused, byte for byte.
Nothing to rebuild -> go=false, and the run ends without a release.

--countries restricts the set (control runs: MD, then MD,RO). --rebuild all forces a full build;
a list forces exactly those. --md5-json replaces the Geofabrik lookup ({"MD": "<md5>"}) for tests.

THE RUN IS PINNED TO DATED FILES. `-latest` and `-latest.osm.pbf.md5` are updated separately and can
disagree for hours (run 36367106205: germany-latest -> germany-260927 while its .md5 still named
germany-260926; the graph and map jobs waited 7 x 10 min and failed). So the precheck resolves each
country ONCE — the file `-latest` redirects to, checked against that file's OWN .md5 — and hands
{url, md5} to the graph and map jobs (output sources_json), which download exactly that file. A
dated file never changes, so the two jobs of one country can no longer read different extracts.
"""
import argparse, json, os, sys, urllib.error, urllib.request


def keyvals(path):
    out = {}
    for line in open(path, encoding="utf-8"):
        s = line.split("#", 1)[0].strip()
        if "=" in s:
            k, v = (x.strip() for x in s.split("=", 1))
            out[k] = v
    return out


GEOFABRIK = "https://download.geofabrik.de/"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def md5_of(md5_text, name):
    """The md5 in a Geofabrik .md5 file, which must name exactly `name`."""
    parts = md5_text.split()
    if len(parts) < 2 or len(parts[0]) != 32 or parts[1].lstrip("*") != name:
        raise SystemExit("Geofabrik .md5 does not describe %s: %r" % (name, md5_text[:120]))
    return parts[0]


def dated_name(location, path):
    """The dated file a -latest redirect points to (europe/germany -> germany-260927.osm.pbf)."""
    name = location.rstrip("/").rsplit("/", 1)[-1]
    base = path.rsplit("/", 1)[-1]
    if not (name.startswith(base + "-") and name.endswith(".osm.pbf") and "latest" not in name):
        raise SystemExit("%s-latest redirects to an unexpected file: %s" % (path, location))
    return name


def _latest_location(url):
    """Location of the redirect behind a -latest URL, or None when it does not redirect."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(urllib.request.Request(url, method="HEAD"), timeout=60)
        return None
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308) or not e.headers.get("Location"):
            raise SystemExit("%s: HTTP %s" % (url, e.code))
        return e.headers["Location"]


def _get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode()


def _served_directly(url):
    """True when Geofabrik itself answers 200 for url (no redirect followed)."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, method="HEAD"), timeout=60) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False


def state_date(state_text):
    """YYMMDD of the extract a Geofabrik <region>-updates/state.txt describes, or SystemExit.

    Geofabrik names the dated file after this date: verified 2026-10-02 on FR, RU, MD and RO,
    where state.txt said 2026-10-01T20:22:06Z and -latest redirected to <name>-261001.osm.pbf."""
    for line in state_text.splitlines():
        if line.startswith("timestamp="):
            ts = line.split("=", 1)[1].replace("\\:", ":").strip()
            if len(ts) >= 10 and ts[4] == "-" and ts[7] == "-" and ts[:4].isdigit() and ts[5:7].isdigit() and ts[8:10].isdigit():
                return ts[2:4] + ts[5:7] + ts[8:10]
    raise SystemExit("Geofabrik state.txt has no usable timestamp: %r" % state_text[:120])


def geofabrik_source(path):
    """{url, md5} of the dated file behind <path>-latest.osm.pbf — never an undated source.

    Normally -latest redirects to the dated file itself. Geofabrik may instead send a large
    extract to an external mirror (2026-10-02: germany-latest -> ftp5.gwdg.de/.../germany-latest,
    a mirror that was a day behind). Then the date comes ONLY from Geofabrik's own
    <path>-updates/state.txt, the dated file must be served by Geofabrik directly, and its .md5
    must name it; any of those failing stops the run (fail closed, no guessing)."""
    base = path.rsplit("/", 1)[-1]
    folder = GEOFABRIK + (path.rsplit("/", 1)[0] + "/" if "/" in path else "")
    latest = GEOFABRIK + path + "-latest.osm.pbf"
    loc = _latest_location(latest)
    if loc is None:
        raise SystemExit("%s did not redirect to a dated file" % latest)
    if loc.startswith(GEOFABRIK) or "/" not in loc:
        name = dated_name(loc, path)            # Geofabrik's own redirect: must be dated
    else:
        name = "%s-%s.osm.pbf" % (base, state_date(_get(GEOFABRIK + path + "-updates/state.txt")))
        if not _served_directly(folder + name):
            raise SystemExit("%s-latest redirects to %s and Geofabrik does not serve %s, the dated "
                             "file its state.txt names" % (path, loc, name))
        print("%s-latest redirects to a mirror (%s); pinned to %s from Geofabrik's state.txt"
              % (path, loc, name), file=sys.stderr)
    url = folder + name
    md5 = md5_of(_get(url + ".md5"), name)
    return {"url": url, "md5": md5}


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
    # `regional-precheck.py source <geofabrik path>` prints {url, md5} of the dated file, resolved
    # exactly as for a country. The Europe Lite factory uses it for `europe` (2026-10-03:
    # europe-latest went to ftp5.gwdg.de and europe-latest.osm.pbf.md5 answered 404), so both
    # factories pin their source by one rule and neither ever downloads an undated file.
    if len(sys.argv) == 3 and sys.argv[1] == "source":
        print(json.dumps(geofabrik_source(sys.argv[2]), sort_keys=True))
        return 0
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
        sources = {c: {"url": "", "md5": m} for c, m in md5s.items()}
    else:
        sources = {c: geofabrik_source(cfg["countries"][c]["geofabrik"]) for c in countries}
        md5s = {c: s["md5"] for c, s in sources.items()}
        for c in countries:
            print("%-3s source %s  %s" % (c, sources[c]["md5"], sources[c]["url"]))

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
        "sources_json": json.dumps({c: sources[c] for c in rebuild if c in sources},
                                   separators=(",", ":")),
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
