"""Find real probe coordinates for a city, from OSM.

Adding a country to regions.yml means adding probes for it, and probes are only worth anything
if their coordinates are real. Inventing them is how the Moldova grade-separation probe spent a
morning pointing at Strada Visterniceni — a junction that IS connected — and reading as a graph
defect when it was a typo.

So this asks OSM for junctions with the property we want to assert, rather than asking a person
to remember one:

  * ONE-WAY      a oneway=yes street long enough that going against it must cost a detour.
  * BRIDGE       two ways whose geometries cross within a few metres while sharing NO node —
                 which in OSM is the definition of grade separation, and the thing the tile
                 builder must preserve.

    usage: python3 find-probes.py "<name>" <south> <west> <north> <east>
    e.g.   python3 find-probes.py "Iasi" 47.10 27.50 47.22 27.70

Prints a YAML block ready to paste into regions.yml. VERIFY THE RESULT by routing it once
(scripts/probe.py will) before trusting it — this narrows the search to candidates, it does not
prove the graph behaves.
"""
import json
import math
import sys
import time
import urllib.parse
import urllib.request

# Several, because the main instance answers 504 under load often enough to stop a run that
# has nothing wrong with it. This is a developer tool, not the factory — no build depends on
# Overpass being up, only on a person adding a country.
OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

DRIVABLE = "motorway|trunk|primary|secondary|tertiary|unclassified|residential"

# Ramps are fetched but never used as probe candidates. They are how an INTERCHANGE is
# recognised: where a bridge and the road under it are joined by a slip road, the graph may
# legitimately route between them in a few tens of metres, and asserting a long detour there
# fails a correct build. Measured 2026-09-17 on Bulevardul Tudor Vladimirescu over Bulevardul
# Profesor Dimitrie Mangeron in Iași: both points snap cleanly to their own way, 0.0 m error,
# and the router still answers 35 m — because a ramp connects them, exactly as the tarmac does.
LINKS = "motorway_link|trunk_link|primary_link|secondary_link|tertiary_link"

# How close a ramp may be to the crossing before the candidate is treated as an interchange.
INTERCHANGE_RADIUS_M = 150

# The floor is 25 m because of SNAPPING, not because of geometry. A 9 m pair found in Iași
# produced "9 m for a 9 m gap — the bridge has become a junction" against a graph that is
# perfectly correct: both points land on the same edge, so the router answers the distance
# between them along it and the probe asserts nothing.
MIN_SEPARATION_M, MAX_SEPARATION_M = 25, 80


def overpass(query: str) -> dict:
    payload = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for url in OVERPASS_MIRRORS:
        for attempt in range(2):
            try:
                request = urllib.request.Request(
                    url, data=payload, headers={"User-Agent": "wedrive-map-probe/1.0"})
                return json.load(urllib.request.urlopen(request, timeout=180))
            except Exception as error:               # noqa: BLE001 — any failure means "try the next"
                last = error
                print(f"# {url} attempt {attempt + 1}: {error}", file=sys.stderr)
                time.sleep(5)
    raise SystemExit(f"every Overpass mirror failed; last was {last}")


def metres(a: tuple, b: tuple) -> float:
    """Good enough at city scale, and it keeps this script dependency-free."""
    dy = (a[0] - b[0]) * 111_320
    dx = (a[1] - b[1]) * 111_320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def way_length(geom: list) -> float:
    return sum(metres(geom[i], geom[i + 1]) for i in range(len(geom) - 1))


def fetch_ways(bbox: str) -> list:
    data = overpass(
        f'[out:json][timeout:180];way["highway"~"^({DRIVABLE}|{LINKS})$"]({bbox});out geom tags;'
    )
    ways = []
    for element in data.get("elements", []):
        geometry = [(p["lat"], p["lon"]) for p in element.get("geometry", [])]
        if len(geometry) >= 2:
            ways.append({
                "id": element["id"],
                "tags": element.get("tags", {}),
                "nodes": element.get("nodes", []),
                "geom": geometry,
            })
    return ways


