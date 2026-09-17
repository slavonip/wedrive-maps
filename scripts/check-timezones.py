"""Refuse a timezone database the build will choke on, before the build starts.

    usage: check-timezones.py <timezones.sqlite> [--expect-dataset 2026d]

WHY THIS EXISTS. A four-country build aborted for a week and cost eight bisecting runs plus four
wrong hypotheses — Hungarian data, scale, Ukraine, our own scripts — before the message that
named the real cause appeared:

    Failed tile 2/781272/0: Europe/Zagreb can't be resolved to a non-deprecated time zone.
    terminate called after throwing an instance of 'std::exception'

The vendored database was built from timezone-boundary-builder **2024a**, in which Zagreb,
Ljubljana, Sarajevo, Skopje and Podgorica are still distinct zones. tzdata merged them into
Europe/Belgrade, and the date library inside Valhalla refuses a deprecated identifier outright.

**The same fault wore two faces**, which is why it took so long. Fed through one path it produced
the sentence above; through another, `vector::_M_range_check: __n (which is 29) >= this->size()
(which is 0)` — an index into an empty list of zones, with nothing at all to say it was about
time. A build that fails in two unrelated-looking ways is a build nobody bisects correctly.

So the dataset is a VERSIONED INPUT now, checked before use rather than vendored once and
forgotten. Two questions, both cheap, both asked here:

  * does it still carry an identifier the runtime will reject?
  * is it the dataset the manifest claims?

A build refused here costs a second. The same build refused by Valhalla costs half an hour and
a core dump, and — as this week showed — does not necessarily say why.
"""
import argparse
import json
import pathlib
import sqlite3
import sys

# Merged into Europe/Belgrade when tzdata stopped distinguishing zones with identical rules since
# 1970. Any of these in the database means it predates that merge, whatever its version string
# says. Listed explicitly rather than derived: the point is to name the exact failure that cost
# this week, so the next person reading a refusal knows what they are looking at.
DEPRECATED = {
    "Europe/Zagreb": "Europe/Belgrade",
    "Europe/Ljubljana": "Europe/Belgrade",
    "Europe/Sarajevo": "Europe/Belgrade",
    "Europe/Skopje": "Europe/Belgrade",
    "Europe/Podgorica": "Europe/Belgrade",
    # Others tzdata has linked away, kept here so the list is about the CLASS of fault rather
    # than the one instance of it.
    "Europe/Bratislava": "Europe/Prague",
    "Europe/Busingen": "Europe/Zurich",
    "Europe/Vaduz": "Europe/Zurich",
    "Europe/San_Marino": "Europe/Rome",
    "Europe/Vatican": "Europe/Rome",
    "Europe/Mariehamn": "Europe/Helsinki",
    "Arctic/Longyearbyen": "Europe/Berlin",
    "Atlantic/Jan_Mayen": "Europe/Berlin",
    "America/Nassau": "America/Toronto",
    "Asia/Kuwait": "Asia/Riyadh",
    "Asia/Aden": "Asia/Riyadh",
}

# A database with far fewer zones than this is not a timezone database; one with far more is
# from before the 1970 merge. Bounds, not equality — the count moves legitimately with each
# release, and pinning it would refuse a good dataset next quarter.
MIN_ZONES, MAX_ZONES = 250, 400


