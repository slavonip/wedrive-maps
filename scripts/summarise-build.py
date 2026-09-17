"""Turn a build's stage timings and resource samples into the row that answers "how big can a
master be?".

    usage: summarise-build.py <stages.csv> <samples.csv> <tile-dir> <cuts-dir> [options]

THE QUESTION THIS EXISTS FOR. Country packages cut from one coherent master are proved (86 of 86
shared tiles byte-identical across four countries). What is NOT known is how large that master may
be before a GitHub-hosted runner refuses — and the answer decides whether Europe is one master or
two or three overlapping ones, which is the last open question in the whole architecture.

Three limits, and they bind in an order nobody should guess at:

    wall time    6 h per job, hard
    peak memory  the runner's RAM, and the one most likely to bind first — a build that wants
                 17 GB on a 16 GB runner is killed by the OOM killer, which looks like a crash
                 rather than like a limit
    peak disk    measured at 86 GB free after reclaiming, an order of magnitude more than this
                 repo assumed for weeks

**The previous estimate was measuring the wrong thing, which is why it is being replaced rather
than refined.** "Roughly half an hour per gigabyte" came from whole-JOB wall times that included
downloading gigabytes of PBF and extracting a basemap over HTTP range requests. The four-country
master then built its tiles in 357 s at 1.5 GB of source. Both numbers were real; only one of them
was about tile building. Stages are separated here so that can never happen again.
"""
import argparse
import csv
import re
import json
import pathlib
import sys


def stages(path):
    rows = {}
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            # A stage can legitimately appear once; if it ever repeats, sum it rather than lose it.
            rows[row["stage"]] = rows.get(row["stage"], 0) + int(row["seconds"])
    return rows


def peaks(path):
    memory = disk = 0
    samples = 0
    try:
        with open(path, newline="") as handle:
            for row in csv.DictReader(handle):
                samples += 1
                memory = max(memory, int(row["mem_bytes"] or 0))
                disk = max(disk, int(row["disk_kb"] or 0) * 1024)
    except FileNotFoundError:
        return None, None, 0
    return memory, disk, samples


# WHAT ACTUALLY PREDICTS THE COST OF A BUILD. Gigabytes of PBF are a weak proxy: Austria takes
# longer than its size suggests because its road network is denser, and a master is chosen by what
# it costs rather than by how many countries are in it. The builder already counts the better
# quantities and prints them; nobody was reading them.
#
# Recorded in rising order of expected explanatory power — source bytes, then OSM ways and nodes,
# then graph edges and tiles. Several points of history will say which one actually explains peak
# memory, and that is a question to answer with data rather than in advance.
COUNTERS = {
    "routableWays": r"Finished with (\d+) routable ways",
    "osmNodesInRoutableWays": r"Finished with (\d+) nodes contained in routable ways",
    "graphEdges": r"Finished with (\d+) graph edges",
    "directedEdges": r"Directed Edge Count = (\d+)",
    "tilesBuilt": r"Building (\d+) tiles with",
}


def counters(path):
    """Pull the builder's own counts out of its output. Absent keys are absent, never zero — a
    quantity nobody measured and a quantity measured as nothing are different facts, and the
    whole point of this file is to stop conflating those."""
    if not path:
        return {}
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {}
    found = {}
    for name, pattern in COUNTERS.items():
        match = re.search(pattern, text)
        if match:
            found[name] = int(match.group(1))
    return found


