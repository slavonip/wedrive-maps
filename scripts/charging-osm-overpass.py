#!/usr/bin/env python3
"""Charging stations of every regional.json country from OpenStreetMap, ONLY the stations.

    charging-osm-overpass.py <regional.json> <OUT dir> <provenance.jsonl> [--only CC,CC] [--cache DIR]

One Overpass query per country, strictly one after another (never in parallel): the country's
bbox from regional.json, RU through `poly:` built from its polygon (its bbox spans the whole
antimeridian), never a bbox. Each answer is written to OUT/<CC>.osm with `out meta`, so the
versions are there when the countries are merged where their bboxes overlap.

A country that Overpass will not serve — after ATTEMPTS tries with exponential backoff on each
endpoint in turn — falls back to its own dated Geofabrik extract (regional-precheck.py `source`,
md5-checked), filtered by osmium with the same nine keys into OUT/<CC>.osm.pbf. If that fails too,
the run stops with exit 1 (fail closed: no catalogue is built from a partial Europe).

An answer counts as a failure when it is not 200, when it carries a <remark> (Overpass reports a
runtime error or a timeout that way, with status 200 and a partial body), when it does not end in
</osm>, or when it does not name its data date (<meta osm_base>).

Each country appends one provenance row to provenance.jsonl:
{name, url, md5, sha256, bytes, replication, sequence, stations} — url is the Overpass endpoint or
the Geofabrik URL, md5/sha256/bytes describe what was downloaded, replication is Overpass's
osm_base or the PBF header's timestamp, stations counts the charging objects themselves.

--cache DIR keeps every country that succeeded (its <CC>.osm or <CC>.osm.pbf, that file's own
sha256 and its provenance row, written atomically), and a country with a complete entry there is
reused — copied into OUT, its row appended, no network. So a run killed by one country loses none
of the countries it already had. An entry whose file does not match its recorded sha256, or that
is incomplete, is ignored and the country fetched again. Freshness is the caller's job: the
workflow restores only the same UTC day's cache.

Standard library only; osmium and curl are needed for the Geofabrik fallback alone.
"""
import hashlib, http.client, importlib.util, json, os, re, shutil, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

ENDPOINTS = ("https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter")
# generic and polite: names the tool, carries nothing about whoever runs it
USER_AGENT = "wedrive-charging-factory/1 (OSM charging stations, one query per country, sequential)"
PREFIXES = ("disused", "construction", "proposed", "planned", "abandoned", "demolished", "removed", "razed")
KEYS = ("amenity",) + tuple(p + ":amenity" for p in PREFIXES)   # the same nine as charging-osm-extract.sh
VALUE = "charging_station"
POLY_COUNTRIES = ("RU",)
ATTEMPTS = 3                 # per endpoint
BACKOFF = 30.0               # seconds before the 2nd try, doubled after each failure
BACKOFF_MAX = 300.0
HTTP_TIMEOUT = 660           # the query's own [timeout:600], plus room for the answer to arrive
PAUSE = 2.0                  # between two countries


class Failure(Exception):
    pass


def area_clauses(cc, cfg, root=ROOT):
    """-> the area filters of one country: ['(S,W,N,E)'] or one ['(poly:"lat lon ...")'] per part."""
    c = cfg["countries"][cc]
    if cc not in POLY_COUNTRIES:
        w, s, e, n = c["bbox"]
        return ["(%s,%s,%s,%s)" % (s, w, n, e)]
    geo = json.load(open(os.path.join(root, c["polygon"]), encoding="utf-8"))
    g = geo.get("geometry", geo)
    polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
    out = []
    for poly in polys:
        ring = poly[0]                               # the outer ring; poly: has no holes
        if ring and ring[0] == ring[-1]:
            ring = ring[:-1]
        if len(ring) < 3:
            continue
        out.append('(poly:"%s")' % " ".join("%.5f %.5f" % (y, x) for x, y in ring))
    if not out:
        raise Failure("%s: polygon %s has no usable ring" % (cc, c["polygon"]))
    return out


def query(clauses):
    life = "^(%s):amenity$" % "|".join(PREFIXES)
    body = []
    for a in clauses:
        body.append("  nwr[amenity=%s]%s;" % (VALUE, a))
        body.append('  nwr[~"%s"~"^%s$"]%s;' % (life, VALUE, a))
    return "[out:xml][timeout:600];\n(\n%s\n);\n(._;>;);\nout meta;\n" % "\n".join(body)


def check_body(body):
    """-> osm_base of a good answer, or Failure."""
    text = body.decode("utf-8", "replace")
    if "<remark" in text:
        m = re.search(r"<remark>(.*?)</remark>", text, re.S)
        raise Failure("Overpass remark: %s" % ((m.group(1) if m else text[text.find("<remark"):][:200]).strip()[:200]))
    if not text.rstrip().endswith("</osm>"):
        raise Failure("answer does not end in </osm> (%d bytes, truncated)" % len(body))
    m = re.search(r'<meta\s[^>]*osm_base="([^"]+)"', text)
    if not m:
        raise Failure("answer names no osm_base: its data date cannot be named")
    try:                                    # well-formed, or it is a retry, not a traceback later
        ET.fromstring(body)
    except ET.ParseError as e:
        raise Failure("answer is not well-formed XML: %s" % e)
    return m.group(1)


