"""Derive a package's probe plan from regions.yml, and REFUSE a package that is under-covered.

This is the half of the gate that runs BEFORE the build, on the runner, in a second. The other
half (probe.py) runs after, inside the Valhalla container, and can only test what this hands it.

    usage: python3 probe-plan.py <package-id> [<output.json>]
           python3 probe-plan.py --check-all          # coverage only, every package
           python3 probe-plan.py --all-plans <dir>    # one <package>.json per named package

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

import packages as packages_module
import probe_plan_shared as shared

REQUIRED_COUNTRY_PROBES = shared.REQUIRED_COUNTRY_PROBES


def load() -> dict:
    return packages_module.load()


border_key = shared.border_key


def coverage_problems(config: dict, package_id: str) -> list:
    """Everything this package is missing, as sentences. Empty means covered."""
    countries = config.get("countries", {})
    borders = config.get("borders", {})
    problems = []

    try:
        listed = packages_module.countries_of(config, package_id)
    except ValueError as error:
        return [str(error)]
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
    """The concrete probes, built by the module the region pipeline also uses.

    Shared deliberately: a country's one-way street and its timezone are the same facts whether
    its tiles ship whole in a package or cut out of a super-region, and two copies of this logic
    would drift into two different ideas of what "covered" means.
    """
    return shared.plan_for_countries(
        config, package_id, packages_module.countries_of(config, package_id))


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

    if "--all-plans" in sys.argv:
        import os
        target = sys.argv[sys.argv.index("--all-plans") + 1]
        wanted = [a for a in sys.argv[1:]
                  if a not in ("--all-plans", target)] or list(config.get("packages", {}))
        os.makedirs(target, exist_ok=True)
        for package_id in wanted:
            problems = coverage_problems(config, package_id)
            if problems:
                print(f"UNCOVERED {package_id}:", file=sys.stderr)
                for problem in problems:
                    print(f"  - {problem}", file=sys.stderr)
                return 2
            plan = plan_for(config, package_id)
            path = os.path.join(target, f"{package_id}.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(plan, handle, ensure_ascii=False, indent=2)
            print(f"{package_id}: {len(plan['probes'])} probes, "
                  f"{len(plan['regions'])} regions -> {path}")
        return 0

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
