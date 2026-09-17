"""Count what the admin build dropped, and refuse it when it dropped something we are building.

    usage: check-admins.py <admin-build.log> --expect MD RO AT HU [--record verdict.json]

WHY THIS IS NOT "SUPPRESS A KNOWN WARNING". `valhalla_build_admins` prints, on every one of our
builds, dozens of lines like:

    [ERROR] sqlite3_step() error: NOT NULL constraint failed: admin_access.admin_id.
            Ignore if not using a planet extract or check if there was a name change for Greece

Upstream says to ignore it on an extract, and that is probably right — but "probably right,
according to someone else, about a message we have never read" is exactly the shape in which the
timezone abort survived eight bisecting runs. A message nobody has checked is not a message known
to be harmless; it is a message nobody has checked.

So the warning is QUANTIFIED rather than silenced. Measured on the four-country build: **30
failures, and not one of them names a country we were building** — every one is a country absent
from the extract, which is precisely the case upstream describes. That turns "ignore it" from
folklore into a property, and the property is what this checks:

    a dropped admin record for a country NOT in this build   →  expected, and counted
    a dropped admin record for a country we ARE building     →  refused

The second has never been observed. If it ever happens, it means a country's access rules did not
make it into the graph, which affects driving side, country-crossing costs and access defaults —
none of which any existing probe would notice, because routes still come back.

The functional half lives in `probe.py` as the `admin` probe: it asks the finished graph which
country a point is in, which is the only way to know the records actually landed.
"""
import argparse
import json
import pathlib
import re
import sys

FAILURE = re.compile(
    r"NOT NULL constraint failed: admin_access\.admin_id\..*?name change for (.+?)\s*$",
    re.MULTILINE)

# What each country is called in OSM's admin boundaries, where that differs from what we call it.
# Only the ones we build need an entry; anything absent is matched on its own name.
ALIASES = {
    "MD": ["Moldova"],
    "RO": ["Romania", "România"],
    "AT": ["Austria", "Österreich"],
    "HU": ["Hungary", "Magyarország"],
    "UA": ["Ukraine", "Україна"],
    "BG": ["Bulgaria", "България"],
    "GR": ["Greece", "Ελλάδα"],
    "PL": ["Poland", "Polska"],
    "CZ": ["Czechia", "Czech Republic", "Česko"],
    "SK": ["Slovakia", "Slovensko"],
    "DE": ["Germany", "Deutschland"],
    "CH": ["Switzerland", "Schweiz", "Suisse"],
    "SI": ["Slovenia", "Slovenija"],
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("log")
    parser.add_argument("--expect", nargs="+", default=[],
                        help="the country codes this build actually contains")
    parser.add_argument("--record", default=None)
    args = parser.parse_args()

    try:
        text = pathlib.Path(args.log).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print(f"   no admin log at {args.log} — nothing to check")
        return 0

    dropped = sorted({name.strip() for name in FAILURE.findall(text)})

    # Which of the dropped names belong to countries this build is FOR. Matched case-insensitively
    # against every name we know a country by, because the log prints OSM's name and we hold ISO
    # codes.
    ours = {}
    for code in args.expect:
        names = {n.lower() for n in ALIASES.get(code, [code])} | {code.lower()}
        hit = [name for name in dropped if name.lower() in names]
        if hit:
            ours[code] = hit

    print(f"   {len(dropped)} admin record(s) dropped by the build")
    if dropped:
        shown = ", ".join(dropped[:8]) + (f", and {len(dropped) - 8} more" if len(dropped) > 8 else "")
        print(f"      {shown}")
    print(f"   {len(ours)} of them belong to the {len(args.expect)} countries being built")

    verdict = "FAIL" if ours else "PASS"
    if args.record:
        pathlib.Path(args.record).write_text(json.dumps({
            "adminsGate": verdict,
            "droppedCount": len(dropped),
            "dropped": dropped,
            "droppedInThisBuild": ours,
        }, indent=2))

    if ours:
        print("\nREFUSED: an admin record was dropped for a country this build contains.",
              file=sys.stderr)
        for code, names in ours.items():
            print(f"   - {code}: {', '.join(names)}", file=sys.stderr)
        print("\n   Access rules, driving side and country-crossing costs come from these "
              "records.\n   Routes would still be returned, so no routing probe would notice.",
              file=sys.stderr)
        return 2

    print("   every dropped record is a country absent from this extract, which is the case "
          "upstream describes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
