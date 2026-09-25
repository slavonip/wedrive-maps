# -*- coding: utf-8 -*-
"""Step 1 of the connector search: the ends of CAR ferries in the filtered Lite.

Reads the ferry ways of Europe Lite as OPL (osmium's text format — plain python, no pyosmium)
and writes the boxes around every car-ferry end, glued on a grid so that a harbour with many
berths becomes one box, not hundreds.

Usage: python3 ferry_ends.py FERRIES.opl OUT_BOXES.geojson OUT_END_NODES.txt
"""
import argparse, json, math, urllib.parse

BOX = 0.025          # ~2.8 km: the longest chain found was 2.4 km


def tags(field):
    out = {}
    for kv in field.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[urllib.parse.unquote(k.replace("%20%", " "))] = \
                urllib.parse.unquote(v.replace("%20%", " "))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ferries_opl")
    ap.add_argument("boxes_geojson")
    ap.add_argument("end_nodes_txt")
    a = ap.parse_args()

    nodes, ferries = {}, []
    for line in open(a.ferries_opl, encoding="utf-8", errors="replace"):
        if line[0] == "n":
            x = y = None
            for f in line.rstrip("\n").split(" "):
                if f[:1] == "x" and len(f) > 1:
                    x = f[1:]
                elif f[:1] == "y" and len(f) > 1:
                    y = f[1:]
            if x and y:
                try:
                    nodes[int(line.split(" ", 1)[0][1:])] = (float(y), float(x))
                except ValueError:
                    pass
        elif line[0] == "w":
            wid = int(line.split(" ", 1)[0][1:])
            t, refs = {}, []
            for f in line.rstrip("\n").split(" "):
                if f[:1] == "T" and len(f) > 1:
                    t = tags(f[1:])
                elif f[:1] == "N" and len(f) > 1:
                    refs = [int(r[1:]) for r in f[1:].split(",") if r[:1] == "n"]
            if refs:
                ferries.append((wid, t, refs))

    # A car ferry unless it says "no"; most route=ferry ways carry no access tags at all.
    car, ped = [], []
    for wid, t, refs in ferries:
        mv = t.get("motor_vehicle", t.get("motorcar", ""))
        (ped if mv == "no" else car).append((wid, t, refs))
    print("ferry ways in Lite: %d, car %d, pedestrian only %d" % (len(ferries), len(car), len(ped)))

    pts, missing = [], 0
    for wid, t, refs in car:
        for nid in (refs[0], refs[-1]):
            if nid in nodes:
                pts.append(nodes[nid])
            else:
                missing += 1
    print("end points: %d (without coordinates %d)" % (len(pts), missing))

    grid = {}
    for lat, lon in pts:
        grid.setdefault((round(lat / BOX), round(lon / BOX)), (lat, lon))
    print("after gluing on the grid: %d boxes" % len(grid))

    polys = []
    for lat, lon in grid.values():
        dlat = BOX
        dlon = BOX / max(0.2, abs(math.cos(math.radians(lat))))
        polys.append([[[lon - dlon, lat - dlat], [lon + dlon, lat - dlat],
                       [lon + dlon, lat + dlat], [lon - dlon, lat + dlat],
                       [lon - dlon, lat - dlat]]])
    with open(a.boxes_geojson, "w") as f:
        json.dump({"type": "Feature", "properties": {},
                   "geometry": {"type": "MultiPolygon", "coordinates": polys}}, f)
    with open(a.end_nodes_txt, "w") as f:
        for wid, t, refs in car:
            f.write("%d %d %d\n" % (wid, refs[0], refs[-1]))
    print("written %s, %s" % (a.boxes_geojson, a.end_nodes_txt))


if __name__ == "__main__":
    main()