def pick_oneway(ways: list) -> dict | None:
    """A one-way long enough that the detour around it is unmistakable, and central enough that
    an alternative exists — a one-way with no parallel street produces a 'no route', which
    proves nothing about direction."""
    best = None
    for way in ways:
        tags = way["tags"]
        if tags.get("oneway") not in ("yes", "1", "true", "-1"):
            continue
        if tags.get("highway") in ("motorway", "trunk"):
            continue          # a motorway's "detour" is tens of kilometres and tells us nothing
        if not tags.get("name"):
            continue          # an unnamed service spur is not a landmark anyone can re-check
        length = way_length(way["geom"])
        if not 200 <= length <= 900:
            continue
        # Prefer the most ordinary street of the right length: the fewer lanes and slip roads,
        # the more likely the detour is one block rather than a ring road.
        if best is None or abs(length - 400) < abs(best[0] - 400):
            best = (length, way)
    if best is None:
        return None
    length, way = best
    geom = way["geom"]
    start, end = geom[0], geom[-1]
    if way["tags"].get("oneway") == "-1":
        start, end = end, start
    return {"name": way["tags"]["name"], "way": way["id"], "length_m": round(length),
            "from": start, "to": end}


def pick_bridge(ways: list) -> dict | None:
    """Two ways crossing within a few metres and sharing NO node. The lower one must not be a
    bridge itself, or the pair is two decks of the same structure and may legitimately join."""
    def is_link(way):
        return way["tags"].get("highway", "").endswith("_link")

    bridges = [w for w in ways
               if not is_link(w)
               and w["tags"].get("bridge") in ("yes", "viaduct")
               and w["tags"].get("layer", "0") not in ("0", "-1", "-2")]
    ground = [w for w in ways
              if not is_link(w) and not w["tags"].get("bridge") and not w["tags"].get("tunnel")]
    ramp_points = [p for w in ways if is_link(w) for p in w["geom"]]

    # A FLOOR as well as a ceiling, and the floor is the one that was missing. The first run of
    # this over Iași returned "Pasajul Alexandru cel Bun over Pasajul Alexandru cel Bun, 0 m
    # apart" — one structure's own deck and ramp, whose geometries touch at a point where OSM
    # happens not to share a node. As a probe that is worthless: zero metres apart asserts
    # nothing about grade separation, and a router that DID join them would be arguably right.
    # A usable probe wants two clearly different roads, one visibly above the other.
    best = None
    for bridge in bridges:
        bridge_nodes = set(bridge["nodes"])
        bridge_name = bridge["tags"].get("name")
        for road in ground:
            if bridge_nodes & set(road["nodes"]):
                continue      # they share a node: in OSM that IS a junction, correctly
            if bridge_name and bridge_name == road["tags"].get("name"):
                continue      # same street above and below is one structure, not two roads
            for bp in bridge["geom"]:
                for rp in road["geom"]:
                    distance = metres(bp, rp)
                    if not MIN_SEPARATION_M <= distance <= MAX_SEPARATION_M:
                        continue
                    if any(metres(bp, ramp) <= INTERCHANGE_RADIUS_M for ramp in ramp_points):
                        continue          # a slip road joins them: connection is CORRECT here
                    if best is None or distance < best[0]:
                        best = (distance, bridge, road, bp, rp)
    if best is None:
        return None
    distance, bridge, road, bp, rp = best
    return {
        "bridge": bridge["tags"].get("name", f"way {bridge['id']}"),
        "under": road["tags"].get("name", f"way {road['id']}"),
        "separation_m": round(distance),
        "from": bp,
        "to": rp,
    }


