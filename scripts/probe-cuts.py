"""Prove the CUTS behave — each on its own, and assembled. The gate for the region pipeline.

    usage: probe-cuts.py <cuts-dir> <plans-dir>

Two questions no single-package gate can ask, and both decide whether per-country downloads are
honest:

  1. **Does a country's cut work ALONE?** Its own one-way, its own bridge, its own timezone — a
     car that has installed one country must get correct answers inside it.
  2. **Do the cuts REASSEMBLE?** Copied into one directory, do the frontier routes come back at
     the distance the whole build gave? A shared border tile is the SAME FILE in both cuts, which
     is the entire difference from merging two separately-built countries (35 shared paths, 0
     byte-identical, measured 2026-09-17).

And one the whole build cannot ask at all: **does a lone cut correctly REFUSE to leave its
country?** Verified by hand before this existed — Moldova's cut alone answers NO ROUTE to
Bucharest rather than following the hierarchy into missing tiles and inventing something
plausible. That refusal is what makes a partially installed car safe.

Exit code is the gate.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe as package_probe  # noqa: E402  — the per-probe runners, shared with the packages


def config_for(tile_dir: str, path: str) -> str:
    """A Valhalla config reading a DIRECTORY. The extract key is popped, never blanked: left as
    an empty string, Valhalla looks for a file called "" and yields a graph with no tiles and no
    error (measured 2026-09-17)."""
    subprocess.run(["valhalla_build_config", "--mjolnir-tile-dir", tile_dir],
                   stdout=open(path, "w"), check=True)
    config = json.load(open(path))
    config["mjolnir"].pop("tile_extract", None)
    json.dump(config, open(path, "w"), indent=2)
    return path


def run(config: str, plan: dict, report: package_probe.Report, only: set | None = None) -> None:
    for probe in plan["probes"]:
        if only is not None and probe["kind"] not in only:
            continue
        runner = package_probe.PROBES.get(probe["kind"])
        if runner is None:
            report.fail(probe.get("name", "?"), f"unknown probe kind '{probe['kind']}'")
            continue
        runner(config, probe, report)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    cuts = pathlib.Path(sys.argv[1])
    plans = pathlib.Path(sys.argv[2])

    report = package_probe.Report()
    assembled_plan = json.load(open(plans / "_assembled.json", encoding="utf-8"))
    members = assembled_plan["countries"]

    # ── 1. each cut on its own ──────────────────────────────────────────────────────────────
    for code in members:
        print(f"== {code}, alone ==")
        cut = cuts / code
        if not cut.is_dir():
            report.fail(f"{code}: cut exists", "no tiles were cut for it")
            continue
        config = config_for(str(cut), f"/tmp/conf_{code}.json")
        # Border probes are skipped deliberately: a lone cut MUST NOT reach the next country,
        # and asserting that here would need the destination's tiles, which is the opposite
        # arrangement. The assembled pass below is where frontiers are tested.
        run(config, json.load(open(plans / f"{code}.json", encoding="utf-8")), report)
        print()

    # ── 2. all of them together, which is what a car with several countries actually holds ──
    print(f"== {'+'.join(members)}, assembled ==")
    both = pathlib.Path("/tmp/assembled")
    shutil.rmtree(both, ignore_errors=True)
    both.mkdir(parents=True)
    for code in members:
        cut = cuts / code
        if cut.is_dir():
            # Frontier tiles are written more than once with identical bytes, because they came
            # out of ONE build. That is the property the whole design rests on.
            shutil.copytree(cut, both, dirs_exist_ok=True)
    config = config_for(str(both), "/tmp/conf_assembled.json")
    run(config, assembled_plan, report)

    verdict = "PROMOTABLE" if report.failed == 0 else f"BLOCKED ({report.failed} failed)"
    print()
    print(f"-- {assembled_plan['package']}: {verdict}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
