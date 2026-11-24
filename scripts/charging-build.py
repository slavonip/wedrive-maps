#!/usr/bin/env python3
"""Build and check the European charging catalogue (wedrive-charging/1, and /2 beside it).

    charging-build.py build <regional.json> <OCM dir> <OSM dir> <OUT> [<NAP dir>]
    charging-build.py validate <OUT> <regional.json> <previous manifest.json | -> [--no-min-ocm]

NAP dir = charging-nap-fetch.py's output (national registries). With it, build ALSO writes
OUT/charging-v2.sqlite(.gz), format wedrive-charging/2: the same OSM and OCM records plus the
national registries' sites, their physical EVSEs and the live-feed keys (charging_db SCHEMA2). The
/1 file is built exactly as without it — an app that reads only /1 keeps updating — and
charging.json gains a `v2` block describing the second file. validate checks both.

OCM dir = charging-ocm-fetch.py's output (referencedata.json, <CC>.jsonl.gz, report.json).
OSM dir = charging-osm-extract.sh's output (osm.geojsonseq, osm-source.json).

build writes OUT/charging.sqlite, OUT/charging.sqlite.gz, OUT/charging.json (the descriptor
charging-manifest.py puts into manifest.json) and OUT/report.json. Identical inputs give identical
bytes: nothing in the file comes from the clock of the build — `created` IS the OCM fetch time.

validate runs the gates (plan §6.6) and exits 1 when any fails: integrity, enums, no removed
station, licences, kW range, the minimum for Moldova, and against the PREVIOUS release — a country
with 50+ OCM stations losing more than 20 %, or the whole of OCM falling below 90 %, or a country
with 50+ OSM stations losing more than 20 %, fails the publish (a broken fetch must not replace a
good catalogue). Above 150 MB sqlite / 60 MB gz it
stops and says so.
"""
import gzip, importlib.util, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import charging_db as cdb  # noqa: E402
import charging_nap as nap  # noqa: E402

_spec = importlib.util.spec_from_file_location("charging_ocm_fetch", os.path.join(HERE, "charging-ocm-fetch.py"))
ocm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ocm)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


AREA_TYPES = ("Polygon", "MultiPolygon")


def read_osm(path):
    """geojsonseq -> features, one per OSM object (the country files overlap: the first wins).

    osmium emits a closed way both as a line and as an area ('a' id, see cdb.osm_id); both are the
    same object. The ids are normalised to n/w/r and the AREA geometry is kept when both arrive
    (its centroid is the station, a ring's first vertex is not)."""
    index, out = {}, []
    for line in open(path, encoding="utf-8"):
        line = line.strip().lstrip("\x1e")
        if not line:
            continue
        f = json.loads(line)
        f["id"] = cdb.osm_id(f.get("id"))
        i = index.get(f["id"])
        if i is None:
            index[f["id"]] = len(out)
            out.append(f)
        elif ((f.get("geometry") or {}).get("type") in AREA_TYPES
              and (out[i].get("geometry") or {}).get("type") not in AREA_TYPES):
            out[i] = f
    return out


