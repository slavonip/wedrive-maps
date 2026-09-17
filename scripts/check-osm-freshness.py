"""Refuse OSM data that is stale, or that goes BACKWARDS from what we already published.

    usage: check-osm-freshness.py --pbf MD=/data/src/moldova-latest.osm.pbf [...]
                                  [--index index-tiles.json] [--max-age-days 30]
                                  [--record verdict.json]

WHY THE DOWNLOAD TIME IS THE WRONG THING TO RECORD. A build that fetched its PBF this morning can
still be routing on OSM from three months ago: the file's age and the DATA's age are different
facts, and only one of them is visible in the file listing. Geofabrik writes the real one into the
PBF header as `osmosis_replication_timestamp` — the moment of the OSM replication state the
extract was cut from — so it can be read rather than assumed.

TWO QUESTIONS, AND THE SECOND IS THE ONE THAT BITES.

  * **Is it too old?** A mirror serving a months-old file yields a graph that is quietly wrong
    about every road built since, and nothing downstream says so. An absolute bound catches that.

  * **Is it older than what we already shipped?** This is the failure an absolute bound misses
    entirely. A mirror that rolls back — restored from backup, a partial sync, a CDN node serving
    a stale object — hands us data inside the allowed window that is nevertheless a REGRESSION,
    and the resulting release looks newer by its date while containing older roads. Promotion
    would then replace a good graph with a worse one, and the only evidence would be a driver
    finding a road that used to be there.

So the rule is **strictly monotonic per country**: today's snapshot must be at least as new as the
one the last promoted release recorded. Equal is allowed — Geofabrik publishes daily and a rerun
on the same day is legitimate — but earlier never is.

This is the same shape as `check-timezones.py`, deliberately: an external dataset is checked
against what we know before the build spends an hour on it, and its verdict is recorded in the
manifest so a graph can always say what it was built from.
"""
import argparse
import datetime
import json
import pathlib
import re
import subprocess
import sys

TIMESTAMP_KEY = "header.option.osmosis_replication_timestamp"


def snapshot_of(pbf: str) -> str | None:
    """The replication timestamp in the PBF header, or None if it carries none.

    `osmium fileinfo -g` prints just that value. An extract with no such header is not
    automatically a failure — some legitimate sources omit it — but it cannot be checked, and
    the caller is told so rather than being given a reassuring default.
    """
    try:
        done = subprocess.run(["osmium", "fileinfo", "-g", TIMESTAMP_KEY, pbf],
                              capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        print("   osmium is not installed, so no extract can be checked", file=sys.stderr)
        return None
    text = (done.stdout or "").strip()
    match = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text)
    if match:
        return match.group(0)
    # SAY WHY. "Unchecked" is a fair verdict but a useless one if nobody can tell whether the
    # header is absent, the key is spelled differently in this osmium, or the call failed — and
    # a gate that quietly checks nothing is the failure mode this whole file exists to prevent.
    reason = (done.stderr or "").strip().splitlines()
    print(f"      osmium said: {reason[-1][:100] if reason else text[:100] or 'nothing'}",
          file=sys.stderr)
    return None


def parse(stamp: str) -> datetime.datetime:
    return datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)


def previously_published(index_path) -> dict:
    """What the last promoted release recorded, per country. A missing or unreadable index is not
    an error: the first build of a country has nothing to regress from."""
    try:
        index = json.loads(pathlib.Path(index_path).read_text(encoding="utf-8"))
    except Exception:                               # noqa: BLE001
        return {}
    published = {}
    for code, entry in (index.get("countries") or {}).items():
        stamp = (entry.get("osm") or {}).get("snapshot") or entry.get("osmSnapshot")
        if stamp:
            published[code] = stamp
    return published


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pbf", action="append", default=[], metavar="CC=PATH")
    parser.add_argument("--index", default="index-tiles.json")
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--record", default=None)
    parser.add_argument("--now", default=None, help="override the clock, for tests")
    args = parser.parse_args()

    now = parse(args.now) if args.now else datetime.datetime.now(datetime.timezone.utc)
    published = previously_published(args.index)

    problems, unchecked, snapshots = [], [], {}
    for pair in args.pbf:
        code, _, path = pair.partition("=")
        stamp = snapshot_of(path)
        if not stamp:
            unchecked.append(code)
            print(f"   {code}: no replication timestamp in the header — cannot be checked")
            continue
        snapshots[code] = stamp
        age = (now - parse(stamp)).days
        before = published.get(code)

        note = f"   {code}: {stamp} ({age} days old)"
        if before:
            note += f", last published {before}"
        print(note)

        if age > args.max_age_days:
            problems.append(f"{code} is {age} days old, older than the {args.max_age_days}-day "
                            f"limit — the mirror is serving stale data")
        if before and parse(stamp) < parse(before):
            problems.append(f"{code} GOES BACKWARDS: {stamp} is older than the {before} already "
                            f"published. A mirror has rolled back; building this would replace a "
                            f"good graph with an older one that looks newer")

    verdict = "PASS"
    if problems:
        verdict = "FAIL"
    elif unchecked:
        verdict = "UNCHECKED"

    if args.record:
        pathlib.Path(args.record).write_text(json.dumps({
            "snapshots": snapshots,
            "freshnessGate": verdict,
            "maxAgeDays": args.max_age_days,
            "unchecked": sorted(unchecked),
        }, indent=2))

    if problems:
        print("\nREFUSED: the OSM data would not be an improvement.", file=sys.stderr)
        for problem in problems:
            print(f"   - {problem}", file=sys.stderr)
        print("\n   Check the Geofabrik mirror before overriding. The age limit is a judgement "
              "call\n   and can be raised; a snapshot going backwards is not, and never should "
              "be.", file=sys.stderr)
        return 2

    if unchecked:
        print(f"   {len(unchecked)} extract(s) carry no replication timestamp; "
              f"freshness UNCHECKED")
    else:
        print(f"   every extract is current and no country went backwards")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
