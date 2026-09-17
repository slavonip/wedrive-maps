"""Cut ONE build's tiles into per-country sets — the Sygic/HERE model, measured before adoption.

MEASURED 2026-09-17 on a Moldova+Romania build, before any of the surrounding pipeline existed:

    Moldova's cut alone : Chișinău → Bălți 137.9 km; Chișinău → Bucharest NO ROUTE
    both cuts together  : Chișinău → Bucharest 456.5 km — identical to the whole build
    timezones           : Europe/Chisinau and Europe/Bucharest, correct per country
    overlap             : 36 shared tiles, 1.11x the whole build across both cuts

The refusal matters as much as the success: without Romania's tiles the router says NO ROUTE
rather than following the hierarchy into a hole and answering something plausible and wrong.


    usage: cut-by-country.py <tile-dir> <out-dir> <NAME:path/to/country.poly> [...]

The order is the whole point. Building two countries separately and merging afterwards does not
work: measured 2026-09-17, 35 tile paths shared between a Moldova build and a Romania build, 0
byte-identical, and all three of Moldova's level-0 tiles overwritten. Building ONCE and cutting
afterwards should work, because a frontier tile then belongs to both sets as the SAME file.

Valhalla tiles are geographic and their path encodes the id: `<level>/AAA/BBB/CCC.gph` where the
digits joined are the tile id, and the id is `row * columns + column` over a grid that starts at
(-180, -90). Level 0 is 4 degrees, level 1 is 1, level 2 is 0.25. So a tile's bounding box comes
out of its filename with no index to consult.
"""
import io
import pathlib
import shutil
import sys

DEGREES = {0: 4.0, 1: 1.0, 2: 0.25}


def tile_bbox(level: int, tile_id: int) -> tuple:
    size = DEGREES[level]
    columns = int(360 / size)
    row, column = divmod(tile_id, columns)
    west = -180 + column * size
    south = -90 + row * size
    return west, south, west + size, south + size


def tile_id_from(path: pathlib.Path, root: pathlib.Path) -> tuple:
    parts = path.relative_to(root).with_suffix("").parts
    level = int(parts[0])
    return level, int("".join(parts[1:]))


def read_poly(path: str) -> list:
    """Geofabrik's `.poly`: a name, then one or more rings of `lon lat` lines ended by END.

    BOUNDING BOXES ARE NOT ENOUGH and that is not a detail. Cutting by bbox put 100% of the
    build in Romania's set, because Moldova sits entirely inside Romania's bounding rectangle.
    Any two countries where one nests in the other's box would do the same, which is most of
    them once the list grows.
    """
    rings, ring = [], None
    for line in io.open(path, encoding="utf-8"):
        text = line.strip()
        if not text:
            continue
        if text == "END":
            if ring:
                rings.append(ring)
                ring = None
            continue
        parts = text.split()
        if len(parts) == 2:
            try:
                lon, lat = float(parts[0]), float(parts[1])
            except ValueError:
                continue
            if ring is None:
                ring = []
            ring.append((lon, lat))
        else:
            ring = None          # a ring header line
    return rings


def inside(rings: list, lon: float, lat: float) -> bool:
    """Ray casting over every ring. Geofabrik's holes are marked with a leading `!`, which this
    ignores — a hole would only ever make a cut slightly generous, never short."""
    hit = False
    for ring in rings:
        for i in range(len(ring)):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % len(ring)]
            if (y1 > lat) != (y2 > lat):
                x = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
                if x > lon:
                    hit = not hit
    return hit


def touches(rings: list, box: tuple) -> bool:
    """A tile belongs to a country if ANY of a small sample of its points falls inside.

    Corners, centre, and edge midpoints: a tile is 0.25 to 4 degrees across and a country can
    clip a corner of it. Being generous is the safe direction — a tile included unnecessarily
    costs bytes, a tile left out costs a hole in the graph.
    """
    west, south, east, north = box
    cx, cy = (west + east) / 2, (south + north) / 2
    samples = [
        (west, south), (east, south), (west, north), (east, north), (cx, cy),
        (cx, south), (cx, north), (west, cy), (east, cy),
    ]
    if any(inside(rings, x, y) for x, y in samples):
        return True
    # Or if any of the country's own vertices falls inside the tile — catches a country far
    # smaller than the tile, which happens at level 0 where a tile is 4 degrees across.
    return any(west <= x <= east and south <= y <= north
               for ring in rings for x, y in ring)


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__, file=sys.stderr)
        return 2
    root = pathlib.Path(sys.argv[1])
    out = pathlib.Path(sys.argv[2])

    countries = {}
    for spec in sys.argv[3:]:
        name, poly = spec.split(":", 1)
        countries[name] = read_poly(poly)
        print(f"{name}: {len(countries[name])} ring(s), "
              f"{sum(len(r) for r in countries[name])} points")

    tiles = sorted(root.rglob("*.gph"))
    print(f"{len(tiles)} tiles in {root}")

    counts = {name: 0 for name in countries}
    bytes_of = {name: 0 for name in countries}
    shared = 0

    for tile in tiles:
        level, tile_id = tile_id_from(tile, root)
        box = tile_bbox(level, tile_id)
        belongs = [name for name, rings in countries.items() if touches(rings, box)]
        if len(belongs) > 1:
            shared += 1
        for name in belongs:
            target = out / name / tile.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            # copyfile, not copy2: preserving mtime onto a Windows bind mount raises
            # "Operation not permitted" and leaves a partial tree, which cost an hour
            # earlier today in a different script. The bytes are what matter here.
            shutil.copyfile(tile, target)
            counts[name] += 1
            bytes_of[name] += tile.stat().st_size

    total_bytes = sum(t.stat().st_size for t in tiles)
    print()
    for name in countries:
        print(f"  {name:8s} {counts[name]:4d} tiles, {bytes_of[name]:,} bytes "
              f"({bytes_of[name] / total_bytes:5.1%} of the whole build)")
    print(f"  shared by more than one country: {shared} tiles")
    print(f"  whole build: {len(tiles)} tiles, {total_bytes:,} bytes")
    print(f"  sum of the cuts: {sum(bytes_of.values()):,} bytes "
          f"({sum(bytes_of.values()) / total_bytes:.2f}x the whole)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
