# -*- coding: utf-8 -*-
"""The charging catalogue, formats wedrive-charging/1 and /2: schema, OSM records, dedup, writing, gates.

Imported by charging-build.py. OCM records arrive here already translated by
charging-ocm-fetch.py (`normalise`), which is the only factory file that reads OCM JSON.

A ROW IS ONE SOURCE RECORD, AND NOTHING IS EVER MERGED. Four licences meet in this file (ODbL for
OSM, CC BY, CC0 and CC BY-NC-SA for OCM's providers), so a station row carries exactly one
record's fields under exactly one `source` row. Dedup only assigns `cluster` — the ids of records
that describe the same physical place — and the app merges fields per cluster when it shows one.

FORMAT /2 (2026-11-24) IS /1 PLUS TABLES, NEVER A CHANGED ONE. Every /1 table keeps its columns and
meaning; /2 adds `evse` (one row per physical charging point: the id as its source wrote it, the
normalised eMI3 id), `live_key` (ids a live feed uses that are not EVSE ids), `station_ref`
(registry ids) and `source_registry` (every static and live source with its licence), and a third
source kind, `nap` — the national access points read by charging_nap.py. A /2 file is published
BESIDE a /1 file (manifest `charging.v2`), so an app that reads only /1 keeps updating.

Standard library only (the factory runs on a bare runner and in containers without pip).
"""
import gzip, hashlib, io, json, math, os, re, sqlite3, unicodedata

FORMAT = "wedrive-charging/1"
FORMAT2 = "wedrive-charging/2"
STATUSES = ("OPERATIONAL", "PARTLY", "NOT_OPERATIONAL", "PLANNED", "UNKNOWN")
USAGES = ("PUBLIC", "PUBLIC_MEMBERSHIP", "PUBLIC_PAY", "CUSTOMERS", "PRIVATE", "UNKNOWN")
OSM_SOURCE_ID = 1
OCM_SOURCE_BASE = 100                  # source.id of OCM provider P = 100 + P
NAP_SOURCE_BASE = 1000                 # a national access point: its nap-sources.json source_id (1001..)

# dedup (plan §6.5)
NEAR_M = 50.0                          # OSM-OCM: one place unless they conflict
FAR_M = 100.0                          # 50..100 m: one place only with the same operator or name

# gates (plan §6.6)
MAX_SQLITE = 150 * 1024 * 1024
MAX_GZ = 60 * 1024 * 1024
# /2 carries the national registries (~+200 k sites, ~+700 k EVSE rows): the architect measured
# 56 MB gz for the same six registries (2026-11-23); the gate stays a gate, 25 % above that.
MAX_SQLITE2 = 260 * 1024 * 1024
MAX_GZ2 = 70 * 1024 * 1024
MIN_OCM = {"MD": 40}                   # Moldova has had 46 OCM stations (API, 2026-11-21)
COUNTRY_LOSS = 0.20                    # a country with >= 50 OCM stations may not lose more
COUNTRY_LOSS_FLOOR = 50               # (the same two numbers gate OSM stations per country)
TOTAL_KEEP = 0.90                      # total OCM may not fall below this share of the previous
BBOX_SLACK = 0.2

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE source(id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
  provider_id INTEGER, name TEXT NOT NULL, licence TEXT NOT NULL, licence_url TEXT,
  attribution TEXT NOT NULL, url TEXT);
CREATE TABLE station(id INTEGER PRIMARY KEY, cluster INTEGER NOT NULL,
  source INTEGER NOT NULL REFERENCES source(id), src_id TEXT NOT NULL,
  country TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
  name TEXT, address TEXT, operator TEXT, operator_id INTEGER,
  usage TEXT NOT NULL,
  status TEXT NOT NULL,
  status_raw TEXT NOT NULL,
  status_date TEXT, verified_date TEXT, created_date TEXT,
  points INTEGER, capacity INTEGER, max_kw REAL, has_dc INTEGER NOT NULL,
  cost TEXT, opening_hours TEXT, evse_ids TEXT,
  UNIQUE(source, src_id));
CREATE TABLE connector(station INTEGER NOT NULL REFERENCES station(id),
  standard TEXT NOT NULL,
  current TEXT, kw REAL, qty INTEGER, status TEXT);
