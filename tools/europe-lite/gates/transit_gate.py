# -*- coding: utf-8 -*-
"""Transit gate: did a lifted destination_only open a land through-route?

For every way the structural gate listed as having lost destination_only on an explicit access
(lifted.json), 12 pairs of points on rings of 1.5 and 4 km around it are routed on the new and the
baseline graph. A FAIL is a land-only new route (no ferry in the manoeuvres) that passes THROUGH a
lifted way which the baseline route did not use.

Usage (GRAPH = DIR or DIR:CONFIG, see vh.py):
  transit_gate.py --baseline /data/prod:c_europe2.json --new /data/lite --gates /data/lite/gates
  (--gates is the struct_gate output directory: lifted.json and conn_src.opl)
  --baseline-manifest FILE / --record-baseline FILE: as in struct_gate.py
Exit code 0 on PASS, 1 on FAIL, 2 when a baseline manifest does not cover the pairs.
"""
import argparse, json, math, os, random, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vh import svc, load_manifest, record_manifest, not_covered


def offs(lat, lon, m, brg):
    dd = m / 6371000.0; b = math.radians(brg); la = math.radians(lat); lo = math.radians(lon)
    la2 = math.asin(math.sin(la) * math.cos(dd) + math.cos(la) * math.sin(dd) * math.cos(b))
    return math.degrees(la2), math.degrees(lo + math.atan2(math.sin(b) * math.sin(dd) * math.cos(la),
                                                           math.cos(dd) - math.sin(la) * math.sin(la2)))


def route(cfg, a, b):
    r, _ = svc(cfg, "route", {"locations": [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}], "costing": "auto"})
    if "trip" not in r:
        return None
    t = r["trip"]
    ferry = any(m.get("travel_type") == "ferry" or m.get("type") in (28, 29) for L in t["legs"] for m in L["maneuvers"])
    ta, _ = svc(cfg, "trace_attributes", {"encoded_polyline": t["legs"][0]["shape"], "shape_match": "edge_walk",
                "costing": "auto", "filters": {"attributes": ["edge.way_id"], "action": "include"}})
    return round(t["summary"]["length"], 3), ferry, [str(e["way_id"]) for e in ta.get("edges", [])]


def main():
    ap = argparse.ArgumentParser(description="Transit gate for lifted destination_only.")
    b = ap.add_mutually_exclusive_group(required=True)
    b.add_argument("--baseline", help="baseline GRAPH: DIR or DIR:CONFIG")
    b.add_argument("--baseline-manifest", help="recorded baseline answers instead of a graph (vh.py)")
    ap.add_argument("--record-baseline", help="with --baseline: also write its answers to this manifest")
    ap.add_argument("--new", required=True)
    ap.add_argument("--gates", required=True, help="struct_gate --out directory (host path)")
    a = ap.parse_args()
    lifted = set(json.load(open(os.path.join(a.gates, "lifted.json"))))
    nodes, ways = {}, {}
    for l in open(os.path.join(a.gates, "conn_src.opl"), encoding="utf-8"):
        p = l.split(); d = {f[0]: f[1:] for f in p[1:]}
        if p[0][0] == "n" and "x" in d:
            nodes[p[0][1:]] = (float(d["y"]), float(d["x"]))
        elif p[0][0] == "w" and p[0][1:] in lifted:
            ways[p[0][1:]] = [r[1:] for r in d["N"].split(",")]
    plan = {}
    for w in sorted(lifted):
        pts = [nodes[n] for n in ways.get(w, []) if n in nodes]
        if not pts:
            plan[w] = None; continue
        c = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        ring = [offs(c[0], c[1], m, b) for m in (1500, 4000) for b in range(0, 360, 45)]
        pairs = [(ring[i], ring[j]) for i in range(len(ring)) for j in range(len(ring)) if i != j]
        random.Random(w).shuffle(pairs)
        plan[w] = (c, pairs[:12])

    # A baseline answer is used only for the very pair it was recorded for; checked before any
    # routing, so an uncovered run stops at once instead of half-way through.
    M = rec = None
    if a.baseline_manifest:
        M = load_manifest(a.baseline_manifest, "transit")
        missing = [w for w, p in plan.items() if p is not None and
                   (w not in M or [[list(x), list(y)] for x, y in p[1]] != [e[:2] for e in M[w]])]
        if missing:
            not_covered("lifted ways (or their pairs)", missing)
    elif a.record_baseline:
        rec = {}

    tot = land = bad = 0
    for w in sorted(lifted):
        if plan[w] is None:
            print("w%s: no coordinates" % w); bad += 1; continue
        c, pairs = plan[w]
        nb = 0
        for i, (x, y) in enumerate(pairs):
            n = route(a.new, x, y)
            if M is not None:
                o = M[w][i][2]
            else:
                o = route(a.baseline, x, y)
                if rec is not None:
                    rec.setdefault(w, []).append([list(x), list(y), list(o) if o else None])
            tot += 1
            if n is None or n[1]:
                continue
            land += 1
            hits = sorted(lifted & set(n[2][1:-1]))
            old_hits = sorted(lifted & set(o[2][1:-1])) if o else []
            new_transit = [h for h in hits if h not in old_hits]
            if new_transit:
                nb += 1; bad += 1
                print("   TRANSIT w%s: %s -> %s new %.3f km through %s; baseline %s" % (w, x, y, n[0], new_transit, o[0] if o else None))
        print("w%-11s centre %.4f,%.4f  pairs 12, new transit %d" % (w, c[0], c[1], nb), flush=True)
    if rec is not None:
        record_manifest(a.record_baseline, "transit", a.baseline, rec)
    print("pairs %d, land-only %d, new transits through lifted destonly: %d" % (tot, land, bad))
    print("TRANSIT GATE:", "PASS" if bad == 0 else "FAIL")
    sys.exit(0 if bad == 0 else 1)


if __name__ == "__main__":
    main()