def http_post(endpoint, q, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(endpoint, data=urllib.parse.urlencode({"data": q}).encode(),
                                 headers={"User-Agent": USER_AGENT,
                                          "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise Failure("HTTP %s" % r.status)
        return r.read()


def overpass(cc, q, endpoints=ENDPOINTS, attempts=ATTEMPTS, post=http_post, sleep=time.sleep, log=print):
    """-> (endpoint, body, osm_base), or Failure after `attempts` tries on every endpoint."""
    for ep in endpoints:
        delay = BACKOFF
        for i in range(1, attempts + 1):
            wait = delay
            try:
                body = post(ep, q)
                return ep, body, check_body(body)
            except urllib.error.HTTPError as e:
                why = "HTTP %s" % e.code
                ra = (e.headers or {}).get("Retry-After") if e.headers is not None else None
                if ra and str(ra).strip().isdigit():
                    wait = min(max(float(ra), delay), BACKOFF_MAX)
            # HTTPException: a transfer cut short (IncompleteRead) or a bad status line — neither is
            # an OSError, and both are the real-world shape of a truncated answer
            except (Failure, urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
                why = "%s: %s" % (type(e).__name__, e)
            log("   %s: %s attempt %d/%d failed: %s" % (cc, ep, i, attempts, why), file=sys.stderr)
            if i < attempts:
                sleep(wait)
                delay = min(delay * 2, BACKOFF_MAX)
    raise Failure("%s: Overpass failed %d times on each of %s" % (cc, attempts, ", ".join(endpoints)))


def count_xml(path):
    """The charging objects themselves (not the nodes their ways reference)."""
    n = 0
    for _, el in ET.iterparse(path):
        if el.tag in ("node", "way", "relation"):
            tags = {t.get("k"): t.get("v") for t in el.iter("tag")}
            if any(tags.get(k) == VALUE for k in KEYS):
                n += 1
            el.clear()
    return n


def digest(path):
    m, s = hashlib.md5(), hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            m.update(b); s.update(b)
    return m.hexdigest(), s.hexdigest()


def _precheck():
    spec = importlib.util.spec_from_file_location("regional_precheck", os.path.join(HERE, "regional-precheck.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def geofabrik_source(path):
    return _precheck().geofabrik_source(path)


def curl(url, dst):
    subprocess.run(["curl", "-fsSL", "--retry", "3", "-C", "-", "-o", dst, url], check=True)


def osmium(*args):
    return subprocess.run(["osmium"] + list(args), check=True, capture_output=True, text=True).stdout.strip()


FILTER = ["nwr/%s=%s" % (k, VALUE) for k in KEYS]


def geofabrik(cc, cfg, out_dir, source=geofabrik_source, fetch=curl, run=osmium, log=print):
    """-> provenance row of <CC>.osm.pbf filtered from the country's dated Geofabrik extract."""
    path = cfg["countries"][cc]["geofabrik"]
    src = source(path)
    url, md5 = src["url"], src["md5"]
    base = path.rsplit("/", 1)[-1]
    if not re.fullmatch(re.escape(base) + r"-[0-9]{6}\.osm\.pbf", url.rsplit("/", 1)[-1]):
        raise Failure("%s is not a dated Geofabrik extract" % url)
    pbf = os.path.join(out_dir, "%s-geofabrik.osm.pbf" % cc)
    ok = False
    for attempt in (1, 2):
        fetch(url, pbf + ".part")
        if os.path.isfile(pbf + ".part") and digest(pbf + ".part")[0] == md5:
            ok = True
            break
        if os.path.exists(pbf + ".part"):
            os.remove(pbf + ".part")
        log("   %s: md5 of %s does not match %s (attempt %d)" % (cc, url, md5, attempt), file=sys.stderr)
    if not ok:
        raise Failure("Geofabrik never served a consistent %s" % url)
    os.replace(pbf + ".part", pbf)
    try:
        rep = run("fileinfo", "-g", "header.option.osmosis_replication_timestamp", pbf)
        if not rep:
            raise Failure("%s has no replication timestamp — its data date cannot be named" % pbf)
        _, sha = digest(pbf)
        size = os.path.getsize(pbf)
        out = os.path.join(out_dir, "%s.osm.pbf" % cc)
        run("tags-filter", "--overwrite", "-o", out, pbf, *FILTER)
        tagged = os.path.join(out_dir, "%s-tagged.osm.pbf" % cc)
        run("tags-filter", "--overwrite", "-R", "-o", tagged, out, *FILTER)
        n = sum(int(run("fileinfo", "-e", "-g", "data.count." + t, tagged) or 0) for t in ("nodes", "ways", "relations"))
        if os.path.exists(tagged):
            os.remove(tagged)
    finally:
        if os.path.exists(pbf):
            os.remove(pbf)
    return {"name": cc.lower(), "url": url, "md5": md5, "sha256": sha, "bytes": size,
            "replication": rep, "sequence": None, "stations": n}


def one(cc, cfg, out_dir, root=ROOT, overpass_fn=overpass, geofabrik_fn=geofabrik, log=print):
    """Fetch one country -> its provenance row (Overpass, else Geofabrik), or Failure."""
    for stale in ("%s.osm" % cc, "%s.osm.pbf" % cc):
        if os.path.exists(os.path.join(out_dir, stale)):
            os.remove(os.path.join(out_dir, stale))
    q = query(area_clauses(cc, cfg, root))
    try:
        ep, body, base = overpass_fn(cc, q)
    except Failure as e:
        log("   %s: %s — falling back to the Geofabrik extract" % (cc, e), file=sys.stderr)
        try:
            return geofabrik_fn(cc, cfg, out_dir)
        except (Failure, SystemExit, subprocess.CalledProcessError, OSError, KeyError) as g:
            raise Failure("%s: Overpass and Geofabrik both failed (%s)" % (cc, g))
    path = os.path.join(out_dir, "%s.osm" % cc)
    with open(path + ".part", "wb") as f:
        f.write(body)
    os.replace(path + ".part", path)
    md5, sha = digest(path)
    return {"name": cc.lower(), "url": ep, "md5": md5, "sha256": sha, "bytes": len(body),
            "replication": base, "sequence": None, "stations": count_xml(path)}


OUTPUTS = ("%s.osm", "%s.osm.pbf")


def _atomic_copy(src, dst):
    shutil.copyfile(src, dst + ".part")
    os.replace(dst + ".part", dst)


def cache_put(cache, cc, out_dir, row):
    """Keep a country that succeeded: its output file, the file's own sha256 and its provenance row.
    (The row's sha256 describes what was DOWNLOADED — for Geofabrik the whole extract, not the
    filtered file — so the file gets its own.)"""
    files = [n % cc for n in OUTPUTS if os.path.isfile(os.path.join(out_dir, n % cc))]
    if len(files) != 1:
        return False
    name = files[0]
    os.makedirs(cache, exist_ok=True)
    meta = os.path.join(cache, "%s.json" % cc)
    if os.path.exists(meta):                      # the old entry goes first: never an old row beside a new file
        os.remove(meta)
    _atomic_copy(os.path.join(out_dir, name), os.path.join(cache, name))
    entry = {"file": name, "sha256": digest(os.path.join(cache, name))[1], "row": row}
    with open(meta + ".part", "w", encoding="utf-8") as f:
        json.dump(entry, f, sort_keys=True)
    os.replace(meta + ".part", meta)
    return True


def cache_get(cache, cc, out_dir):
    """-> the provenance row of a complete cache entry (its file copied into out_dir), or None."""
    meta = os.path.join(cache, "%s.json" % cc)
    try:
        with open(meta, encoding="utf-8") as f:
            entry = json.load(f)
        name, want, row = entry["file"], entry["sha256"], entry["row"]
        if name not in [n % cc for n in OUTPUTS] or not isinstance(row, dict) or row.get("name") != cc.lower():
            return None
        src = os.path.join(cache, name)
        if not os.path.isfile(src) or digest(src)[1] != want:
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    for stale in OUTPUTS:
        if os.path.exists(os.path.join(out_dir, stale % cc)):
            os.remove(os.path.join(out_dir, stale % cc))
    _atomic_copy(src, os.path.join(out_dir, name))
    return row


def main(argv, root=ROOT, one_fn=one, sleep=time.sleep, clock=time.monotonic):
    import argparse
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("cfg"); p.add_argument("out"); p.add_argument("prov")
    p.add_argument("--only", default="")
    p.add_argument("--cache", default="")
    try:
        a = p.parse_args(argv)
    except SystemExit:
        print(__doc__)
        return 2
    cfg_path, out_dir, prov = a.cfg, a.out, a.prov
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    codes = sorted(cfg["countries"])
    if a.only:
        want = {c.strip().upper() for c in a.only.split(",") if c.strip()}
        codes = [c for c in codes if c in want]
    os.makedirs(out_dir, exist_ok=True)
    fetched = False                               # PAUSE is owed only between two network fetches
    for cc in codes:
        row = cache_get(a.cache, cc, out_dir) if a.cache else None
        if row is not None:
            with open(prov, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, sort_keys=True) + "\n")
            print("==> %s: from cache (%d charging objects, data %s, %s)"
                  % (cc, row["stations"], row["replication"], row["url"]))
            continue
        if fetched:
            sleep(PAUSE)
        fetched = True
        t = clock()
        try:
            row = one_fn(cc, cfg, out_dir, root)
        except Failure as e:
            print("FAIL: %s" % e, file=sys.stderr)
            return 1
        with open(prov, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
        if a.cache and not cache_put(a.cache, cc, out_dir, row):
            print("   %s: not cached (no single output file in %s)" % (cc, out_dir), file=sys.stderr)
        print("==> %s: %d charging objects, data %s, %d bytes, %.1f s, %s"
              % (cc, row["stations"], row["replication"], row["bytes"], clock() - t, row["url"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