CREATE TABLE ocm_operator(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE ocm_connection(id INTEGER PRIMARY KEY, standard TEXT NOT NULL, current TEXT);
CREATE INDEX station_pos ON station(lat, lon);
CREATE INDEX station_cluster ON station(cluster);
CREATE INDEX connector_station ON connector(station);
"""

# /2: added tables only. evse_norm = the eMI3 id normalised (upper case, letters and digits only)
# when the source's id IS one — the dedup and live key, unique across clusters (validate);
# evse_id = the id exactly as the source wrote it, stored only when it differs from evse_norm (NULL
# with evse_norm set: the source wrote it normalised; both NULL: a physical point the source names
# by no id — still a row). A national registry record's ids live here only: its station.evse_ids is
# NULL, so /2 carries no id twice. No index on evse_norm: nothing looks a point up by id in the
# file (the app reads a station's rows, the factory groups once at validation) — 14 MB saved.
SCHEMA2 = """
CREATE TABLE evse(id INTEGER PRIMARY KEY, station INTEGER NOT NULL REFERENCES station(id),
  evse_id TEXT, evse_norm TEXT, standards TEXT, current TEXT, max_kw REAL);
CREATE TABLE live_key(station INTEGER NOT NULL REFERENCES station(id), source TEXT NOT NULL, key TEXT NOT NULL);
CREATE TABLE station_ref(station INTEGER NOT NULL REFERENCES station(id), scheme TEXT NOT NULL, ref TEXT NOT NULL);
CREATE TABLE source_registry(source TEXT NOT NULL, country TEXT NOT NULL, kind TEXT NOT NULL,
  url TEXT NOT NULL, format TEXT NOT NULL, query TEXT, update_frequency TEXT, last_success TEXT,
  record_count INTEGER, licence TEXT NOT NULL, attribution TEXT NOT NULL,
  authentication_required INTEGER NOT NULL DEFAULT 0, ttl_s INTEGER, source_id INTEGER,
  PRIMARY KEY(source, kind));
CREATE INDEX evse_station ON evse(station);
CREATE INDEX live_key_station ON live_key(station);
CREATE INDEX station_ref_station ON station_ref(station);
"""


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def gzip_file(src, dst):
    """gzip -9 with no name and mtime 0: the same sqlite is the same .gz, byte for byte."""
    with open(src, "rb") as f, open(dst, "wb") as out:
        with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=9) as g:
            for b in iter(lambda: f.read(1 << 20), b""):
                g.write(b)


# ─────────────────────────────────── countries ───────────────────────────────────

class Countries:
    """Which regional.json country a point belongs to: addr:country when it names one of ours,
    else the countries whose bbox holds the point, narrowed by their polygon where several do,
    then the smallest bbox. Deterministic; no point is in two countries."""
    ALIASES = {"UK": "GB", "JE": "XG", "GG": "XG"}

    def __init__(self, cfg, root=None):
        self.bbox = {c: v["bbox"] for c, v in cfg["countries"].items()}
        self.area = {c: (b[2] - b[0]) * (b[3] - b[1]) for c, b in self.bbox.items()}
        self.polys, self.pbox = {}, {}
        for c, v in cfg["countries"].items():
            p = v.get("polygon")
            if root and p and os.path.isfile(os.path.join(root, p)):
                geo = json.load(open(os.path.join(root, p), encoding="utf-8"))
                self.polys[c] = _bands(geo)
                self.pbox[c] = _extent(geo)

    def covers(self, lat, lon):
        """Inside any of our country polygons (a country without one: inside its bbox). A station
        outside all of them is not ours, whatever bbox happens to hold it (Armenia inside GE's and
        TR's, Japan inside RU's)."""
        for c, b in self.bbox.items():
            if c in self.polys:
                w, s, e, n = self.pbox[c]
                if w <= lon <= e and s <= lat <= n and _inside(self.polys[c], lat, lon):
                    return True
            elif b[0] <= lon <= b[2] and b[1] <= lat <= b[3]:
                return True
        return False

    def of(self, lat, lon, addr=None):
        a = (addr or "").strip().upper()
        a = self.ALIASES.get(a, a)
        if a in self.bbox:
            return a
        cands = [c for c, b in self.bbox.items() if b[0] <= lon <= b[2] and b[1] <= lat <= b[3]]
        if not cands:
            return None
        if len(cands) > 1:
            inside = [c for c in cands if c in self.polys and _inside(self.polys[c], lat, lon)]
            if inside:
                cands = inside
        return min(cands, key=lambda c: (self.area[c], c))

    def in_bbox(self, code, lat, lon, slack=BBOX_SLACK):
        b = self.bbox.get(code)
        return bool(b) and b[0] - slack <= lon <= b[2] + slack and b[1] - slack <= lat <= b[3] + slack


BAND = 0.1


def _extent(geo):
    g = geo.get("geometry", geo)
    polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
    xs = [x for poly in polys for ring in poly for x, _ in ring]
    ys = [y for poly in polys for ring in poly for _, y in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _bands(geo):
    """Polygon edges bucketed by latitude band: an even-odd ray test then looks at a few dozen
    edges instead of every vertex of a country."""
    g = geo.get("geometry", geo)
    polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
    bands = {}
    for poly in polys:
        for ring in poly:
            for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
                if y1 == y2:
                    continue
                for k in range(math.floor(min(y1, y2) / BAND), math.floor(max(y1, y2) / BAND) + 1):
                    bands.setdefault(k, []).append((x1, y1, x2, y2))
    return bands


def _inside(bands, lat, lon):
    hit = False
    for x1, y1, x2, y2 in bands.get(math.floor(lat / BAND), ()):
        if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


# ─────────────────────────────────── OSM records ───────────────────────────────────

CS = "charging_station"
CLOSED = ("closed", "out_of_order", "out_of_service", "broken", "non_operational",
          "not_operational", "defunct")
GONE = ("removed", "demolished", "razed", "abandoned")
COMING = ("construction", "proposed", "planned")


def osm_status(tags):
    """-> (status, status_raw); (None, raw) = dropped as gone; (None, None) = not a charger."""
    if tags.get("amenity") == CS:
        op = (tags.get("operational_status") or "").strip().lower()
        if op in CLOSED:
            return "NOT_OPERATIONAL", "osm:operational_status=" + op
        if tags.get("disused") == "yes" or tags.get("disused:amenity") == CS:
            return "NOT_OPERATIONAL", "osm:disused"
        for p in COMING:
            if tags.get(p) == "yes" or tags.get(p + ":amenity") == CS:
                return "PLANNED", "osm:" + p
        return "UNKNOWN", "osm:present"
    for p in GONE:
        if tags.get(p + ":amenity") == CS:
            return None, "osm:" + p
    if tags.get("disused:amenity") == CS:
        return "NOT_OPERATIONAL", "osm:disused"
    for p in COMING:
        if tags.get(p + ":amenity") == CS:
            return "PLANNED", "osm:" + p
    return None, None


def osm_usage(tags):
    a = (tags.get("access") or "").strip().lower()
    if a in ("yes", "public", "permissive"):
        return "PUBLIC_PAY" if tags.get("fee") == "yes" else "PUBLIC"
    if a in ("customers", "destination"):
        return "CUSTOMERS"
    if a in ("private", "no"):
        return "PRIVATE"
    if a == "members":
        return "PUBLIC_MEMBERSHIP"
    return "UNKNOWN"


NUM_UNIT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kw|kva|w)?", re.I)


def osm_kw(value):
    """`22 kW`, `22`, `11000 W`, `22;11 kW` -> the largest, in kW; None when none is sane."""
    best = None
    for part in (value or "").split(";"):
        m = NUM_UNIT.search(part.strip())
        if not m:
            continue
        v = float(m.group(1).replace(",", "."))
        if (m.group(2) or "").lower() == "w" or (not m.group(2) and v > 1000):
            v /= 1000.0
        if 0 < v <= 1000 and (best is None or v > best):
            best = v
    return best


def osm_connectors(tags, connectors, report):
    found = {}
    for k in tags:
        if k.startswith("socket:"):
            parts = k.split(":")
            if len(parts) == 2 or (len(parts) == 3 and parts[2] == "output"):
                found.setdefault(parts[1], None)
    out = []
    for t in sorted(found):
        std = connectors["osm"].get(t)
        if std is None:
            std = "OTHER"
            report.setdefault("unknown_osm_sockets", {})
            report["unknown_osm_sockets"][t] = report["unknown_osm_sockets"].get(t, 0) + 1
        cnt = (tags.get("socket:" + t) or "").strip()
        qty = int(cnt) if cnt.isdigit() and int(cnt) > 0 else None
        if cnt == "0" or cnt == "no":
            continue
        out.append({"standard": std, "current": "DC" if std in connectors["dc_standards"] else None,
                    "kw": osm_kw(tags.get("socket:%s:output" % t)), "qty": qty, "status": None})
    return out


def centroid(geom):
    t, c = geom["type"], geom["coordinates"]
    if t == "Point":
        return c[1], c[0]
    if t == "LineString":
        pts = c
    elif t == "Polygon":
        pts = c[0][:-1] or c[0]
    elif t == "MultiPolygon":
        pts = c[0][0][:-1] or c[0][0]
    else:
        return None
    return sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts)


# an EU EVSE id (`DE*ION*E12345`): country, operator, E, the rest. The ONE copy: OSM ref:EU:EVSE and
# OCM connection references both go through evse_id, so `yes`, `1` or a note never become an id.
EVSE = re.compile(r"^[A-Z]{2}\*?[A-Z0-9]{3}\*?E[A-Z0-9*]+$")


def evse_id(raw):
    """-> the normalised EVSE id (upper case, no spaces, no `*`), or None when `raw` is not one."""
    v = (raw or "").strip().upper().replace(" ", "")
    return v.replace("*", "") if EVSE.match(v) else None


def evse_key(raw):
    """Any id as a live feed compares it (the app's normalizeEvseId): upper case, letters, digits
    and `#` (LT `<site>#<index>`); every separator dropped. None when nothing is left."""
    v = "".join(ch for ch in (raw or "").upper() if ch.isalnum() or ch == "#")
    return v or None


def evse_norm(value):
    """OSM ref:EU:EVSE (`;`-separated) -> the sorted valid normalised ids; anything else is dropped."""
    return sorted({e for e in (evse_id(v) for v in (value or "").split(";")) if e})


def osm_record(feature, connectors, countries, report):
    """One geojsonseq feature -> our station dict, or None (dropped, the reason counted)."""
    tags = feature.get("properties") or {}
    status, raw = osm_status(tags)
    if status is None:
        if raw:
            report["dropped_removed_osm"] = report.get("dropped_removed_osm", 0) + 1
        return None
    pos = centroid(feature.get("geometry") or {"type": "None", "coordinates": None})
    if pos is None:
        report["dropped_nocoords_osm"] = report.get("dropped_nocoords_osm", 0) + 1
        return None
    lat, lon = pos
    if not countries.covers(lat, lon):
        report["dropped_outside"] = report.get("dropped_outside", 0) + 1
        return None
    cc = countries.of(lat, lon, tags.get("addr:country"))
    if cc is None:
        report["dropped_outside"] = report.get("dropped_outside", 0) + 1
        return None
    conns = osm_connectors(tags, connectors, report)
    kws = [c["kw"] for c in conns if c["kw"] is not None]
    kw_station = osm_kw(tags.get("charging_station:output"))
    if kw_station is not None:
        kws.append(kw_station)
    cap = (tags.get("capacity") or "").strip()
    street = " ".join(x for x in (tags.get("addr:street"), tags.get("addr:housenumber")) if x)
    addr = ", ".join(x for x in (street, tags.get("addr:city"), tags.get("addr:postcode")) if x)
    cost = tags.get("charge") or ("free" if tags.get("fee") == "no" else None)
    return {
        "kind": "osm", "provider_id": None, "src_id": feature["id"], "country": cc, "lat": lat, "lon": lon,
        "name": tags.get("name") or tags.get("brand") or None, "address": addr or None,
        "operator": tags.get("operator") or None, "operator_id": None,
        "usage": osm_usage(tags), "status": status, "status_raw": raw,
        "status_date": None, "verified_date": tags.get("check_date") or tags.get("survey:date"),
        "created_date": None, "points": None,
        "capacity": int(cap) if cap.isdigit() else None,
        "max_kw": max(kws) if kws else None,
        "has_dc": int(any(c["current"] == "DC" for c in conns)),
        "cost": cost, "opening_hours": tags.get("opening_hours"),
        "evse_ids": evse_norm(tags.get("ref:EU:EVSE")),
        "evse_raw": {evse_id(v): v.strip() for v in (tags.get("ref:EU:EVSE") or "").split(";") if evse_id(v)},
        "connectors": conns}


def osm_id(raw):
    """osmium --add-unique-id=type_id -> our 'n123'/'w45'/'r6'.

    With polygon output osmium also emits AREAS as 'a<area id>': area id = 2*way id for a closed
    way, 2*relation id + 1 for a multipolygon (osmium's area numbering). Seen on the first real
    Europe run 2026-11-21 (191 383 features for 178 554 objects). Map them back to the OSM object
    so one station is one src_id; anything else is refused by name."""
    if not raw or raw[0] not in "nwra" or not raw[1:].isdigit():
        raise ValueError("unexpected OSM feature id %r" % (raw,))
    if raw[0] != "a":
        return raw
    n = int(raw[1:])
    return ("w%d" % (n // 2)) if n % 2 == 0 else ("r%d" % ((n - 1) // 2))


def osm_sort_key(src_id):
    return ("nwr".index(src_id[0]), int(src_id[1:]))


# ─────────────────────────────────── dedup ───────────────────────────────────

LEGAL = {"gmbh", "ag", "kg", "co", "srl", "sa", "sas", "sarl", "spa", "sl", "slu", "bv", "nv",
         "ltd", "limited", "plc", "inc", "llc", "ab", "as", "asa", "oy", "oyj", "aps", "sro", "zoo",
         "sp", "doo", "kft", "zrt", "ou", "uab", "sia", "se", "the", "und", "and", "et", "ii"}
FAMILY = {"TYPE2_TETHERED": "TYPE2"}


def tokens(text):
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).casefold()
    return frozenset(w for w in re.split(r"[^0-9a-zа-яё]+", t) if w and w not in LEGAL)


def same_operator(a, b):
    """Normalised operators name the same company: equal token sets, or one inside the other
    (`IONITY` and `IONITY GmbH`). Empty = unknown, never equal to anything."""
    return bool(a) and bool(b) and (a <= b or b <= a)


# charging/operators.json, loaded by the build: squashed spelling -> canonical squashed spelling
ALIASES = {}


def squash(text):
    """An operator's words without legal forms, run together: `In Balance grid, UAB` ->
    `inbalancegrid`, `E.ON` -> `eon`, a leading tab or `IZIVIA` -> `izivia`."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).casefold()
    t = re.sub(r"\(\s*[a-z]{2}\s*\)\s*$", "", t)          # OCM's country suffix: `Inbalance grid (LT)`
    return "".join(w for w in re.split(r"[^0-9a-zа-яё]+", t) if w and w not in LEGAL)


def operator_key(text, aliases=None):
    """The operator as dedup compares it: squashed, then mapped through the alias table."""
    s = squash(text)
    return (ALIASES if aliases is None else aliases).get(s, s)


def load_aliases(path):
    """charging/operators.json {"operators": {canonical: [alias, ...]}} -> {squash(alias): squash(canonical)}."""
    data = json.load(open(path, encoding="utf-8"))
    out = {}
    for canon, names in sorted(data["operators"].items()):
        c = squash(canon)
        for n in [canon] + list(names):
            out[squash(n)] = c
    return out


def same_op(x, y):
    """Two records' operators name one company: equal after the alias table, or token sets one
    inside the other (same_operator). Unknown on either side is never the same."""
    if x["_sq"] and y["_sq"] and x["_sq"] == y["_sq"]:
        return True
    return same_operator(x["_op"], y["_op"])


def families(conns):
    return frozenset(FAMILY.get(c["standard"], c["standard"]) for c in conns if c["standard"] != "OTHER")


def conflict(o, c):
    """Both say who runs it and they disagree, or both list connectors and none is shared."""
    if o["_op"] and c["_op"] and not same_op(o, c):
        return True
    if o["_fam"] and c["_fam"] and not (o["_fam"] & c["_fam"]):
        return True
    return False


def metres(a, b):
    la1, la2 = math.radians(a["lat"]), math.radians(b["lat"])
    dla, dlo = la2 - la1, math.radians(b["lon"] - a["lon"])
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(h)))


def eligible(o, c, d):
    if d <= NEAR_M:
        return not conflict(o, c)
    if d <= FAR_M:
        return (same_op(o, c) or (o["_name"] and o["_name"] == c["_name"])) \
            and not conflict(o, c)
    return False


CELL = 0.002


def _grid(recs):
    g = {}
    for r in recs:
        g.setdefault((math.floor(r["lat"] / CELL), math.floor(r["lon"] / CELL)), []).append(r)
    return g


def _near(grid, r):
    span = max(1, math.ceil(FAR_M / (CELL * 111320 * max(0.05, math.cos(math.radians(r["lat"]))))))
    ci, cj = math.floor(r["lat"] / CELL), math.floor(r["lon"] / CELL)
    for i in (ci - 1, ci, ci + 1):
        for j in range(cj - span, cj + span + 1):
            yield from grid.get((i, j), ())


def best_partner(r, grid):
    best = None
    for x in _near(grid, r):
        d = metres(r, x)
        a, b = (r, x) if r["kind"] == "osm" else (x, r)
        if eligible(a, b, d) and (best is None or (d, x["id"]) < best[:2]):
            best = (d, x["id"], x)
    return best


def cluster(stations, mutual=True, stats=None):
    """{station id: cluster id}. Rules (plan §6.5): (1) the same normalised EVSE id is one
    place, for one OSM and one OCM record only; (2) OCM–OCM is never joined spatially; (3) OSM–OCM within NEAR_M unless they
    conflict; (4) 50..100 m only with the same operator or name; (5) OSM–OSM never spatially;
    (6) only MUTUAL nearest eligible partners, one to one — no chains through a third record.
    cluster = the smallest station id of the group."""
    parent = {s["id"]: s["id"] for s in stations}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for s in stations:
        s["_op"] = tokens(s.get("operator"))
        s["_sq"] = operator_key(s.get("operator"))
        s["_name"] = tokens(s.get("name"))
        s["_fam"] = families(s["connectors"])
    # (1) EVSE joins an OSM record to an OCM record, never two of one kind, and one to one like the
    # spatial rule: the first (OSM id, OCM id) pair in id order wins, and a record paired here
    # takes no spatial partner, so no cluster ever holds two OSM or two OCM records.
    by_evse = {}
    for s in stations:
        if s["kind"] == "nap":
            continue
        for e in s["evse_ids"]:
            by_evse.setdefault(e, {"osm": [], "ocm": []})[s["kind"]].append(s["id"])
    paired = set()
    for o, c in sorted({(o, c) for g in by_evse.values() for o in g["osm"] for c in g["ocm"]}):
        if o not in paired and c not in paired:
            union(o, c)
            paired.update((o, c))
    osm = [s for s in stations if s["kind"] == "osm" and s["id"] not in paired]
    ocm = [s for s in stations if s["kind"] == "ocm" and s["id"] not in paired]
    g_osm, g_ocm = _grid(osm), _grid(ocm)
    best_ocm = {o["id"]: best_partner(o, g_ocm) for o in osm}
    best_osm = {c["id"]: best_partner(c, g_osm) for c in ocm}
    for oid, b in best_ocm.items():
        if b is None:
            continue
        back = best_osm.get(b[1])
        if not mutual or (back is not None and back[1] == oid):
            union(oid, b[1])
    nap = [s for s in stations if s["kind"] == "nap"]
    if nap:
        _cluster_nap(nap, [s for s in stations if s["kind"] != "nap"], find, union, stats if stats is not None else {})
    for s in stations:
        for k in ("_op", "_sq", "_name", "_fam"):
            s.pop(k, None)
    return {s["id"]: find(s["id"]) for s in stations}


def _cluster_nap(nap, others, find, union, stats):
    """National registry sites onto the OSM/OCM clusters already made (/2 only). At most ONE NAP
    record per cluster and NAP records never join each other. In order, each step only for what the
    step before left alone:
      (a) a shared eMI3 EVSE id: the cluster of the lowest-id OSM/OCM record holding it;
      (b) a registry id: OCM's DataProvidersReference of the provider that copies this registry
          (nap-sources.json ocm_provider / ref_scheme), equal after normalisation;
      (c) position, by the OSM-OCM rules (<= NEAR_M unless they conflict, NEAR_M..FAR_M only with
          the same operator or name), the nearest eligible record; when several NAP sites want one
          cluster the nearest keeps it and the others stay separate places."""
    taken = {}                                     # cluster root -> the NAP id that joined it

    def join(n, other):
        root = find(other)
        if root in taken:
            return False
        taken[root] = n["id"]
        union(n["id"], other)
        return True

    by_evse, by_ref = {}, {}
    for o in others:
        for e in o["evse_ids"]:
            by_evse.setdefault(e, []).append(o["id"])
        for r in o.get("refs") or ():
            by_ref.setdefault(tuple(r), []).append(o["id"])
    left = []
    for n in sorted(nap, key=lambda x: x["id"]):
        cands = sorted({o for e in n["evse_ids"] for o in by_evse.get(e, ())})
        if cands and join(n, cands[0]):
            stats["nap_matched_evse"] = stats.get("nap_matched_evse", 0) + 1
            continue
        cands = sorted({o for r in n.get("refs") or () for o in by_ref.get(tuple(r), ())})
        if cands and join(n, cands[0]):
            stats["nap_matched_ref"] = stats.get("nap_matched_ref", 0) + 1
            continue
        left.append(n)
    joined = set(taken.values())
    grid = _grid(others)
    claim = {}
    for n in left:
        best = None
        for x in _near(grid, n):
            if find(x["id"]) in taken:
                continue
            d = metres(n, x)
            if eligible(x, n, d) and (best is None or (d, x["id"]) < best[:2]):
                best = (d, x["id"])
        if best is not None:
            root = find(best[1])
            if root not in claim or (best[0], n["id"]) < claim[root][:2]:
                claim[root] = (best[0], n["id"], best[1])
    for root, (d, nid, oid) in sorted(claim.items()):
        if root not in taken and nid not in joined:
            taken[root] = nid
            joined.add(nid)
            union(nid, oid)
            stats["nap_matched_geo"] = stats.get("nap_matched_geo", 0) + 1
    stats["nap_new"] = len(nap) - len(joined)


# ─────────────────────────────────── writing ───────────────────────────────────

def source_rows(providers, used):
    o = providers["osm"]
    rows = [(OSM_SOURCE_ID, "osm", None, o["name"], o["licence"], o["licence_url"], o["attribution"], o["url"])]
    allow = {p["id"]: p for p in providers["allow"]}
    for pid in sorted(used):
        p = allow[pid]
        rows.append((OCM_SOURCE_BASE + pid, "ocm", pid, p["name"], p["licence"], p.get("licence_url"),
                     p["attribution"], p.get("url")))
    return rows


def write(path, meta, sources, stations, clusters, ref, connectors, extra=None):
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    with db:
        db.executemany("INSERT INTO meta VALUES (?,?)", sorted(meta.items()))
        db.executemany("INSERT INTO source VALUES (?,?,?,?,?,?,?,?)", sources)
        for s in stations:
            db.execute("INSERT INTO station VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                s["id"], clusters[s["id"]], s["source"], s["src_id"], s["country"], s["lat"], s["lon"],
                s["name"], s["address"], s["operator"], s["operator_id"], s["usage"], s["status"],
                s["status_raw"], s["status_date"], s["verified_date"], s["created_date"], s["points"],
                s["capacity"], s["max_kw"], s["has_dc"], s["cost"], s["opening_hours"],
                json.dumps(s["evse_ids"]) if s["evse_ids"] and s["kind"] != "nap" else None))
            db.executemany("INSERT INTO connector VALUES (?,?,?,?,?,?)",
                           [(s["id"], c["standard"], c["current"], c["kw"], c["qty"], c["status"])
                            for c in s["connectors"]])
        db.executemany("INSERT INTO ocm_operator VALUES (?,?)",
                       sorted((o["ID"], o["Title"]) for o in ref.get("Operators", []) if o.get("Title")))
        rows = []
        for t in sorted(ref.get("ConnectionTypes", []), key=lambda t: t["ID"]):
            std = connectors["ocm"].get(str(t["ID"]), "OTHER")
            rows.append((t["ID"], std, "DC" if std in connectors["dc_standards"] else None))
        db.executemany("INSERT INTO ocm_connection VALUES (?,?,?)", rows)
        if extra is not None:
            extra(db)
    db.execute("VACUUM")
    db.close()


