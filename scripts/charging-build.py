#!/usr/bin/env python3
"""Build and check the European charging catalogue (wedrive-charging/1).

    charging-build.py build <regional.json> <OCM dir> <OSM dir> <OUT>
    charging-build.py validate <OUT> <regional.json> <previous manifest.json | -> [--no-min-ocm]

OCM dir = charging-ocm-fetch.py's output (referencedata.json, <CC>.jsonl.gz, report.json).
OSM dir = charging-osm-extract.sh's output (osm.geojsonseq, osm-source.json).

build writes OUT/charging.sqlite, OUT/charging.sqlite.gz, OUT/charging.json (the descriptor
charging-manifest.py puts into manifest.json) and OUT/report.json. Identical inputs give identical
bytes: nothing in the file comes from the clock of the build — `created` IS the OCM fetch time.

validate runs the gates (plan §6.6) and exits 1 when any fails: integrity, enums, no removed
station, licences, kW range, the minimum for Moldova, and against the PREVIOUS release — a country
with 50+ OCM stations losing more than 20 %, or the whole of OCM falling below 90 %, fails the
publish (a broken fetch must not replace a good catalogue). Above 150 MB sqlite / 60 MB gz it
stops and says so.
"""
import gzip, importlib.util, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import charging_db as cdb  # noqa: E402

_spec = importlib.util.spec_from_file_location("charging_ocm_fetch", os.path.join(HERE, "charging-ocm-fetch.py"))
ocm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ocm)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


AREA_TYPES = ("Polygon", "MultiPolygon")


def read_osm(path):
    """geojsonseq -> features, one per OSM object (europe and russia overlap: the first wins).

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


def build(cfg_path, ocm_dir, osm_dir, out, providers_path=None, connectors_path=None):
    cfg = load_json(cfg_path)
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
    for name, obj in (("charging.json", desc), ("report.json", report)):
        with open(os.path.join(out, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
    return desc, report


def previous_counts(path):
    if not path or path == "-" or not os.path.isfile(path):
        return None
    c = (load_json(path).get("charging") or {}).get("counts") or {}
    return c.get("ocm_by_country")


def main(argv):
    if len(argv) >= 5 and argv[0] == "build":
        desc, rep = build(*argv[1:5])
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
        return 0
    if len(argv) >= 4 and argv[0] == "validate":
        out, cfg = argv[1], load_json(argv[2])
        min_ocm = {} if "--no-min-ocm" in argv else cdb.MIN_OCM
        problems, notes = cdb.validate(os.path.join(out, "charging.sqlite"), os.path.join(out, "charging.sqlite.gz"),
                                       cfg, previous_counts(argv[3]), min_ocm)
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
