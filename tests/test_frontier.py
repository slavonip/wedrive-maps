# -*- coding: utf-8 -*-
"""Tests for scripts/regional-frontier.py — the frontier file and the D1 rule. Standard library only.

    python3 tests/test_frontier.py

tests/frontier-vectors.json is shared, byte for byte, with the app's Kotlin tests
(wedrive: data/src/test/resources/frontier-vectors.json). Two implementations of one rule are
only as good as the cases they are both held to.
"""
import importlib.util, json, os, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("regional_frontier", os.path.join(ROOT, "scripts", "regional-frontier.py"))
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)

CFG = json.load(open(os.path.join(ROOT, "regional.json"), encoding="utf-8"))
VECTORS = json.load(open(os.path.join(ROOT, "tests", "frontier-vectors.json"), encoding="utf-8"))


def rows(v):
    return [tuple(r) for r in v]


class Vectors(unittest.TestCase):
    def test_every_vector(self):
        for case in VECTORS["cases"]:
            with self.subTest(case["name"]):
                pairs, stats = F.match(rows(case["A"]), rows(case["B"]))
                got = [[i, j, m, round(d, 2)] for i, j, m, d in pairs]
                self.assertEqual(got, case["pairs"])
                self.assertEqual(stats["ambiguous"], case["ambiguous"])

    def test_vectors_cover_every_required_case(self):
        names = " ".join(c["name"] for c in VECTORS["cases"])
        for tag in ("A:", "B:", "C:", "D:", "E:", "F:", "artificial 5 m fallback", "mutual"):
            self.assertIn(tag, names)


class FileFormat(unittest.TestCase):
    ROWS = [(2, 476943833, 169752574, 18, 2330850050), (0, 470000000, 280000000, 8, None),
            (1, 470000000, 280000000, 9, 5)]

    def roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.frontier")
            F.write(p, "HU", "2026-09-25", self.ROWS)
            with open(p, encoding="ascii") as f:
                text = f.read()
            return text, F.load(p)

    def test_write_is_canonical_and_parses_back(self):
        text, fr = self.roundtrip()
        self.assertTrue(text.startswith("wedrive-frontier 1\ncountry HU\ngraph_version 2026-09-25\nentries 3\n"))
        self.assertEqual([r[0] for r in fr["rows"]], [0, 1, 2])
        self.assertEqual(fr["rows"][0][4], None)
        self.assertEqual(F.validate(fr), [])

    def test_same_rows_same_bytes(self):
        a, _ = self.roundtrip()
        b, _ = self.roundtrip()
        self.assertEqual(a, b)

    def test_malformed_is_refused(self):
        good, _ = self.roundtrip()
        for bad in (good.replace("wedrive-frontier 1", "wedrive-frontier 2"),
                    good.replace("entries 3", "entries 4"),
                    good.replace("2330850050", "x"),
                    good + "1 2 3\n",
                    good.replace("level lat_e7 lon_e7 local_id osm_node_id\n", "")):
            with self.assertRaises(F.FrontierError):
                F.parse(bad)


class Gates(unittest.TestCase):
    def fr(self, rows):
        return {"country": "XX", "graph_version": "v", "rows": sorted(rows, key=lambda r: r[:4])}

    def test_coordinate_collision_fails(self):
        p = F.validate(self.fr([(2, 1, 1, 18, None), (2, 1, 1, 26, None)]))
        self.assertTrue(any("collision" in x for x in p))

    def test_osm_collision_fails(self):
        p = F.validate(self.fr([(2, 1, 1, 18, 7), (2, 5, 5, 26, 7)]))
        self.assertTrue(any("OSM node 7 twice" in x for x in p))

    def test_same_osm_on_two_levels_is_fine(self):
        self.assertEqual(F.validate(self.fr([(1, 1, 1, 17, 7), (2, 1, 1, 18, 7)])), [])

    def test_impossible_level_fails(self):
        self.assertTrue(F.validate(self.fr([(3, 1, 1, 19, None)])))

    def test_level_bits_must_agree(self):
        p = F.validate(self.fr([(2, 1, 1, 17, None)]))
        self.assertTrue(any("is level 1" in x for x in p))

    def test_region_bits_fail(self):
        self.assertTrue(F.validate(self.fr([(2, 1, 1, (1 << 46) | 18, None)])))

    def test_empty_fails(self):
        self.assertTrue(F.validate(self.fr([])))