# ─────────────────────────────────── /2 tables ───────────────────────────────────

def evse_rows(stations, clusters, report):
    """-> [(station, evse_id, evse_norm, standards, current, max_kw)] for every physical point.

    NAP records give one row per point (an id or not); OSM and OCM records one row per EVSE id
    they carry. evse_norm is set only for a valid eMI3 id, and ONE id never names points of two
    clusters: where it would, the cluster holding it through a national registry keeps it (else
    the smallest cluster id) and the other rows keep their original id with evse_norm NULL —
    counted as `evse_conflicts`."""
    rows = []
    for s in sorted(stations, key=lambda x: x["id"]):
        if s["kind"] == "nap":
            for e in s["evses"]:
                norm = evse_key(e["raw"]) if e["raw"] and evse_id(e["raw"]) else None
                rows.append([s["id"], e["raw"] if e["raw"] != norm else None, norm, ",".join(e["standards"]) or None,
                             e["current"], e["kw"], True])
        else:
            raw = s.get("evse_raw") or {}
            for e in s["evse_ids"]:
                orig = raw.get(e) or e
                norm = evse_key(orig)
                rows.append([s["id"], orig if orig != norm else None, norm, None, None, None, False])
    holders = {}
    for r in rows:
        if r[2]:
            holders.setdefault(r[2], {}).setdefault(clusters[r[0]], False)
            if r[6]:
                holders[r[2]][clusters[r[0]]] = True
    keep = {}
    for norm, cl in holders.items():
        nap_cl = sorted(c for c, n in cl.items() if n)
        keep[norm] = nap_cl[0] if nap_cl else min(cl)
    conflicts = 0
    for r in rows:
        if r[2] and clusters[r[0]] != keep[r[2]]:
            r[2] = None
            conflicts += 1
    report["evse_conflicts"] = conflicts
    return [tuple(r[:6]) for r in rows]


