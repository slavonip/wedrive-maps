"""Derive a package's probe plan from regions.yml, and REFUSE a package that is under-covered.

This is the half of the gate that runs BEFORE the build, on the runner, in a second. The other
half (probe.py) runs after, inside the Valhalla container, and can only test what this hands it.

    usage: python3 probe-plan.py <package-id> [<output.json>]
           python3 probe-plan.py --check-all          # coverage only, every package

Exit codes: 0 covered, 2 not covered (or the package is unknown).

WHY THIS EXISTS AS A SEPARATE STEP. The first md-ro build passed its gate on four probes that
all pointed at Chișinău. Romania was in the package, was half its bytes, and was asserted by
nothing — the border route was the only thing that touched it, and a border route proves the
frontier joined, not that Romanian one-ways survived tile building. Nobody noticed, because a
gate that runs the probes it has always looks like a gate that ran.

So coverage is a property of the CONFIGURATION, checked without building anything:

  * every country in the package declares timezone, oneway and grade_separation;
  * every adjacent pair of countries in the package declares a border route.

Add Ukraine to a package tomorrow and this fails in one second with the reason, instead of four
hours later with a green tick.
"""
import itertools
import json
import sys

import yaml

REQUIRED_COUNTRY_PROBES = ("timezone", "timezone_at", "oneway", "grade_separation")


def load() -> dict:
    return yaml.safe_load(open("regions.yml", encoding="utf-8"))


def border_key(a: str, b: str) -> str:
    """Borders are undirected, so the key is the pair sorted — MD-RO, never RO-MD."""
    return "-".join(sorted((a, b)))


def coverage_problems(config: dict, package_id: str) -> list:
    """Everything this package is missing, as sentences. Empty means covered."""
    package = config.get("packages", {}).get(package_id)
    if package is None:
        return [f"package '{package_id}' is not in regions.yml"]

    countries = config.get("countries", {})
    borders = config.get("borders", {})
    problems = []

    listed = package.get("countries", [])
    if not listed:
        problems.append(f"{package_id} lists no countries")

    for code in listed:
        probes = countries.get(code)
        if probes is None:
            problems.append(
                f"{code} is in package '{package_id}' but has no entry under `countries:` — "
                f"run scripts/find-probes.py for one of its cities and add one")
            continue
        for field in REQUIRED_COUNTRY_PROBES:
            if not probes.get(field):
                problems.append(f"{code} has no `{field}`")

    # A pair is expected to have a border route unless it is declared non-adjacent. Silence is
    # not taken as "they do not touch": that is indistinguishable from forgetting.
    for a, b in itertools.combinations(sorted(listed), 2):
        key = border_key(a, b)
        entry = borders.get(key)
        if entry is None:
            problems.append(
                f"{key} is two countries in one package with no `borders:` entry — add a route "
                f"across the frontier, or `{key}: {{adjacent: false}}` if they do not share one")
        elif entry.get("adjacent") is False:
            continue
        elif not (entry.get("from") and entry.get("to")):
            problems.append(f"{key} has no `from`/`to`")

    return problems


def plan_for(config: dict, package_id: str) -> dict:
    """The concrete probes, as the container-side runner wants them."""
    package = config["packages"][package_id]
    countries = config.get("countries", {})
    borders = config.get("borders", {})
    listed = package.get("countries", [])

    probes = []
    for code in listed:
        c = countries[code]
        label = c.get("name", code)

        lat, lon = c["timezone_at"]
        probes.append({"kind": "timezone", "name": f"{label}: timezone",
                       "at": [lat, lon], "expect": c["timezone"]})

        f_lat, f_lon, t_lat, t_lon = c["oneway"]
        probes.append({"kind": "oneway", "name": f"{label}: one-way",
                       "from": [f_lat, f_lon], "to": [t_lat, t_lon]})

        b_lat, b_lon, u_lat, u_lon, separation = c["grade_separation"]
        probes.append({"kind": "grade_separation", "name": f"{label}: grade separation",
                       "from": [b_lat, b_lon], "to": [u_lat, u_lon],
                       "separation_m": separation})

    for a, b in itertools.combinations(sorted(listed), 2):
        entry = borders.get(border_key(a, b), {})
        if entry.get("adjacent") is False:
            continue
        probes.append({
            "kind": "border", "name": f"{a} → {b}: across the frontier",
            "from": entry["from"], "to": entry["to"],
            "min_km": entry.get("min_km", 1), "max_km": entry.get("max_km", 2000),
        })

    return {"package": package_id, "countries": listed, "probes": probes}


def main() -> int:
    config = load()

    if "--check-all" in sys.argv:
        failed = False
        for package_id in config.get("packages", {}):
            problems = coverage_problems(config, package_id)
            if problems:
                failed = True
                print(f"UNCOVERED {package_id}:", file=sys.stderr)
                for problem in problems:
                    print(f"  - {problem}", file=sys.stderr)
            else:
                count = len(plan_for(config, package_id)["probes"])
                print(f"covered   {package_id}: {count} probes")
        return 2 if failed else 0

    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    package_id = sys.argv[1]
    problems = coverage_problems(config, package_id)
    if problems:
        print(f"UNCOVERED {package_id}:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    plan = plan_for(config, package_id)
    text = json.dumps(plan, ensure_ascii=False, indent=2)
    if len(sys.argv) > 2:
        open(sys.argv[2], "w", encoding="utf-8").write(text + "\n")
        print(f"{package_id}: {len(plan['probes'])} probes → {sys.argv[2]}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
