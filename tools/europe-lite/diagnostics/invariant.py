# -*- coding: utf-8 -*-
"""DIAGNOSTICS ONLY: does the cost Thor used to CHOOSE a route equal the cost of that route?

For each pair, the diagnostic Thor (valhalla_service_tw, see build-diag-image.sh) prints the chosen
connection's estimate and the cost after FormPath/recost_forward. A large difference means the
search compared candidates with wrong prices; this is how the shortcut-through-border-control
defect (patch C) was found: Portsmouth -> Ouistreham 22640.3 vs 24528.9 before C, 23174.2 vs
23174.2 after.

Usage (GRAPH = DIR or DIR:CONFIG, see gates/vh.py):
  invariant.py --graph /data/lite [--graph /data/prev ...] [--image wedrive-valhalla:lite-diag]
"""
import argparse, os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gates"))
from vh import svc, ferries_of

P = {"Portsmouth": (50.8198, -1.0880), "Ouistreham": (49.2800, -0.2489), "Cherbourg": (49.6337, -1.6222),
     "London": (51.5074, -0.1278), "Paris": (48.8566, 2.3522)}
PAIRS = [("Portsmouth", "Ouistreham"), ("Portsmouth", "Cherbourg"), ("London", "Paris")]


def main():
    ap = argparse.ArgumentParser(description="Connection estimate vs recost on chosen routes.")
    ap.add_argument("--graph", action="append", required=True)
    ap.add_argument("--image", default="wedrive-valhalla:lite-diag")
    a = ap.parse_args()
    worst = {g: 0.0 for g in a.graph}
    for x0, y0 in PAIRS:
        for x, y in ((x0, y0), (y0, x0)):
            print("=== %s -> %s" % (x, y))
            for g in a.graph:
                r, err = svc(g, "route", {"locations": [{"lat": P[x][0], "lon": P[x][1]}, {"lat": P[y][0], "lon": P[y][1]}],
                                          "costing": "auto"}, image=a.image, env={"VH_FP": "1", "VH_WATCH": "0/0/0"},
                             service="valhalla_service_tw")
                if "trip" not in r:
                    print("  %-28s no route" % g); continue
                s = r["trip"]["summary"]
                c = re.search(r"FP\| conn .* c=([\d.]+)", err); f = re.search(r"final_cost=([\d.]+)", err)
                if not (c and f):
                    print("  %-28s no FP output (is the image the diagnostic one?)" % g); continue
                cv, fv = float(c.group(1)), float(f.group(1))
                worst[g] = max(worst[g], abs(fv - cv))
                print("  %-28s %7.1f km %6.1f min | connection %8.1f recost %8.1f diff %7.1f | %s"
                      % (g, s["length"], s["time"] / 60, cv, fv, fv - cv, "; ".join(ferries_of(r["trip"]))[:50] or "no ferry"))
    print("largest |recost - connection|:", {g: round(v, 1) for g, v in worst.items()})


if __name__ == "__main__":
    main()