def runtime_links(zoneinfo):
    """Aliases the RUNTIME ITSELF considers deprecated, read out of its own tzdata.

    The hardcoded list above names the failure that cost this week. This names the failures
    nobody has met yet, and it is the half that actually answers the owner's question — *is this
    dataset compatible with the Valhalla we are about to run it through* — because it reads the
    tzdata shipped in the same image Valhalla runs in, rather than anything I believed in advance.

    `tzdata.zi` is the compiled source tzdata installs beside the binary zones. Its `L <target>
    <name>` lines are precisely the ids that are no longer zones in their own right, which is the
    property the runtime refuses.

    **The form of the data decides the answer, and that is correct rather than unfortunate.**
    tzdata ships in two flavours: *rearguard*, where Europe/Zagreb is still a real `Z` zone, and
    the main form, where it is `L Europe/Belgrade Europe/Zagreb`. Ubuntu 22.04 uses rearguard, so
    the very alias that aborted our build is NOT flagged there — and that is the right answer for
    that runtime, which would accept it. The check adapts to whichever image it is run in instead
    of asserting one global truth about time.
    """
    source = pathlib.Path(zoneinfo) / "tzdata.zi"
    if not source.exists():
        return None, None, None
    links, canonical, version = {}, set(), None
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("# version"):
            version = line.split()[-1]
        elif line.startswith("L"):
            parts = line.split()
            if len(parts) >= 3:
                links[parts[2]] = parts[1]      # L <target> <alias>
        elif line.startswith("Z"):
            parts = line.split()
            if len(parts) >= 2:
                canonical.add(parts[1])         # Z <name> <offset> ...
    return links, canonical, version


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database")
    parser.add_argument("--expect-dataset", default=None,
                        help="the version the manifest claims, for cross-checking")
    parser.add_argument("--record", default=None,
                        help="write this gate's verdict here as JSON, for the manifest")
    parser.add_argument("--zoneinfo", default="/usr/share/zoneinfo",
                        help="the RUNTIME's own tzdata, which decides what it will accept")
    args = parser.parse_args()

    try:
        connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
        zones = [row[0] for row in connection.execute("SELECT DISTINCT tzid FROM tz_world")]
    except Exception as error:                      # noqa: BLE001
        print(f"FAIL cannot read {args.database}: {error}", file=sys.stderr)
        return 2

    print(f"   {len(zones)} zones in {args.database}")
    if args.expect_dataset:
        print(f"   manifest claims dataset {args.expect_dataset}")

    problems = []

    found = sorted(set(zones) & set(DEPRECATED))
    if found:
        problems.append(
            "carries identifiers the runtime refuses:\n" +
            "\n".join(f"        {z}  (tzdata merged it into {DEPRECATED[z]})" for z in found))

    links, canonical, runtime_version = runtime_links(args.zoneinfo)
    if links is None:
        print(f"   {args.zoneinfo}/tzdata.zi absent — checking the named list only")
    else:
        print(f"   runtime tzdata {runtime_version or 'unknown'}: "
              f"{len(canonical)} zones, {len(links)} links")
        aliased = sorted(set(zones) & set(links))
        if aliased:
            problems.append(
                "carries identifiers THIS RUNTIME lists as links, not zones:\n" +
                "\n".join(f"        {z}  (this image's tzdata links it to {links[z]})"
                           for z in aliased))

        # THIS IS THE COMPATIBILITY TEST, and it is exhaustive where a build is a sample. A
        # master build only exercises the zones its countries happen to touch — ours covers
        # Moldova and Romania, so it would have passed happily on a dataset whose Balkan zones
        # were the ones about to abort a later region. Every id in the dataset is asked of the
        # runtime here instead, so a dataset is cleared for countries nobody has built yet.
        unknown = sorted(set(zones) - set(links) - canonical)
        if unknown:
            problems.append(
                "carries identifiers this runtime has never heard of:\n" +
                "\n".join(f"        {z}" for z in unknown[:12]) +
                (f"\n        ... and {len(unknown) - 12} more" if len(unknown) > 12 else ""))

    if not MIN_ZONES <= len(zones) <= MAX_ZONES:
        problems.append(f"has {len(zones)} zones, expected {MIN_ZONES}..{MAX_ZONES} — "
                        f"either not a timezone database or from before the 1970 merge")

    # A spot check that the polygons are usable at all, not merely present. Chișinău, because it
    # is the one place every other probe in this factory also asks about.
    try:
        row = connection.execute(
            "SELECT tzid FROM tz_world WHERE ST_Contains(geom, MakePoint(28.8638, 47.0105, 4326))"
            " LIMIT 1").fetchone()
        if row:
            print(f"   Chișinău resolves to {row[0]}")
    except Exception:                               # noqa: BLE001
        # Spatialite is not always loaded in a plain sqlite3 connection; the absence of this
        # check is not itself a failure, and saying so beats a warning nobody can act on.
        pass

    if problems:
        print("REFUSED: this timezone database would abort the build.", file=sys.stderr)
        for problem in problems:
            print(f"   - it {problem}", file=sys.stderr)
        print("\n   Rebuild it from a current timezone-boundary-builder release. A build fed "
              "this\n   aborts partway through tile building, sometimes with a message about "
              "time and\n   sometimes with an out-of-range error that mentions nothing of the "
              "sort.", file=sys.stderr)
        return 2

    print("   no deprecated identifiers; safe to build with")
    if args.record:
        # Handed back to the build so a graph records the rules it was built under. Both versions,
        # because a conditional-routing oddity a year from now is only diagnosable if the graph
        # says which polygon dataset it was cut from AND which tzdata accepted those identifiers.
        #
        # `zoneCount` is NOT a compatibility check and must not be read as one — it is a forensic
        # fingerprint. A release that suddenly carries 444 zones where every previous one carried
        # 304 is visible in one glance at the manifest, before anyone opens a CI log.
        pathlib.Path(args.record).write_text(json.dumps({
            "dataset": args.expect_dataset or "unknown",
            "runtime": runtime_version or "unknown",
            "compatibilityGate": "PASS",
            "zoneCount": len(zones),
        }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
