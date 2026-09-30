# -*- coding: utf-8 -*-
"""The PMTiles contract the app reads — checked before an archive can be published.

The app's country tiles have ONE reader, its own parallel PMTiles reader (wedrive:
data/ParallelTiles.kt + PmtilesTiles.kt), and it supports exactly what this file allows. The WORLD
overview is read by MapLibre itself, but is held to the same contract so that one rule covers
every archive the factory ships. What the reader does with anything else is not an error message
but a blank country: a brotli directory is parsed as if it were raw, a zstd tile is handed to
MapLibre as if it were a vector tile. So an unexpected encoding from upstream (Protomaps) must stop
the release here, loudly, naming the field and the value found — never be accepted silently.

The same rule lives in the app (core: PmtilesContract.kt). Change both or neither.

    python3 scripts/pmtiles_contract.py FILE.pmtiles [--maxzoom N]     exit 0 = PASS, 1 = FAIL

Standard library only.
"""
import argparse
import gzip
import struct
import sys

HEADER_BYTES = 127
MAGIC = b"PMTiles"
SPEC_VERSION = 3
# PMTiles v3 codes: compression 0 unknown, 1 none, 2 gzip, 3 brotli, 4 zstd; tile type 1 mvt,
# 2 png, 3 jpeg, 4 webp, 5 avif.
COMPRESSIONS = {1: "none", 2: "gzip"}
TILE_TYPES = {1: "mvt"}
# Protomaps basemaps stop at z15; a country ships z0-14, the WORLD z0-6.
MAX_ZOOM_LIMIT = 15

_NAMES = {0: "unknown", 1: "none", 2: "gzip", 3: "brotli", 4: "zstd"}
_TYPES = {0: "unknown", 1: "mvt", 2: "png", 3: "jpeg", 4: "webp", 5: "avif"}


def check_header(raw, file_size=None, expect_max_zoom=None):
    """Every violation of the contract in a header, as readable sentences; empty = PASS."""
    if len(raw) < HEADER_BYTES:
        return ["header: %d bytes, a PMTiles v3 header is %d" % (len(raw), HEADER_BYTES)]
    if raw[:7] != MAGIC:
        return ["header: magic is %r, expected %r — not a PMTiles archive" % (bytes(raw[:7]), MAGIC)]
    problems = []
    if raw[7] != SPEC_VERSION:
        problems.append("spec version: found %d, supported %d" % (raw[7], SPEC_VERSION))
    u64 = lambda o: struct.unpack_from("<Q", raw, o)[0]
    i32 = lambda o: struct.unpack_from("<i", raw, o)[0]
    for field, offset in (("directory compression", 97), ("tile compression", 98)):
        code = raw[offset]
        if code not in COMPRESSIONS:
            problems.append("%s: found %d (%s), supported %s" % (
                field, code, _NAMES.get(code, "?"), ", ".join("%d (%s)" % kv for kv in COMPRESSIONS.items())))
    if raw[99] not in TILE_TYPES:
        problems.append("tile type: found %d (%s), supported %s" % (
            raw[99], _TYPES.get(raw[99], "?"), ", ".join("%d (%s)" % kv for kv in TILE_TYPES.items())))
    min_zoom, max_zoom = raw[100], raw[101]
    if not (min_zoom <= max_zoom <= MAX_ZOOM_LIMIT):
        problems.append("zoom range: found %d-%d, expected min <= max <= %d" % (min_zoom, max_zoom, MAX_ZOOM_LIMIT))
    if expect_max_zoom is not None and max_zoom != expect_max_zoom:
        problems.append("max zoom: found %d, this artifact is built to %d" % (max_zoom, expect_max_zoom))
    w, s, e, n = (i32(o) / 1e7 for o in (102, 106, 110, 114))
    if not (-180.0 <= w < e <= 180.0 and -90.0 <= s < n <= 90.0):
        problems.append("bounds: found [%.5f, %.5f, %.5f, %.5f], expected west < east and south < north "
                        "within [-180, -90, 180, 90]" % (w, s, e, n))
    if file_size is not None:
        for field, off in (("root directory", 8), ("metadata", 24), ("leaf directories", 40), ("tile data", 56)):
            start, length = u64(off), u64(off + 8)
            if start + length > file_size:
                problems.append("%s: bytes %d..%d lie beyond the file (%d bytes)" % (field, start, start + length, file_size))
    return problems


def check_file(path, expect_max_zoom=None):
    """[check_header] plus: the root directory really decodes with the declared compression."""
    with open(path, "rb") as f:
        raw = f.read(HEADER_BYTES)
        f.seek(0, 2)
        size = f.tell()
        problems = check_header(raw, size, expect_max_zoom)
        if problems:
            return problems
        start, length = struct.unpack_from("<QQ", raw, 8)
        f.seek(start)
        blob = f.read(length)
    try:
        data = gzip.decompress(blob) if raw[97] == 2 else blob
        count, _ = _varint(data, 0)
        if count == 0:
            return ["root directory: decodes to no entries"]
    except Exception as e:  # noqa: BLE001 — any decode failure is the finding
        return ["root directory: does not decode with %s: %s" % (_NAMES.get(raw[97], "?"), e)]
    return []


def _varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        if c < 0x80:
            return r, i
        s += 7


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("--maxzoom", type=int, default=None, help="the max zoom this artifact was built to")
    a = ap.parse_args(argv)
    problems = check_file(a.file, a.maxzoom)
    if problems:
        for p in problems:
            print("PMTiles contract FAIL: %s: %s" % (a.file, p), file=sys.stderr)
        return 1
    print("PMTiles contract PASS: %s" % a.file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
