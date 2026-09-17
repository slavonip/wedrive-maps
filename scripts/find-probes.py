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
import sys

from probe_finding import (
    bridge_candidates,
    fetch_ways,
    pick_bridge,
    pick_oneway,
)


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
