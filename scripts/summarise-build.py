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
    """Peak memory, peak disk, the disk BASELINE and where the memory figure came from.

    Disk is sampled filesystem-wide, and a runner starts with ~59 GB already used — so a raw
    peak of 66.6 GB on a two-country build says almost nothing about what the build needs. The
    first sample is the baseline and the growth above it is this build's own footprint, which is
    the number that answers "will a bigger master fit".

    The memory source matters for the same reason in the other direction: a cgroup reading is
    this container, while the /proc/meminfo fallback is the whole machine.
    """
    memory = anon = disk = baseline = 0
    samples = 0
    source = "unknown"
    try:
        lines = open(path, newline="").read().splitlines()
        if lines and lines[0].startswith("#"):
            source = lines[0].split(":", 1)[-1].strip()
            lines = lines[1:]
        for row in csv.DictReader(lines):
            if not row.get("epoch"):
                continue
            samples += 1
            memory = max(memory, int(row["mem_bytes"] or 0))
            # `anon_bytes` is absent from samples taken before 2026-09-17; a missing column is
            # not zero, and 0 here simply means "this build did not measure it".
            anon = max(anon, int(row.get("anon_bytes") or 0))
            used = int(row["disk_kb"] or 0) * 1024
            if baseline == 0:
                baseline = used
            disk = max(disk, used)
    except FileNotFoundError:
        return None, None, 0, 0, source, 0
    return memory, disk, samples, baseline, source, anon


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
    peak_memory, peak_disk, samples, disk_baseline, memory_source, peak_anon = peaks(args.samples)
    disk_growth = (peak_disk - disk_baseline) if peak_disk else None
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
        ("peakAnon", peak_anon or None),
        ("peakDisk", peak_disk or None),
        ("buildSeconds", args.elapsed or None),
        ("tileSeconds", tile_seconds or None),
    ) if not value]

    # ── WHAT EACH NUMBER IS, stored beside the number ────────────────────────────────────────
    # A figure without its provenance decays into folklore. "12.83 GB" read in six months is
    # almost useless; "12.83 GB · memory.current · cgroup-v2 · includes reclaimable file cache"
    # can still be interpreted correctly after the instruments have changed underneath it — and
    # can be recognised as NOT comparable with a later figure measured differently.
    #
    # This is not decoration. Two conclusions in this project were drawn from numbers that
    # measured something other than they claimed, and both were quoted onward before anyone
    # checked. Provenance is what makes that catchable by reading rather than by re-running.
    cgroup = {"cgroup2": "cgroup-v2", "cgroup1": "cgroup-v1"}.get(memory_source, memory_source)
    provenance = {
        "memoryGb": f"memory.current · {cgroup} · anon + file cache + kernel · "
                    f"OVERSTATES what must fit in RAM",
        "anonGb": f"memory.stat anon · {cgroup} · not reclaimable · THE OOM PREDICTOR",
        "cacheGb": "memoryGb minus anonGb · reclaimable · released under pressure",
        "diskPeakAbsoluteGb": "df --output=used · FILESYSTEM-WIDE, not this build",
        "diskPeakDeltaGb": "absolute minus the first sample · this build's own footprint",
        "diskBaselineGb": "the first sample · what the runner already held",
        "seconds": "wall time per stage, bounded by the build script itself",
        "graph counters": "parsed from valhalla_build_tiles' own log lines",
        "sampledEvery": "5 seconds",
        "engine": args.engine or "unknown",
    }

    report = {
        "telemetry": "VALID" if not missing else "INVALID",
        "provenance": provenance,
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
            # `memory.current` — anon + PAGE CACHE + kernel. Valhalla writes gigabytes of
            # temporary sequence files and every byte of them lands in cache and counts here,
            # so this overstates what the build needs, sometimes greatly.
            "memoryBytes": peak_memory,
            "memoryGb": round(peak_memory / 1e9, 2) if peak_memory else None,
            # THE NUMBER THAT PREDICTS AN OOM. Anonymous memory cannot be reclaimed; page cache
            # is released under pressure instead of killing the process. "Does a bigger master
            # fit in this runner" is a question about THIS figure, not the one above.
            "anonBytes": peak_anon or None,
            "anonGb": round(peak_anon / 1e9, 2) if peak_anon else None,
            "cacheGb": round((peak_memory - peak_anon) / 1e9, 2)
                       if peak_memory and peak_anon else None,
            # WHERE the memory figure came from, because a cgroup reading is this container and
            # the meminfo fallback is the whole machine. Without it the number is unusable.
            "memorySource": memory_source,
            # THREE DISK NUMBERS, because they answer three different questions and one of them
            # alone is misleading:
            #
            #   diskPeakDeltaGb     what OUR factory needs — the number that scales with a master
            #   diskPeakAbsoluteGb  whether a given runner survives this build at all
            #   diskBaselineGb      why two identical builds can report different absolutes
            #
            # Read alone, the absolute suggested a two-country build needs 66 GB. It does not;
            # it needs 7.8 GB on a volume that already had 60 GB in it.
            "diskPeakDeltaBytes": disk_growth,
            "diskPeakDeltaGb": round(disk_growth / 1e9, 2) if disk_growth else None,
            "diskPeakAbsoluteBytes": peak_disk,
            "diskPeakAbsoluteGb": round(peak_disk / 1e9, 2) if peak_disk else None,
            "diskBaselineGb": round(disk_baseline / 1e9, 2) if disk_baseline else None,
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
            # The same slope against the figure that actually binds.
            "anonPerMillionDirectedEdges": round(
                peak_anon / (counted["directedEdges"] / 1e6) / 1e9, 3)
                if peak_anon and counted.get("directedEdges") else None,
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
        if report["peak"]["anonGb"]:
            print(f"   peak anon   {report['peak']['anonGb']} GB  <- what must fit in RAM "
                  f"({report['peak']['cacheGb']} GB of the total was reclaimable cache)",
                  file=sys.stderr)
        print(f"   peak memory {report['peak']['memoryGb']} GB (from {memory_source}) · "
              f"disk +{report['peak']['diskPeakDeltaGb']} GB ours, "
              f"{report['peak']['diskPeakAbsoluteGb']} GB absolute "
              f"over a {report['peak']['diskBaselineGb']} GB baseline", file=sys.stderr)
    if report["rates"]["tileSecondsPerSourceGb"]:
        print(f"   {report['rates']['tileSecondsPerSourceGb']} s/GB building tiles, "
              f"{report['rates']['totalSecondsPerSourceGb']} s/GB for the whole job",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
