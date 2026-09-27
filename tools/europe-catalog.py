#!/usr/bin/env python3
"""The European country catalog of the regional factory, generated from Geofabrik's own index.

    tools/europe-catalog.py <geofabrik index-v1.json> <regional.json> [--polygons DIR] [--write]

Owner decision A (2026-09-27): the factory builds the WHOLE European catalog, every country
independent. This tool turns Geofabrik's index (https://download.geofabrik.de/index-v1.json,
with geometry) into the `countries` and `borders` of regional.json, so no Geofabrik path, bbox or
neighbour is typed by hand:

  countries   every direct child of `europe`, minus the aggregates that duplicate countries,
              plus Russia (a top-level region in Geofabrik, added on the owner's call). Path from
              the index's own pbf URL, bbox from its polygon, the polygon itself written to
              polygons/<cc>.geojson (the basemap is cut by it, not by the bbox: Norway's rectangle
              holds all of Sweden and Finland).
  region_id   STABLE FOREVER: an id already in regional.json is kept; a new country gets max+1.
              Ids are written into portal tables and graph ids, and 255 is the ceiling.
  borders     every pair whose Geofabrik polygons intersect (Geofabrik cuts with an overlap, so
              neighbours always do), box = the overlap's bounds + 0.2 degrees. Existing borders keep
              their box and gain "legacy": true (pair tables are still published for them only);
              new ones are "required": false until a published run proves they join (ratchet).

Special cases, decided here and nowhere else:
  - aggregates alps, dach, britain-and-ireland and united-kingdom are skipped: they duplicate
    countries (united-kingdom = great-britain + Northern Ireland, which ireland-and-northern-ireland
    also holds, and one place must belong to one country);
  - great-britain -> GB, ireland-and-northern-ireland -> IE (Northern Ireland lives there);
  - no ISO 3166-1 code: kosovo -> XK (the common convention), azores -> XA, guernsey-jersey -> XG
    (ISO user-assigned range), isle-of-man -> IM;
  - San Marino and the Vatican are inside italy; Monaco, Andorra, Liechtenstein stand alone.

Development tool: its output is committed and reviewed. The monthly factory reads regional.json,
never the Geofabrik index (a new country is a reviewed commit, not a surprise in production).
"""
import argparse, json, os, sys

SKIP = {"alps", "dach", "britain-and-ireland", "united-kingdom"}
EXTRA_TOP = {"russia"}
CODE = {"great-britain": "GB", "ireland-and-northern-ireland": "IE", "kosovo": "XK",
        "azores": "XA", "guernsey-jersey": "XG", "isle-of-man": "IM"}
TITLE = {"GB": "Great Britain", "IE": "Ireland and Northern Ireland", "XK": "Kosovo",
         "XA": "Azores", "XG": "Guernsey and Jersey", "IM": "Isle of Man", "MK": "North Macedonia",
         "CZ": "Czechia", "BA": "Bosnia and Herzegovina", "UA": "Ukraine", "RU": "Russia"}


def rings(geom):
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    return polys


def bbox_of(polys):
    xs = [c[0] for p in polys for r in p for c in r]
    ys = [c[1] for p in polys for r in p for c in r]
    return [min(xs), min(ys), max(xs), max(ys)]


def inside(pt, polys):
    """Point in (multi)polygon, even-odd over every ring (holes included)."""
    x, y = pt
    hit = False
    for p in polys:
        for r in p:
            j = len(r) - 1
            for i in range(len(r)):
                xi, yi = r[i][0], r[i][1]
                xj, yj = r[j][0], r[j][1]
                if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
                    hit = not hit
                j = i
    return hit


