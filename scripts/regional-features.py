#!/usr/bin/env python3
"""Navigation features of ONE country: speed cameras, enforcement (red light, section control),
level crossings and traffic signals — extracted from the same PBF the country graph is built from.

  usage:
    regional-features.py build <pbf> <CODE> <graph_version> <out.features> [--source S]
    regional-features.py build-lite <europe_lite_final.osm.pbf> <europe.osm.pbf> <version> <out> [--source S]
    regional-features.py gates-lite <europe_lite.features> <previous europe-lite.json|->
    regional-features.py validate <file.features> [--bbox W,S,E,N]
    regional-features.py check <file.features> <CODE> <regional.json> <previous-manifest.json|->
    regional-features.py counts <file.features>
  factory (reads <work>/<code>.package.json):
    regional-features.py backfill <work>                            carried countries without features
    regional-features.py gates <work> <regional.json> <previous-manifest.json|->

GATES (owner, 2026-09-26 — the first contract, until there is history): HARD FAIL on an invalid
file, a file of another country, objects far outside the country, and a type that had objects in
the previous release and has none now. A change over 25 % is a WARNING, not a failure: there is no
history yet to set a threshold on. Counts ride in every manifest, so the history accumulates.

FORMAT wedrive-features/1 — a text file like the frontier (same discipline: strict parser, sorted
rows, identical bytes for identical input):

    wedrive-features 1
    country MD
    graph_version 2026-09-24
    source <replication timestamp of the extract>
    entries N
    counts camera_speed=81,...
    id type lat_e7 lon_e7 bearing maxspeed way_ids attrs
    n123 camera_speed 470123456 288765432 135 50 w44,w45 bsrc=deg

    id        n<osm node id> or r<osm relation id>. Stable across months while OSM keeps the object.
    type      camera_speed | camera_red_light | camera_section | level_crossing |
              tram_level_crossing | traffic_signals
    bearing   the compass direction of the TRAVEL the object applies to, degrees 0..359; several
              separated by ';' (both ways); '-' when OSM does not say. Never guessed.
    maxspeed  km/h, '-' when unknown.
    way_ids   the drivable OSM ways the node lies on ('-' for an object beside the road: the app
              falls back to projecting it onto the route).
    attrs     k=v;k=v or '-': bsrc (where the bearing came from: fwd|bwd|deg|card|rel|relrev),
              rel (the enforcement relation that enriched a node), to (section end lat_e7,lon_e7),
              sections (section way ids), signal (the traffic_signals=* value).

Only what OSM states is written: a camera with no direction has bearing '-', not a guess from the
road. railway=crossing (people over rails) is NOT a road crossing and is not extracted.
"""
import collections
import json
import math
import os
import re
import subprocess
import sys
import tempfile

FORMAT = "wedrive-features"
VERSION = 1
COLUMNS = "id type lat_e7 lon_e7 bearing maxspeed way_ids attrs"
TYPES = ("camera_speed", "camera_red_light", "camera_section", "level_crossing",
         "tram_level_crossing", "traffic_signals")
# roads a car uses; a feature on a footway or a platform is not a navigation feature
DRIVABLE = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
            "living_street", "service", "road", "track", "motorway_link", "trunk_link",
            "primary_link", "secondary_link", "tertiary_link"}
CARDINAL = {d: i * 22.5 for i, d in enumerate(
    "N NNE NE ENE E ESE SE SSE S SSW SW WSW W WNW NW NNW".split())}
BBOX_MARGIN = 0.2
LITE_CODE = "EU"
EUROPE_BBOX = [-32.0, 27.0, 69.0, 82.0]          # Geofabrik's Europe, with the Atlantic islands


class FeaturesError(Exception):
    pass


# ---------------------------------------------------------------- OPL

def opl_tags(field):
    out = {}
    if not field:
        return out
    for kv in field.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[unesc(k)] = unesc(v)
    return out


def unesc(s):
    return re.sub(r"%([0-9a-fA-F]+)%", lambda m: chr(int(m.group(1), 16)), s)