def write_v2_tables(db, stations, clusters, live_source, registry, report):
    """The /2 tables into an open catalogue (charging_db.write's `extra`)."""
    db.executescript(SCHEMA2)
    db.executemany("INSERT INTO evse(station, evse_id, evse_norm, standards, current, max_kw) VALUES (?,?,?,?,?,?)",
                   evse_rows(stations, clusters, report))
    keys, refs = [], []
    for s in sorted(stations, key=lambda x: x["id"]):
        for k in s.get("live_keys") or ():
            keys.append((s["id"], live_source.get(s["source"], ""), k))
        for scheme, ref in s.get("refs") or ():
            refs.append((s["id"], scheme, ref))
    db.executemany("INSERT INTO live_key VALUES (?,?,?)", keys)
    db.executemany("INSERT INTO station_ref VALUES (?,?,?)", refs)
    db.executemany("INSERT INTO source_registry VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", registry)


def nap_gate(current, previous):
    """current/previous: {nap source id: stations}. A national registry with >= 50 stations in the
    previous release losing more than 20 % (a source that failed to download: all of them) fails
    the publish — a short registry must not replace a good catalogue."""
    problems = []
    for src, before in sorted((previous or {}).items()):
        now = current.get(src, 0)
        if before >= COUNTRY_LOSS_FLOOR and now < before * (1 - COUNTRY_LOSS):
            problems.append("%s: %d national registry stations, the previous release had %d (-%.0f %%, more than %d %%)"
                            % (src, now, before, 100.0 * (before - now) / max(before, 1), COUNTRY_LOSS * 100))
    return problems


