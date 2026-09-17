"""The ladder the scale probe climbs, and the reason each rung is shaped the way it is.

    usage: scale-ladder.py all | 2,4,7

Emits a GitHub Actions matrix. Lives in a file rather than inside the workflow because a list of
countries with a rationale is content, and content embedded in YAML stops being read.

**EVERY RUNG IS GEOGRAPHICALLY CONTIGUOUS**, which is not decoration. A master over countries that
do not touch has no frontier tiles, so it would measure tile building while telling us nothing
about the thing country packages depend on — and it is not a shape we would ever build. Each rung
here is a plausible master in its own right: if rung 7 fits and rung 10 does not, rung 7 IS the
eastern master.

Sizes are Geofabrik's `.osm.pbf`, as of 2026-09, and are approximate — the probe measures the real
figure and reports it, so these are only for choosing the spacing.
"""
import json
import sys

LADDER = [
    # Where we already are. Reproduces a known build so the instrumentation can be checked
    # against a number we have seen twice.
    {"rung": 2, "gb": 0.7,
     "members": "MD:europe/moldova RO:europe/romania"},

    # The four-country master that took 357 s of tile building — the point that broke the old
    # "half an hour per gigabyte" rule, re-measured with the stages separated.
    {"rung": 4, "gb": 1.5,
     "members": "AT:europe/austria HU:europe/hungary MD:europe/moldova RO:europe/romania"},

    # A genuine eastern-Europe master: everything from the Baltic down to the Black Sea that this
    # car could plausibly drive in one trip. If this fits comfortably, the "two or three
    # overlapping masters" plan is already viable.
    {"rung": 7, "gb": 3.6,
     "members": "AT:europe/austria CZ:europe/czech-republic HU:europe/hungary "
                "MD:europe/moldova PL:europe/poland RO:europe/romania SK:europe/slovakia"},

    # Germany is the step that matters, and it is deliberately the biggest single jump. It is by
    # far the densest country in the region — Austria already showed that road density costs more
    # than bytes do — so this rung asks the real question rather than a polite one. 8.4 GB is in
    # the 5–10 GB band where we expect to meet whichever limit binds first.
    {"rung": 10, "gb": 8.4,
     "members": "AT:europe/austria CH:europe/switzerland CZ:europe/czech-republic "
                "DE:europe/germany HU:europe/hungary MD:europe/moldova PL:europe/poland "
                "RO:europe/romania SI:europe/slovenia SK:europe/slovakia"},
]


def main() -> int:
    want = (sys.argv[1] if len(sys.argv) > 1 else "all").strip()
    rungs = LADDER
    if want and want != "all":
        keep = {int(part) for part in want.replace(" ", "").split(",") if part}
        rungs = [rung for rung in LADDER if rung["rung"] in keep]
        missing = keep - {rung["rung"] for rung in LADDER}
        if missing:
            print(f"no such rung: {sorted(missing)}; have "
                  f"{[r['rung'] for r in LADDER]}", file=sys.stderr)
            return 2
    if not rungs:
        print("no rungs selected", file=sys.stderr)
        return 2

    for rung in rungs:
        print(f"rung {rung['rung']:2d}  ~{rung['gb']} GB  "
              f"{len(rung['members'].split())} countries", file=sys.stderr)
    print("matrix=" + json.dumps({"include": rungs}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