class PortalRows(unittest.TestCase):
    def test_same_point_zero_moved_point_real_distance(self):
        A = [(2, 476943833, 169752574, 18, 1), (0, 470000000, 280000000, 8, 2)]
        B = [(2, 476943833, 169752218, 26, 1), (0, 470000000, 280000000, 16, 2)]
        pairs, _ = F.match(A, B)
        out = F.portal_rows(A, 3, B, 4, pairs)
        self.assertEqual(len(out), 4)                          # both directions
        f = [l.split("#")[0].split() for l in out]
        self.assertEqual(f[0], [str((3 << 46) | 18), str((4 << 46) | 26), "2.67"])
        self.assertEqual(f[1], [str((4 << 46) | 26), str((3 << 46) | 18), "2.67"])
        self.assertEqual(f[2][2], "0")                         # patch 21: 0 only for one point

    def test_build_portals_all_pairs_and_require_borders(self):
        fr = {"MD": {"graph_version": "a", "rows": [(2, 1000, 1000, 18, 9)]},
              "RO": {"graph_version": "b", "rows": [(2, 1000, 1000, 26, 9)]},
              "DE": {"graph_version": "c", "rows": [(2, 99999, 99999, 34, 10)]}}
        rows, report = F.build_portals(CFG, fr)
        self.assertEqual(list(report), ["MD-RO"])
        self.assertEqual(report["MD-RO"]["versions"], ["a", "b"])
        self.assertEqual(len(rows), 2)


class FactorySet(unittest.TestCase):
    """set_portals over <work>/<code>.package.json — exactly what the regression runs on."""

    def make(self, d, code, rows, version="v"):
        path = os.path.join(d, "%s.frontier" % code.lower())
        F.write(path, code, version, rows)
        with open(os.path.join(d, "%s.package.json" % code.lower()), "w") as f:
            json.dump({"code": code, "graph_version": version, "frontier": F.frontier_block(path)}, f)
        return path

    def test_set_builds_the_table_and_requires_every_border(self):
        with tempfile.TemporaryDirectory() as d:
            self.make(d, "MD", [(2, 470000000, 280000000, 18, 1)], "2026-09-19")
            self.make(d, "RO", [(2, 470000000, 280000000, 26, 1)], "2026-09-24")
            out = os.path.join(d, "set.d1")
            self.assertEqual(F.set_portals(CFG, d, out), [])
            with open(out) as f:
                self.assertEqual(len(f.read().splitlines()), 2)
            self.make(d, "HU", [(2, 460000000, 210000000, 34, 9)])        # RO-HU: no common node
            problems = F.set_portals(CFG, d, out)
            self.assertTrue(any("RO-HU" in p for p in problems), problems)

    def test_frontier_not_matching_its_description_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.make(d, "MD", [(2, 470000000, 280000000, 18, 1)])
            with open(path, "a") as f:
                f.write("\n")
            with self.assertRaises(F.FrontierError):
                F.set_portals(CFG, d, os.path.join(d, "set.d1"))

    def test_dated_extract_is_derived_from_provenance(self):
        self.assertEqual(F.dated_extract({"url": "https://download.geofabrik.de/europe/moldova-latest.osm.pbf",
                                          "replication": "2026-09-24T20:21:20Z"}),
                         "https://download.geofabrik.de/europe/moldova-260924.osm.pbf")
        self.assertIsNone(F.dated_extract({}))


class Diff(unittest.TestCase):
    def test_classification(self):
        old = {"rows": [(2, 476943833, 169752574, 18, 2330850050),   # moves 2.66 m
                        (2, 461583385, 203768083, 26, 306893621),    # disappears
                        (1, 10, 10, 9, None),                        # kept, no id
                        (0, 20, 20, 8, 44)]}                         # kept
        new = {"rows": [(2, 476943833, 169752218, 18, 2330850050),
                        (2, 461532964, 203692478, 34, 14207550809),  # new crossing
                        (1, 10, 10, 17, None),
                        (0, 20, 20, 16, 44)]}
        d = F.diff(old, new)
        self.assertEqual(d["counts"], {"retained": 1, "moved_same_osm_id": 1, "retained_by_coordinate": 1,
                                       "removed": 1, "added": 1})
        self.assertEqual(d["moved"], [(2330850050, 2, 2.66)])


if __name__ == "__main__":
    unittest.main(verbosity=1)