def build(cfg_path, ocm_dir, osm_dir, out, nap_dir=None, providers_path=None, connectors_path=None,
          nap_sources_path=None, operators_path=None):
    cfg = load_json(cfg_path)
    cdb.ALIASES = cdb.load_aliases(operators_path or os.path.join(ROOT, "charging", "operators.json"))
    providers = load_json(providers_path or os.path.join(ROOT, "charging", "providers.json"))
    connectors = load_json(connectors_path or os.path.join(ROOT, "charging", "connectors.json"))
    ref = load_json(os.path.join(ocm_dir, "referencedata.json"))
    ocm_rep = load_json(os.path.join(ocm_dir, "report.json"))
    osm_src = load_json(os.path.join(osm_dir, "osm-source.json"))
    countries = cdb.Countries(cfg, ROOT)
    allow = {p["id"] for p in providers["allow"]}
    excluded = {p["id"] for p in providers["excluded"]}
    report = {"dropped_licence": 0, "unknown_providers": {}}
    stats = {}

    recs = []
    for f in read_osm(os.path.join(osm_dir, "osm.geojsonseq")):
        r = cdb.osm_record(f, connectors, countries, report)
        if r:
            r["source"] = cdb.OSM_SOURCE_ID
            recs.append(r)
    osm_n = len(recs)
    used = set()
    for cc in sorted(ocm_rep["countries"]):
        path = os.path.join(ocm_dir, "%s.jsonl.gz" % cc)
        if os.path.isfile(path):
            pois = ocm.read_jsonl_gz(path)
        else:                    # the test fixtures keep their records readable, ungzipped
            pois = [json.loads(l) for l in open(path[:-3], encoding="utf-8") if l.strip()]
        for poi in pois:
            pid = poi.get("DataProviderID")
            if pid not in allow:
                report["dropped_licence"] += 1
                if pid not in excluded:
                    report["unknown_providers"][str(pid)] = report["unknown_providers"].get(str(pid), 0) + 1
                continue
            r = ocm.normalise(poi, ref, connectors, cc, stats)
            if r:
                r["source"] = cdb.OCM_SOURCE_BASE + pid
                used.add(pid)
                recs.append(r)
    recs.sort(key=lambda r: (0, cdb.osm_sort_key(r["src_id"])) if r["kind"] == "osm" else (1, (0, int(r["src_id"]))))
    for i, r in enumerate(recs, 1):
        r["id"] = i
    clusters = cdb.cluster(recs)

    for k, v in stats.items():
        if not k.startswith("_"):
            report[k] = v
    by_status, ocm_by, osm_by = {}, {}, {}
    for r in recs:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        d = osm_by if r["kind"] == "osm" else ocm_by
        d[r["country"]] = d.get(r["country"], 0) + 1
    counts = {"stations": len(recs), "clusters": len(set(clusters.values())), "osm": osm_n,
              "ocm": len(recs) - osm_n, "status": dict(sorted(by_status.items())),
              "ocm_by_country": dict(sorted(ocm_by.items())), "osm_by_country": dict(sorted(osm_by.items())),
              "dropped_removed": stats.get("dropped_removed", 0) + report.get("dropped_removed_osm", 0),
              "dropped_licence": report["dropped_licence"]}

    fetched = ocm_rep["fetched"]
    build_id = fetched[:10]
    meta = {"format": cdb.FORMAT, "build": build_id, "created": fetched, "ocm_fetched": fetched,
            "osm_replication": json.dumps([s["replication"] for s in osm_src]),
            "osm_md5": json.dumps([s["md5"] for s in osm_src]),
            "counts": json.dumps(counts, sort_keys=True)}
    os.makedirs(out, exist_ok=True)
    sq, gz = os.path.join(out, "charging.sqlite"), os.path.join(out, "charging.sqlite.gz")
    cdb.write(sq, meta, cdb.source_rows(providers, used), recs, clusters, ref, connectors)
    cdb.gzip_file(sq, gz)
    desc = {"format": cdb.FORMAT, "build": build_id, "file": "charging.sqlite.gz",
            "bytes": os.path.getsize(gz), "sha256": cdb.sha256(gz),
            "sqlite_bytes": os.path.getsize(sq), "sqlite_sha256": cdb.sha256(sq),
            "counts": counts,
            "sources": {"ocm": {"fetched": fetched, "requests": ocm_rep.get("requests")},
                        "osm": [{k: s.get(k) for k in ("name", "url", "md5", "replication")} for s in osm_src]}}
    if nap_dir:
        desc["v2"] = build_v2(cfg, countries, providers, used, recs, ref, connectors, meta, nap_dir, out,
                              nap_sources_path or os.path.join(ROOT, "charging", "nap-sources.json"), report)
    for name, obj in (("charging.json", desc), ("report.json", report)):
        with open(os.path.join(out, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
    return desc, report


def ocm_refs(recs, sources):
    """An OCM record copied from a national registry carries that registry's id as its
    DataProvidersReference: give it the NAP's ref scheme, so the NAP site can find it (dedup step b)."""
    by_provider = {s["ocm_provider"]: s for s in sources if s.get("ocm_provider")}
    for r in recs:
        src = by_provider.get(r.get("provider_id"))
        ref = r.get("provider_ref")
        if r["kind"] != "ocm" or not src or not ref:
            continue
        if src["ref_scheme"] == "LT-site":
            m = re.search(r"(\d+)$", ref)
            key = m.group(1) if m else None
        else:
            key = nap.ref_key(ref)
        if key:
            r["refs"] = [(src["ref_scheme"], key)]


def build_v2(cfg, countries, providers, used, recs, ref, connectors, meta, nap_dir, out, nap_sources_path, report):
    """The /2 file: the /1 records plus every national registry fetched, clustered together."""
    srcs = load_json(nap_sources_path)["sources"]
    nrep = load_json(os.path.join(nap_dir, "report.json"))
    by_country = {}
    nap_recs, failed = [], []
    for src in srcs:
        info = nrep["sources"].get(src["id"]) or {}
        path = os.path.join(nap_dir, src["id"] + ".bin")
        if not info.get("ok") or not os.path.isfile(path):
            failed.append(src["id"])
            continue
        data = open(path, "rb").read()
        spath = os.path.join(nap_dir, src["id"] + ".status.bin")
        extra = open(spath, "rb").read() if os.path.isfile(spath) else None
        for r in nap.load(src, data, cdb.ALIASES, report, extra):
            if not countries.covers(r["lat"], r["lon"]) or r["country"] not in countries.bbox:
                report["dropped_outside_nap"] = report.get("dropped_outside_nap", 0) + 1
                continue
            nap_recs.append(r)
    report["nap_failed"] = failed
    ocm_refs(recs, srcs)
    nap_recs.sort(key=lambda r: (r["source"], r["src_id"]))
    base = len(recs)
    for i, r in enumerate(nap_recs, base + 1):
        r["id"] = i
    allrecs = recs + nap_recs
    stats = {}
    clusters = cdb.cluster(allrecs, stats=stats)
    report.update(stats)

    live_source = {s["source_id"]: (s.get("live") or {}).get("source", "") for s in srcs}
    registry = []
    nap_by_src = {}
    for r in nap_recs:
        nap_by_src[r["source"]] = nap_by_src.get(r["source"], 0) + 1
    for s in srcs:
        info = nrep["sources"].get(s["id"]) or {}
        registry.append((s["id"], s["country"], "static", info.get("url") or s.get("url") or s.get("url_from_page"),
                         s["format"], "file", s.get("update_frequency"), nrep["fetched"] if info.get("ok") else None,
                         nap_by_src.get(s["source_id"], 0), s["licence"], s["attribution"], 0, None, s["source_id"]))
        lv = s.get("live")
        if lv:
            registry.append((lv["source"], s["country"], "live", lv["url"], lv["format"], lv["query"], "live", None,
                             None, lv["licence"], lv["attribution"], 0, lv.get("ttl_s"), None))
    registry.sort()
    sources = cdb.source_rows(providers, used) + [
        (s["source_id"], "nap", None, s["name"], s["licence"], s.get("licence_url"), s["attribution"],
         s.get("url") or s.get("url_from_page")) for s in sorted(srcs, key=lambda x: x["source_id"])]

    sq, gz = os.path.join(out, "charging-v2.sqlite"), os.path.join(out, "charging-v2.sqlite.gz")
    rep2 = {}
    cdb.write(sq, dict(meta, format=cdb.FORMAT2, nap_fetched=nrep["fetched"]), sources, allrecs, clusters, ref,
              connectors, extra=lambda db: cdb.write_v2_tables(db, allrecs, clusters, live_source, registry, rep2))
    # the counts go into meta after the tables exist, so they describe what was written
    db = cdb.sqlite3.connect(sq)
    q = lambda sql, *a: db.execute(sql, a).fetchall()
    evse_by = dict(q("SELECT s.country, count(*) FROM evse e JOIN station s ON s.id = e.station "
                     "WHERE e.evse_norm IS NOT NULL GROUP BY 1"))
    keys_by = dict(q("SELECT s.country, count(*) FROM live_key k JOIN station s ON s.id = k.station GROUP BY 1"))
    counts = {"stations": len(allrecs), "clusters": len(set(clusters.values())), "nap": len(nap_recs),
              "nap_by_source": {s["id"]: nap_by_src.get(s["source_id"], 0) for s in srcs},
              "nap_failed": failed,
              "nap_matched": {k[len("nap_"):]: v for k, v in sorted(stats.items())},
              "evse_rows": q("SELECT count(*) FROM evse")[0][0],
              "evse_ids": q("SELECT count(*) FROM evse WHERE evse_norm IS NOT NULL")[0][0],
              "evse_ids_by_country": dict(sorted(evse_by.items())),
              "live_keys_by_country": dict(sorted(keys_by.items())),
              "evse_conflicts": rep2.get("evse_conflicts", 0)}
    db.execute("UPDATE meta SET value = ? WHERE key = 'counts'", (json.dumps(counts, sort_keys=True),))
    db.commit()
    db.execute("VACUUM")
    db.close()
    report["evse_conflicts"] = rep2.get("evse_conflicts", 0)
    cdb.gzip_file(sq, gz)
    return {"format": cdb.FORMAT2, "build": meta["build"], "file": "charging-v2.sqlite.gz",
            "bytes": os.path.getsize(gz), "sha256": cdb.sha256(gz),
            "sqlite_bytes": os.path.getsize(sq), "sqlite_sha256": cdb.sha256(sq), "counts": counts,
            "sources": {"nap": {"fetched": nrep["fetched"],
                                "registries": {k: {x: v.get(x) for x in ("ok", "url", "bytes", "sha256", "last_modified", "error")}
                                               for k, v in sorted(nrep["sources"].items())}}}}


def previous_counts(path):
    """-> (ocm_by_country, osm_by_country) of the previous release; (None, None) without one."""
    if not path or path == "-" or not os.path.isfile(path):
        return None, None
    c = (load_json(path).get("charging") or {}).get("counts") or {}
    return c.get("ocm_by_country"), c.get("osm_by_country")


def previous_nap(path):
    """-> the previous release's national registry stations per source, or None without a /2."""
    if not path or path == "-" or not os.path.isfile(path):
        return None
    v2 = (load_json(path).get("charging") or {}).get("v2") or {}
    return (v2.get("counts") or {}).get("nap_by_source")


def main(argv):
    if len(argv) >= 5 and argv[0] == "build":
        desc, rep = build(*argv[1:6])
        c = desc["counts"]
        print("charging %s: %d stations (%d OSM, %d OCM) in %d clusters; sqlite %d B, gz %d B"
              % (desc["build"], c["stations"], c["osm"], c["ocm"], c["clusters"], desc["sqlite_bytes"], desc["bytes"]))
        print("status %s; dropped: removed %d, licence %d" % (c["status"], c["dropped_removed"], c["dropped_licence"]))
        for cc in sorted(set(c["ocm_by_country"]) | set(c["osm_by_country"])):
            print("   %-3s OCM %6d  OSM %6d" % (cc, c["ocm_by_country"].get(cc, 0), c["osm_by_country"].get(cc, 0)))
        if rep.get("unknown_providers"):
            print("::warning::OCM providers in neither allow nor excluded list (dropped, review their licence): %s"
                  % rep["unknown_providers"])
        for k in ("unknown_connection_types", "unknown_osm_sockets"):
            if rep.get(k):
                print("::warning::%s mapped to OTHER: %s" % (k, rep[k]))
        print("kW carried %s of %s, quantity %s of %s"
              % (rep.get("kw_out", 0), rep.get("kw_in", 0), rep.get("qty_out", 0), rep.get("qty_in", 0)))
        v2 = desc.get("v2")
        if v2:
            c2 = v2["counts"]
            print("charging /2: %d stations (%d national) in %d clusters; sqlite %d B, gz %d B; EVSE rows %d, ids %d, conflicts %d"
                  % (c2["stations"], c2["nap"], c2["clusters"], v2["sqlite_bytes"], v2["bytes"], c2["evse_rows"],
                     c2["evse_ids"], c2["evse_conflicts"]))
            print("national registries %s; matched %s" % (c2["nap_by_source"], c2["nap_matched"]))
            for cc in sorted(set(c2["evse_ids_by_country"]) | set(c2["live_keys_by_country"])):
                print("   %-3s EVSE ids %7d  live keys %6d" % (cc, c2["evse_ids_by_country"].get(cc, 0),
                                                              c2["live_keys_by_country"].get(cc, 0)))
            for f in c2["nap_failed"]:
                print("::warning::national registry %s is not in this catalogue (fetch failed)" % f)
        return 0
    if len(argv) >= 4 and argv[0] == "validate":
        out, cfg = argv[1], load_json(argv[2])
        min_ocm = {} if "--no-min-ocm" in argv else cdb.MIN_OCM
        prev_ocm, prev_osm = previous_counts(argv[3])
        problems, notes = cdb.validate(os.path.join(out, "charging.sqlite"), os.path.join(out, "charging.sqlite.gz"),
                                       cfg, prev_ocm, min_ocm, prev_osm)
        if os.path.isfile(os.path.join(out, "charging-v2.sqlite")):
            p2, n2 = cdb.validate2(os.path.join(out, "charging-v2.sqlite"), os.path.join(out, "charging-v2.sqlite.gz"),
                                   cfg, previous_nap(argv[3]))
            problems += ["/2: " + p for p in p2]
            notes += ["/2: " + n for n in n2]
        for n in notes:
            print("   note: " + n)
        for p in problems:
            print("   PROBLEM: " + p)
        print("CHARGING GATES: " + ("PASS" if not problems else "FAIL"))
        return 1 if problems else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
