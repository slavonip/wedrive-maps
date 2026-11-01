#!/usr/bin/env python3
"""Bus lanes of ONE country: the OSM ways that carry a dedicated bus/PSV lane and WHICH SIDE of
the way it runs on — extracted from the same PBF the country graph is built from, so every way id
names a way of this graph version. The app joins the file with a route by way id and puts a BUS
cell on the synthesized lane card (BusLanes.kt, commit 578e133).

  usage:
    regional-buslanes.py build <pbf> <CODE> <out.buslanes>
    regional-buslanes.py validate <file.buslanes>
    regional-buslanes.py counts <file.buslanes>
  factory (reads <work>/<code>.package.json):
    regional-buslanes.py gates <work>

FORMAT wedrive-buslanes/1 — THE APP'S CONTRACT, stricter than the factory's own files: the first
line is exactly `wedrive-buslanes 1` and every other line is exactly `w<osm way id> <side>`,
side in {left, right, both}. No header beyond the format line — the app's parser rejects any other
shape of line, and a single malformed line makes it discard the whole file. Provenance (country,
graph version, source md5, counts) therefore lives in <code>.package.json and the manifest, never
in the file. Sorted by way id: identical input gives identical bytes.

    wedrive-buslanes 1
    w24760042 right
    w30748227 right

THE SIDE IS DIRECTED (owner, 2026-11-01): the app builds the lane card for ONE direction of
travel, so `side` means "where the bus lane sits relative to the DRIVING direction", never "which
side of the street". A two-way road with `busway=lane` has, in each direction, a bus lane on that
direction's right — so it is `right`, NOT `both`: `both` would paint a BUS cell on the left of a
card whose direction has no lane there (the Bd. Ștefan cel Mare case, w30748227, confirmed on the
road as right). Do not "fix" this back to both.

THE TAGS (approved 2026-11-01, matching the hand-built Moldova sample byte for byte):
    busway=lane              -> right  (a lane per direction, each on that direction's right)
    busway:both=lane         -> both   (explicitly both sides of one carriageway)
    busway:right=lane        -> right
    busway:left=lane         -> left
    lanes:bus=N / lanes:psv=N            (N >= 1) -> right  (the same directed rule as busway=lane)
    lanes:bus:forward=N / lanes:psv:forward=N     -> right
    lanes:bus:backward=N / lanes:psv:backward=N   -> left
Left + right from different tags collapse to both. Only what OSM states is written, the features
discipline: busway=opposite_lane (a deprecated contraflow tag), `*:conditional` part-time lanes
and non-numeric lane counts are NOT extracted — a BUS cell drawn from a guess misleads exactly
where the card matters.
"""
import collections
import json
import os
import re
import subprocess
import sys
import tempfile

FORMAT = "wedrive-buslanes"
VERSION = 1
SIDES = ("right", "left", "both")
# the exact keys osmium filters on and side_of() reads; `*:conditional` deliberately absent
BUSWAY_KEYS = ("busway", "busway:both", "busway:right", "busway:left")
LANE_PLAIN_KEYS = ("lanes:bus", "lanes:psv")
LANE_FWD_KEYS = ("lanes:bus:forward", "lanes:psv:forward")
LANE_BWD_KEYS = ("lanes:bus:backward", "lanes:psv:backward")


class BusLanesError(Exception):
    pass


# ---------------------------------------------------------------- OPL (ways only)

def unesc(s):
    return re.sub(r"%([0-9a-fA-F]+)%", lambda m: chr(int(m.group(1), 16)), s)


def parse_opl_ways(path):
    """-> {way id: tags}; nodes and relations in the file are ignored."""
    ways = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split(" ")
            if not parts or not parts[0] or parts[0][0] != "w":
                continue
            tags = {}
            for p in parts[1:]:
                if p.startswith("T") and len(p) > 1:
                    for kv in p[1:].split(","):
                        if "=" in kv:
                            k, v = kv.split("=", 1)
                            tags[unesc(k)] = unesc(v)
            ways[int(parts[0][1:])] = tags
    return ways


def osmium(*args):
    r = subprocess.run(["osmium"] + list(args), capture_output=True, text=True)
    if r.returncode != 0:
        raise BusLanesError("osmium %s failed: %s" % (args[0], r.stderr[-400:]))


# ---------------------------------------------------------------- extraction

def lane_count(v):
    """A dedicated-lane count only when OSM states a plain number; '0' is no lane, text is unknown."""
    return int(v) if v is not None and re.fullmatch(r"\d+", v.strip()) else None


def side_of(tags):
    """-> 'right' | 'left' | 'both' | None (tagged, but no present-tense dedicated lane stated).

    DIRECTED semantics (module docstring): `busway=lane` and an undirected `lanes:bus`/`lanes:psv`
    are RIGHT — in every direction the card is built for, the lane rides that direction's right.
    Only an explicit `busway:both=lane` (or left + right together) is both."""
    both = tags.get("busway:both") == "lane"
    right = tags.get("busway") == "lane" or tags.get("busway:right") == "lane" \
        or any((lane_count(tags.get(k)) or 0) >= 1 for k in LANE_PLAIN_KEYS + LANE_FWD_KEYS)
    left = tags.get("busway:left") == "lane" \
        or any((lane_count(tags.get(k)) or 0) >= 1 for k in LANE_BWD_KEYS)
    if both or (left and right):
        return "both"
    if right:
        return "right"
    if left:
        return "left"
    return None


