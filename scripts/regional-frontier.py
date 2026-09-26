#!/usr/bin/env python3
"""Frontier of ONE country graph, and the D1 rule that joins two frontiers.

WHY THIS EXISTS. A pair portal table named nodes of TWO specific builds, so updating one country
invalidated every table it took part in and the whole set had to move together. Measured
2026-09-26 on the published v8/v13 packages: graphs of different versions join perfectly — the
border nodes are the same OSM nodes — as long as the join is made by something that survives a
rebuild. So each country now ships its OWN frontier, built from its own graph and its own source
extract, and the device joins two frontiers when it opens the set (Frontier.kt mirrors the rule
below line for line; tests/frontier-vectors.json is shared by both test suites).

FORMAT (text, deterministic, versioned):

    wedrive-frontier 1
    country MD
    graph_version 2026-09-24
    entries 117
    osm_resolved 117
    level lat_e7 lon_e7 local_id osm_node_id
    0 470101234 288638000 123456789 306893621
    ...

rows sorted by (level, lat_e7, lon_e7, local_id); osm_node_id is '-' when it could not be named.
local_id is the node's GraphId inside THIS package (region bits 0) — meaningless for any other
graph, which is the point: nothing here names a neighbour.

D1 — joining frontier A with frontier B (one pair of countries):
    1. (osm_node_id, level) equal, unique on both sides                      -> portal
    2. among the rest: (level, lat_e7, lon_e7) equal                          -> portal
    3. among the rest: same level, distance <= 5 m, the ONLY candidate for a
       AND a the only candidate for it (mutual uniqueness)                    -> portal
    4. otherwise no portal. Never "nearest within 5 m" without uniqueness.
A portal whose ends are not the same point carries its real distance (metres, rounded UP to a
centimetre): patch 21 in the engine accepts distance 0 only for ends within 2 m, which is exactly
right for "the same point" and exactly wrong for a node OSM moved by 2.66 m (HU-AT, measured).

OSM IDS come from the SOURCE extract (option A). Option B, keep_osm_node_ids in the graph, was
measured on Moldova: identical node numbering, identical ids, but +13.3 % on the package the car
downloads, for ids only the factory needs.

  usage:
    regional-frontier.py build <tiles> <pbf> <CODE> <graph_version> <out.frontier> [--dump F]
    regional-frontier.py validate <file.frontier>...
    regional-frontier.py diff <old.frontier> <new.frontier>
    regional-frontier.py portals <regional.json> <out.portals> <CODE=file.frontier>...
                                 [--report R.json] [--require-borders]
  factory (reads <work>/<code>.package.json):
    regional-frontier.py backfill <work> <installed-dir>   carried countries published without one
    regional-frontier.py set <regional.json> <work> <out.table> [--report R.json]
    regional-frontier.py report <work> <previous-manifest.json>
"""
import collections
import json
import math
import os
import subprocess
import sys
import tempfile

FORMAT = "wedrive-frontier"
VERSION = 1
COLUMNS = "level lat_e7 lon_e7 local_id osm_node_id"
NEAR_M = 5.0
EARTH_R = 6371000.0
REGION_SHIFT = 46
LOCAL_MASK = (1 << REGION_SHIFT) - 1


class FrontierError(Exception):
    pass


# ---------------------------------------------------------------- the file

def write(path, country, graph_version, rows):
    """rows: iterable of (level, lat_e7, lon_e7, local_id, osm_or_None)."""
    rows = sorted(rows, key=lambda r: (r[0], r[1], r[2], r[3]))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="ascii", newline="\n") as f:
        f.write("%s %d\n" % (FORMAT, VERSION))
        f.write("country %s\n" % country)
        f.write("graph_version %s\n" % graph_version)
        f.write("entries %d\n" % len(rows))
        f.write("osm_resolved %d\n" % sum(1 for r in rows if r[4] is not None))
        f.write(COLUMNS + "\n")
        for lv, la, lo, local, osm in rows:
            f.write("%d %d %d %d %s\n" % (lv, la, lo, local, "-" if osm is None else osm))
    os.replace(tmp, path)