def parse_opl(path):
    """-> nodes {id: (lat, lon, tags)}, ways {id: (refs, tags)}, rels {id: (members, tags)}"""
    nodes, ways, rels = {}, {}, {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split(" ")
            if not parts or not parts[0]:
                continue
            kind, oid = parts[0][0], int(parts[0][1:])
            fields = {p[0]: p[1:] for p in parts[1:] if p}
            tags = opl_tags(fields.get("T", ""))
            if kind == "n":
                if fields.get("x") and fields.get("y"):
                    nodes[oid] = (float(fields["y"]), float(fields["x"]), tags)
            elif kind == "w":
                refs = [int(r[1:]) for r in fields.get("N", "").split(",") if r.startswith("n")]
                ways[oid] = (refs, tags)
            elif kind == "r":
                members = []
                for m in fields.get("M", "").split(","):
                    if not m:
                        continue
                    ref, _, role = m.partition("@")
                    members.append((ref[0], int(ref[1:]), unesc(role)))
                rels[oid] = (members, tags)
    return nodes, ways, rels


def osmium(*args):
    r = subprocess.run(["osmium"] + list(args), capture_output=True, text=True)
    # getid answers 1 when some requested objects are not in the file: a neighbour node of a way cut
    # at the edge of an extract. That node only costs a bearing, never a wrong one.
    if r.returncode != 0 and not (args[0] == "getid" and r.returncode == 1):
        raise FeaturesError("osmium %s failed: %s" % (args[0], r.stderr[-400:]))


# ---------------------------------------------------------------- geometry

def bearing(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def angle_diff(a, b):
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def maxspeed_kmh(v):
    if not v:
        return None
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(mph)?\s*", v)
    if not m:
        return None                                   # "RO:urban", "signals", "none": unknown
    n = float(m.group(1))
    return int(round(n * 1.609344)) if m.group(2) else int(round(n))


def parse_direction(v):
    """OSM direction value -> ('fwd'|'bwd', None) | ('deg'|'card', [degrees]) | (None, None)."""
    if v is None:
        return None, None
    v = v.strip()
    if v in ("forward",):
        return "fwd", None
    if v in ("backward", "reverse"):
        return "bwd", None
    if v == "both":
        return "both", None
    degs, src = [], None
    for part in v.split(";"):
        part = part.strip()
        if re.fullmatch(r"-?\d+(\.\d+)?", part):
            degs.append(float(part) % 360.0); src = src or "deg"
        elif part.upper() in CARDINAL:
            degs.append(CARDINAL[part.upper()]); src = "card" if src in (None, "card") else src
        else:
            return None, None                          # a range "0-90" or free text: unknown
    return (src, degs) if degs else (None, None)


# ---------------------------------------------------------------- extraction

def build(pbf, code, graph_version, out, source="", enforcement_pbf=None, lite=False, log=print):
    """Country: pbf is the country extract, everything comes from it.

    Europe Lite (lite=True): pbf is europe_lite_final.osm.pbf — by construction the Lite road
    network with every node of it, tags included — so node features and their road association come
    from the very ways Lite routes on. Enforcement relations are not in it (the Lite filter keeps
    only turn restrictions): they come from enforcement_pbf, the full European source, and are kept
    only where their device, from or to lies on a Lite road."""
    with tempfile.TemporaryDirectory() as tmp:
        feat, enf = os.path.join(tmp, "feat.opl"), os.path.join(tmp, "enf.opl")
        osmium("tags-filter", "-R", "-O", "-f", "opl", "-o", feat, pbf,
               "n/highway=speed_camera", "n/railway=level_crossing",
               "n/railway=tram_level_crossing", "n/highway=traffic_signals")
        osmium("tags-filter", "-O", "-f", "opl", "-o", enf, enforcement_pbf or pbf, "r/type=enforcement")
        fnodes, _, _ = parse_opl(feat)
        enodes, eways, erels = parse_opl(enf)
        # an enforcement DEVICE may be a node that is not itself tagged as a camera (red-light
        # cameras often are): it is associated with its road the same way
        devices = {ref for members, tags in erels.values()
                   if tags.get("enforcement") in ("maxspeed", "traffic_signals")
                   for t, ref, role in members if t == "n" and role == "device"}
        located = set(fnodes) | {d for d in devices if d in enodes}
        if lite:
            located |= {ref for members, tags in erels.values() for t, ref, role in members
                        if t == "n" and role in ("from", "to") and ref in enodes}

        # parent ways of every feature node, and the neighbours that give a way its direction
        ids = os.path.join(tmp, "ids.txt")
        with open(ids, "w") as f:
            f.write("".join("n%d\n" % i for i in sorted(located)))
        parents_opl = os.path.join(tmp, "parents.opl")
        osmium("getparents", "-O", "-f", "opl", "-o", parents_opl, "-i", ids, pbf)
        _, pways, _ = parse_opl(parents_opl)
        pways = {w: v for w, v in pways.items() if v[1].get("highway") in DRIVABLE}
        need = set()
        for refs, _ in pways.values():
            for i, r in enumerate(refs):
                if r in located:
                    if i > 0:
                        need.add(refs[i - 1])
                    if i + 1 < len(refs):
                        need.add(refs[i + 1])
        coords = {i: (n[0], n[1]) for i, n in fnodes.items()}
        coords.update({i: (n[0], n[1]) for i, n in enodes.items()})
        need -= set(coords)
        if need:
            nid, nopl = os.path.join(tmp, "need.txt"), os.path.join(tmp, "need.opl")
            with open(nid, "w") as f:
                f.write("".join("n%d\n" % i for i in sorted(need)))
            osmium("getid", "-O", "-f", "opl", "-o", nopl, pbf, "-i", nid)
            nn, _, _ = parse_opl(nopl)
            coords.update({i: (n[0], n[1]) for i, n in nn.items()})

    on_ways = collections.defaultdict(list)          # node -> [(way id, forward bearing, interior)]
    for w, (refs, _) in pways.items():
        for i, r in enumerate(refs):
            if r not in located:
                continue
            a = refs[i - 1] if i > 0 else r
            b = refs[i + 1] if i + 1 < len(refs) else r
            if a == b or a not in coords or b not in coords:
                continue
            on_ways[r].append((w, bearing(coords[a], coords[b]), 0 < i < len(refs) - 1))

    def way_bearing(node):
        """The way's own direction at the node, if every parent way agrees within 30 degrees."""
        cand = on_ways.get(node, [])
        interior = [c for c in cand if c[2]]
        pick = interior if len(interior) == 1 else cand
        if not pick:
            return None
        if all(angle_diff(pick[0][1], c[1]) <= 30.0 for c in pick):
            return pick[0][1]
        return None

    rows, stats = {}, collections.Counter()
    for nid, (lat, lon, tags) in fnodes.items():
        if tags.get("highway") == "speed_camera":
            typ, dkey = "camera_speed", "direction"
        elif tags.get("railway") == "level_crossing":
            typ, dkey = "level_crossing", None
        elif tags.get("railway") == "tram_level_crossing":
            typ, dkey = "tram_level_crossing", None
        else:
            typ, dkey = "traffic_signals", "traffic_signals:direction"
        ways = sorted(w for w, _, _ in on_ways.get(nid, []))
        if typ != "camera_speed" and not ways:
            stats["dropped_off_road_" + typ] += 1       # a crossing/signal on no drivable road
            continue
        if lite and not ways:
            stats["dropped_off_lite_" + typ] += 1
            continue
        brg, bsrc = [], None
        if dkey:
            src, degs = parse_direction(tags.get(dkey) if dkey == "traffic_signals:direction"
                                        else tags.get("direction"))
            if src in ("fwd", "bwd"):
                wb = way_bearing(nid)
                if wb is not None:
                    brg, bsrc = [(wb + (180.0 if src == "bwd" else 0.0)) % 360.0], src
                else:
                    stats["direction_unresolved"] += 1
            elif src in ("deg", "card"):
                brg, bsrc = degs, src
        attrs = {}
        if bsrc:
            attrs["bsrc"] = bsrc
        if typ == "traffic_signals" and tags.get("traffic_signals"):
            attrs["signal"] = re.sub(r"[^A-Za-z0-9_:.-]", "_", tags["traffic_signals"])
        rows["n%d" % nid] = {"type": typ, "lat": lat, "lon": lon, "bearing": brg,
                             "maxspeed": maxspeed_kmh(tags.get("maxspeed")) if typ.startswith("camera") else None,
                             "ways": ways, "attrs": attrs}

    # enforcement relations: direction (from -> to), red-light type, section control
    for rid, (members, tags) in erels.items():
        kind = tags.get("enforcement")
        if kind not in ("maxspeed", "traffic_signals", "average_speed"):
            continue
        by = collections.defaultdict(list)
        for t, ref, role in members:
            by[(t, role)].append(ref)
        if lite and not any(on_ways.get(n) for n in by[("n", "device")] + by[("n", "from")] + by[("n", "to")]):
            stats["relation_off_lite"] += 1
            continue
        frm = [coords[n] for n in by[("n", "from")] if n in coords]
        to = [coords[n] for n in by[("n", "to")] if n in coords]
        dev = [n for n in by[("n", "device")] if n in coords]
        ms = maxspeed_kmh(tags.get("maxspeed"))
        if kind == "average_speed":
            if not frm or not to:
                stats["relation_incomplete"] += 1
                continue
            sections = sorted(set(by[("w", "section")]))
            rows["r%d" % rid] = {"type": "camera_section", "lat": frm[0][0], "lon": frm[0][1],
                                 "bearing": [bearing(frm[0], to[0])], "maxspeed": ms, "ways": [],
                                 "attrs": {"bsrc": "rel", "to": "%d,%d" % e7(to[0]),
                                           **({"sections": ",".join("w%d" % w for w in sections)} if sections else {})}}
            continue
        typ = "camera_red_light" if kind == "traffic_signals" else "camera_speed"
        targets = dev or []
        if not targets and not frm:
            stats["relation_incomplete"] += 1
            continue
        for d in targets or [None]:
            pos = coords[d] if d is not None else frm[0]
            if frm and d is not None and coords[d] != frm[0]:
                rb, src = bearing(frm[0], coords[d]), "rel"
            elif to and d is not None and coords[d] != to[0]:
                rb, src = bearing(coords[d], to[0]), "relrev"
            elif frm and to:
                rb, src = bearing(frm[0], to[0]), "rel"
            else:
                rb, src = None, None
            key = "n%d" % d if d is not None else "r%d" % rid
            row = rows.get(key)
            if row is not None and row["type"].startswith("camera"):
                row["type"] = typ if typ == "camera_red_light" else row["type"]
                if not row["bearing"] and rb is not None:
                    row["bearing"], row["attrs"]["bsrc"] = [rb], src
                if row["maxspeed"] is None:
                    row["maxspeed"] = ms
                row["attrs"]["rel"] = "r%d" % rid
                stats["relation_enriched_node"] += 1
            else:
                rows["r%d" % rid if d is None or key in rows else key] = {
                    "type": typ, "lat": pos[0], "lon": pos[1],
                    "bearing": [rb] if rb is not None else [], "maxspeed": ms,
                    "ways": sorted(w for w, _, _ in on_ways.get(d, [])) if d is not None else [],
                    "attrs": {**({"bsrc": src} if src else {}), "rel": "r%d" % rid}}
                stats["relation_row"] += 1

    write(out, code, graph_version, source, rows)
    counts = counts_of(rows)
    log("   features %s %s: %s | %s" % (code, graph_version,
        ", ".join("%s %d" % (t, counts[t]) for t in TYPES),
        ", ".join("%s %d" % kv for kv in sorted(stats.items())) or "-"))
    return rows, stats


def e7(p):
    return int(round(p[0] * 1e7)), int(round(p[1] * 1e7))


def counts_of(rows):
    c = collections.Counter(r["type"] for r in rows.values())
    return {t: c.get(t, 0) for t in TYPES}


# ---------------------------------------------------------------- the file

def fmt_row(fid, r):
    la, lo = e7((r["lat"], r["lon"]))
    brg = ";".join(str(int(round(b)) % 360) for b in r["bearing"]) or "-"
    ms = str(r["maxspeed"]) if r["maxspeed"] else "-"
    ways = ",".join("w%d" % w for w in r["ways"]) or "-"
    attrs = ";".join("%s=%s" % kv for kv in sorted(r["attrs"].items())) or "-"
    return "%s %s %d %d %s %s %s %s" % (fid, r["type"], la, lo, brg, ms, ways, attrs)


def sort_key(fid):
    return (fid[0], int(fid[1:]))


def write(path, country, graph_version, source, rows):
    counts = counts_of(rows)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="ascii", newline="\n") as f:
        f.write("%s %d\n" % (FORMAT, VERSION))
        f.write("country %s\n" % country)
        f.write("graph_version %s\n" % graph_version)
        f.write("source %s\n" % (source or "-"))
        f.write("entries %d\n" % len(rows))
        f.write("counts %s\n" % ",".join("%s=%d" % (t, counts[t]) for t in TYPES))
        f.write(COLUMNS + "\n")
        for fid in sorted(rows, key=sort_key):
            f.write(fmt_row(fid, rows[fid]) + "\n")
    os.replace(tmp, path)


def parse(text, name="features"):
    """Strict. -> dict(country, graph_version, source, counts, rows=[dict])."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or lines[0] != "%s %d" % (FORMAT, VERSION):
        raise FeaturesError("%s: not a %s %d file" % (name, FORMAT, VERSION))
    head, i = {}, 1
    while i < len(lines) and lines[i] != COLUMNS:
        k, _, v = lines[i].partition(" ")
        if not v:
            raise FeaturesError("%s: malformed header line %d" % (name, i + 1))
        head[k] = v
        i += 1
    if i == len(lines):
        raise FeaturesError("%s: no column line" % name)
    for k in ("country", "graph_version", "source", "entries", "counts"):
        if k not in head:
            raise FeaturesError("%s: header has no %s" % (name, k))
    rows, seen = [], set()
    for n, line in enumerate(lines[i + 1:], start=i + 2):
        f = line.split(" ")
        if len(f) != 8:
            raise FeaturesError("%s: line %d has %d fields" % (name, n, len(f)))
        fid, typ = f[0], f[1]
        if not re.fullmatch(r"[nr]\d+", fid):
            raise FeaturesError("%s: line %d: bad id %r" % (name, n, fid))
        if fid in seen:
            raise FeaturesError("%s: line %d: duplicate id %s" % (name, n, fid))
        seen.add(fid)
        if typ not in TYPES:
            raise FeaturesError("%s: line %d: unknown type %r" % (name, n, typ))
        try:
            la, lo = int(f[2]), int(f[3])
            brg = [] if f[4] == "-" else [int(b) for b in f[4].split(";")]
            ms = None if f[5] == "-" else int(f[5])
        except ValueError:
            raise FeaturesError("%s: line %d is not numeric" % (name, n))
        if not (-900000000 <= la <= 900000000 and -1800000000 <= lo <= 1800000000):
            raise FeaturesError("%s: line %d: coordinate out of range" % (name, n))
        if any(not 0 <= b < 360 for b in brg):
            raise FeaturesError("%s: line %d: bearing out of range" % (name, n))
        if ms is not None and not 0 < ms <= 300:
            raise FeaturesError("%s: line %d: implausible maxspeed %d" % (name, n, ms))
        ways = [] if f[6] == "-" else f[6].split(",")
        if any(not re.fullmatch(r"w\d+", w) for w in ways):
            raise FeaturesError("%s: line %d: bad way id" % (name, n))
        attrs = {} if f[7] == "-" else dict(kv.split("=", 1) for kv in f[7].split(";"))
        if typ == "camera_section" and "to" not in attrs:
            raise FeaturesError("%s: line %d: section control without its end" % (name, n))
        if typ in ("level_crossing", "tram_level_crossing", "traffic_signals") and not ways:
            raise FeaturesError("%s: line %d: %s on no road" % (name, n, typ))
        rows.append({"id": fid, "type": typ, "lat_e7": la, "lon_e7": lo, "bearing": brg,
                     "maxspeed": ms, "ways": ways, "attrs": attrs})
    if len(rows) != int(head["entries"]):
        raise FeaturesError("%s: header says %s entries, file has %d" % (name, head["entries"], len(rows)))
    counts = dict(kv.split("=") for kv in head["counts"].split(","))
    real = collections.Counter(r["type"] for r in rows)
    if any(int(counts.get(t, 0)) != real.get(t, 0) for t in TYPES):
        raise FeaturesError("%s: header counts disagree with the rows" % name)
    return {"country": head["country"], "graph_version": head["graph_version"],
            "source": head["source"], "counts": {t: int(counts.get(t, 0)) for t in TYPES}, "rows": rows}


def load(path):
    with open(path, encoding="ascii") as f:
        return parse(f.read(), os.path.basename(path))


# ---------------------------------------------------------------- gates

def gate(fe, code, bbox, previous_counts):
    """-> (problems, warnings). HARD: format (load), wrong country, coordinates far outside the
    country, a type that had objects in the previous release and now has none. WARNING only: a
    large change — there is no history yet to set a threshold on (owner, 2026-09-26)."""
    problems, warnings = [], []
    if fe["country"] != code:
        problems.append("file is for %s, not %s" % (fe["country"], code))
    w, s, e, n = bbox
    far = [r["id"] for r in fe["rows"] if not (s - BBOX_MARGIN <= r["lat_e7"] / 1e7 <= n + BBOX_MARGIN
                                              and w - BBOX_MARGIN <= r["lon_e7"] / 1e7 <= e + BBOX_MARGIN)]
    if far:
        problems.append("%d objects outside %s (+%.1f deg), e.g. %s" % (len(far), code, BBOX_MARGIN, far[:3]))
    for t in TYPES:
        now, before = fe["counts"][t], (previous_counts or {}).get(t)
        if before:
            if now == 0:
                problems.append("%s: %d in the previous release, 0 now" % (t, before))
            elif abs(now - before) / before > 0.25:
                warnings.append("%s: %d -> %d (%+.0f %%)" % (t, before, now, 100.0 * (now - before) / before))
    return problems, warnings


def dated_extract(source):
    """The dated Geofabrik file a package was built from: <name>-latest -> <name>-YYMMDD."""
    url, rep = source.get("url", ""), source.get("replication", "")
    if not url.endswith("-latest.osm.pbf") or len(rep) < 10:
        return None
    return url.replace("-latest.osm.pbf", "-%s%s%s.osm.pbf" % (rep[2:4], rep[5:7], rep[8:10]))


def md5(path):
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backfill(work):
    """MIGRATION: a carried country published before features existed gets them here, from the very
    extract its graph was built from (Geofabrik's dated file, md5 = provenance), so the way ids
    name ways of THAT graph. Not served any more -> WARNING and the country ships without features
    until its next rebuild; features are never built from a different snapshot than the graph."""
    made = []
    for name in sorted(os.listdir(work)):
        if not name.endswith(".package.json"):
            continue
        path = os.path.join(work, name)
        d = json.load(open(path, encoding="utf-8"))
        if d.get("features"):
            continue
        if not d.get("carried"):
            raise FeaturesError("%s was built in this run but has no features" % d["code"])
        src = d.get("source") or {}
        url = dated_extract(src)
        if not url:
            print("   WARNING %s: no provenance to find the source extract — no features until rebuilt" % d["code"])
            continue
        pbf = os.path.join(work, "src-features-%s.osm.pbf" % d["code"].lower())
        r = subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", pbf, url])
        if r.returncode != 0 or md5(pbf) != src.get("md5"):
            print("   WARNING %s: %s not served or not the recorded extract — no features until rebuilt" % (d["code"], url))
            if os.path.exists(pbf):
                os.remove(pbf)
            continue
        out = os.path.join(work, "%s-%s.features" % (d["code"].lower(), d["graph_version"]))
        build(pbf, d["code"], d["graph_version"], out, source=src.get("replication", ""))
        os.remove(pbf)
        fe = load(out)
        d["features"] = {"format": "%s/%d" % (FORMAT, VERSION), "file": os.path.basename(out),
                         "bytes": os.path.getsize(out), "sha256": sha256(out),
                         "entries": len(fe["rows"]), "counts": fe["counts"], "backfilled": True}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
        made.append(d["code"])
    return made


def main(argv):
    if len(argv) < 2:
        print(__doc__); return 2
    cmd, a = argv[1], argv[2:]
    try:
        if cmd == "build":
            opts = dict(zip(a[4::2], a[5::2]))
            build(a[0], a[1], a[2], a[3], source=opts.get("--source", ""))
            load(a[3])
            return 0
        if cmd == "build-lite":
            # build-lite <europe_lite_final.osm.pbf> <europe.osm.pbf> <version> <out> [--source S]
            opts = dict(zip(a[4::2], a[5::2]))
            build(a[0], LITE_CODE, a[2], a[3], source=opts.get("--source", ""),
                  enforcement_pbf=a[1], lite=True)
            load(a[3])
            return 0
        if cmd == "gates-lite":
            # gates-lite <europe_lite.features> <previous europe-lite.json|->
            fe = load(a[0])
            prev = None
            if a[1] != "-" and os.path.exists(a[1]):
                prev = (json.load(open(a[1], encoding="utf-8")).get("features") or {}).get("counts")
            problems, warnings = gate(fe, LITE_CODE, EUROPE_BBOX, prev)
            print("   features %s: %s" % (LITE_CODE, ", ".join("%s %d" % (t, fe["counts"][t]) for t in TYPES)))
            for wmsg in warnings:
                print("   WARNING %s: %s" % (LITE_CODE, wmsg))
            for p in problems:
                print("   FAIL %s: %s" % (LITE_CODE, p))
            return 1 if problems else 0
        if cmd == "validate":
            fe = load(a[0])
            print("   %s: %d entries, %s" % (os.path.basename(a[0]), len(fe["rows"]), fe["counts"]))
            return 0
        if cmd == "counts":
            print(json.dumps(load(a[0])["counts"]))
            return 0
        if cmd == "backfill":
            made = backfill(a[0])
            print("   features backfilled: %s" % (", ".join(made) or "none needed"))
            return 0
        if cmd == "gates":
            # every country of this run: <work>/<code>.package.json with features, against the
            # previous production manifest
            work, cfg_path, prev_path = a[0], a[1], a[2]
            cfg = json.load(open(cfg_path, encoding="utf-8"))
            pm = json.load(open(prev_path, encoding="utf-8")) if prev_path != "-" and os.path.exists(prev_path) else {}
            bad = 0
            for name in sorted(os.listdir(work)):
                if not name.endswith(".package.json"):
                    continue
                d = json.load(open(os.path.join(work, name), encoding="utf-8"))
                code, fb = d["code"], d.get("features")
                if not fb:
                    print("   WARNING %s: no features in this run" % code)
                    continue
                fe = load(os.path.join(work, fb["file"]))
                prev = (((pm.get("regions") or {}).get(code) or {}).get("features") or {}).get("counts")
                problems, warnings = gate(fe, code, cfg["countries"][code]["bbox"], prev)
                print("   %s %s: %s" % (code, fe["graph_version"], ", ".join("%s %d" % (t, fe["counts"][t]) for t in TYPES)))
                for wmsg in warnings:
                    print("   WARNING %s: %s" % (code, wmsg))
                for p in problems:
                    print("   FAIL %s: %s" % (code, p))
                bad += len(problems)
            return 1 if bad else 0
        if cmd == "check":
            fe = load(a[0])
            cfg = json.load(open(a[2], encoding="utf-8"))
            prev = None
            if a[3] != "-" and os.path.exists(a[3]):
                pm = json.load(open(a[3], encoding="utf-8"))
                prev = (((pm.get("regions") or {}).get(a[1]) or {}).get("features") or {}).get("counts")
            problems, warnings = gate(fe, a[1], cfg["countries"][a[1]]["bbox"], prev)
            print("   features %s: %s" % (a[1], ", ".join("%s %d" % (t, fe["counts"][t]) for t in TYPES)))
            for wmsg in warnings:
                print("   WARNING %s: %s" % (a[1], wmsg))
            for p in problems:
                print("   FAIL %s: %s" % (a[1], p))
            return 1 if problems else 0
    except FeaturesError as e:
        print("FAIL: %s" % e, file=sys.stderr)
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