def validate2(sqlite_path, gz_path, cfg, previous_nap=None, min_nap=None):
    """-> (problems, notes) of a /2 file: everything validate() checks of /1 (with format /2 and the
    /2 size gates), plus: every EVSE row on a station, one normalised EVSE id in one cluster only,
    every NAP source with a licence and attribution in `source` and `source_registry`, the national
    registry counts against their minimum and the previous release."""
    problems, notes = validate(sqlite_path, gz_path, cfg, None, {}, None, fmt=FORMAT2,
                               max_sqlite=MAX_SQLITE2, max_gz=MAX_GZ2)
    db = sqlite3.connect(sqlite_path)
    q = lambda sql, *a: db.execute(sql, a).fetchall()
    if q("SELECT count(*) FROM evse WHERE station NOT IN (SELECT id FROM station)")[0][0]:
        problems.append("an evse row without its station")
    n = q("SELECT count(*) FROM (SELECT e.evse_norm FROM evse e JOIN station s ON s.id = e.station "
          "WHERE e.evse_norm IS NOT NULL GROUP BY 1 HAVING count(DISTINCT s.cluster) > 1)")[0][0]
    if n:
        problems.append("%d EVSE ids name points of two clusters" % n)
    if q("SELECT count(*) FROM source WHERE kind = 'nap' AND (licence = '' OR attribution = '')")[0][0] or \
            q("SELECT count(*) FROM source_registry WHERE trim(licence) = '' OR trim(attribution) = ''")[0][0]:
        problems.append("a national source without licence or attribution")
    by_src = dict(q("SELECT r.source, count(s.id) FROM source_registry r LEFT JOIN station s ON s.source = r.source_id "
                    "WHERE r.kind = 'static' GROUP BY 1"))
    for src, want in sorted((min_nap or {}).items()):
        if by_src.get(src, 0) < want:
            problems.append("%s: %d national registry stations, at least %d expected" % (src, by_src.get(src, 0), want))
    if previous_nap is not None:
        problems += nap_gate(by_src, previous_nap)
    notes.append("national registries: %s" % ", ".join("%s %d" % kv for kv in sorted(by_src.items())))
    db.close()
    return problems, notes