def build(pbf, code, out, log=print):
    with tempfile.TemporaryDirectory() as tmp:
        opl = os.path.join(tmp, "bus.opl")
        osmium("tags-filter", "-R", "-O", "-f", "opl", "-o", opl, pbf,
               *["w/%s" % k for k in BUSWAY_KEYS + LANE_PLAIN_KEYS + LANE_FWD_KEYS + LANE_BWD_KEYS])
        ways = parse_opl_ways(opl)
    rows, skipped = {}, 0
    for wid, tags in ways.items():
        side = side_of(tags)
        if side is None:
            skipped += 1        # busway=opposite_lane, lanes:bus=0, free text: never guessed
            continue
        rows[wid] = side
    write(out, rows)
    c = counts_of(rows)
    log("   bus lanes %s: %d ways (%s)%s" % (code, len(rows),
        ", ".join("%s %d" % (s, c[s]) for s in SIDES),
        ", skipped %d tagged without a stated lane" % skipped if skipped else ""))
    return rows, skipped


def counts_of(rows):
    c = collections.Counter(rows.values())
    return {s: c.get(s, 0) for s in SIDES}


# ---------------------------------------------------------------- the file

def write(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="ascii", newline="\n") as f:
        f.write("%s %d\n" % (FORMAT, VERSION))
        for wid in sorted(rows):
            f.write("w%d %s\n" % (wid, rows[wid]))
    os.replace(tmp, path)


def parse(text, name="bus.lanes"):
    """Strict — the mirror of the app's parser (BusLanes.parse), plus a duplicate-id check the app
    does not need (it keeps the last) and the factory's determinism does. -> {way id: side}."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or lines[0] != "%s %d" % (FORMAT, VERSION):
        raise BusLanesError("%s: not a %s %d file" % (name, FORMAT, VERSION))
    rows = {}
    for n, line in enumerate(lines[1:], start=2):
        f = line.split(" ")
        if len(f) != 2 or not re.fullmatch(r"w\d+", f[0]):
            raise BusLanesError("%s: line %d is not 'w<id> <side>': %r" % (name, n, line))
        if f[1] not in SIDES:
            raise BusLanesError("%s: line %d: unknown side %r" % (name, n, f[1]))
        wid = int(f[0][1:])
        if wid in rows:
            raise BusLanesError("%s: line %d: duplicate way %s" % (name, n, f[0]))
        rows[wid] = f[1]
    return rows


def load(path):
    with open(path, encoding="ascii") as f:
        return parse(f.read(), os.path.basename(path))


# ---------------------------------------------------------------- gates

def gates(work):
    """The factory's format gate: every <code>.package.json that carries a bus_lanes block must
    name a file that parses strictly (header, every line, side from the dictionary) and whose row
    count matches the block. A country without the block is a WARNING — published before bus lanes
    existed, it ships without them until its next rebuild (the app degrades softly). -> problems."""
    problems = 0
    for name in sorted(os.listdir(work)):
        if not name.endswith(".package.json"):
            continue
        d = json.load(open(os.path.join(work, name), encoding="utf-8"))
        code, bl = d["code"], d.get("bus_lanes")
        if not bl:
            print("   WARNING %s: no bus lanes in this run" % code)
            continue
        try:
            rows = load(os.path.join(work, bl["file"]))
        except (BusLanesError, OSError) as e:
            print("   FAIL %s: %s" % (code, e)); problems += 1
            continue
        if len(rows) != bl.get("entries"):
            print("   FAIL %s: %s has %d rows, the package says %s"
                  % (code, bl["file"], len(rows), bl.get("entries"))); problems += 1
            continue
        c = counts_of(rows)
        if bl.get("counts") and any(int(bl["counts"].get(s, 0)) != c[s] for s in SIDES):
            print("   FAIL %s: %s counts disagree with the package" % (code, bl["file"])); problems += 1
            continue
        print("   %s: %s ok, %d ways (%s)" % (code, bl["file"], len(rows),
              ", ".join("%s %d" % (s, c[s]) for s in SIDES)))
    return problems


def main(argv):
    if len(argv) < 3:
        print(__doc__); return 2
    cmd, a = argv[1], argv[2:]
    try:
        if cmd == "build":
            build(a[0], a[1], a[2])
            load(a[2])                        # what was written parses strictly, or the build fails
            return 0
        if cmd == "validate":
            rows = load(a[0])
            c = counts_of(rows)
            print("   %s: %d ways, %s" % (os.path.basename(a[0]), len(rows), c))
            return 0
        if cmd == "counts":
            print(json.dumps(counts_of(load(a[0]))))
            return 0
        if cmd == "gates":
            return 1 if gates(a[0]) else 0
    except BusLanesError as e:
        print("FAIL: %s" % e, file=sys.stderr)
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
