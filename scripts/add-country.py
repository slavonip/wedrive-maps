"""Add a country to regions.yml — the one expensive step, made one command.

    usage: python3 add-country.py <CC> <geofabrik-path> <city-lat> <city-lon>
                         [--radius-km 12] [--timezone Europe/Kyiv]
    e.g.   python3 add-country.py UA europe/ukraine 50.4501 30.5234

Writes the country's block: Geofabrik path, bounding box, IANA timezone, and probe coordinates
for a one-way street and a grade separation, all taken from OSM around the city given. Then says
what is still missing — the border entries to its neighbours — because those need a route on
each side and cannot be guessed from one point.

WHY A COMMAND AND NOT A PARAGRAPH OF INSTRUCTIONS. A package derives from its country codes, so
combinations are free (scripts/packages.py); the country itself is not, and everything expensive
about it is expensive in the same way — coordinates that have to be real. Typed from memory they
point at the wrong junction, which cost a morning on the Moldova bridge probe; picked from OSM
tags alone they can be an interchange, which cost another on the Romanian one. So the tags narrow
and the ROUTER decides, and the only way to make that repeatable for country number seven is to
put it behind one command.

THE CANDIDATES ARE NOT TRUSTED HERE. This writes a block with the best candidate in it and marks
it `verified: false`. `scripts/verify-candidates.py` against a built graph is what flips that, and
the coverage gate refuses a country that never got there.
"""
import json
import os
import subprocess
import sys
import urllib.request

import yaml

sys.path.insert(0, "scripts")
import probe_finding  # noqa: E402  — the importable half of find-probes.py

VALHALLA_IMAGE = "ghcr.io/gis-ops/docker-valhalla/valhalla:3.5.1"


def geofabrik_bbox(region: str) -> list:
    """The extract's own bounding box, from Geofabrik's index — so the basemap is cut to the
    same ground the routing extract covers."""
    data = json.load(urllib.request.urlopen(
        "https://download.geofabrik.de/index-v1.json", timeout=180))
    short = region.rsplit("/", 1)[-1]
    for feature in data["features"]:
        if feature["properties"].get("id") != short:
            continue
        xs, ys = [], []

        def walk(geometry):
            if isinstance(geometry[0], (int, float)):
                xs.append(geometry[0])
                ys.append(geometry[1])
            else:
                for part in geometry:
                    walk(part)

        walk(feature["geometry"]["coordinates"])
        return [round(min(xs), 2), round(min(ys), 2), round(max(xs), 2), round(max(ys), 2)]
    raise SystemExit(f"Geofabrik has no extract called '{short}'")


def timezone_at(lat: float, lon: float) -> str:
    """The IANA zone of the city, from the same timezone dataset the graph is built with.

    Asked of the vendored database rather than of a web service: this is the number the probe
    will compare against, and comparing the graph to a DIFFERENT authority would make the probe
    fail whenever the two disagreed rather than when the build lost its timezones.
    """
    query = (f"SELECT tzid FROM tz_world WHERE ST_Contains(geom, MakePoint({lon},{lat},4326)) "
             f"AND ROWID IN (SELECT ROWID FROM SpatialIndex WHERE f_table_name='tz_world' "
             f"AND search_frame=MakePoint({lon},{lat},4326)) LIMIT 1;")
    for command in (
        # On a machine with spatialite and the vendored database to hand.
        ["spatialite", "vendor/timezones.sqlite", query],
        # Otherwise through the same image the factory builds in, which certainly has both.
        ["docker", "run", "--rm", "-v", f"{os.getcwd()}:/w", "-w", "/w",
         "--entrypoint", "spatialite", VALHALLA_IMAGE, "vendor/timezones.sqlite", query],
    ):
        try:
            out = subprocess.run(command, capture_output=True, text=True,
                                 timeout=300).stdout.strip()
            if out:
                return out.splitlines()[-1].strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return ""


def main() -> int:
    if len(sys.argv) < 5:
        print(__doc__, file=sys.stderr)
        return 2
    code = sys.argv[1].upper()
    region = sys.argv[2]
    lat, lon = float(sys.argv[3]), float(sys.argv[4])
    radius_km = 12.0
    if "--radius-km" in sys.argv:
        radius_km = float(sys.argv[sys.argv.index("--radius-km") + 1])

    config = yaml.safe_load(open("regions.yml", encoding="utf-8"))
    if code in config.get("countries", {}):
        print(f"{code} is already in regions.yml", file=sys.stderr)
        return 2

    degrees = radius_km / 111.0
    south, west = lat - degrees, lon - degrees / 2
    north, east = lat + degrees, lon + degrees / 2

    print(f"# asking OSM around {lat}, {lon} (±{radius_km} km)", file=sys.stderr)
    ways = probe_finding.fetch_ways(f"{south},{west},{north},{east}")
    oneway = probe_finding.pick_oneway(ways)
    candidates = probe_finding.bridge_candidates(ways)

    bbox = geofabrik_bbox(region)
    zone = (sys.argv[sys.argv.index("--timezone") + 1] if "--timezone" in sys.argv
            else timezone_at(lat, lon))

    print(f"""
  {code}:
    name: {code}
    region: {region}
    bbox: {bbox}
    timezone: {zone or "UNKNOWN — pass --timezone, or run where the vendored db is"}
    timezone_at: [{lat}, {lon}]""")
    if oneway:
        print(f"""    # {oneway['name']} (OSM way {oneway['way']}), {oneway['length_m']} m one-way
    oneway: [{oneway['from'][0]:.6f}, {oneway['from'][1]:.6f}, """
              f"""{oneway['to'][0]:.6f}, {oneway['to'][1]:.6f}]""")
    else:
        print("    # NO ONE-WAY FOUND — widen --radius-km or pick another city")

    if candidates:
        best = candidates[0]
        print(f"""    # {best['bridge']} over {best['under']}, {best['separation_m']} m apart
    # UNVERIFIED: run verify-candidates.py against a built graph before trusting this
    grade_separation: [{best['from'][0]}, {best['from'][1]}, """
              f"""{best['to'][0]}, {best['to'][1]}, {best['separation_m']}]""")
        path = f"candidates-{code.lower()}.json"
        json.dump({"city": code, "candidates": candidates},
                  open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"\n# {len(candidates)} bridge candidates written to {path}", file=sys.stderr)
        print(f"# NEXT: build any package containing {code}, then\n"
              f"#   python3 scripts/verify-candidates.py <config> {path}\n"
              f"# and paste the highest ratio. A candidate that looks perfect in OSM can still "
              f"be an interchange.", file=sys.stderr)
    else:
        print("    # NO GRADE SEPARATION FOUND — widen --radius-km or pick another city")

    neighbours = [c for c in config.get("countries", {}) if c != code]
    if neighbours:
        print(f"\n# AND a `borders:` entry for each neighbour you will put in a package with "
              f"{code}:", file=sys.stderr)
        for other in neighbours:
            key = "-".join(sorted((code, other)))
            print(f"#   {key}: from/to across the frontier, or {{adjacent: false}}",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
