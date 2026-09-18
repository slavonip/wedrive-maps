"""Can a car drive across a seam between two SEPARATE masters?

    usage: stitch-probe.py <cutsA> <cutsB> --seam HU RO --plan <plans-dir>

THE LAST FUNDAMENTAL QUESTION IN THE ROUTING ARCHITECTURE. Everything proven so far is proven
INSIDE one master: build once over several countries, cut along national boundaries, and a
frontier tile is the same file in both packages — 86 of 86 byte-identical across four countries.
That is what lets a car install and remove countries independently and still route between them.

But a standard runner holds a master of about 11 GB of source, and Europe is 32.6 GB. So Europe
needs three or four masters, and the moment two neighbouring countries come from DIFFERENT
masters, the property they rely on is no longer guaranteed by construction — it is exactly the
situation the whole design was created to escape:

    within a master   the frontier tile is ONE file, built once, appearing in two packages
    across a seam     each master built its own version of that cell, from different inputs

**What this predicts, and what is therefore worth measuring rather than assuming.** A Valhalla
tile is a cell of a global grid. Master A (AT+HU) builds the cell straddling the Hungarian border
from Hungarian data only; master B (RO+MD) builds the same cell from Romanian data only. Neither
is wrong; they are different graphs of the same square, and `GraphId` indices are assigned per
build, so an edge in one refers to a node in the other by a number that means nothing there.
Unpacking both gives whichever arrived last, and the roads of the other country in that cell are
simply absent.

So the expected failure is NOT a crash and NOT a wrong distance. It is a route that refuses, or
one that detours absurdly, precisely at the seam — while both halves route perfectly inside
themselves. That is the hardest kind of defect to notice in production and the easiest to
measure deliberately, which is why this exists.

WHAT IT REPORTS, in order of what it settles:

  1. how many tile paths the two sides share at all, and how many are byte-identical
     (inside one master this was 86 of 86; across a seam it is expected to be near zero)
  2. whether a route crossing the seam succeeds, and at what distance
  3. the same route inside each master, as a control — if those fail too, the experiment is
     broken rather than the seam
"""
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe as package_probe  # noqa: E402 — the route helpers, shared with the gate


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tiles_of(root: pathlib.Path) -> dict:
    return {str(p.relative_to(root)).replace("\\", "/"): p for p in root.rglob("*.gph")}


def config_for(tile_dir: str, path: str) -> str:
    subprocess.run(["valhalla_build_config", "--mjolnir-tile-dir", tile_dir],
                   stdout=open(path, "w"), check=True)
    config = json.load(open(path))
    # Popped, never blanked: an empty string sends Valhalla looking for a file called "".
    config["mjolnir"].pop("tile_extract", None)
    json.dump(config, open(path, "w"), indent=2)
    return path


def compare_seam(a_root: pathlib.Path, b_root: pathlib.Path, seam: list) -> dict:
    """The two countries either side of the seam, each from its own master."""
    left, right = seam
    a = tiles_of(a_root / left)
    b = tiles_of(b_root / right)
    shared = sorted(set(a) & set(b))
    identical = [name for name in shared if digest(a[name]) == digest(b[name])]

    print(f"   {left} (master A): {len(a)} tiles")
    print(f"   {right} (master B): {len(b)} tiles")
    print(f"   tile paths in both: {len(shared)}")
    print(f"   of those, byte-identical: {len(identical)}")
    if shared and not identical:
        print(f"   -> the same grid cell, built twice from different data. Whichever is unpacked")
        print(f"      second replaces the first, and that country's roads in the cell vanish.")
    for name in shared[:4]:
        same = "identical" if name in identical else "DIFFERS"
        print(f"      {name}: {a[name].stat().st_size:>10,} vs {b[name].stat().st_size:>10,}  {same}")
    return {"shared": len(shared), "identical": len(identical), "paths": shared}


def assemble(sources: list, target: pathlib.Path) -> None:
    """Exactly what a car does: unpack each country package into one directory, in order.

    `copyfile`, never `copy2`: preserving metadata calls utime, which fails on a Windows mount
    and has bitten this repo before.
    """
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for source in sources:
        for name, path in tiles_of(source).items():
            out = target / name
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, out)


def route_km(config: str, frm: list, to: list):
    answer = package_probe.route(config, frm, to)
    metres = package_probe.metres(answer)
    return (metres / 1000) if metres else None