def count(root, pattern="*.gph"):
    files = list(pathlib.Path(root).rglob(pattern))
    return len(files), sum(f.stat().st_size for f in files)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stages")
    parser.add_argument("samples")
    parser.add_argument("tile_dir")
    parser.add_argument("cuts_dir")
    parser.add_argument("--region", default="")
    parser.add_argument("--build-id", default="")
    parser.add_argument("--engine", default="")
    parser.add_argument("--sources", type=int, default=0)
    parser.add_argument("--members", type=int, default=0)
    parser.add_argument("--elapsed", type=int, default=0)
    parser.add_argument("--log", default=None,
                        help="the tile builder's own output, which counts what actually predicts cost")
    args = parser.parse_args()

    timings = stages(args.stages)
    peak_memory, peak_disk, samples = peaks(args.samples)
    counted = counters(args.log)
    tiles, tile_bytes = count(args.tile_dir)
    cut_tiles, cut_bytes = count(args.cuts_dir)

    gigabytes = args.sources / 1e9 or None
    tile_seconds = timings.get("tiles", 0)

    # ── IS THIS MEASUREMENT USABLE? Said out loud, not implied by null fields ────────────────
    # A rung already came back green carrying `samples: 0`, because "the build succeeded" and
    # "the build was measured" are different claims and only one of them was being made. They are
    # separated here, and the two consumers want opposite things:
    #
    #   a monthly production build   graph PASS + telemetry INVALID  →  publish the graph anyway.
    #                                An observability failure must never stop the car getting maps.
    #   a scaling rung               telemetry INVALID               →  the rung FAILED. There the
    #                                measurement IS the deliverable and timings alone cannot say
    #                                which limit binds first.
    missing = [name for name, value in (
        ("samples", samples or None),
        ("peakMemory", peak_memory or None),
        ("peakDisk", peak_disk or None),
        ("buildSeconds", args.elapsed or None),
        ("tileSeconds", tile_seconds or None),
    ) if not value]

    report = {
        "telemetry": "VALID" if not missing else "INVALID",
        "telemetryMissing": missing,
        "buildId": args.build_id,
        "region": args.region,
        "engineVersion": args.engine,
        "countries": args.members,
        "source": {"bytes": args.sources, "gb": round(args.sources / 1e9, 2)},
        "graph": {
            **counted,
            "tiles": tiles,
            "bytes": tile_bytes,
            "cutTiles": cut_tiles,
            "cutBytes": cut_bytes,
            # How much the country cuts cost over the single build — the price of a frontier tile
            # living in two packages. On the car, content addressing collapses it back to one.
            "cutOverhead": round(cut_bytes / tile_bytes, 3) if tile_bytes else None,
        },
        "seconds": {**timings, "total": args.elapsed},
        "peak": {
            "memoryBytes": peak_memory,
            "memoryGb": round(peak_memory / 1e9, 2) if peak_memory else None,
            "diskBytes": peak_disk,
            "diskGb": round(peak_disk / 1e9, 2) if peak_disk else None,
            "samples": samples,
        },
        # The numbers that actually extrapolate. Rates are per GB of SOURCE, and are reported for
        # tile building alone as well as for the whole job, because conflating those is precisely
        # the error this file replaces.
        "rates": {
            "tileSecondsPerSourceGb": round(tile_seconds / gigabytes) if gigabytes else None,
            "totalSecondsPerSourceGb": round(args.elapsed / gigabytes) if gigabytes else None,
            "tilesPerSourceGb": round(tiles / gigabytes) if gigabytes else None,
            "peakMemoryPerSourceGb": round(peak_memory / gigabytes / 1e9, 2)
                                     if gigabytes and peak_memory else None,
            # The candidates for "what actually explains peak memory", each expressed per million
            # of its unit so the numbers are comparable at a glance across rungs.
            "peakMemoryPerMillionNodes": round(
                peak_memory / (counted["osmNodesInRoutableWays"] / 1e6) / 1e9, 3)
                if peak_memory and counted.get("osmNodesInRoutableWays") else None,
            "peakMemoryPerMillionDirectedEdges": round(
                peak_memory / (counted["directedEdges"] / 1e6) / 1e9, 3)
                if peak_memory and counted.get("directedEdges") else None,
            "tileSecondsPerMillionDirectedEdges": round(
                tile_seconds / (counted["directedEdges"] / 1e6), 1)
                if tile_seconds and counted.get("directedEdges") else None,
        },
    }

    print(json.dumps(report, indent=2))

    if missing:
        # A GitHub annotation, so it is visible on the run rather than buried in a log. Production
        # carries on; the scale probe turns this same condition into a failure.
        print(f"::warning::telemetry INVALID — missing {', '.join(missing)}", file=sys.stderr)

    # A second copy on stderr, formatted for a human reading the log rather than the artifact.
    def hours(seconds):
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m" if seconds >= 3600 \
            else f"{seconds // 60}m{seconds % 60:02d}s"

    print(f"\n   {args.members} countries, {report['source']['gb']} GB of source", file=sys.stderr)
    for name in ("download", "merge", "config", "admins", "tiles", "cut", "archives"):
        if name in timings:
            print(f"      {name:10s} {hours(timings[name])}", file=sys.stderr)
    print(f"      {'TOTAL':10s} {hours(args.elapsed)}", file=sys.stderr)
    if peak_memory:
        print(f"   peak memory {report['peak']['memoryGb']} GB · "
              f"peak disk {report['peak']['diskGb']} GB", file=sys.stderr)
    if report["rates"]["tileSecondsPerSourceGb"]:
        print(f"   {report['rates']['tileSecondsPerSourceGb']} s/GB building tiles, "
              f"{report['rates']['totalSecondsPerSourceGb']} s/GB for the whole job",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
