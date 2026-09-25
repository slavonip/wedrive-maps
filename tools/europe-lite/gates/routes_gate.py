# -*- coding: utf-8 -*-
"""Route acceptance: which FERRY a route actually uses, both directions, not "a route exists".

Every pair is routed on the new graph and, if given, on a previous build and on the old
production graph. Verdicts:
  PASS      the expected crossing (or a land route where one is expected), no Channel-Islands
            detour for Britain <-> continent, no detour longer than 2.5x the straight line;
  KNOWN     fails the expectation but is identical to the old production route (pre-existing);
  ACCEPTED  a limitation the owner accepted on 2026-09-24 (listed below with the reason);
  FAIL      anything else.

Usage (GRAPH = DIR or DIR:CONFIG, see vh.py):
  routes_gate.py --new /data/lite [--prev /data/prev] [--prod /data/prod:c_europe2.json | --prod-manifest FILE]
  Without --prod or --prod-manifest there is nothing to call KNOWN, and those pairs FAIL.
Exit code 0 when there is no FAIL.
"""
import argparse, math, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vh import svc, ferries_of, load_manifest, record_manifest, not_covered

P = {"London": (51.5074, -0.1278), "Paris": (48.8566, 2.3522), "Rome": (41.9028, 12.4964),
     "Dover": (51.1279, 1.3134), "Calais": (50.9513, 1.8587), "Dunkerque": (51.0343, 2.3768),
     "Dover pier": (51.1237412, 1.3323926), "Dublin": (53.3498, -6.2603), "Holyhead": (53.3094, -4.6330),
     "Belfast": (54.5973, -5.9301), "Cairnryan": (54.9730, -5.0205), "Newhaven": (50.7920, 0.0550),
     "Dieppe": (49.9229, 1.0775), "Portsmouth": (50.8198, -1.0880), "Cherbourg": (49.6337, -1.6222),
     "Ouistreham": (49.2800, -0.2489), "Plymouth": (50.3755, -4.1427), "Roscoff": (48.7267, -3.9853),
     "Rostock": (54.0924, 12.0991), "Trelleborg": (55.3751, 13.1569), "Gedser": (54.5745, 11.9275),
     "Hirtshals": (57.5881, 9.9594), "Larvik": (59.0533, 10.0352), "Kristiansand": (58.1467, 7.9956),
     "Den Helder": (52.9563, 4.7600), "Texel": (53.0554, 4.7970), "Piombino": (42.9253, 10.5256),
     "Portoferraio": (42.8130, 10.3150), "Messina": (38.1938, 15.5540), "Reggio Calabria": (38.1113, 15.6473),
     "Helsingor": (56.0361, 12.6136), "Helsingborg": (56.0465, 12.6945), "Lyon": (45.7640, 4.8357),
     "Chisinau": (47.0105, 28.8638), "Iasi": (47.1585, 27.6014)}
# (A, B, expected substrings of the ferry name or None for a land route, Britain <-> continent)
PAIRS = [("London", "Paris", ["Calais", "Dunkerque"], True), ("London", "Rome", ["Calais", "Dunkerque"], True),
         ("Dover", "Calais", ["Calais"], True), ("Dover", "Paris", ["Calais"], True),
         ("Dover", "Dunkerque", ["Dunkerque", "Calais"], True), ("London", "Dover pier", None, False),
         ("Dublin", "Holyhead", ["Holyhead"], False), ("Dublin", "London", ["Holyhead"], False),
         ("Belfast", "Cairnryan", ["Cairnryan", "Larne", "E 18"], False), ("Newhaven", "Dieppe", ["Dieppe"], True),
         ("Portsmouth", "Ouistreham", ["Ouistreham"], True), ("Portsmouth", "Cherbourg", ["Cherbourg"], True),
         ("Plymouth", "Roscoff", ["Roscoff"], True),
         ("Rostock", "Trelleborg", ["Trelleborg"], False), ("Rostock", "Gedser", ["Gedser"], False),
         ("Hirtshals", "Larvik", ["Larvik"], False), ("Hirtshals", "Kristiansand", ["Kristiansand"], False),
         ("Den Helder", "Texel", ["Texel"], False),
         ("Piombino", "Portoferraio", ["Piombino", "Portoferraio", "Rio Marina", "Cavo"], False),
         ("Messina", "Reggio Calabria", ["Messina", "Reggio", "Villa San Giovanni", "Tremestieri"], False),
         ("Helsingor", "Helsingborg", ["Helsing"], False), ("Paris", "Lyon", None, False), ("Chisinau", "Iasi", None, False)]
