# -*- coding: utf-8 -*-
"""Tests for scripts/pmtiles_contract.py — the PMTiles contract of the app's reader. Standard library only.

    python3 tests/test_pmtiles_contract.py

The app holds the same rule in core/PmtilesContract.kt; the cases below are mirrored there.
"""
import gzip, importlib.util, os, struct, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("pmtiles_contract", os.path.join(ROOT, "scripts", "pmtiles_contract.py"))
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)


def varint(v):
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def archive(version=3, internal=2, tile=2, tile_type=1, minz=0, maxz=14,
            bounds=(26.61, 45.46, 30.19, 48.49), magic=b"PMTiles", root=None):
    """A small but complete archive: header, a gzip root directory with one entry, one tile."""
    entries = varint(1) + varint(0) + varint(1) + varint(10) + varint(1)   # 1 entry: id 0, run 1, len 10, offset 0
    root_bytes = root if root is not None else (gzip.compress(entries) if internal == 2 else entries)
    tile_data = b"\x00" * 10
    h = bytearray(127)
    h[0:7] = magic
    h[7] = version
    root_off = 127
    data_off = root_off + len(root_bytes)
    struct.pack_into("<QQQQQQQQ", h, 8, root_off, len(root_bytes), data_off, 0, data_off, 0, data_off, len(tile_data))
    struct.pack_into("<QQQ", h, 72, 1, 1, 1)
    h[97], h[98], h[99], h[100], h[101] = internal, tile, tile_type, minz, maxz
    w, s, e, n = bounds
    struct.pack_into("<iiii", h, 102, int(w * 1e7), int(s * 1e7), int(e * 1e7), int(n * 1e7))
    return bytes(h) + root_bytes + tile_data


def check(blob, maxzoom=None):
    with tempfile.NamedTemporaryFile(suffix=".pmtiles", delete=False) as f:
        f.write(blob)
    try:
        return C.check_file(f.name, maxzoom)
    finally:
        os.unlink(f.name)


class Contract(unittest.TestCase):
    def test_the_current_production_contract_passes(self):
        self.assertEqual([], check(archive(), maxzoom=14))
        self.assertEqual([], check(archive(maxz=6, bounds=(-180, -85.05, 180, 85.05)), maxzoom=6))   # the WORLD

    def test_uncompressed_directories_and_tiles_are_supported_too(self):
        self.assertEqual([], check(archive(internal=1, tile=1)))

    def test_a_wrong_spec_version_fails(self):
        self.assert_names(check(archive(version=2)), "spec version: found 2")

    def test_an_unsupported_tile_type_fails(self):
        self.assert_names(check(archive(tile_type=2)), "tile type: found 2 (png)")

    def test_an_unsupported_compression_fails(self):
        self.assert_names(check(archive(tile=4)), "tile compression: found 4 (zstd)")
        self.assert_names(check(archive(internal=3, root=b"\x0b\x01\x80brotli")), "directory compression: found 3 (brotli)")

    def test_an_invalid_header_fails(self):
        self.assert_names(check(archive(magic=b"NOTPMTL")), "not a PMTiles archive")
        self.assert_names(check(archive()[:60]), "header: 60 bytes")

    def test_insane_zooms_and_bounds_fail(self):
        self.assert_names(check(archive(minz=9, maxz=7)), "zoom range: found 9-7")
        self.assert_names(check(archive(), maxzoom=15), "max zoom: found 14, this artifact is built to 15")
        self.assert_names(check(archive(bounds=(30.0, 45.0, 26.0, 48.0))), "bounds:")

    def test_a_root_directory_that_does_not_decode_fails(self):
        self.assert_names(check(archive(root=b"not gzip at all")), "root directory: does not decode")

    def test_the_cli_exits_1_on_failure(self):
        with tempfile.NamedTemporaryFile(suffix=".pmtiles", delete=False) as f:
            f.write(archive(tile=3))
        try:
            self.assertEqual(1, C.main([f.name]))
        finally:
            os.unlink(f.name)

    def assert_names(self, problems, fragment):
        self.assertTrue(any(fragment in p for p in problems), "expected %r in %r" % (fragment, problems))


if __name__ == "__main__":
    unittest.main(verbosity=1)