def route_detail(config: str, frm: list, to: list):
    """Length, duration, and the average speed those imply.

    THE FIRST RUN CAME BACK SHORTER THAN THE TRUE ROUTE — 937 km against the 1345.7 km the same
    journey takes inside one master — and a detour cannot be shorter. Straight-line Chisinau to
    Vienna is about 900 km, so a 937 km "road route" is barely above the crow's flight.

    The suspicion this measures: level-0 tiles carry the long shortcut edges of the hierarchy,
    and `0/003/110.gph` differs between the two masters. A mismatched tile at that level can
    offer edges joining places that are not joined, and the router will use them without
    complaint. That is far worse than a refusal — it is a plausible route that does not
    physically exist — and the implied speed is what exposes it, because a shortcut across
    nothing costs almost no time.
    """
    answer = package_probe.route(config, frm, to)
    metres = package_probe.metres(answer)
    if not metres:
        return None
    summary = answer["trip"]["summary"]
    seconds = summary.get("time") or 0
    km = metres / 1000
    return {"km": km, "seconds": seconds,
            "kmh": (km / (seconds / 3600)) if seconds else None}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cuts_a")
    parser.add_argument("cuts_b")
    parser.add_argument("--seam", nargs=2, required=True, metavar=("LEFT", "RIGHT"))
    parser.add_argument("--across", nargs=4, type=float, required=True,
                        metavar=("LAT1", "LON1", "LAT2", "LON2"),
                        help="a route that must cross the seam")
    parser.add_argument("--expect-km", type=float, default=None)
    args = parser.parse_args()

    a_root, b_root = pathlib.Path(args.cuts_a), pathlib.Path(args.cuts_b)
    print("=== 1. the seam tiles themselves ===")
    seam = compare_seam(a_root, b_root, args.seam)

    print("\n=== 2. assembled as a car would, from two different masters ===")
    both = pathlib.Path("/data/stitched")
    assemble(sorted(a_root.iterdir()) + sorted(b_root.iterdir()), both)
    print(f"   {len(tiles_of(both))} tiles in one directory")

    config = config_for(str(both), "/tmp/stitched.json")
    lat1, lon1, lat2, lon2 = args.across
    detail = route_detail(config, [lat1, lon1], [lat2, lon2])
    km = detail["km"] if detail else None

    print("\n=== 3. the route that has to cross it ===")
    if detail:
        speed = f"{detail['kmh']:.0f} km/h" if detail["kmh"] else "no duration reported"
        print(f"   {detail['km']:.1f} km in {detail['seconds'] / 3600:.2f} h -> {speed}")
        if detail["kmh"] and detail["kmh"] > 130:
            print("   AN IMPLIED SPEED THIS HIGH IS NOT A ROAD. The router traversed edges that")
            print("      do not correspond to driveable distance — the signature of a mismatched")
            print("      level-0 tile offering shortcuts between places that are not joined.")
    if km is None:
        print(f"   NO ROUTE across the seam — the two masters do not join")
        verdict = "SEAM BROKEN"
    elif args.expect_km and abs(km - args.expect_km) / args.expect_km > 0.15:
        print(f"   {km:.1f} km, but the same route inside one master is {args.expect_km:.1f} km "
              f"— a detour of {km - args.expect_km:+.1f} km around the seam")
        verdict = "SEAM DETOURS"
    else:
        print(f"   {km:.1f} km" + (f" against {args.expect_km:.1f} km inside one master"
                                   if args.expect_km else ""))
        verdict = "SEAM HOLDS"

    print("\n=== 4. controls — each master alone, so a broken experiment is distinguishable ===")
    # EACH CONTROL MUST START INSIDE THE MASTER IT TESTS. The first version used the same start
    # point for both, so master A — which contains no Moldova at all — was asked to route from
    # Chișinău and "failed" exactly as it should have. A broken control reporting a broken master
    # is the one thing a control exists to prevent, and it invalidated the whole first reading.
    for label, root, point in (("master A", a_root, [lat2, lon2]),
                               ("master B", b_root, [lat1, lon1])):
        alone = config_for(str(root), f"/tmp/{label.replace(' ', '_')}.json")
        inside = route_km(alone, point, [point[0] + 0.02, point[1] + 0.02])
        print(f"   {label} from {point[0]:.3f},{point[1]:.3f}: "
              + (f"works ({inside:.1f} km)" if inside
                 else "FAILS — this control should never fail"))

    print(f"\nVERDICT: {verdict}")
    print(json.dumps({"verdict": verdict, "seamSharedTiles": seam["shared"],
                      "seamIdenticalTiles": seam["identical"], "acrossKm": km}, indent=2))
    # Reports; never gates. This is an experiment about what IS, not a check on what should be.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