# ─────────────────────────────────── gates ───────────────────────────────────

def previous_gate(current, previous):
    """current/previous: {country: OCM station count}. -> problems (publish must fail)."""
    problems = []
    for cc, before in sorted((previous or {}).items()):
        now = current.get(cc, 0)
        if before >= COUNTRY_LOSS_FLOOR and now < before * (1 - COUNTRY_LOSS):
            problems.append("%s: %d OCM stations, the previous release had %d (-%.0f %%, more than %d %%)"
                            % (cc, now, before, 100.0 * (before - now) / before, COUNTRY_LOSS * 100))
    tb, tn = sum((previous or {}).values()), sum(current.values())
    if tb and tn < tb * TOTAL_KEEP:
        problems.append("total %d OCM stations, the previous release had %d (below %.0f %%)"
                        % (tn, tb, TOTAL_KEEP * 100))
    return problems


def osm_gate(current, previous):
    """current/previous: {country: OSM station count}. A country with >= 50 OSM stations in the
    previous release losing more than 20 % fails the publish: a country Overpass answered short
    must not replace a good catalogue."""
    problems = []
    for cc, before in sorted((previous or {}).items()):
        now = current.get(cc, 0)
        if before >= COUNTRY_LOSS_FLOOR and now < before * (1 - COUNTRY_LOSS):
            problems.append("%s: %d OSM stations, the previous release had %d (-%.0f %%, more than %d %%)"
                            % (cc, now, before, 100.0 * (before - now) / before, COUNTRY_LOSS * 100))
    return problems


