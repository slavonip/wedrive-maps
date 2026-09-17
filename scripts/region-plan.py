"""What a super-region is made of, and whether every country in it is probed.

    usage: region-plan.py <region-id>            # GITHUB_OUTPUT lines for the workflow
           region-plan.py <region-id> --check    # coverage only, exit 2 if incomplete
           region-plan.py <region-id> --plans <dir>   # one <CC>.json probe plan per country

The package pipeline's twin (`probe-plan.py`), for the pipeline that builds once and cuts
afterwards. It reuses the same `countries:` and `borders:` blocks, because a country's one-way
street and its timezone are the same facts whichever way its tiles are shipped — paying for them
twice would be the surest way to let the two drift apart.
"""
import itertools
import json
import os
import sys

import packages as packages_module
import probe_plan_shared as shared


def region(config: dict, region_id: str) -> dict:
    found = config.get("superregions", {}).get(region_id)
    if found is None:
        known = ", ".join(config.get("superregions", {})) or "none"
        raise SystemExit(f"'{region_id}' is not a super-region in regions.yml (have: {known})")
    return found


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    region_id = sys.argv[1]
    config = packages_module.load()
    members = sorted(region(config, region_id).get("countries", []))
    if not members:
        print(f"{region_id} lists no countries", file=sys.stderr)
        return 2

    countries = config.get("countries", {})
    borders = config.get("borders", {})

    # ── coverage, exactly as strict as the package pipeline ────────────────────────────────
    problems = []
    for code in members:
        block = countries.get(code)
        if block is None:
            problems.append(f"{code} has no entry under `countries:` — run "
                            f"scripts/add-country.py for one of its cities")
            continue
        for field in shared.REQUIRED_COUNTRY_PROBES:
            if not block.get(field):
                problems.append(f"{code} has no `{field}`")
    for a, b in itertools.combinations(members, 2):
        key = "-".join(sorted((a, b)))
        entry = borders.get(key)
        if entry is None:
            problems.append(f"{key} is two countries in one region with no `borders:` entry — "
                            f"add a route across the frontier, or `{key}: {{adjacent: false}}`")
        elif entry.get("adjacent") is not False and not (entry.get("from") and entry.get("to")):
            problems.append(f"{key} has no `from`/`to`")

    if problems:
        print(f"UNCOVERED {region_id}:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    if "--check" in sys.argv:
        print(f"covered   {region_id}: {len(members)} countries")
        return 0

    if "--plans" in sys.argv:
        target = sys.argv[sys.argv.index("--plans") + 1]
        os.makedirs(target, exist_ok=True)
        # ONE PLAN PER COUNTRY, plus one for the assembled whole. The per-country plan is what
        # proves a cut works on its own; the assembled plan is what proves the cuts reassemble,
        # and only the second can see the frontier.
        for code in members:
            plan = shared.plan_for_countries(config, code, [code])
            json.dump(plan, open(os.path.join(target, f"{code}.json"), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        whole = shared.plan_for_countries(config, region_id, members)
        json.dump(whole, open(os.path.join(target, "_assembled.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"{region_id}: {len(members)} country plans + the assembled one -> {target}")
        return 0

    # Workflow outputs: the build script's arguments, and the country list.
    members_arg = " ".join(f"{code}:{countries[code]['region']}" for code in members)
    print(f"members={members_arg}")
    print("countries=" + json.dumps(members))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