def parse(text, name="frontier"):
    """-> dict(country, graph_version, rows=[(level, lat, lon, local, osm|None)]). Strict."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or lines[0] != "%s %d" % (FORMAT, VERSION):
        raise FrontierError("%s: not a %s %d file" % (name, FORMAT, VERSION))
    head = {}
    i = 1
    while i < len(lines) and lines[i] != COLUMNS:
        k, _, v = lines[i].partition(" ")
        if not v:
            raise FrontierError("%s: malformed header line %d" % (name, i + 1))
        head[k] = v
        i += 1
    if i == len(lines):
        raise FrontierError("%s: no column line" % name)
    for k in ("country", "graph_version", "entries"):
        if k not in head:
            raise FrontierError("%s: header has no %s" % (name, k))
    rows = []
    for n, line in enumerate(lines[i + 1:], start=i + 2):
        f = line.split(" ")
        if len(f) != 5:
            raise FrontierError("%s: line %d has %d fields" % (name, n, len(f)))
        try:
            lv, la, lo, local = int(f[0]), int(f[1]), int(f[2]), int(f[3])
            osm = None if f[4] == "-" else int(f[4])
        except ValueError:
            raise FrontierError("%s: line %d is not numeric" % (name, n))
        rows.append((lv, la, lo, local, osm))
    if len(rows) != int(head["entries"]):
        raise FrontierError("%s: header says %s entries, file has %d" % (name, head["entries"], len(rows)))
    return {"country": head["country"], "graph_version": head["graph_version"], "rows": rows}


def load(path):
    with open(path, encoding="ascii") as f:
        return parse(f.read(), os.path.basename(path))


def validate(fr):
    """Gates of one frontier. -> list of problems (empty = PASS)."""
    p = []
    rows = fr["rows"]
    if not rows:
        p.append("empty frontier: a country graph always has a border")
    if rows != sorted(rows, key=lambda r: (r[0], r[1], r[2], r[3])):
        p.append("rows not in canonical order")
    coords, ids = collections.Counter(), collections.Counter()
    for lv, la, lo, local, osm in rows:
        if lv not in (0, 1, 2):
            p.append("impossible level %d" % lv)
        if local < 0 or local > LOCAL_MASK:
            p.append("local_id %d carries region bits" % local)
        elif local & 7 != lv:
            p.append("local_id %d is level %d, row says %d" % (local, local & 7, lv))
        if not (-900000000 <= la <= 900000000 and -1800000000 <= lo <= 1800000000):
            p.append("coordinate out of range %d,%d" % (la, lo))
        coords[(lv, la, lo)] += 1
        if osm is not None:
            ids[(osm, lv)] += 1
    p += ["collision: %d nodes at level %d, %d,%d" % (n, k[0], k[1], k[2]) for k, n in coords.items() if n > 1]
    p += ["collision: OSM node %d twice at level %d" % (k[0], k[1]) for k, n in ids.items() if n > 1]
    return p


# ---------------------------------------------------------------- D1

def distance_m(a, b):
    la1, lo1, la2, lo2 = (math.radians(x / 1e7) for x in (a[1], a[2], b[1], b[2]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(h)))


def portal_distance(a, b):
    """0 for the same point; otherwise the real distance, rounded UP to a centimetre."""
    if (a[1], a[2]) == (b[1], b[2]):
        return 0.0
    return max(0.01, math.ceil(distance_m(a, b) * 100.0) / 100.0)


def match(A, B):
    """D1. A, B: lists of rows. -> (pairs [(ia, ib, method, distance)], stats)."""
    pairs = []
    free_a, free_b = set(range(len(A))), set(range(len(B)))

    # 1. (osm_node_id, level), unique on both sides
    ka, kb = collections.defaultdict(list), collections.defaultdict(list)
    for i, r in enumerate(A):
        if r[4] is not None:
            ka[(r[4], r[0])].append(i)
    for j, r in enumerate(B):
        if r[4] is not None:
            kb[(r[4], r[0])].append(j)
    ambiguous_id = 0
    for k, ia in ka.items():
        jb = kb.get(k, [])
        if len(ia) == 1 and len(jb) == 1:
            pairs.append((ia[0], jb[0], "osm", portal_distance(A[ia[0]], B[jb[0]])))
            free_a.discard(ia[0]); free_b.discard(jb[0])
        elif jb:
            ambiguous_id += len(ia)

    # 2. exact (level, lat_e7, lon_e7), one-to-one
    ca, cb = collections.defaultdict(list), collections.defaultdict(list)
    for i in free_a:
        ca[(A[i][0], A[i][1], A[i][2])].append(i)
    for j in free_b:
        cb[(B[j][0], B[j][1], B[j][2])].append(j)
    for k, ia in ca.items():
        jb = cb.get(k, [])
        if len(ia) == 1 and len(jb) == 1:
            pairs.append((ia[0], jb[0], "coord", 0.0))
            free_a.discard(ia[0]); free_b.discard(jb[0])

    # 3. mutual unique nearest within NEAR_M, same level — decided on the SAME free sets for all
    ra, rb = sorted(free_a), sorted(free_b)

    def near(x, pool, rows):
        return [y for y in pool if rows[y][0] == x[0] and distance_m(x, rows[y]) <= NEAR_M]

    ambiguous_near = 0
    found = []
    for i in ra:
        cand = near(A[i], rb, B)
        if not cand:
            continue
        if len(cand) == 1 and len(near(B[cand[0]], ra, A)) == 1:
            found.append((i, cand[0]))
        else:
            ambiguous_near += 1
    for i, j in found:
        pairs.append((i, j, "near", portal_distance(A[i], B[j])))
        free_a.discard(i); free_b.discard(j)

    pairs.sort()
    stats = {"osm": sum(1 for p in pairs if p[2] == "osm"),
             "coord": sum(1 for p in pairs if p[2] == "coord"),
             "near": sum(1 for p in pairs if p[2] == "near"),
             "ambiguous": ambiguous_id + ambiguous_near,
             "unmatched_a": len(free_a), "unmatched_b": len(free_b)}
    return pairs, stats


def portal_rows(A, ra, B, rb, pairs):
    """Table rows in the engine's existing format, both directions: 'from to distance  # lat, lon'."""
    out = []
    for i, j, _method, d in pairs:
        ga, gb = (ra << REGION_SHIFT) | A[i][3], (rb << REGION_SHIFT) | B[j][3]
        tail = "   # %.7f, %.7f" % (A[i][1] / 1e7, A[i][2] / 1e7)
        dist = "0" if d == 0.0 else "%.2f" % d
        out.append("%d %d %s%s" % (ga, gb, dist, tail))
        out.append("%d %d %s%s" % (gb, ga, dist, tail))
    return out


