"""Predict a rung BEFORE it runs, then compare the prediction with what happened.

    usage: predict-scale.py [--source-gb 3.6] [--label "rung 7"]
           predict-scale.py --compare measurements/rung-7.json

An extrapolation nobody wrote down in advance is not a prediction, it is a story told afterwards
about whatever number arrived. So this states the models, states what they expect, and is
committed before the run — which makes a WRONG prediction as useful as a right one, because it
says the model is missing a phase rather than that the runner is mysterious.

WHAT THE FIRST TWO RUNGS ESTABLISHED (measured, cgroup-v2, anon not `memory.current`):

    rung 2   5,458,140 directed edges   2.21 GB anon   111 s tiles    3.25 GB disk
    rung 4  18,836,908 directed edges   4.69 GB anon   420 s tiles   10.86 GB disk

Three of the four ratios hold across a 3.45x jump and one does not:

    tile seconds / M edges    20.3 -> 22.3    10% — LINEAR, a good predictor
    disk GB / M edges         0.60 -> 0.58     3% — LINEAR, an excellent predictor
    M edges / GB of source    12.7 -> 12.1     5% — so source size predicts edge count
    anon GB / M edges         0.41 -> 0.25    39% — SUB-LINEAR, and that is the whole question

**Anon falling per edge is the favourable direction**, and two points cannot say how far it
continues. Two models fit these points about equally and disagree exactly where the decision is:

    LINEAR+FIXED   anon = 1.20 GB + 0.185 GB per million edges
                   a fixed cost plus a proportional one — the conservative reading

    POWER          anon = 2.21 GB * (edges / 5.46M) ^ 0.607
                   growth that keeps slowing — the optimistic reading

They agree that rung 7 fits comfortably. They disagree about rung 10 ACROSS THE RUNNER'S LIMIT —
20.4 GB against 13.3 GB on a 15.6 GB machine — which is what makes rung 10 the experiment worth
paying for rather than one more point on a curve.
"""
import argparse
import json
import pathlib
import sys

RUNNER_RAM_GB = 15.6
RUNNER_DISK_FREE_GB = 86.0
JOB_LIMIT_SECONDS = 6 * 3600

# Measured, not assumed. Both from cgroup-v2 `anon`, which is the figure that predicts an OOM;
# `memory.current` for the same runs was 4.80 and 12.42 GB and counts reclaimable page cache.
POINTS = [
    {"label": "rung 2", "edges": 5_458_140, "anon": 2.21, "tileSeconds": 111,
     "diskDelta": 3.25, "sourceGb": 0.43, "totalSeconds": 180},
    {"label": "rung 4", "edges": 18_836_908, "anon": 4.69, "tileSeconds": 420,
     "diskDelta": 10.86, "sourceGb": 1.56, "totalSeconds": 630},
]

EDGES_PER_SOURCE_GB = 12.4e6      # 12.7 and 12.1 measured; the mean
TILE_SECONDS_PER_M_EDGE = 21.3    # 20.3 and 22.3
DISK_GB_PER_M_EDGE = 0.586        # 0.595 and 0.577


def anon_linear(edges_m: float) -> float:
    """A fixed cost plus a proportional one, fitted through both points."""
    return 1.198 + 0.1854 * edges_m


def anon_power(edges_m: float) -> float:
    """Growth that keeps slowing, fitted through both points."""
    return 2.21 * (edges_m / 5.458) ** 0.607


def predict(source_gb: float) -> dict:
    edges_m = source_gb * EDGES_PER_SOURCE_GB / 1e6
    tiles = TILE_SECONDS_PER_M_EDGE * edges_m
    # The other stages, from the same two runs: download is by far the largest of them and scales
    # with bytes rather than with graph complexity.
    download = 55 * source_gb          # 30 s at 0.43 GB, 83 s at 1.56 GB
    other = 0.35 * tiles               # merge + admins + cut + archives, both runs
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
