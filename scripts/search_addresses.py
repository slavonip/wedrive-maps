#!/usr/bin/env python3
"""House numbers for Search (block 10, owner 2026-09-27): OSM addresses of one country into the
country's own `<cc>.search.sqlite`, in the COMPACT form.

    usage: search_addresses.py <country.osm.pbf> <search.sqlite> [--work DIR] [--source-md5 MD5]

Why this exists: the search index is built from the Protomaps tiles, and the tiles carry no
address at all (every attribute key of all eight layers was scanned, 2026-09-27). The addresses are
in the Geofabrik PBF the graph is built from — Moldova 1 019 629 objects with addr:housenumber.

THE COMPACT FORM, and why not one FTS row per house: full text is needed only to find the STREET
(tens of thousands of rows); the number is an exact lookup inside that street. So:

    addr_street      one row per (canonical street name, locality) — name, aliases, city, a point
    addr_street_fts  FTS4 over name / aliases / city (city is searchable: "Chișinău București 51")
    addr             (street, key, osm) -> num, lat, lon  — WITHOUT ROWID, ~22 bytes a house
    addr_meta        format, counts, source md5

Measured on Moldova: compact 22 MB vs 87 MB for an FTS row per house.

NORMALISATION — the only fuzziness allowed, and it is not fuzzy:
  * street_key: case, diacritics (ș/ş both), and punctuation → one space. "Alba-Iulia" and
    "Alba Iulia" become one street; "Strada X" and "Stradela X" stay two (words are kept).
  * house_key: lower case, spaces out, Cyrillic look-alike letters to Latin (10А → 10a). "10/1",
    "200/5", "26D" are kept as text — a house number is NOT an integer.
The same two functions exist in the app (`AddressKeys` in :core) and both sides are tested against
one vector file, so the factory and the car can never disagree about what "10a" is.

DEDUPLICATION: one physical address is often a building AND a point. Inside one street and one
key, objects closer than DEDUP_M collapse to one, deterministically (node first, then lower id).
Two houses with the same number further apart than that stay two — they are two houses.
"""
import argparse
import collections
import itertools
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata

import search_localities

FORMAT = "wedrive-address/2"
DEDUP_M = 50.0
ALIAS_KEYS = ("name:ru", "name:ro", "name:en", "name:uk")

LOOKALIKE = str.maketrans("авекмнорстух", "abekmhopctyx")


def fold(s):
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def street_key(name):
    return " ".join(re.sub(r"[\W_]+", " ", fold(name)).split())


def house_keys(raw):
    """One addr:housenumber value → its keys (a value may list several: "10;12", "10, 12")."""
    out = []
    for part in re.split(r"[;,]", raw):
        k = re.sub(r"\s+", "", fold(part)).replace("\\", "/").translate(LOOKALIKE)
        if k and any(ch.isdigit() for ch in k) and len(k) <= 16 and k not in out:
            out.append(k)
    return out


def house_display(raw, key):
    """What the driver reads for one key of a (maybe multi-valued) housenumber."""
    for part in re.split(r"[;,]", raw):
        p = part.strip()
        if house_keys(p) == [key]:
            return p
    return key


def osm_code(kind, oid):
    return oid * 4 + {"n": 0, "w": 1, "r": 2}[kind]


def run(*cmd):
    subprocess.run(cmd, check=True)


def meters(a_lat, a_lon, b_lat, b_lon):
    k = math.cos(math.radians((a_lat + b_lat) / 2)) * 111_320.0
    return math.hypot((b_lon - a_lon) * k, (b_lat - a_lat) * 110_540.0)


# ---------------------------------------------------------------- OSM reading


def opl_tags(s):
    """OPL tag list 'k=v,k2=v2' with %xx% escapes."""
    def unesc(t):
        return re.sub(r"%([0-9a-fA-F]+)%", lambda m: chr(int(m.group(1), 16)), t)
    out = {}
    if not s:
        return out
    for kv in s.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[unesc(k)] = unesc(v)
    return out