def build_portals(cfg, frontiers):
    """frontiers: {CODE: parsed}. All pairs of countries. -> (rows, {pair: stats})."""
    rows, by_pair = [], {}
    codes = sorted(frontiers)
    for x in range(len(codes)):
        for y in range(x + 1, len(codes)):
            a, b = codes[x], codes[y]
            A, B = frontiers[a]["rows"], frontiers[b]["rows"]
            pairs, stats = match(A, B)
            if not pairs and not stats["ambiguous"]:
                continue
            rows += portal_rows(A, cfg["countries"][a]["region_id"], B, cfg["countries"][b]["region_id"], pairs)
            stats["versions"] = [frontiers[a]["graph_version"], frontiers[b]["graph_version"]]
            by_pair["%s-%s" % (a, b)] = stats
    return rows, by_pair


# ---------------------------------------------------------------- building a frontier

def dump(tiles):
    r = subprocess.run(["frontier_dump", tiles], capture_output=True, text=True)
    if r.returncode != 0:
        raise FrontierError("frontier_dump failed: %s" % r.stderr[-300:])
    rows = []
    for line in r.stdout.splitlines():
        f = line.split(" ")
        if len(f) != 9:
            raise FrontierError("frontier_dump: malformed line %r" % line[:120])
        rows.append((int(f[0]), int(f[1]), int(f[2]), int(f[3]), f[8]))
    return rows, r.stdout


