"""Localities of ONE country for the address index, format wedrive-address/2 (owner, 2026-10-01).

Format /1 tied a street to its locality by NAME: `addr_street.city` was the addr:city string, or the
nearest settlement of the tile-derived place table. Measured on the published indexes: in Moldova
only 68.9 % of streets named exactly one settlement (Hîncești x3, Taraclia x4 share names; Congaz,
Bubuieci are missing from the tile places altogether), and 166 of its 2 559 settlements lay outside
the country (Geofabrik overlap). A driver choosing "Бендеры" and then a street needs the street to
belong to THAT Бендеры, so /2 gives every locality an id and every street a locality id, decided by
geography rather than by string equality:

    1. addr:city naming a locality (its name or any alias) within PLAUSIBLE_KM   -> "name"
    2. the place area (the locality's own polygon in OSM) that contains the house -> "area"
    3. a same-named street of this build, resolved by 1 or 2, within ANCHOR_KM    -> "anchor"
    4. the nearest locality point within NEAR_KM                                 -> "nearest"
    5. otherwise unresolved (locality NULL)

Everything comes from the same country PBF the graph and the addresses are built from, cut to the
country's own admin_level=2 boundary (fallback: the Geofabrik polygon of regional.json), so a
locality or a house of the neighbouring country is never part of this country's index.

Standard library only. Geometry is a grid of cells over each polygon set: a cell no polygon edge
crosses is classified once by its centre, so almost every point is answered by a dictionary lookup
and only points in edge cells pay for an exact even-odd test.
"""
import collections
import json
import math
import os
import re
import subprocess
import sys
import unicodedata

KINDS = ("city", "town", "village", "hamlet")
KIND_RANK = {k: i for i, k in enumerate(KINDS)}
ALIAS_KEYS = ("name:ro", "name:ru", "name:en", "name:uk", "name:hu", "name:de",
              "alt_name", "old_name", "official_name", "short_name", "int_name")
# the admin level whose name tells two same-named localities apart (raion / judeţ / megye / Bezirk)
DISTRICT_LEVEL = {"MD": "4", "RO": "4", "HU": "6", "AT": "6"}
# a level BELOW the district that names the administrative unit a locality belongs to, only where
# the country has one that tells same-named villages apart: Romania's comună / oraș / municipiu
# (401 same-name pairs inside one județ). Not forced on countries where it does not exist or help.
COMMUNE_LEVEL = {"RO": "8"}
PLAUSIBLE_KM = 25.0
ANCHOR_KM = 3.0
NEAR_KM = 15.0
MERGE_KM = 5.0
# "mun. Chișinău", "or. Soroca", "s. Bubuieci", "Municipiul București", "comuna X"
CITY_PREFIX = re.compile(r"^(mun|municipiul|or|oras|orasul|s|sat|satul|c|com|comuna|sec|sectorul|г|пгт|с)\.?\s+", re.I)


def fold(s):
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def name_key(s):
    return " ".join(re.sub(r"[\W_]+", " ", fold(s)).split())


def city_keys(raw):
    """addr:city as written, and without an administrative prefix."""
    k = name_key(raw)
    out = [k]
    stripped = name_key(CITY_PREFIX.sub("", raw.strip()))
    if stripped and stripped != k:
        out.append(stripped)
    return out


def km(a_lat, a_lon, b_lat, b_lon):
    k = math.cos(math.radians((a_lat + b_lat) / 2)) * 111.32
    return math.hypot((b_lon - a_lon) * k, (b_lat - a_lat) * 110.54)


# ---------------------------------------------------------------- geometry


def rings_of(geom):
    """GeoJSON Polygon / MultiPolygon -> every ring (outer and holes) as a list of (x, y)."""
    if geom["type"] == "Polygon":
        return [list(map(tuple, r)) for r in geom["coordinates"]]
    if geom["type"] == "MultiPolygon":
        return [list(map(tuple, r)) for part in geom["coordinates"] for r in part]
    return []


