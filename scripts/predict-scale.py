"""Predict a rung BEFORE it runs, then compare the prediction with what happened.

    usage: predict-scale.py [--source-gb 11] [--label "east master"]
           predict-scale.py --compare measurements/rung-10.json

An extrapolation nobody wrote down in advance is not a prediction, it is a story told afterwards
about whatever number arrived. So the models are committed before the run — which makes a WRONG
prediction as useful as a right one, and all four of them were wrong in instructive ways.

FOUR MEASURED RUNGS (anon from cgroup-v2, never `memory.current`):

    rung       M edges   anon GB   tile s/M   disk GB/M   source GB
    2              5.5      2.21       20.3       0.595        0.43
    4             18.8      4.69       22.3       0.577        1.56
    7             56.9      5.22       21.5       0.542        4.95
    10           126.2      8.81       32.3       0.531       10.65

WHAT HELD AND WHAT DID NOT.

**Disk is a metronome.** 0.595 -> 0.531 GB per million directed edges across a 23x range, with a
marginal of 0.523 between the last two rungs. It is the one quantity that extrapolates without
argument, and it is the one that binds.

**Anon stayed flat and both models were badly wrong about it, twice.** Predicting rung 7 they
overshot by 76% and 125%; predicting rung 10, by 69% and 179%. The truth is that Valhalla's build
is disk-backed and its resident working set barely grows — 8.81 GB at 126M edges, 56% of a 15.6 GB
runner. `memory.current` meanwhile reached 15.97 and 16.14 GB, AT OR ABOVE the machine's total,
and neither build was killed: the kernel reclaimed cache exactly as it should. Sizing a runner by
`memory.current` would have split the factory into three for no reason.

**Tile time broke linearity at rung 10, and that is the new finding.** It had been 20-22 s per
million edges across three rungs; between rungs 7 and 10 the marginal cost is 41.2 s/M, and the
prediction missed by -34%. A plausible mechanism, untested: at rung 10 `memory.current` sat at the
machine's ceiling for the whole tile stage, so the kernel was evicting page cache continuously and
the build went from cache-served to disk-served. If that is right, a runner with more RAM would
build FASTER without needing more RAM to succeed — an argument for a bigger machine that is about
memory but shows up as time. Until it is tested, the pessimistic 41 s/M is used for anything above
100M edges.

CONCLUSION, from the measurements rather than from caution:

    the binding constraint is DISK, then TIME, and RAM does not bind at all
    a standard runner holds a master of about 11 GB of source (134M edges) with a 15% margin
    rung 10, at 10.65 GB and 67 GB of disk, IS essentially that ceiling
    Europe (32.6 GB, ~388M edges) needs THREE such masters before overlap, four with it

Europe in one pass is not a near miss to be optimised into: it wants 203 GB of scratch against 82
available. That experiment is not worth running.
"""
import argparse
import json
import pathlib
import sys

RUNNER_RAM_GB = 15.6
RUNNER_DISK_TOTAL_GB = 145.0
RUNNER_BASELINE_GB = 63.34        # what a runner already holds after `reclaim disk`, measured
RUNNER_DISK_FREE_GB = RUNNER_DISK_TOTAL_GB - RUNNER_BASELINE_GB   # 81.7
JOB_LIMIT_SECONDS = 6 * 3600

# Measured, not assumed. Both from cgroup-v2 `anon`, which is the figure that predicts an OOM;
# `memory.current` for the same runs was 4.80 and 12.42 GB and counts reclaimable page cache.
POINTS = [
    {"label": "rung 2", "edges": 5_458_140, "anon": 2.21, "tileSeconds": 111,
     "diskDelta": 3.25, "sourceGb": 0.43, "totalSeconds": 180},
    {"label": "rung 4", "edges": 18_836_908, "anon": 4.69, "tileSeconds": 420,
     "diskDelta": 10.86, "sourceGb": 1.56, "totalSeconds": 630},
    {"label": "rung 7", "edges": 56_857_708, "anon": 5.22, "tileSeconds": 1221,
     "diskDelta": 30.83, "sourceGb": 4.95, "totalSeconds": 1977},
    {"label": "rung 10", "edges": 126_222_238, "anon": 8.81, "tileSeconds": 4081,
     "diskDelta": 67.08, "sourceGb": 10.65, "totalSeconds": 5288},
]

EDGES_PER_SOURCE_GB = 11.9e6      # 12.7, 12.1, 11.5, 11.9 — settles near 11.9
TILE_SECONDS_PER_M_EDGE = 41.2    # the MARGINAL cost above 100M edges, not the mean
DISK_GB_PER_M_EDGE = 0.523        # marginal between rungs 7 and 10


