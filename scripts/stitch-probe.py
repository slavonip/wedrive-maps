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
import geometry  # noqa: E402 — one definition of the continuity check, shared with the gate
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
    shape = ""
    for leg in answer["trip"].get("legs") or []:
        shape += leg.get("shape") or ""
    return {"km": km, "seconds": seconds, "shape": shape,
            "kmh": (km / (seconds / 3600)) if seconds else None}


# `decode_polyline6`, `GEOMETRY_GAP_KM` and `largest_gap_km` MOVED to geometry.py on
# 2026-09-18. They lived here, in an experiment, while the gate that decides what ships
# had no continuity check at all — so the one measurement that caught a 294.89 km
# teleport could never have caught it on the production path.
decode_polyline6 = geometry.decode_polyline6
largest_gap_km = geometry.largest_gap_km
GEOMETRY_GAP_KM = geometry.GEOMETRY_GAP_KM


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cuts_a")
    parser.add_argument("cuts_b")
    parser.add_argument("--seam", nargs=2, required=True, metavar=("LEFT", "RIGHT"))
    parser.add_argument("--across", nargs=4, type=float, required=True,
                        metavar=("LAT1", "LON1", "LAT2", "LON2"))
    parser.add_argument("--expect-km", type=float, required=True,
                        help="the same route measured inside ONE master")
    parser.add_argument("--inside-a", nargs=5, type=float, required=True,
                        metavar=("LAT1", "LON1", "LAT2", "LON2", "KM"),
                        help="a cross-border route WITHIN master A, and its known length")
    parser.add_argument("--inside-b", nargs=5, type=float, required=True,
                        metavar=("LAT1", "LON1", "LAT2", "LON2", "KM"))
    args = parser.parse_args()

    a_root, b_root = pathlib.Path(args.cuts_a), pathlib.Path(args.cuts_b)

    print("=== 1. the seam tiles themselves ===")
    seam = compare_seam(a_root, b_root, args.seam)

    # ── 2. CONTROLS FIRST, because a result from a broken apparatus is worse than no result ──
    # Each control is a route that CROSSES A FRONTIER INSIDE ITS OWN MASTER, with a length already
    # measured on eu-core. If either fails, nothing below means anything — and the first version
    # of this probe failed both by asking master A to route from a country it does not contain.
    print("\n=== 2. controls: each master crossing its OWN internal frontier ===")
    controls_ok = True
    for label, root, spec in (("master A", a_root, args.inside_a),
                              ("master B", b_root, args.inside_b)):
        lat1, lon1, lat2, lon2, known = spec
        # A CUTS DIRECTORY IS NOT A TILE DIRECTORY. `cuts_stitch-a` holds `AT/0/003/107.gph` and
        # `HU/...`, while Valhalla wants `0/003/107.gph` at the root — so pointing a config at
        # the cuts root gives a graph with no tiles and no error, and every route refuses.
        #
        # The assembled case worked precisely because it goes through `assemble()`, which
        # flattens the country prefixes. The controls did not, and "NO ROUTE" from a directory
        # Valhalla cannot read looked exactly like "this master is broken" — for the third time
        # today a control reported on my own mistake instead of on its subject.
        flat = pathlib.Path(f"/data/flat_{label.replace(' ', '_')}")
        assemble(sorted(root.iterdir()), flat)
        alone = config_for(str(flat), f"/tmp/{label.replace(' ', '_')}.json")
        got = route_km(alone, [lat1, lon1], [lat2, lon2])
        if got is None:
            print(f"   {label}: NO ROUTE — the apparatus is broken, not the seam")
            controls_ok = False
        else:
            drift = abs(got - known) / known * 100
            ok = drift < 5
            controls_ok &= ok
            print(f"   {label}: {got:.1f} km against {known:.1f} known "
                  f"({drift:.1f}% {'ok' if ok else 'OFF — investigate before reading on'})")

    print("\n=== 3. assembled as a car would, from two different masters ===")
    both = pathlib.Path("/data/stitched")
    assemble(sorted(a_root.iterdir()) + sorted(b_root.iterdir()), both)
    print(f"   {len(tiles_of(both))} tiles in one directory")

    config = config_for(str(both), "/tmp/stitched.json")
    lat1, lon1, lat2, lon2 = args.across
    detail = route_detail(config, [lat1, lon1], [lat2, lon2])

    print("\n=== 4. the route that has to cross the seam ===")
    verdict, gap = "SEAM BROKEN", None
    if detail is None:
        print("   NO ROUTE across the seam — the two masters do not join at all")
    else:
        speed = f"{detail['kmh']:.0f} km/h" if detail["kmh"] else "no duration"
        print(f"   {detail['km']:.1f} km in {detail['seconds'] / 3600:.2f} h -> {speed}")
        print(f"   inside one master the same journey is {args.expect_km:.1f} km")

        points = decode_polyline6(detail["shape"]) if detail["shape"] else []
        if points:
            gap, where = largest_gap_km(points)
            print(f"   geometry: {len(points)} points, largest gap between consecutive "
                  f"points {gap:.2f} km")
            if where and gap > GEOMETRY_GAP_KM:
                (la1, lo1), (la2, lo2) = where
                print(f"      the jump is {la1:.4f},{lo1:.4f} -> {la2:.4f},{lo2:.4f}")

        # THE THREE OUTCOMES, distinguished by evidence rather than by distance alone.
        if gap is not None and gap > GEOMETRY_GAP_KM:
            print("   -> THE GEOMETRY IS NOT CONTINUOUS. The router traversed an edge whose ends")
            print("      are not joined on the ground — a synthetic connection from a mismatched")
            print("      tile. This is WORSE than a refusal: a car would follow it.")
            verdict = "SEAM CORRUPT"
        elif detail["kmh"] and detail["kmh"] > 130:
            print("   -> AN IMPLIED SPEED THIS HIGH IS NOT DRIVING.")
            verdict = "SEAM CORRUPT"
        elif detail["km"] < args.expect_km * 0.9:
            print("   -> SHORTER than the true route, with continuous geometry and a plausible")
            print("      speed. That should not be possible and needs explaining before anything")
            print("      here is trusted.")
            verdict = "SEAM SUSPECT"
        elif detail["km"] > args.expect_km * 1.15:
            print(f"   -> a detour of {detail['km'] - args.expect_km:+.1f} km around the seam")
            verdict = "SEAM DETOURS"
        else:
            verdict = "SEAM HOLDS"

    if not controls_ok:
        verdict = f"INCONCLUSIVE (controls failed) — was: {verdict}"

    print(f"\nVERDICT: {verdict}")
    print(json.dumps({"verdict": verdict, "seamSharedTiles": seam["shared"],
                      "seamIdenticalTiles": seam["identical"],
                      "acrossKm": detail["km"] if detail else None,
                      "acrossKmh": detail["kmh"] if detail else None,
                      "largestGeometryGapKm": gap, "controlsOk": controls_ok}, indent=2))
    # Reports; never gates. An experiment about what IS, not a check on what should be.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
