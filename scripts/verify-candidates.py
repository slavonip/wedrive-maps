"""Route every grade-separation candidate against a real graph, and rank them by detour.

    usage: python3 verify-candidates.py <valhalla-config.json> <candidates.json>

Runs inside the Valhalla container (stdlib only). Prints the candidates worth putting in
regions.yml, best first, and the ones that are not — with the reason.

WHY THE ROUTER IS THE ONLY JUDGE HERE. OSM tags cannot distinguish a bridge nobody can turn off
from an interchange whose ramps make that turn legal. Both look identical in the data: two named
ways, no shared node, tens of metres apart. Two candidates picked on tags alone today answered 9 m
and 35 m, and both times the first reading was "the tile builder joined them", and both times the
tile builder was right. A candidate is only usable once a route has been asked for and cost a
detour.
"""
import json
import subprocess
import sys

# What the probe will later assert. A candidate that does not clear it on a graph KNOWN to be
# good would fail every future build for a reason that has nothing to do with the build.
REQUIRED_RATIO = 5

# Above this the detour is so long that a future OSM edit adding one connection could halve it
# and still pass — useful, but the interesting ones are the tight, unambiguous ones.
SANE_MAX_M = 8_000


def route_m(config: str, frm: list, to: list) -> int | None:
    request = {"locations": [{"lat": frm[0], "lon": frm[1]}, {"lat": to[0], "lon": to[1]}],
               "costing": "auto"}
    try:
        out = subprocess.run(["valhalla_service", config, "route", json.dumps(request)],
                             capture_output=True, text=True, timeout=120).stdout
        return int(json.loads(out)["trip"]["summary"]["length"] * 1000)
    except Exception:                              # noqa: BLE001
        return None


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    config, path = sys.argv[1], sys.argv[2]
    data = json.load(open(path, encoding="utf-8"))

    usable, rejected = [], []
    for candidate in data["candidates"]:
        gap = candidate["separation_m"]
        driven = route_m(config, candidate["from"], candidate["to"])
        if driven is None:
            rejected.append((candidate, "no route at all"))
            continue
        ratio = driven / gap
        if driven > SANE_MAX_M:
            rejected.append((candidate, f"{driven} m — implausibly far"))
        elif ratio < REQUIRED_RATIO:
            rejected.append((candidate, f"{driven} m for {gap} m = {ratio:.1f}× — connected "
                                        f"nearby, so this is an interchange not a separation"))
        else:
            usable.append((candidate, driven, ratio))

    usable.sort(key=lambda item: -item[2])

    print(f"== {data.get('city', '?')}: {len(usable)} usable of {len(data['candidates'])} ==")
    for candidate, driven, ratio in usable:
        print(f"  {ratio:5.1f}x  {driven:5d} m round {candidate['separation_m']:3d} m  "
              f"{candidate['bridge']} over {candidate['under']}")
        print(f"          grade_separation: [{candidate['from'][0]}, {candidate['from'][1]}, "
              f"{candidate['to'][0]}, {candidate['to'][1]}, {candidate['separation_m']}]")
    if not usable:
        print("  none — widen the bbox or try another city")
    print(f"-- rejected {len(rejected)}:")
    for candidate, why in rejected:
        print(f"     {candidate['bridge']} over {candidate['under']}: {why}")
    return 0 if usable else 1


if __name__ == "__main__":
    raise SystemExit(main())