def contains(rings, x, y):
    """Even-odd rule over all rings: holes and multipolygon parts come out right by themselves."""
    inside = False
    for ring in rings:
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i]
            xj, yj = ring[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def ring_area(rings):
    a = 0.0
    for ring in rings:
        s = 0.0
        for i in range(len(ring) - 1):
            s += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
        a += abs(s) / 2
    return a


class Shapes:
    """Polygons with a cell grid: `find(x, y)` -> ids of every polygon containing the point."""

    def __init__(self, cell):
        self.cell = cell
        self.polys = {}          # id -> (rings, bbox)
        self.full = collections.defaultdict(list)    # cell -> ids whose interior covers the whole cell
        self.edge = collections.defaultdict(list)    # cell -> ids whose boundary crosses the cell

    def _c(self, x, y):
        return (math.floor(x / self.cell), math.floor(y / self.cell))

    def add(self, pid, rings):
        if not rings:
            return
        xs = [p[0] for r in rings for p in r]
        ys = [p[1] for r in rings for p in r]
        self.polys[pid] = (rings, (min(xs), min(ys), max(xs), max(ys)))
        edge = set()
        step = self.cell / 8
        for ring in rings:
            for i in range(len(ring) - 1):
                (x0, y0), (x1, y1) = ring[i], ring[i + 1]
                n = max(1, int(max(abs(x1 - x0), abs(y1 - y0)) / step))
                for k in range(n + 1):
                    edge.add(self._c(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n))
        for c in edge:
            self.edge[c].append(pid)
        cx0, cy0 = self._c(min(xs), min(ys))
        cx1, cy1 = self._c(max(xs), max(ys))
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                if (cx, cy) in edge:
                    continue
                if contains(rings, (cx + 0.5) * self.cell, (cy + 0.5) * self.cell):
                    self.full[(cx, cy)].append(pid)

    def find(self, x, y):
        c = self._c(x, y)
        out = list(self.full.get(c, ()))
        for pid in self.edge.get(c, ()):
            rings, (bx0, by0, bx1, by1) = self.polys[pid]
            if bx0 <= x <= bx1 and by0 <= y <= by1 and contains(rings, x, y):
                out.append(pid)
        return out


# ---------------------------------------------------------------- OSM reading


def export(pbf, work, name, filters, geometry_types):
    sub = os.path.join(work, name + ".osm.pbf")
    subprocess.run(["osmium", "tags-filter", "-O", "-o", sub, pbf, *filters], check=True)
    p = subprocess.Popen(["osmium", "export", "-f", "geojsonseq", "-a", "type,id",
                          "--geometry-types=" + geometry_types, sub],
                         stdout=subprocess.PIPE, text=True, encoding="utf-8", stderr=subprocess.DEVNULL)
    for line in p.stdout:
        line = line.lstrip("\x1e").strip()
        if line:
            yield json.loads(line)
    p.wait()


def load_country_shape(pbf, work, country, polygon_path):
    """The country's own admin_level=2 area from the PBF; the Geofabrik polygon when it is missing."""
    shape = Shapes(0.05)
    found = None
    if country:
        for f in export(pbf, work, "country", ["r/admin_level=2"], "polygon"):
            p = f["properties"]
            if p.get("ISO3166-1") == country or p.get("ISO3166-1:alpha2") == country:
                found = rings_of(f["geometry"])
                break
    source = "osm admin_level=2"
    if not found and polygon_path and os.path.exists(polygon_path):
        g = json.load(open(polygon_path, encoding="utf-8"))
        found = rings_of(g.get("geometry", g))
        source = "geofabrik polygon"
    if not found:
        return None, "none"
    shape.add("country", found)
    return shape, source


def load_districts(pbf, work, country):
    return load_admin(pbf, work, "district", DISTRICT_LEVEL.get(country or "", "6"))


def load_admin(pbf, work, what, level):
    """Areas of one admin level: shapes by pid, and the tags of each (name, official_name, ...)."""
    shapes = Shapes(0.02)
    names = {}
    for f in export(pbf, work, what, ["r/admin_level=" + level], "polygon"):
        p = f["properties"]
        # tags-filter also brings in the MEMBER relations of what it matched: a county lists its
        # communes as subareas, so a commune (admin_level=8) arrives here too and must not be the district
        if p.get("boundary") == "administrative" and p.get("admin_level") == level and p.get("name"):
            pid = "%s%s" % (p["@type"][0], p["@id"])
            shapes.add(pid, rings_of(f["geometry"]))
            names[pid] = p["name"] if what == "district" else p
    return shapes, names


def commune_display(tags):
    """"Comuna Albești", "Municipiul Iași", "Oraș Băile Tușnad": official_name, else prefix + name."""
    name = tags["name"]
    full = (tags.get("official_name") or "").strip()
    if not full and tags.get("name:prefix"):
        full = "%s %s" % (tags["name:prefix"].strip(), name)
    full = full or name
    return full[:1].upper() + full[1:]


class Commune:
    __slots__ = ("id", "name", "display", "aliases", "district", "pid")

    def __init__(self, pid, tags, district):
        self.id = 0
        self.pid, self.name, self.district = pid, tags["name"], district
        self.display = commune_display(tags)
        self.aliases = [a for a in aliases_of(tags, self.name) if a != self.display]


def yo_variants(names):
    """Russian writes ё as е more often than not ("Кишинев"), and SQLite's unicode61 does not fold
    Cyrillic: a name with ё gets its е spelling as one more alias, or "кишинев" never finds it."""
    out = []
    for n in names:
        v = n.replace("ё", "е").replace("Ё", "Е")
        if v != n and v not in names and v not in out:
            out.append(v)
    return out


def aliases_of(tags, name):
    out = []
    for k in ALIAS_KEYS:
        for v in (tags.get(k) or "").split(";"):
            v = v.strip()
            if v and name_key(v) != name_key(name) and v not in out and len(v) <= 80:
                out.append(v)
    return out + yo_variants([name] + out)


def population(tags):
    m = re.match(r"\d+", (tags.get("population") or "").replace(" ", "").replace(",", ""))
    return int(m.group(0)) if m else 0


class Locality:
    __slots__ = ("id", "name", "aliases", "kind", "district", "commune", "lat", "lon", "pop", "houses", "osm",
                 "wikidata", "is_in", "area_only")

    def __init__(self, name, aliases, kind, lat, lon, pop, osm, tags=None):
        self.id = 0
        self.name, self.aliases, self.kind = name, aliases, kind
        self.lat, self.lon, self.pop, self.osm = lat, lon, pop, osm
        self.district = ""
        self.commune = None        # Commune, where the country has that level (COMMUNE_LEVEL)
        self.houses = 0
        self.wikidata = (tags or {}).get("wikidata") or ""
        self.is_in = (tags or {}).get("is_in") or ""
        self.area_only = False     # made from a place area that held no place point of its own


class Localities:
    """Every locality of the country, with lookups by name, by containing area and by distance."""

    def __init__(self, items, area_shapes, area_owner, stats):
        self.items = items
        self.area_shapes = area_shapes
        self.area_owner = area_owner               # area pid -> (Locality, area size)
        self.stats = stats
        self.by_name = collections.defaultdict(list)
        for loc in items:
            for n in [loc.name] + loc.aliases:
                k = name_key(n)
                if loc not in self.by_name[k]:
                    self.by_name[k].append(loc)
        self.cells = collections.defaultdict(list)
        for loc in items:
            self.cells[(int(loc.lat * 10), int(loc.lon * 10))].append(loc)

    def by_city(self, raw, lat, lon):
        best, best_d = None, PLAUSIBLE_KM
        for k in city_keys(raw):
            for loc in self.by_name.get(k, ()):
                d = km(lat, lon, loc.lat, loc.lon)
                if d < best_d:
                    best, best_d = loc, d
        return best

    def by_area(self, lat, lon):
        hits = [self.area_owner[pid] for pid in self.area_shapes.find(lon, lat) if pid in self.area_owner] if self.area_shapes else []
        return min(hits, key=lambda h: (h[1], KIND_RANK[h[0].kind]))[0] if hits else None

    def nearest(self, lat, lon, limit_km=NEAR_KM):
        best, best_d = None, limit_km
        cy, cx = int(lat * 10), int(lon * 10)
        r = int(limit_km / 11) + 1
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                for loc in self.cells.get((cy + dy, cx + dx), ()):
                    d = km(lat, lon, loc.lat, loc.lon)
                    if d < best_d or (d == best_d and best is not None and KIND_RANK[loc.kind] < KIND_RANK[best.kind]):
                        best, best_d = loc, d
        return best


DUPLICATE_KM = 2.0


def merge_duplicates(kept, area_owner, stats):
    """One village mapped twice in OSM is one locality (owner, 2026-10-01). Conservative, and only
    INSIDE one commune (by id, never by its display name): two same-named villages of different
    communes are two places by definition. Within a commune, a same-named locality merges into the
    most informative one (wikidata, then population, then a point over an area) when
      1. both carry the same wikidata;
      2. it has neither wikidata nor population and lies within DUPLICATE_KM;
      3. it is an area-only locality (an area that held no place point of its own) with neither
         wikidata nor population: the area is the territory of the named place, however far its
         centroid sits. An area WITH its own wikidata or population is a place of its own.
    Anything else stays apart, however close (Poiu, 5 km, no evidence either way). Conflicting
    is_in tags on a merge are reported on stderr and counted, never decided silently."""
    groups = collections.defaultdict(list)
    for loc in kept:
        if loc.commune is not None:
            groups[(loc.commune.id if loc.commune.id else id(loc.commune), name_key(loc.name))].append(loc)
    gone = {}
    for _, members in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        if len(members) < 2:
            continue
        members.sort(key=lambda l: (not l.wikidata, -l.pop, l.area_only, l.osm))
        survivor = members[0]
        for m in members[1:]:
            bare = not m.wikidata and not m.pop
            if m.wikidata and m.wikidata == survivor.wikidata:
                why = "wikidata"
            elif bare and m.area_only:
                why = "area"
            elif bare and (survivor.wikidata or survivor.pop) and km(m.lat, m.lon, survivor.lat, survivor.lon) <= DUPLICATE_KM:
                why = "near"
            else:
                continue
            for a in [m.name] + m.aliases:
                if name_key(a) != name_key(survivor.name) and a not in survivor.aliases:
                    survivor.aliases.append(a)
            survivor.pop = max(survivor.pop, m.pop)
            survivor.wikidata = survivor.wikidata or m.wikidata
            if m.is_in and survivor.is_in and name_key(m.is_in) != name_key(survivor.is_in):
                stats["merged_with_conflicting_is_in"] += 1
                print("diagnostic: merged %s %s into %s %s although is_in differs: %r vs %r"
                      % (m.name, m.osm, survivor.name, survivor.osm, m.is_in, survivor.is_in), file=sys.stderr)
            survivor.is_in = survivor.is_in or m.is_in
            gone[id(m)] = survivor
            stats["duplicates_merged_" + why] += 1
    if not gone:
        return kept, area_owner
    area_owner = {pid: ((gone.get(id(o), o)), size) for pid, (o, size) in area_owner.items()}
    return [l for l in kept if id(l) not in gone], area_owner


def load(pbf, work, country=None, polygon_path=None, fallback_places=()):
    """Localities of the country. `fallback_places` (name, lat, lon, pop) are used only when the PBF
    carries no place at all (a synthetic test file)."""
    stats = collections.Counter()
    shape, stats_source = load_country_shape(pbf, work, country, polygon_path)
    stats["country_shape_" + stats_source.replace(" ", "_").replace("=", "")] = 1

    def in_country(lat, lon):
        return shape is None or bool(shape.find(lon, lat))

    nodes, areas = [], []
    for f in export(pbf, work, "places", ["nwr/place=" + ",".join(KINDS)], "point,polygon"):
        p, g = f["properties"], f["geometry"]
        name = p.get("name")
        if not name or p.get("place") not in KINDS:
            continue
        pid = "%s%s" % (p["@type"][0], p["@id"])
        if g["type"] == "Point":
            nodes.append((pid, name, p, g["coordinates"][1], g["coordinates"][0]))
        elif g["type"] in ("Polygon", "MultiPolygon"):
            areas.append((pid, name, p, rings_of(g)))

    items = []
    by_key = collections.defaultdict(list)
    for pid, name, p, lat, lon in sorted(nodes, key=lambda n: n[0]):
        loc = Locality(name, aliases_of(p, name), p["place"], lat, lon, population(p), pid, p)
        items.append(loc)
        by_key[name_key(name)].append(loc)

    node_cells = collections.defaultdict(list)
    for loc in items:
        node_cells[(int(math.floor(loc.lat * 10)), int(math.floor(loc.lon * 10)))].append(loc)

    area_shapes = Shapes(0.005)
    area_owner = {}
    for pid, name, p, rings in sorted(areas, key=lambda a: a[0]):
        k = name_key(name)
        owner = None
        tmp = Shapes(1.0)
        tmp.add("a", rings)
        # the place points inside the area: an area is the territory of the place it contains
        bx0, by0, bx1, by1 = tmp.polys["a"][1]
        inside = [loc for cy in range(int(math.floor(by0 * 10)), int(math.floor(by1 * 10)) + 1)
                  for cx in range(int(math.floor(bx0 * 10)), int(math.floor(bx1 * 10)) + 1)
                  for loc in node_cells.get((cy, cx), ())
                  if bx0 <= loc.lon <= bx1 and by0 <= loc.lat <= by1 and tmp.find(loc.lon, loc.lat)]
        area_names = {name_key(n) for n in [name] + aliases_of(p, name)}
        # 1. the same name; 2. the same name in any language (Rîbnița's point, Рыбница's area);
        # 3. the only place inside it (a "городской совет" is the territory of the town it holds)
        owner = next((l for l in inside if name_key(l.name) == k), None) \
            or next((l for l in inside if area_names & {name_key(n) for n in [l.name] + l.aliases}), None) \
            or (inside[0] if len(inside) == 1 else None)
        if owner is not None and name_key(owner.name) != k and name not in owner.aliases:
            owner.aliases.append(name)          # the area's own name finds the place too
        if owner is None:
            xs = [pt[0] for pt in rings[0]]
            ys = [pt[1] for pt in rings[0]]
            cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
            if not tmp.find(cx, cy):
                cx, cy = rings[0][0]
            for loc in by_key.get(k, ()):
                if km(cy, cx, loc.lat, loc.lon) <= MERGE_KM:
                    owner = loc
                    break
            if owner is None:
                owner = Locality(name, aliases_of(p, name), p["place"], cy, cx, population(p), pid, p)
                owner.area_only = True
                items.append(owner)
                by_key[k].append(owner)
                stats["area_only_localities"] += 1
        else:
            for a in aliases_of(p, name):
                if a not in owner.aliases:
                    owner.aliases.append(a)
            owner.pop = owner.pop or population(p)
            owner.wikidata = owner.wikidata or p.get("wikidata") or ""
        area_shapes.add(pid, rings)
        area_owner[pid] = (owner, ring_area(rings))

    if not items:
        for name, lat, lon, pop in fallback_places:
            items.append(Locality(name, [], "town", lat, lon, pop, "index"))
        stats["fallback_index_places"] = len(items)

    kept = []
    for loc in items:
        if in_country(loc.lat, loc.lon):
            kept.append(loc)
        else:
            stats["foreign_localities_dropped"] += 1
    kept_set = set(map(id, kept))
    area_owner = {pid: v for pid, v in area_owner.items() if id(v[0]) in kept_set}

    districts, dnames = load_districts(pbf, work, country)
    for loc in kept:
        hits = districts.find(loc.lon, loc.lat)
        loc.district = dnames[min(hits)] if hits else ""
    communes = []
    if (country or "") in COMMUNE_LEVEL:
        cshapes, ctags = load_admin(pbf, work, "commune", COMMUNE_LEVEL[country])
        by_pid = {}
        for loc in kept:
            hits = cshapes.find(loc.lon, loc.lat)
            if hits:
                pid = min(hits)
                if pid not in by_pid:
                    by_pid[pid] = Commune(pid, ctags[pid], loc.district)
                loc.commune = by_pid[pid]
            else:
                stats["localities_without_commune"] += 1
        communes = sorted(by_pid.values(), key=lambda c: (c.district, c.name, c.pid))
        for i, c in enumerate(communes, 1):
            c.id = i
        stats["communes"] = len(communes)
        kept, area_owner = merge_duplicates(kept, area_owner, stats)
    kept.sort(key=lambda l: (KIND_RANK[l.kind], -l.pop, l.name, l.osm))
    for i, loc in enumerate(kept, 1):
        loc.id = i
    stats["localities"] = len(kept)
    stats["localities_with_area"] = len({id(v[0]) for v in area_owner.values()})
    result = Localities(kept, area_shapes if area_owner else None, area_owner, stats)
    result.in_country = in_country
    result.communes = communes
    return result