def opl_lines(pbf):
    p = subprocess.Popen(["osmium", "cat", "-f", "opl", pbf], stdout=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace")
    for line in p.stdout:
        yield line.rstrip("\n")
    if p.wait():
        raise SystemExit("osmium cat failed on %s" % pbf)


def opl_fields(line):
    f = {}
    for tok in line.split(" ")[1:]:
        if tok:
            f[tok[0]] = tok[1:]
    return f


def associated_streets(pbf, work):
    """(kind, id) of a 'house' member -> street name, from type=associatedStreet relations."""
    rel = os.path.join(work, "assoc.osm.pbf")
    run("osmium", "tags-filter", "-R", "-O", "-o", rel, pbf, "r/type=associatedStreet")
    out = {}
    for line in opl_lines(rel):
        f = opl_fields(line)
        name = opl_tags(f.get("T", "")).get("name")
        if not name:
            continue
        for m in (f.get("M") or "").split(","):
            if "@" not in m:
                continue
            ref, role = m.split("@", 1)
            if role == "house" and ref[:1] in "nwr":
                out.setdefault((ref[0], int(ref[1:])), name)
    return out


def street_aliases(pbf, work):
    """street_key -> other-language names, from named highways carrying name:ru/ro/en/uk."""
    ways = os.path.join(work, "names.osm.pbf")
    run("osmium", "tags-filter", "-R", "-O", "-o", ways, pbf, *("w/" + k for k in ALIAS_KEYS))
    out = collections.defaultdict(list)
    for line in opl_lines(ways):
        t = opl_tags(opl_fields(line).get("T", ""))
        name = t.get("name")
        if not name or "highway" not in t:
            continue
        key = street_key(name)
        for k in ALIAS_KEYS:
            v = t.get(k)
            if v and street_key(v) != key and v not in out[key] and len(out[key]) < 6 and len(v) <= 80:
                out[key].append(v)
    return out


def address_objects(pbf, work):
    """Yield (kind, id, tags, lat, lon) for every object with addr:housenumber."""
    sub = os.path.join(work, "addr.osm.pbf")
    run("osmium", "tags-filter", "-O", "-o", sub, pbf, "nwr/addr:housenumber")
    p = subprocess.Popen(["osmium", "export", "-f", "geojsonseq", "-a", "type,id",
                          "--geometry-types=point,polygon", sub],
                         stdout=subprocess.PIPE, text=True, encoding="utf-8")
    for line in p.stdout:
        f = json.loads(line.lstrip("\x1e"))
        props, g = f["properties"], f["geometry"]
        if not props.get("addr:housenumber"):
            continue
        t = g["type"]
        if t == "Point":
            lon, lat = g["coordinates"]
        else:
            if t == "LineString":
                pts = g["coordinates"]
            elif t == "Polygon":
                pts = g["coordinates"][0]
            else:  # MultiPolygon: the first outer ring
                pts = g["coordinates"][0][0]
            lon = sum(c[0] for c in pts) / len(pts)
            lat = sum(c[1] for c in pts) / len(pts)
        kind = {"node": "n", "way": "w", "relation": "r"}[props["@type"]]
        yield kind, int(props["@id"]), props, lat, lon
    if p.wait():
        raise SystemExit("osmium export failed")


# ---------------------------------------------------------------- building


SCHEMA = """
DROP TABLE IF EXISTS addr_street; DROP TABLE IF EXISTS addr_street_fts;
DROP TABLE IF EXISTS addr; DROP TABLE IF EXISTS addr_meta;
DROP TABLE IF EXISTS addr_locality; DROP TABLE IF EXISTS addr_locality_fts;
-- /2: every locality of the country, cut to its own border (search_localities.py)
CREATE TABLE addr_locality(
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,    -- OSM name
    aliases  TEXT,             -- JSON array: name:ro/ru/en/uk/..., alt/old/official names, addr:city spellings
    kind     TEXT NOT NULL,    -- city / town / village / hamlet
    district TEXT NOT NULL,    -- raion / judet / megye / Bezirk: tells same-named localities apart
    lat      REAL NOT NULL, lon REAL NOT NULL,
    pop      INTEGER NOT NULL,
    houses   INTEGER NOT NULL
);
CREATE VIRTUAL TABLE addr_locality_fts USING fts4(name, aliases, tokenize=unicode61, content='addr_locality');
-- /1 columns first and unchanged (a /1 app reads them by name); /2 adds locality and city_alt
CREATE TABLE addr_street(
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,     -- canonical spelling: the most frequent addr:street of this group
    aliases TEXT,              -- JSON array: name:ru/ro/en/uk of the street, NULL when none
    city    TEXT NOT NULL,     -- the locality's name (display); '' when unresolved
    lat     REAL NOT NULL, lon REAL NOT NULL,   -- median of its houses: "drive to the street"
    houses  INTEGER NOT NULL,
    locality INTEGER,          -- addr_locality.id; NULL when unresolved
    city_alt TEXT              -- the locality's aliases (JSON), so "Bender Leningradskaya 52" matches too
);
CREATE INDEX addr_street_locality ON addr_street(locality);
CREATE VIRTUAL TABLE addr_street_fts USING fts4(name, aliases, city, city_alt, tokenize=unicode61, content='addr_street');
CREATE TABLE addr(
    street INTEGER NOT NULL,
    key    TEXT NOT NULL,      -- house_keys(): "10a", "10/1", "200/5"
    osm    INTEGER NOT NULL,   -- id*4 + (0 node, 1 way, 2 relation): the object the coordinate is of
    num    TEXT,               -- as written in OSM; NULL when identical to key (most of them)
    lat    INTEGER NOT NULL, lon INTEGER NOT NULL,   -- 1e-7 degrees
    PRIMARY KEY(street, key, osm)
) WITHOUT ROWID;
CREATE TABLE addr_meta(k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID;
"""


def build(pbf, db_path, work, source_md5=None, country=None, polygon=None):
    """Streams through an on-disk scratch table: Germany has ~20 M addresses, far too many for lists."""
    t0 = time.time()
    db = sqlite3.connect(db_path)
    fallback = [(n, la, lo, p) for n, la, lo, p in db.execute("SELECT name, lat, lon, pop FROM place WHERE kind = 0")]
    locs = search_localities.load(pbf, work, country, polygon, fallback)
    assoc = associated_streets(pbf, work)
    aliases = street_aliases(pbf, work)
    scratch = os.path.join(work, "raw.sqlite")
    if os.path.exists(scratch):
        os.remove(scratch)      # a leftover from an earlier run in the same work dir
    tmp = sqlite3.connect(scratch)
    tmp.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
                      "CREATE TABLE raw(skey TEXT, street TEXT, loc INTEGER, method TEXT, city TEXT, kind INTEGER,"
                      " oid INTEGER, num TEXT, key TEXT, lat REAL, lon REAL);")

    # 1. every (object, key) with its street and its locality by name or by area (search_localities)
    stats = collections.Counter(locs.stats)
    batch = []
    learned = collections.Counter()      # (locality id, addr:city as written) seen via area/anchor/nearest
    for kind, oid, t, lat, lon in address_objects(pbf, work):
        stats["objects"] += 1
        if not locs.in_country(lat, lon):
            stats["foreign_objects_dropped"] += 1
            continue
        street = t.get("addr:street") or assoc.get((kind, oid)) or t.get("addr:place")
        if not street:
            stats["no_street"] += 1
            continue
        if not t.get("addr:street"):
            stats["via_associatedStreet" if (kind, oid) in assoc else "via_addr_place"] += 1
        hn = t["addr:housenumber"]
        city = t.get("addr:city")
        loc, method = (locs.by_city(city, lat, lon), "name") if city else (None, None)
        if loc is None:
            loc, method = locs.by_area(lat, lon), "area"
            if loc is not None and city:
                learned[(loc.id, city.strip())] += 1
        for key in house_keys(hn):
            batch.append((street_key(street), street, loc.id if loc else None, method if loc else None, city,
                          {"n": 0, "w": 1, "r": 2}[kind], oid, house_display(hn, key), key, lat, lon))
        if len(batch) >= 50_000:
            tmp.executemany("INSERT INTO raw VALUES(?,?,?,?,?,?,?,?,?,?,?)", batch); batch = []
    tmp.executemany("INSERT INTO raw VALUES(?,?,?,?,?,?,?,?,?,?,?)", batch)
    stats["keys"] = tmp.execute("SELECT count(*) FROM raw").fetchone()[0]

    # 2. rows with no locality yet: a same-named street resolved by name/area within ANCHOR_KM (the
    #    mean of its houses), else the nearest locality point within NEAR_KM, else unresolved.
    #    Written to a second on-disk table in batches, then applied in one statement.
    by_id = {l.id: l for l in locs.items}
    anchors = collections.defaultdict(list)
    for skey, loc, lat, lon in tmp.execute(
            "SELECT skey, loc, avg(lat), avg(lon) FROM raw WHERE loc IS NOT NULL GROUP BY skey, loc"):
        anchors[skey].append((loc, lat, lon))
    inferred = os.path.join(work, "inferred.sqlite")
    if os.path.exists(inferred):
        os.remove(inferred)
    writer = sqlite3.connect(inferred)     # its own file: the open reader below locks raw.sqlite
    writer.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
                         "CREATE TABLE inferred(id INTEGER PRIMARY KEY, loc INTEGER, method TEXT)")
    reader = tmp.execute("SELECT rowid, skey, lat, lon, city FROM raw WHERE loc IS NULL")
    while True:
        chunk = reader.fetchmany(100_000)
        if not chunk:
            break
        out = []
        for rowid, skey, lat, lon, city in chunk:
            best, best_d = None, search_localities.ANCHOR_KM
            for loc, alat, alon in anchors.get(skey, ()):
                d = search_localities.km(lat, lon, alat, alon)
                if d < best_d:
                    best, best_d = loc, d
            if best is not None:
                out.append((rowid, best, "anchor"))
            else:
                near = locs.nearest(lat, lon)
                out.append((rowid, near.id if near else None, "nearest" if near else None))
            if out[-1][1] is not None and city:
                learned[(out[-1][1], city.strip())] += 1
        writer.executemany("INSERT INTO inferred VALUES(?,?,?)", out)
        writer.commit()
    writer.close()
    del anchors
    tmp.execute("ATTACH DATABASE ? AS inf", (inferred,))
    tmp.execute("UPDATE raw SET loc = (SELECT loc FROM inf.inferred WHERE id = raw.rowid),"
                " method = (SELECT method FROM inf.inferred WHERE id = raw.rowid) WHERE loc IS NULL")
    tmp.commit()
    tmp.execute("DETACH DATABASE inf")
    tmp.execute("CREATE INDEX raw_order ON raw(skey, loc, key, kind, oid)")
    # an addr:city spelling seen on >= 3 houses of a locality it did not name ("Кицканы") becomes an alias
    for (lid, spelled), n in sorted(learned.items()):
        loc = by_id.get(lid)
        known = {search_localities.name_key(x) for x in ([loc.name] + loc.aliases)} if loc else set()
        if loc and n >= 3 and search_localities.name_key(spelled) not in known and len(loc.aliases) < 12:
            loc.aliases.append(spelled)
            stats["aliases_learned"] += 1

    # 3. streets + deduplicated houses, streamed in a deterministic order
    db.executescript(SCHEMA)
    sid = 0
    groups = itertools.groupby(
        tmp.execute("SELECT skey, loc, key, kind, oid, street, num, lat, lon, method FROM raw"
                    " ORDER BY skey, loc, key, kind, oid"),
        key=lambda r: (r[0], r[1]))
    for (skey, lid), rows in groups:
        rows = list(rows)
        sid += 1
        names = collections.Counter(r[5] for r in rows)
        name = sorted(names.items(), key=lambda x: (-x[1], x[0]))[0][0]
        loc = by_id.get(lid)
        city = loc.name if loc else ""
        methods = collections.Counter(r[9] or "unresolved" for r in rows)
        stats["streets_by_" + sorted(methods.items(), key=lambda x: (-x[1], x[0]))[0][0]] += 1
        lats = sorted(r[7] for r in rows)
        lons = sorted(r[8] for r in rows)
        kept = 0
        for key, same in itertools.groupby(rows, key=lambda r: r[2]):
            accepted = []
            for r in same:                      # already ordered node < way < relation, then id
                if any(meters(r[7], r[8], a[7], a[8]) < DEDUP_M for a in accepted):
                    stats["dedup_dropped"] += 1
                    continue
                accepted.append(r)
                db.execute("INSERT OR IGNORE INTO addr VALUES(?,?,?,?,?,?)",
                           (sid, key, r[4] * 4 + r[3], None if r[6] == key else r[6], round(r[7] * 1e7), round(r[8] * 1e7)))
            kept += len(accepted)
        al = aliases.get(skey)
        db.execute("INSERT INTO addr_street VALUES(?,?,?,?,?,?,?,?,?)",
                   (sid, name, json.dumps(al, ensure_ascii=False) if al else None, city,
                    lats[len(lats) // 2], lons[len(lons) // 2], kept, lid,
                    json.dumps(loc.aliases, ensure_ascii=False) if loc and loc.aliases else None))
        if loc:
            loc.houses += kept
        stats["houses"] += kept
    tmp.close()
    stats["streets"] = sid
    db.executemany("INSERT INTO addr_locality VALUES(?,?,?,?,?,?,?,?,?)",
                   [(l.id, l.name, json.dumps(l.aliases, ensure_ascii=False) if l.aliases else None, l.kind,
                     l.district, l.lat, l.lon, l.pop, l.houses) for l in locs.items])
    db.execute("INSERT INTO addr_locality_fts(addr_locality_fts) VALUES('rebuild')")
    db.execute("INSERT INTO addr_street_fts(addr_street_fts) VALUES('rebuild')")
    meta = {"format": FORMAT, "streets": stats["streets"], "houses": stats["houses"],
            "objects": stats["objects"], "localities": stats["localities"],
            "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if source_md5:
        meta["source_md5"] = source_md5
    db.executemany("INSERT INTO addr_meta VALUES(?,?)", [(k, str(v)) for k, v in meta.items()])
    db.commit()
    db.execute("VACUUM")
    db.close()
    stats["seconds"] = round(time.time() - t0)
    return dict(stats)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pbf")
    ap.add_argument("db")
    ap.add_argument("--work")
    ap.add_argument("--source-md5")
    ap.add_argument("--country", help="ISO code: localities and houses are cut to its admin_level=2 border")
    ap.add_argument("--polygon", help="fallback border (regional.json polygon) when the PBF has none")
    a = ap.parse_args()
    before = os.path.getsize(a.db)
    with tempfile.TemporaryDirectory(dir=a.work) as work:
        stats = build(a.pbf, a.db, work, a.source_md5, a.country, a.polygon)
    after = os.path.getsize(a.db)
    stats["db_bytes_before"], stats["db_bytes_after"] = before, after
    print(json.dumps(stats, indent=1, sort_keys=True))
    if stats.get("houses", 0) == 0:
        print("::error::no addresses indexed — refusing a search asset without house numbers")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