BAD = ("Guernsey", "Jersey", "Peter Port", "Saint Malo", "Saint-Malo", "GBG", "GBJ")
# Accepted by the owner on 2026-09-24 (docs/europe-lite.md §5, "Known limitations of final2").
ACCEPTED = {
    ("Portsmouth", "Cherbourg"): "Cherbourg berth of w10936354 not connected to roads (w320571346 never a connector)",
    ("Ouistreham", "Portsmouth"): "via Calais: longer in time, cheaper by Valhalla cost",
    ("Cherbourg", "Portsmouth"): "via Calais: longer in time, cheaper by Valhalla cost",
}


def hav(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def run(cfg, a, b):
    if not cfg:
        return None
    r, _ = svc(cfg, "route", {"locations": [{"lat": P[a][0], "lon": P[a][1]}, {"lat": P[b][0], "lon": P[b][1]}], "costing": "auto"})
    if "trip" not in r:
        return None
    t = r["trip"]
    return t["summary"]["length"], t["summary"]["time"] / 60, ferries_of(t)


def main():
    ap = argparse.ArgumentParser(description="Route acceptance for Europe Lite.")
    ap.add_argument("--new", required=True)
    ap.add_argument("--prev")
    pg = ap.add_mutually_exclusive_group()
    pg.add_argument("--prod", help="old production GRAPH; KNOWN verdicts need it")
    pg.add_argument("--prod-manifest", help="recorded old-production answers instead of --prod (vh.py)")
    ap.add_argument("--record-prod", help="with --prod: also write its answers to this manifest")
    a = ap.parse_args()
    counts = {"PASS": 0, "KNOWN": 0, "ACCEPTED": 0, "FAIL": 0}
    keys = ["%s|%s" % xy for x0, y0, _, _ in PAIRS for xy in ((x0, y0), (y0, x0))]
    M = load_manifest(a.prod_manifest, "routes") if a.prod_manifest else None
    if M is not None and [k for k in keys if k not in M]:
        not_covered("route pairs", [k for k in keys if k not in M])
    rec = {} if a.prod and a.record_prod else None
    f = lambda r: "no route" if r is None else "%7.1f km %5.0f min %s" % (r[0], r[1], ("; ".join(r[2]) or "no ferry")[:58])
    for x0, y0, exp, bc in PAIRS:
        for x, y in ((x0, y0), (y0, x0)):
            n, p = run(a.new, x, y), run(a.prev, x, y)
            if M is not None:
                o = M["%s|%s" % (x, y)]
            else:
                o = run(a.prod, x, y)
                if rec is not None:
                    rec["%s|%s" % (x, y)] = list(o) if o else None
            why = []
            if n is None:
                why.append("no route")
            else:
                names = " | ".join(n[2])
                if exp is None and n[2]: why.append("ferry where a land route is expected")
                if exp and not any(e in names for e in exp): why.append("wrong ferry")
                if bc and any(bb in names for bb in BAD): why.append("Channel Islands")
                d = hav(P[x], P[y])
                if d > 30 and n[0] > 2.5 * d: why.append("detour x%.1f" % (n[0] / d))
            same_prod = n is not None and o is not None and abs(n[0] - o[0]) < 0.05 and n[2] == o[2]
            if not why: v = "PASS"
            elif (x, y) in ACCEPTED: v = "ACCEPTED"; why.append(ACCEPTED[(x, y)])
            elif same_prod: v = "KNOWN"
            else: v = "FAIL"
            counts[v] += 1
            print("%-28s NEW:  %s" % ("%s -> %s" % (x, y), f(n)))
            if a.prev: print("%-28s PREV: %s" % ("", f(p)))
            if a.prod or M is not None: print("%-28s PROD: %s" % ("", f(o)))
            print("%-28s %s%s" % ("", v, (": " + ", ".join(why)) if why else ""))
    if rec is not None:
        record_manifest(a.record_prod, "routes", a.prod, rec)
    print("TOTAL " + ", ".join("%s %d" % kv for kv in counts.items()))
    print("ROUTES GATE:", "PASS" if counts["FAIL"] == 0 else "FAIL")
    sys.exit(0 if counts["FAIL"] == 0 else 1)


if __name__ == "__main__":
    main()
