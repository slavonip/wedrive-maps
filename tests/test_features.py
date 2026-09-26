#!/usr/bin/env python3
"""regional-features.py: the file format, the gates, and the OSM value parsers.

The extraction itself needs osmium and a real PBF; it is proven by the factory runs and was
measured on Moldova (81 cameras, 932 level crossings, 618 signals) and eastern Austria (section
control and red-light relations). What is tested here is everything that decides whether a file is
accepted and what a tag means.
"""
import importlib.util
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("features", os.path.join(HERE, "..", "scripts", "regional-features.py"))
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)


def rows():
    return {
        "n10": {"type": "camera_speed", "lat": 47.01, "lon": 28.86, "bearing": [135.0], "maxspeed": 50,
                "ways": [7, 8], "attrs": {"bsrc": "deg"}},
        "n11": {"type": "level_crossing", "lat": 47.02, "lon": 28.87, "bearing": [], "maxspeed": None,
                "ways": [9], "attrs": {}},
        "n12": {"type": "traffic_signals", "lat": 47.03, "lon": 28.88, "bearing": [10.0], "maxspeed": None,
                "ways": [9], "attrs": {"bsrc": "fwd", "signal": "signal"}},
        "r5": {"type": "camera_section", "lat": 47.04, "lon": 28.89, "bearing": [90.0], "maxspeed": 80,
               "ways": [], "attrs": {"bsrc": "rel", "to": "470500000,289000000", "sections": "w1,w2"}},
    }


class Format(unittest.TestCase):
    def roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "md.features")
            F.write(p, "MD", "2026-09-24", "2026-09-24T20:21:20Z", rows())
            return open(p, encoding="ascii").read()

    def test_write_is_parseable_and_deterministic(self):
        a, b = self.roundtrip(), self.roundtrip()
        self.assertEqual(a, b)
        fe = F.parse(a)
        self.assertEqual(fe["country"], "MD")
        self.assertEqual(fe["counts"]["camera_speed"], 1)
        self.assertEqual([r["id"] for r in fe["rows"]], ["n10", "n11", "n12", "r5"])
        cam = fe["rows"][0]
        self.assertEqual((cam["bearing"], cam["maxspeed"], cam["ways"]), ([135], 50, ["w7", "w8"]))

    def test_unknown_values_stay_unknown(self):
        line = [l for l in self.roundtrip().split("\n") if l.startswith("n11 ")][0]
        self.assertEqual(line.split(" ")[4:6], ["-", "-"])

    def bad(self, text, pattern):
        with self.assertRaisesRegex(F.FeaturesError, pattern):
            F.parse(text)

    def test_strict_parser(self):
        good = self.roundtrip()
        self.bad(good.replace("wedrive-features 1", "wedrive-features 2"), "not a")
        self.bad(good.replace("entries 4", "entries 5"), "entries")
        self.bad(good.replace("n12 traffic_signals", "n11 traffic_signals"), "duplicate")
        self.bad(good.replace("level_crossing 4", "rail_bridge 4").replace(" level_crossing ", " rail_bridge "), "unknown type|counts")
        self.bad(good.replace(" 135 50 ", " 400 50 "), "bearing out of range")
        self.bad(good.replace(" 135 50 ", " 135 999 "), "maxspeed")
        self.bad(good.replace(";to=470500000,289000000", ""), "section control without its end")
        self.bad(good.replace(" w9 -", " - -"), "on no road")
        self.bad(good.replace("camera_speed=1", "camera_speed=2"), "counts disagree")


class Gates(unittest.TestCase):
    def fe(self, counts, lat_e7=470100000):
        rs = [{"id": "n1", "lat_e7": lat_e7, "lon_e7": 288600000}]
        return {"country": "MD", "counts": {t: counts.get(t, 0) for t in F.TYPES}, "rows": rs}

    BBOX = [26.61, 45.46, 30.19, 48.49]

    def test_first_release_passes(self):
        self.assertEqual(F.gate(self.fe({"camera_speed": 81}), "MD", self.BBOX, None), ([], []))

    def test_type_gone_to_zero_is_hard_fail(self):
        p, _ = F.gate(self.fe({"level_crossing": 932}), "MD", self.BBOX, {"camera_speed": 81, "level_crossing": 900})
        self.assertTrue(any("camera_speed" in x and "0 now" in x for x in p))

    def test_big_change_is_only_a_warning(self):
        p, w = F.gate(self.fe({"camera_speed": 40}), "MD", self.BBOX, {"camera_speed": 81})
        self.assertEqual(p, [])
        self.assertTrue(any("camera_speed: 81 -> 40" in x for x in w))

    def test_type_that_was_empty_may_stay_empty(self):
        p, w = F.gate(self.fe({"camera_speed": 81}), "MD", self.BBOX, {"camera_speed": 81, "camera_section": 0})
        self.assertEqual((p, w), ([], []))

    def test_far_outside_the_country_fails(self):
        p, _ = F.gate(self.fe({"camera_speed": 1}, lat_e7=520000000), "MD", self.BBOX, None)
        self.assertTrue(any("outside MD" in x for x in p))

    def test_wrong_country_fails(self):
        fe = self.fe({"camera_speed": 1}); fe["country"] = "RO"
        p, _ = F.gate(fe, "MD", self.BBOX, None)
        self.assertTrue(any("not MD" in x for x in p))


class Values(unittest.TestCase):
    def test_direction(self):
        self.assertEqual(F.parse_direction("forward"), ("fwd", None))
        self.assertEqual(F.parse_direction("backward"), ("bwd", None))
        self.assertEqual(F.parse_direction("-225"), ("deg", [135.0]))
        self.assertEqual(F.parse_direction("NE"), ("card", [45.0]))
        self.assertEqual(F.parse_direction("90;270"), ("deg", [90.0, 270.0]))
        self.assertEqual(F.parse_direction("0-90"), (None, None))      # a range is not a bearing
        self.assertEqual(F.parse_direction(None), (None, None))

    def test_maxspeed(self):
        self.assertEqual(F.maxspeed_kmh("50"), 50)
        self.assertEqual(F.maxspeed_kmh("30 mph"), 48)
        self.assertIsNone(F.maxspeed_kmh("RO:urban"))                  # never guessed
        self.assertIsNone(F.maxspeed_kmh(None))

    def test_bearing(self):
        self.assertAlmostEqual(F.bearing((47.0, 28.0), (47.1, 28.0)), 0.0, places=3)
        self.assertAlmostEqual(F.bearing((47.0, 28.0), (47.0, 28.1)), 90.0, delta=0.1)

    def test_dated_extract_is_the_graphs_own_snapshot(self):
        self.assertEqual(F.dated_extract({"url": "https://download.geofabrik.de/europe/moldova-latest.osm.pbf",
                                          "replication": "2026-09-24T20:21:20Z"}),
                         "https://download.geofabrik.de/europe/moldova-260924.osm.pbf")


if __name__ == "__main__":
    unittest.main(verbosity=1)
