"""The peak a build reached, read straight from the samples — INCLUDING a build that was killed.

    usage: peak-from-samples.py <samples.csv> [--label "rung 10"]

`summarise-build.py` writes its report at the end of a successful build, which is exactly the
build whose limits we already know. The interesting rung is the one that dies: an OOM kill leaves
no report, no archive and no manifest, and presents as a crash rather than as a limit.

The sampler runs under an EXIT trap, so its CSV survives the abort. This reads what is there and
says how far the build got, which turns "it crashed" into "it wanted 18.2 GB after 41 minutes,
during tile building" — a measurement rather than a mystery.
"""
import argparse
import csv
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("samples")
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    try:
        rows = [row for row in csv.DictReader(open(args.samples, newline="")) if row.get("epoch")]
    except FileNotFoundError:
        print(f"TELEMETRY INVALID: no samples at {args.samples} — the sampler never started",
              file=sys.stderr)
        return 1
    if not rows:
        print("TELEMETRY INVALID: the sampler produced no rows", file=sys.stderr)
        return 1

    memory = [(int(r["mem_bytes"] or 0), int(r["epoch"])) for r in rows]
    disk = [(int(r["disk_kb"] or 0) * 1024, int(r["epoch"])) for r in rows]
    started = int(rows[0]["epoch"])
    peak_memory, at_memory = max(memory)
    peak_disk, at_disk = max(disk)
    span = int(rows[-1]["epoch"]) - started

    def when(epoch):
        seconds = epoch - started
        return f"{seconds // 60}m{seconds % 60:02d}s in"

    head = f"{args.label}: " if args.label else ""
    print(f"{head}sampled for {span // 60}m{span % 60:02d}s over {len(rows)} samples")
    print(f"   peak memory {peak_memory / 1e9:6.2f} GB  ({when(at_memory)})")
    print(f"   peak disk   {peak_disk / 1e9:6.2f} GB  ({when(at_disk)})")

    # Where the memory was heading when sampling stopped separates "finished" from "killed":
    # a build that completed releases everything, a build that was killed was still climbing.
    tail = [m for m, _ in memory[-6:]]
    if len(tail) >= 2:
        trend = "still climbing" if tail[-1] >= max(tail[:-1]) else "settled"
        print(f"   at the end   {tail[-1] / 1e9:6.2f} GB  ({trend})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