def osm_ids_from_pbf(pbf, coords):
    """{(lat_e7, lon_e7): [osm node ids]} for nodes of highway / ferry ways at exactly those points.

    Ferries are NOT optional: a frontier built from highway=* alone lost four AT-DE crossings
    (Danube and Lake Constance ferries), measured 2026-09-26.
    """
    found = collections.defaultdict(list)
    with tempfile.TemporaryDirectory() as tmp:
        roads = os.path.join(tmp, "roads.osm.pbf")
        subprocess.run(["osmium", "tags-filter", pbf, "w/highway", "w/route=ferry,shuttle_train",
                        "-o", roads, "--overwrite"], check=True, capture_output=True)
        p = subprocess.Popen(["osmium", "cat", roads, "-t", "node", "-f", "opl"],
                             stdout=subprocess.PIPE, text=True, bufsize=1 << 20)
        for line in p.stdout:
            f = line.split()
            x = y = None
            for t in f[1:]:
                if t[0] == "x":
                    x = t
                elif t[0] == "y":
                    y = t
            if x is None or y is None or len(x) < 2 or len(y) < 2:
                continue
            k = (round(float(y[1:]) * 1e7), round(float(x[1:]) * 1e7))
            if k in coords:
                found[k].append(int(f[0][1:]))
        if p.wait() != 0:
            raise FrontierError("osmium cat failed")
    return found


def build(tiles, pbf, code, graph_version, out, dump_path=None):
    """pbf '-' = no source extract available: every OSM id stays '-' and D1 joins by coordinate."""
    raw, text = dump(tiles)
    if dump_path:
        with open(dump_path, "w", encoding="ascii") as f:
            f.write(text)
    coords = {(r[1], r[2]) for r in raw}
    by_coord = osm_ids_from_pbf(pbf, coords) if pbf != "-" else {}
    rows, stats = [], collections.Counter()
    for lv, la, lo, local, tile_ids in raw:
        ids = sorted(set(by_coord.get((la, lo), [])))
        if tile_ids != "-" and len(tile_ids.split(",")) == 1:
            osm, why = int(tile_ids), "tile"          # a graph built with keep_osm_node_ids
        elif len(ids) == 1:
            osm, why = ids[0], "pbf"
        elif ids:
            osm, why = None, "ambiguous"               # two OSM nodes on one point: coordinate decides
        else:
            osm, why = None, "absent"
        stats[why] += 1
        rows.append((lv, la, lo, local, osm))
    write(out, code, graph_version, rows)
    fr = load(out)
    problems = validate(fr)
    print("   frontier %s %s: %d entries, OSM id from PBF %d, from tiles %d, two OSM nodes on one point %d, "
          "no OSM node %d" % (code, graph_version, len(rows), stats["pbf"], stats["tile"],
                                stats["ambiguous"], stats["absent"]))
    return fr, problems


# ---------------------------------------------------------------- diff vs previous version

def diff(old, new):
    """What changed between two frontiers of ONE country. -> dict of counts + examples."""
    o, n = old["rows"], new["rows"]
    oi = {(r[4], r[0]): r for r in o if r[4] is not None}
    ni = {(r[4], r[0]): r for r in n if r[4] is not None}
    rep = collections.Counter()
    moved = []
    seen_o, seen_n = set(), set()
    for k, r in ni.items():
        if k in oi:
            seen_o.add(oi[k]); seen_n.add(r)
            if (oi[k][1], oi[k][2]) == (r[1], r[2]):
                rep["retained"] += 1
            else:
                rep["moved_same_osm_id"] += 1
                moved.append((k[0], k[1], round(distance_m(oi[k], r), 2)))
    oc = {(r[0], r[1], r[2]): r for r in o if r not in seen_o}
    for r in n:
        if r in seen_n:
            continue
        k = (r[0], r[1], r[2])
        if k in oc:
            rep["retained_by_coordinate"] += 1
            seen_o.add(oc[k]); seen_n.add(r)
    rep["removed"] = sum(1 for r in o if r not in seen_o)
    rep["added"] = sum(1 for r in n if r not in seen_n)
    return {"counts": dict(rep), "moved": moved}


# ---------------------------------------------------------------- the factory's set

def sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def md5(path):
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url, dst):
    subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", dst + ".part", url], check=True)
    os.replace(dst + ".part", dst)


def descriptions(work):
    out = {}
    for name in sorted(os.listdir(work)):
        if name.endswith(".package.json"):
            with open(os.path.join(work, name), encoding="utf-8") as f:
                d = json.load(f)
            out[d["code"]] = (os.path.join(work, name), d)
    return out