def anon_linear(edges_m: float) -> float:
    """Anchored at rung 10 with the marginal slope measured between rungs 7 and 10. The earlier
    two-point fits overshot by 69-179% and are not kept: a model that wrong twice running is not
    a conservative estimate, it is a wrong one."""
    return 8.81 + 0.052 * (edges_m - 126.222)


def anon_power(edges_m: float) -> float:
    """Kept only so `--compare` can keep scoring the old optimistic curve against new points."""
    return 2.21 * (edges_m / 5.458) ** 0.607


def predict(source_gb: float) -> dict:
    edges_m = source_gb * EDGES_PER_SOURCE_GB / 1e6
    tiles = TILE_SECONDS_PER_M_EDGE * edges_m
    # The other stages, from the same two runs: download is by far the largest of them and scales
    # with bytes rather than with graph complexity.
    # Fitted on rungs 7 and 10, where these stages are large enough to measure: download varies
    # with the network rather than the graph (75 and 31 s/GB — the mean is used and it is noisy),
    # while merge and admins scale with bytes.
    download = 50 * source_gb
    other = (45 + 23) * source_gb      # merge + admins; cut and archives are minutes at most
    return {
        "sourceGb": round(source_gb, 2),
        "directedEdgesM": round(edges_m, 1),
        "anonLinearGb": round(anon_linear(edges_m), 2),
        "anonPowerGb": round(anon_power(edges_m), 2),
        "tileSeconds": round(tiles),
        "totalSeconds": round(tiles + download + other),
        "diskDeltaGb": round(DISK_GB_PER_M_EDGE * edges_m, 1),
    }


def verdict(p: dict) -> list:
    out = []
    worst, best = p["anonLinearGb"], p["anonPowerGb"]
    if worst < RUNNER_RAM_GB and best < RUNNER_RAM_GB:
        out.append(f"RAM: fits under both models ({best}–{worst} GB of {RUNNER_RAM_GB})")
    elif worst >= RUNNER_RAM_GB > best:
        out.append(f"RAM: THE MODELS DISAGREE ACROSS THE LIMIT ({best} vs {worst} GB of "
                   f"{RUNNER_RAM_GB}) — this rung discriminates between them")
    else:
        out.append(f"RAM: exceeds the runner under both models ({best}–{worst} GB of "
                   f"{RUNNER_RAM_GB})")
    out.append(("disk: fits" if p["diskDeltaGb"] < RUNNER_DISK_FREE_GB else "DISK: exceeds")
               + f" ({p['diskDeltaGb']} GB of {RUNNER_DISK_FREE_GB} free)")
    hours = p["totalSeconds"] / 3600
    out.append(("time: fits" if p["totalSeconds"] < JOB_LIMIT_SECONDS else "TIME: exceeds")
               + f" ({hours:.1f} h of 6)")
    return out


def compare(path: str) -> int:
    report = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    actual_edges = report["graph"].get("directedEdges")
    actual_anon = report["peak"].get("anonGb")
    if not actual_edges or not actual_anon:
        print(f"{path} carries no directedEdges/anonGb — nothing to compare", file=sys.stderr)
        return 1
    edges_m = actual_edges / 1e6
    print(f"   measured   {edges_m:8.1f}M edges   {actual_anon:6.2f} GB anon   "
          f"{report['seconds'].get('tiles', 0):5d} s tiles")
    for name, fn in (("linear+fixed", anon_linear), ("power", anon_power)):
        want = fn(edges_m)
        err = (want - actual_anon) / actual_anon * 100
        print(f"   {name:12s} predicted {want:6.2f} GB   off by {err:+5.0f}%")
    want_tiles = TILE_SECONDS_PER_M_EDGE * edges_m
    got_tiles = report["seconds"].get("tiles", 0)
    if got_tiles:
        print(f"   tile time    predicted {want_tiles:6.0f} s    off by "
              f"{(want_tiles - got_tiles) / got_tiles * 100:+5.0f}%")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-gb", type=float, default=None)
    parser.add_argument("--label", default="")
    parser.add_argument("--compare", default=None)
    args = parser.parse_args()

    if args.compare:
        return compare(args.compare)

    targets = ([(args.label or f"{args.source_gb} GB", args.source_gb)] if args.source_gb
               else [("rung 7", 3.6), ("rung 10", 8.4), ("Europe", 32.6)])
    for label, gb in targets:
        p = predict(gb)
        print(f"\n=== {label} — predicted before the run ===")
        print(f"   {p['sourceGb']} GB source -> {p['directedEdgesM']}M directed edges")
        print(f"   anon      {p['anonPowerGb']}–{p['anonLinearGb']} GB   (power / linear+fixed)")
        print(f"   tiles     {p['tileSeconds']} s      total {p['totalSeconds'] / 60:.0f} min")
        print(f"   disk      +{p['diskDeltaGb']} GB")
        for line in verdict(p):
            print(f"   {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