def validate(sqlite_path, gz_path, cfg, previous_counts=None, min_ocm=MIN_OCM, previous_osm=None,
             fmt=FORMAT, max_sqlite=None, max_gz=None):
    """-> (problems, notes). Any problem fails the run; notes are reported only."""
    problems, notes = [], []
    db = sqlite3.connect(sqlite_path)
    q = lambda sql, *a: db.execute(sql, a).fetchall()
    if q("PRAGMA integrity_check") != [("ok",)]:
        problems.append("integrity_check failed")
    if q("PRAGMA foreign_key_check"):
        problems.append("foreign keys broken")
    meta = dict(q("SELECT key, value FROM meta"))
    if meta.get("format") != fmt:
        problems.append("meta.format %r, expected %r" % (meta.get("format"), fmt))
    for col, allowed in (("usage", USAGES), ("status", STATUSES)):
        bad = q("SELECT %s, count(*) FROM station WHERE %s NOT IN (%s) GROUP BY 1"
                % (col, col, ",".join("?" * len(allowed))), *allowed)
        if bad:
            problems.append("station.%s outside the enum: %s" % (col, bad))
    bad = q("SELECT status, count(*) FROM connector WHERE status IS NOT NULL AND status NOT IN (%s) GROUP BY 1"
            % ",".join("?" * len(STATUSES)), *STATUSES)
    if bad:
        problems.append("connector.status outside the enum: %s" % bad)
    n = q("SELECT count(*) FROM station WHERE status_raw IN ('ocm:200','ocm:210')")[0][0]
    if n:
        problems.append("%d removed (OCM 200/210) stations stored" % n)
    if q("SELECT count(*) FROM (SELECT source, src_id FROM station GROUP BY 1,2 HAVING count(*) > 1)")[0][0]:
        problems.append("a (source, src_id) appears twice")
    if q("SELECT count(*) FROM source WHERE licence IS NULL OR trim(licence)='' OR attribution IS NULL OR trim(attribution)=''")[0][0]:
        problems.append("a source without licence or attribution")
    if q("SELECT count(*) FROM station WHERE source NOT IN (SELECT id FROM source)")[0][0]:
        problems.append("a station without a source")
    if q("SELECT licence FROM source WHERE id=?", OSM_SOURCE_ID) != [("ODbL 1.0",)]:
        problems.append("the OSM source row is not ODbL 1.0")
    n = q("SELECT count(*) FROM station WHERE max_kw IS NOT NULL AND NOT (max_kw > 0 AND max_kw <= 1000)")[0][0] + \
        q("SELECT count(*) FROM connector WHERE kw IS NOT NULL AND NOT (kw > 0 AND kw <= 1000)")[0][0]
    if n:
        problems.append("%d kW values outside 0 < kW <= 1000" % n)
    # report only: a station filed under a country it is not near
    countries = Countries(cfg)
    off = [r for r in q("SELECT country, lat, lon FROM station") if not countries.in_bbox(*r)]
    if off:
        notes.append("%d stations outside their country's bbox +-%.1f deg" % (len(off), BBOX_SLACK))
    ocm_by = dict(q("SELECT country, count(*) FROM station WHERE source >= ? AND source < ? GROUP BY 1",
                    OCM_SOURCE_BASE, NAP_SOURCE_BASE))
    for cc, want in sorted((min_ocm or {}).items()):
        if ocm_by.get(cc, 0) < want:
            problems.append("%s: %d OCM stations, at least %d expected" % (cc, ocm_by.get(cc, 0), want))
    if previous_counts is not None:
        problems += previous_gate(ocm_by, previous_counts)
    if previous_osm is not None:
        osm_by = dict(q("SELECT country, count(*) FROM station WHERE source = ? GROUP BY 1", OSM_SOURCE_ID))
        problems += osm_gate(osm_by, previous_osm)
    db.close()
    s, g = os.path.getsize(sqlite_path), os.path.getsize(gz_path)
    name = os.path.basename(sqlite_path)
    max_sqlite = MAX_SQLITE if max_sqlite is None else max_sqlite
    max_gz = MAX_GZ if max_gz is None else max_gz
    if s > max_sqlite:
        problems.append("STOP: %s is %d bytes, above %d — report to the owner" % (name, s, max_sqlite))
    if g > max_gz:
        problems.append("STOP: %s.gz is %d bytes, above %d — report to the owner" % (name, g, max_gz))
    notes.append("sizes: sqlite %d bytes, gz %d bytes" % (s, g))
    return problems, notes