def overlap(a, b):
    """Bounds of the vertices of either polygon lying inside the other, or None."""
    ba, bb = bbox_of(a), bbox_of(b)
    if ba[2] < bb[0] or bb[2] < ba[0] or ba[3] < bb[1] or bb[3] < ba[1]:
        return None
    pts = [c for p in a for r in p for c in r if inside(c, b)] + \
          [c for p in b for r in p for c in r if inside(c, a)]
    if not pts:
        return None
    xs, ys = [c[0] for c in pts], [c[1] for c in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def catalog(index):
    out = {}
    for f in index["features"]:
        p = f["properties"]
        gid = p["id"]
        if not ((p.get("parent") == "europe" and gid not in SKIP) or gid in EXTRA_TOP):
            continue
        iso = p.get("iso3166-1:alpha2") or []
        code = CODE.get(gid) or (iso[0] if len(iso) == 1 else None)
        if not code:
            raise SystemExit("no code for %s (iso %s): add it to CODE" % (gid, iso))
        url = p["urls"]["pbf"]
        prefix = "https://download.geofabrik.de/"
        if not url.startswith(prefix) or not url.endswith("-latest.osm.pbf"):
            raise SystemExit("%s: unexpected pbf url %s" % (gid, url))
        polys = rings(f["geometry"])
        if code in out:
            raise SystemExit("code %s twice (%s, %s)" % (code, out[code]["geofabrik"], gid))
        out[code] = {"geofabrik": url[len(prefix):-len("-latest.osm.pbf")],
                     "title": TITLE.get(code) or p["name"], "polys": polys}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("index"); ap.add_argument("config")
    ap.add_argument("--polygons", default=None)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    index = json.load(open(a.index, encoding="utf-8"))
    cfg = json.load(open(a.config, encoding="utf-8"))
    cat = catalog(index)

    old = cfg["countries"]
    for code, c in old.items():
        if code not in cat:
            raise SystemExit("%s is in regional.json but not in the catalog: refusing to drop it" % code)
        if c["geofabrik"] != cat[code]["geofabrik"]:
            raise SystemExit("%s: geofabrik %s in regional.json, %s in the index"
                             % (code, c["geofabrik"], cat[code]["geofabrik"]))
    next_id = max(c["region_id"] for c in old.values()) + 1
    countries = {}
    for code in list(old) + sorted(c for c in cat if c not in old):
        c = cat[code]
        if code in old:
            rid, title = old[code]["region_id"], old[code]["title"]
        else:
            rid, title = next_id, c["title"]
            next_id += 1
        if rid > 255:
            raise SystemExit("region ids exhausted at %s" % code)
        w, s, e, n = bbox_of(c["polys"])
        countries[code] = {"region_id": rid, "title": title, "geofabrik": c["geofabrik"],
                           "bbox": [round(w, 2), round(s, 2), round(e, 2), round(n, 2)],
                           "polygon": "polygons/%s.geojson" % code.lower()}
        if code in old and old[code].get("frontier_may_be_empty"):
            countries[code]["frontier_may_be_empty"] = True      # hand-set, islands only

    known = {tuple(sorted(b["between"])): b for b in cfg["borders"]}
    borders = []
    codes = list(countries)
    for i, x in enumerate(codes):
        for y in codes[i + 1:]:
            key = tuple(sorted((x, y)))
            if key in known:
                b = dict(known[key]); b["legacy"] = True; b["required"] = True
                borders.append(b); continue
            ov = overlap(cat[x]["polys"], cat[y]["polys"])
            if not ov:
                continue
            w, s, e, n = ov
            borders.append({"between": [x, y],
                            "box": [round(s - 0.2, 2), round(w - 0.2, 2), round(n + 0.2, 2), round(e + 0.2, 2)],
                            "required": False})

    for code, c in countries.items():
        print("%-3s %3d %-40s %-28s bbox %s" % (code, c["region_id"], c["geofabrik"], c["title"], c["bbox"]))
    print("%d countries, %d borders (%d legacy)" % (len(countries), len(borders),
                                                   sum(1 for b in borders if b.get("legacy"))))
    for b in borders:
        print("   %s-%s%s" % (b["between"][0], b["between"][1], " legacy" if b.get("legacy") else ""))
    if missing := [k for k in known if k not in {tuple(sorted(b["between"])) for b in borders}]:
        raise SystemExit("existing borders lost: %s" % missing)

    if a.write:
        cfg["countries"] = countries
        cfg["borders"] = borders
        with open(a.config, "w", encoding="utf-8", newline="\n") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2); f.write("\n")
        pdir = a.polygons or os.path.join(os.path.dirname(os.path.abspath(a.config)), "polygons")
        os.makedirs(pdir, exist_ok=True)
        for code in countries:
            geo = {"type": "Feature", "properties": {"code": code, "source": "geofabrik index-v1"},
                   "geometry": {"type": "MultiPolygon", "coordinates": cat[code]["polys"]}}
            with open(os.path.join(pdir, "%s.geojson" % code.lower()), "w", encoding="utf-8", newline="\n") as f:
                json.dump(geo, f, separators=(",", ":")); f.write("\n")
        print("written: %s and %d polygons in %s" % (a.config, len(countries), pdir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
