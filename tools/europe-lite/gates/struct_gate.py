# -*- coding: utf-8 -*-
"""Structural gate: destination_only and hierarchy level of every connector way, baseline vs new.

Rules (settled 2026-09-24, see docs/europe-lite.md §5):
  - a connector that lost destination_only only because promote.py removed an inferred
    service=parking_aisle/driveway/drive-through is expected;
  - an explicit access=private|destination|customers|delivery|permit|residents may lose it ONLY
    on an edge the stock ferry reclassification lifted (Valhalla #3942/#4118) — detected as
    "level 0 with a class below primary", which nothing else produces;
  - outside ferry reclassification an explicit access keeps destination_only;
  - nothing gains destination_only;
  - the Dover–Calais control ways are in the state the fixes produce.

Usage (GRAPH = DIR or DIR:CONFIG, see vh.py):
  struct_gate.py --baseline /data/prod:c_europe2.json --new /data/lite \
                 --connectors /data/lite/connectors.osm.pbf --out /data/lite/gates
  --baseline-manifest FILE replaces --baseline with recorded answers; --record-baseline FILE writes
  them while running against a baseline graph.
Exit code 0 on PASS, 1 on FAIL, 2 when a baseline manifest does not cover the connector set.
"""
import argparse, collections, json, os, subprocess, sys, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vh import svc, load_manifest, record_manifest, not_covered

AUTO = {"parking_aisle", "driveway", "drive-through"}
LEGAL = {"private", "destination", "customers", "delivery", "permit", "residents"}
RANK = {"motorway": 0, "trunk": 1, "primary": 2}


def main():
    ap = argparse.ArgumentParser(description="Structural gate for Europe Lite connectors.")
    b = ap.add_mutually_exclusive_group(required=True)
    b.add_argument("--baseline", help="baseline GRAPH: DIR or DIR:CONFIG")
    b.add_argument("--baseline-manifest", help="recorded baseline answers instead of a graph (vh.py)")
    ap.add_argument("--record-baseline", help="with --baseline: also write its answers to this manifest")
    ap.add_argument("--new", required=True, help="new GRAPH: DIR or DIR:CONFIG")
    ap.add_argument("--connectors", required=True, help="connectors.osm.pbf of the new build (host path)")
    ap.add_argument("--out", required=True, help="directory for conn_src.opl and lifted.json (host path)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    opl = os.path.join(a.out, "conn_src.opl")
    subprocess.run(["osmium", "cat", "-f", "opl", a.connectors, "-o", opl, "--overwrite"], check=True)

    nodes, ways = {}, {}
    for l in open(opl, encoding="utf-8"):
        p = l.split(); d = {f[0]: f[1:] for f in p[1:]}
        if p[0][0] == "n":
            nodes[p[0][1:]] = (float(d["y"]), float(d["x"]))
        elif p[0][0] == "w":
            t = dict(urllib.parse.unquote(kv.replace("%20%", " ")).split("=", 1)
                     for kv in d.get("T", "").split(",") if "=" in kv)
            ways[p[0][1:]] = ([r[1:] for r in d["N"].split(",")], t)
    locs, meta = [], []
    for w in sorted(ways):
        r = ways[w][0]
        x, y = nodes.get(r[0]), nodes.get(r[1])
        if not x or not y:
            continue
        locs.append({"lat": (x[0] + y[0]) / 2, "lon": (x[1] + y[1]) / 2, "radius": 1}); meta.append(w)

    def probe(cfg):
        out = {}
        for i in range(0, len(locs), 400):
            r, _ = svc(cfg, "locate", {"locations": locs[i:i + 400], "costing": "auto", "verbose": True})
            for w, it in zip(meta[i:i + 400], r):
                for e in it.get("edges") or []:
                    if str(e["edge_info"]["way_id"]) != w:
                        continue
                    o = out.setdefault(w, {"dest": set(), "lvl": set(), "use": set(), "cls": set()})
                    o["dest"].add(e["edge"]["destination_only"]); o["lvl"].add(e["edge_id"]["level"])
                    o["use"].add(e["edge"]["classification"]["use"])
                    o["cls"].add(e["edge"]["classification"]["classification"])
        return out

    if a.baseline:
        O = probe(a.baseline)
        if a.record_baseline:
            record_manifest(a.record_baseline, "struct", a.baseline, {
                w: {"at": [l["lat"], l["lon"]],
                    "edges": {k: sorted(v) for k, v in O[w].items()} if w in O else None}
                for w, l in zip(meta, locs)})
    else:
        # The same questions, answered from the record: per way, the probe point must be the one the
        # answer was recorded at, and a way the baseline did not have is recorded as None.
        M, O, missing = load_manifest(a.baseline_manifest, "struct"), {}, []
        for w, l in zip(meta, locs):
            m = M.get(w)
            if m is None or m["at"] != [l["lat"], l["lon"]]:
                missing.append(w)
            elif m["edges"] is not None:
                O[w] = {k: set(v) for k, v in m["edges"].items()}
        if missing:
            not_covered("connector ways", missing)
    N = probe(a.new)
    print("connector ways %d, found in baseline %d, in new %d" % (len(meta), len(O), len(N)))

    def reclassified(x):
        return x["lvl"] == {0} and not any(c in RANK for c in x["cls"])

    exp_auto = 0; lifted = []; lifted_bad = []; kept = total = 0; ft = []; other = []
    for w in meta:
        if w not in O or w not in N:
            continue
        t = ways[w][1]; od, nd = O[w]["dest"], N[w]["dest"]
        legal = any(t.get(k) in LEGAL for k in ("access", "motor_vehicle", "motorcar", "vehicle"))
        if od == {False} and True in nd:
            ft.append(w)
        if not (True in od and nd == {False}):
            if legal and True in od:
                total += 1; kept += 1
            continue
        if legal:
            total += 1
            (lifted if reclassified(N[w]) else lifted_bad).append(w)
        elif t.get("service") in AUTO or t.get("wedrive:service") in AUTO:
            exp_auto += 1
        else:
            other.append(w)
    print("destonly True->False: inferred service %d; explicit access in ferry reclassification %d; "
          "explicit access OUTSIDE it %d; other %d; False->True %d"
          % (exp_auto, len(lifted), len(lifted_bad), len(other), len(ft)))
    print("explicit access outside ferry reclassification kept destonly: %d of %d" % (kept, total - len(lifted)))
    for w in lifted + lifted_bad:
        t = ways[w][1]
        print("   w%-11s access=%-11s level %s -> %s, class %s, reclassified: %s"
              % (w, t.get("access"), sorted(O[w]["lvl"]), sorted(N[w]["lvl"]), sorted(N[w]["cls"]),
                 "yes" if w in lifted else "NO"))
    json.dump(lifted + lifted_bad, open(os.path.join(a.out, "lifted.json"), "w"))

    ctl = {w: N.get(w, {}) for w in ("167792665", "98821382", "98821391")}
    for w, v in ctl.items():
        print("   control w%-11s %s" % (w, {k: sorted(x) for k, x in v.items()}))
    ok = (not lifted_bad and not other and not ft
          and ctl["167792665"].get("dest") == {False} and "parking_aisle" not in ctl["167792665"].get("use", {"parking_aisle"})
          and ctl["98821382"].get("lvl") == {0} and ctl["98821391"].get("lvl") == {0})
    print("STRUCT GATE:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