def frontier_block(path):
    fr = load(path)
    return {"format": "%s/%d" % (FORMAT, VERSION), "file": os.path.basename(path),
            "bytes": os.path.getsize(path), "sha256": sha256(path), "entries": len(fr["rows"]),
            "osm_resolved": sum(1 for r in fr["rows"] if r[4] is not None)}


def dated_extract(source):
    """The dated Geofabrik file a package was built from: <name>-latest -> <name>-YYMMDD."""
    url, rep = source.get("url", ""), source.get("replication", "")
    if not url.endswith("-latest.osm.pbf") or len(rep) < 10:
        return None
    return url.replace("-latest.osm.pbf", "-%s%s%s.osm.pbf" % (rep[2:4], rep[5:7], rep[8:10]))


def backfill(work, installed):
    """MIGRATION: a carried country published before frontiers existed gets one here, from its
    unpacked tiles and — when Geofabrik still serves it — the very extract it was built from (md5
    must match the provenance). Without that extract the frontier still ships, with no OSM ids:
    D1 then joins it by exact coordinate, which is correct for a graph of the same OSM data."""
    made = []
    for code, (desc_path, d) in descriptions(work).items():
        if d.get("frontier"):
            continue
        if not d.get("carried"):
            raise FrontierError("%s was built in this run but has no frontier" % code)
        tiles = os.path.join(installed, code, "tiles")
        pbf = "-"
        url = dated_extract(d.get("source") or {})
        if url:
            dst = os.path.join(work, "src-%s.osm.pbf" % code.lower())
            try:
                fetch(url, dst)
                if md5(dst) == (d.get("source") or {}).get("md5"):
                    pbf = dst
                else:
                    print("   WARNING %s: %s does not match the recorded md5 — frontier without OSM ids" % (code, url))
            except subprocess.CalledProcessError:
                print("   WARNING %s: %s is no longer served — frontier without OSM ids" % (code, url))
        else:
            print("   WARNING %s: no provenance to find the source extract — frontier without OSM ids" % code)
        out = os.path.join(work, "%s-%s.frontier" % (code.lower(), d["graph_version"]))
        _fr, problems = build(tiles, pbf, code, d["graph_version"], out)
        from_source = pbf != "-"
        if pbf != "-":
            os.remove(pbf)
        if problems:
            raise FrontierError("%s: %s" % (code, "; ".join(problems)))
        d["frontier"] = frontier_block(out)
        d["frontier"]["backfilled"] = True
        # source identity (owner 2026-09-27): the PBF the OSM ids came from, or none at all
        if from_source:
            d["frontier"]["source_md5"] = (d.get("source") or {}).get("md5")
        else:
            d["frontier"]["source"] = "graph-only"
        with open(desc_path, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
        made.append(code)
    return made


def set_portals(cfg, work, out, report_path=None):
    """The table the APP would build from this set's frontiers (D1). -> problems."""
    frontiers = {}
    for code, (_p, d) in descriptions(work).items():
        fb = d.get("frontier")
        if not fb:
            raise FrontierError("%s has no frontier" % code)
        path = os.path.join(work, fb["file"])
        if sha256(path) != fb["sha256"]:
            raise FrontierError("%s: %s does not match its description" % (code, fb["file"]))
        frontiers[code] = load(path)
    rows, by_pair = build_portals(cfg, frontiers)
    with open(out, "w", encoding="ascii", newline="\n") as f:
        f.write("\n".join(rows) + ("\n" if rows else ""))
    problems = []
    for name, s in sorted(by_pair.items()):
        print("   %-6s %-23s osm %4d  coord %3d  near %d  ambiguous %d  | unmatched %d/%d"
              % (name, "/".join(s["versions"]), s["osm"], s["coord"], s["near"], s["ambiguous"],
                 s["unmatched_a"], s["unmatched_b"]))
        if s["ambiguous"]:
            problems.append("%s: %d ambiguous frontier entries" % (name, s["ambiguous"]))
    for border in cfg["borders"]:
        a, b = border["between"]
        if a in frontiers and b in frontiers and "%s-%s" % tuple(sorted((a, b))) not in by_pair:
            problems.append("%s-%s: both countries present, no portal joins them" % (a, b))
    if report_path:
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(by_pair, f, indent=2, sort_keys=True)
    print("   portals from frontiers: %d rows" % len(rows))
    return problems


def report(work, manifest):
    """Frontier change of every country rebuilt in this run against its previous published frontier."""
    prev = manifest.get("regions", {})
    lines = []
    for code, (_p, d) in descriptions(work).items():
        if d.get("carried") or not d.get("frontier"):
            continue
        old = (prev.get(code) or {}).get("frontier")
        if not old:
            lines.append("%s %s: no previous frontier" % (code, d["graph_version"]))
            continue
        dst = os.path.join(work, "previous-" + old["file"])
        fetch(old["url"], dst)
        if sha256(dst) != old["sha256"]:
            raise FrontierError("%s: previous frontier does not match the manifest" % code)
        dd = diff(load(dst), load(os.path.join(work, d["frontier"]["file"])))
        lines.append("%s %s -> %s: %s%s" % (code, prev[code]["graph_version"], d["graph_version"],
                     ", ".join("%s %d" % kv for kv in sorted(dd["counts"].items())),
                     "; moved: %s" % dd["moved"][:10] if dd["moved"] else ""))
    return lines


# ---------------------------------------------------------------- cli

def main(argv):
    if len(argv) < 2:
        print(__doc__); return 2
    cmd = argv[1]
    try:
        if cmd == "build":
            if len(argv) < 7:
                print(__doc__); return 2
            dump_path = argv[argv.index("--dump") + 1] if "--dump" in argv else None
            _fr, problems = build(argv[2], argv[3], argv[4].upper(), argv[5], argv[6], dump_path)
            for p in problems:
                print("   FAIL %s" % p)
            return 1 if problems else 0
        if cmd == "validate":
            bad = 0
            for path in argv[2:]:
                problems = validate(load(path))
                print("   %s: %s" % (path, "PASS" if not problems else "FAIL"))
                for p in problems:
                    print("      " + p)
                bad += bool(problems)
            return 1 if bad else 0
        if cmd == "backfill":
            made = backfill(argv[2], argv[3])
            print("   frontier backfilled for: %s" % (", ".join(made) or "nobody"))
            return 0
        if cmd == "set":
            cfg = json.load(open(argv[2], encoding="utf-8"))
            rp = argv[argv.index("--report") + 1] if "--report" in argv else None
            problems = set_portals(cfg, argv[3], argv[4], rp)
            for p in problems:
                print("   FAIL " + p)
            return 1 if problems else 0
        if cmd == "report":
            for line in report(argv[2], json.load(open(argv[3], encoding="utf-8"))):
                print("   " + line)
            return 0
        if cmd == "diff":
            d = diff(load(argv[2]), load(argv[3]))
            print(json.dumps(d, indent=2))
            return 0
        if cmd == "portals":
            cfg = json.load(open(argv[2], encoding="utf-8"))
            out = argv[3]
            rest = argv[4:]
            report_path = rest[rest.index("--report") + 1] if "--report" in rest else None
            frontiers = {}
            for a in rest:
                if "=" in a:
                    code, path = a.split("=", 1)
                    frontiers[code.upper()] = load(path)
            rows, by_pair = build_portals(cfg, frontiers)
            with open(out, "w", encoding="ascii", newline="\n") as f:
                f.write("\n".join(rows) + ("\n" if rows else ""))
            problems = []
            for name, s in sorted(by_pair.items()):
                print("   %-6s %s  osm %d  coord %d  near %d  ambiguous %d  | unmatched %d/%d"
                      % (name, "/".join(s["versions"]), s["osm"], s["coord"], s["near"], s["ambiguous"],
                         s["unmatched_a"], s["unmatched_b"]))
                if s["ambiguous"]:
                    problems.append("%s: %d ambiguous frontier entries" % (name, s["ambiguous"]))
            if "--require-borders" in rest:
                for border in cfg["borders"]:
                    a, b = border["between"]
                    if a in frontiers and b in frontiers and "%s-%s" % tuple(sorted((a, b))) not in by_pair:
                        problems.append("%s-%s: both countries present, no portal joins them" % (a, b))
            if report_path:
                with open(report_path, "w", encoding="utf-8") as f:
                    json.dump(by_pair, f, indent=2, sort_keys=True)
            for p in problems:
                print("   FAIL " + p)
            print("   portals: %d rows" % len(rows))
            return 1 if problems else 0
    except FrontierError as e:
        print("   FAIL %s" % e)
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