def bridge_candidates(ways: list, limit: int = 20) -> list:
    """Every plausible grade separation, best first — because OSM tags cannot tell a genuine
    one from an interchange, and only the ROUTER can.

    Twice today a candidate that looked perfect in OSM (two named roads, no shared node, clean
    snapping) turned out to be connected by a nearby street, so the router answered tens of
    metres and the probe read as a graph defect. The tags said nothing about it either time.
    scripts/verify-candidates.py routes this list against a built graph and keeps only the pairs
    that actually cost a detour."""
    def is_link(way):
        return way["tags"].get("highway", "").endswith("_link")

    bridges = [w for w in ways
               if not is_link(w)
               and w["tags"].get("bridge") in ("yes", "viaduct")
               and w["tags"].get("layer", "0") not in ("0", "-1", "-2")]
    ground = [w for w in ways
              if not is_link(w) and not w["tags"].get("bridge") and not w["tags"].get("tunnel")]

    found = []
    for bridge in bridges:
        bridge_nodes = set(bridge["nodes"])
        bridge_name = bridge["tags"].get("name")
        closest = None
        for road in ground:
            if bridge_nodes & set(road["nodes"]):
                continue
            if bridge_name and bridge_name == road["tags"].get("name"):
                continue
            for bp in bridge["geom"]:
                for rp in road["geom"]:
                    distance = metres(bp, rp)
                    if not MIN_SEPARATION_M <= distance <= MAX_SEPARATION_M:
                        continue
                    if closest is None or distance < closest[0]:
                        closest = (distance, road, bp, rp)
        if closest:
            distance, road, bp, rp = closest
            found.append({
                "bridge": bridge_name or f"way {bridge['id']}",
                "under": road["tags"].get("name") or f"way {road['id']}",
                "separation_m": round(distance),
                "from": [round(bp[0], 6), round(bp[1], 6)],
                "to": [round(rp[0], 6), round(rp[1], 6)],
            })
    # One per bridge already; prefer the widest separations, which are the least likely to be
    # two decks of one structure and the most likely to survive snapping.
    found.sort(key=lambda c: -c["separation_m"])
    return found[:limit]


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--candidates"]
    if "--candidates" in sys.argv:
        args.pop(args.index(sys.argv[sys.argv.index("--candidates") + 1]))
    if len(args) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    name, south, west, north, east = args[0], *map(float, args[1:])
    bbox = f"{south},{west},{north},{east}"

    print(f"# fetching ways for {name} ({bbox}) ...", file=sys.stderr)
    ways = fetch_ways(bbox)
    print(f"# {len(ways)} drivable ways", file=sys.stderr)

    if "--candidates" in sys.argv:
        path = sys.argv[sys.argv.index("--candidates") + 1]
        candidates = bridge_candidates(ways)
        json.dump({"city": name, "candidates": candidates},
                  open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"# {len(candidates)} bridge candidates → {path}", file=sys.stderr)
        for c in candidates:
            print(f"#   {c['separation_m']:3d} m  {c['bridge']} over {c['under']}",
                  file=sys.stderr)
        return 0

    oneway = pick_oneway(ways)
    bridge = pick_bridge(ways)

    if oneway:
        print(f"    # {oneway['name']} (OSM way {oneway['way']}), "
              f"{oneway['length_m']} m one-way")
        print(f"    oneway: [{oneway['from'][0]:.6f}, {oneway['from'][1]:.6f}, "
              f"{oneway['to'][0]:.6f}, {oneway['to'][1]:.6f}]")
    else:
        print("    # NO ONE-WAY CANDIDATE FOUND — widen the bbox", file=sys.stderr)

    if bridge:
        print(f"    # {bridge['bridge']} over {bridge['under']}, "
              f"{bridge['separation_m']} m apart, no shared node")
        print(f"    grade_separation: [{bridge['from'][0]:.6f}, {bridge['from'][1]:.6f}, "
              f"{bridge['to'][0]:.6f}, {bridge['to'][1]:.6f}, {bridge['separation_m']}]")
    else:
        print("    # NO GRADE SEPARATION FOUND — widen the bbox", file=sys.stderr)

    return 0 if (oneway and bridge) else 1


if __name__ == "__main__":
    raise SystemExit(main())
