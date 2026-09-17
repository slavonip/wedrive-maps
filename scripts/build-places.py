"""Extract a searchable place index from a package's basemap.

    usage: build-places.py <package.pmtiles> <W,S,E,N> <out.json> [--zoom 12] [--pmtiles path]

THE THIRD ARTIFACT OF A PACKAGE, and the one that makes "where to?" answerable offline.

The names are already on every car: the basemap's `places` source-layer carries `name`, `kind`
and `population` (§13 noted this as a bonus and nothing had used it). What was missing was a way
to SEARCH them — MapLibre can only query what is currently rendered, so a destination off-screen
is unfindable.

**Decoding happens HERE, not on the head unit.** Reading PMTiles v3 and Mapbox Vector Tiles in
Kotlin is a few hundred lines to own forever, running on the slowest computer in the arrangement,
every time someone types a letter. Doing it once on a runner and shipping a flat index makes the
car's search a ranked scan over a few megabytes. Same data, same offline guarantee, none of the
decoder.

MVT is protobuf and a tile is small, so it is parsed by hand: no library to install, nothing else
that has to be reachable at 02:17.
"""
import argparse
import gzip
import json
import math
import subprocess
import sys

# What a driver might type at. `country` and `region` are kept because "Romania" is a reasonable
# thing to type on the way there, and they cost a handful of rows.
WANTED_KINDS = {"country", "region", "locality", "borough", "neighbourhood", "macrohood"}


def varint(buf: bytes, i: int) -> tuple:
    value = shift = 0
    while True:
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7


def fields(buf: bytes):
    i = 0
    while i < len(buf):
        key, i = varint(buf, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = varint(buf, i)
            yield number, value
        elif wire == 2:
            length, i = varint(buf, i)
            yield number, buf[i:i + length]
            i += length
        elif wire == 5:
            yield number, buf[i:i + 4]
            i += 4
        elif wire == 1:
            yield number, buf[i:i + 8]
            i += 8
        else:
            raise ValueError(f"wire type {wire}")


def decode_value(buf: bytes):
    for number, payload in fields(buf):
        if number == 1:
            return payload.decode("utf-8", "replace")
        if number in (4, 5):
            return payload
        if number == 6:
            return (payload >> 1) ^ -(payload & 1)
    return None


def read_layer(buf: bytes) -> dict:
    name, keys, values, features, extent = None, [], [], [], 4096
    for number, payload in fields(buf):
        if number == 1:
            name = payload.decode("utf-8", "replace")
        elif number == 2:
            features.append(payload)
        elif number == 3:
            keys.append(payload.decode("utf-8", "replace"))
        elif number == 4:
            values.append(decode_value(payload))
        elif number == 5:
            extent = payload
    return {"name": name, "keys": keys, "values": values,
            "features": features, "extent": extent}


def read_feature(buf: bytes, keys: list, values: list) -> dict:
    tags, geometry = [], []
    for number, payload in fields(buf):
        if number == 2:
            i = 0
            while i < len(payload):
                v, i = varint(payload, i)
                tags.append(v)
        elif number == 4:
            i = 0
            while i < len(payload):
                v, i = varint(payload, i)
                geometry.append(v)
    attrs = {}
    for j in range(0, len(tags) - 1, 2):
        if tags[j] < len(keys):
            attrs[keys[tags[j]]] = values[tags[j + 1]] if tags[j + 1] < len(values) else None
    return attrs, geometry


def first_point(geometry: list, extent: int, z: int, x: int, y: int) -> tuple | None:
    """A point feature's position, in degrees.

    MVT geometry is a command stream in tile-local coordinates: `MoveTo` (command 1) followed by
    a zigzag-encoded dx, dy from the cursor, which starts at the tile's top-left.
    """
    if len(geometry) < 3 or (geometry[0] & 0x7) != 1:
        return None
    dx = (geometry[1] >> 1) ^ -(geometry[1] & 1)
    dy = (geometry[2] >> 1) ^ -(geometry[2] & 1)
    n = 2 ** z
    lon = (x + dx / extent) / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * (y + dy / extent) / n)))
    return round(math.degrees(lat_rad), 5), round(lon, 5)


def tile_range(bbox: tuple, z: int):
    west, south, east, north = bbox
    n = 2 ** z

    def col(lon):
        return int((lon + 180.0) / 360.0 * n)

    def row(lat):
        rad = math.radians(lat)
        return int((1.0 - math.asinh(math.tan(rad)) / math.pi) / 2.0 * n)

    for x in range(max(col(west), 0), min(col(east) + 1, n)):
        for y in range(max(row(north), 0), min(row(south) + 1, n)):
            yield x, y


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive")
    parser.add_argument("bbox")
    parser.add_argument("out")
    # z12 is the compromise measured on Moldova: every named settlement appears, while z14 costs
    # sixteen times the tiles for hamlets a driver does not type.
    parser.add_argument("--zoom", type=int, default=12)
    parser.add_argument("--pmtiles", default="pmtiles")
    args = parser.parse_args()

    bbox = tuple(float(v) for v in args.bbox.split(","))
    found: dict = {}
    tiles = scanned = 0

    for x, y in tile_range(bbox, args.zoom):
        tiles += 1
        raw = subprocess.run([args.pmtiles, "tile", args.archive, str(args.zoom), str(x), str(y)],
                             capture_output=True).stdout
        if not raw:
            continue
        scanned += 1
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        for number, payload in fields(raw):
            if number != 3:
                continue
            layer = read_layer(payload)
            if layer["name"] != "places":
                continue
            for feature in layer["features"]:
                attrs, geometry = read_feature(feature, layer["keys"], layer["values"])
                name = attrs.get("name")
                kind = attrs.get("kind")
                if not name or kind not in WANTED_KINDS:
                    continue
                point = first_point(geometry, layer["extent"], args.zoom, x, y)
                if point is None:
                    continue
                # A place straddling a tile boundary is emitted in both, so dedupe on name and
                # rough position rather than on name alone — two villages can share a name and
                # both deserve a row.
                key = (name, round(point[0], 2), round(point[1], 2))
                if key in found:
                    continue
                population = attrs.get("population")
                found[key] = {
                    "n": name,
                    "k": kind,
                    "y": point[0],
                    "x": point[1],
                    # Population is the ranking signal a driver actually means: typing "Ia" should
                    # offer Iași before a hamlet called Iaz. Absent for most small places, which
                    # is itself informative.
                    "p": int(population) if isinstance(population, int) else 0,
                }

    places = sorted(found.values(), key=lambda p: (-p["p"], p["n"]))
    index = {"schema": 1, "zoom": args.zoom, "count": len(places), "places": places}
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(index, handle, ensure_ascii=False, separators=(",", ":"))

    print(f"{len(places)} places from {scanned} non-empty tiles of {tiles} at z{args.zoom}",
          file=sys.stderr)
    for place in places[:8]:
        print(f"   {place['n']} ({place['k']}, pop {place['p']})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
