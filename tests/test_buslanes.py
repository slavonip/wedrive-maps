#!/usr/bin/env python3
"""regional-buslanes.py: the file format (the APP's own contract, BusLanes.kt), the tag
classification, and the factory's format gate.

The extraction itself needs osmium and a real PBF; it was measured on Moldova (102 ways,
byte-identical to the hand-built sample). What is tested here is everything that decides whether a
file is accepted and what a tag means — the side is DIRECTED (where the lane sits relative to the
driving direction), which is why busway=lane is right, not both; see the script's docstring.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("buslanes", os.path.join(HERE, "..", "scripts", "regional-buslanes.py"))
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)


class Format(unittest.TestCase):
    ROWS = {30748227: "right", 24760042: "both", 49565745: "left"}

    def roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bus.lanes")
            B.write(p, self.ROWS)
            return open(p, encoding="ascii").read()

    def test_write_is_the_app_contract_sorted_and_deterministic(self):
        a, b = self.roundtrip(), self.roundtrip()
        self.assertEqual(a, b)
        # exactly what BusLanes.kt parses: the format line, then `w<id> <side>`, nothing else
        self.assertEqual(a, "wedrive-buslanes 1\nw24760042 both\nw30748227 right\nw49565745 left\n")
        self.assertEqual(B.parse(a), self.ROWS)

    def bad(self, text, pattern):
        with self.assertRaisesRegex(B.BusLanesError, pattern):
            B.parse(text)

    def test_strict_parser(self):
        good = self.roundtrip()
        self.bad(good.replace("wedrive-buslanes 1", "wedrive-buslanes 2"), "not a")
        self.bad(good.replace("w30748227 right", "w30748227  right"), "not 'w<id> <side>'")
        self.bad(good.replace("w30748227 right", "30748227 right"), "not 'w<id> <side>'")
        self.bad(good.replace("w30748227 right", "w30748227 middle"), "unknown side")
        self.bad(good.replace("w49565745 left", "w24760042 left"), "duplicate")
        self.bad(good + "w1 right -\n", "not 'w<id> <side>'")
        self.bad("", "not a")


class Tags(unittest.TestCase):
    def test_busway_is_directed(self):
        # the DIRECTED rule (owner 2026-11-01): busway=lane is a lane on each direction's RIGHT —
        # both would paint a BUS cell on the left of a card whose direction has no lane there
        # (Bd. Ștefan cel Mare, w30748227, confirmed on the road as right)
        self.assertEqual(B.side_of({"busway": "lane"}), "right")
        self.assertEqual(B.side_of({"busway:both": "lane"}), "both")
        self.assertEqual(B.side_of({"busway:right": "lane"}), "right")
        self.assertEqual(B.side_of({"busway:left": "lane"}), "left")
        self.assertEqual(B.side_of({"busway:left": "lane", "busway:right": "lane"}), "both")
        self.assertEqual(B.side_of({"busway": "lane", "busway:right": "lane"}), "right")
        self.assertEqual(B.side_of({"busway": "lane", "busway:left": "lane"}), "both")

    def test_lanes_bus_psv_are_directed_too(self):
        self.assertEqual(B.side_of({"lanes:bus": "1"}), "right")
        self.assertEqual(B.side_of({"lanes:psv": "2"}), "right")
        self.assertEqual(B.side_of({"lanes:bus:forward": "1"}), "right")
        self.assertEqual(B.side_of({"lanes:psv:backward": "1"}), "left")
        self.assertEqual(B.side_of({"lanes:bus:forward": "1", "lanes:bus:backward": "1"}), "both")

    def test_never_guessed(self):
        self.assertIsNone(B.side_of({"busway": "opposite_lane"}))      # deprecated contraflow tag
        self.assertIsNone(B.side_of({"busway:right": "opposite_lane"}))
        self.assertIsNone(B.side_of({"lanes:bus": "0"}))               # zero lanes is no lane
        self.assertIsNone(B.side_of({"lanes:psv": "yes"}))             # not a count: unknown
        self.assertIsNone(B.side_of({"highway": "primary"}))


class Gate(unittest.TestCase):
    def run_gate(self, file_text, block):
        with tempfile.TemporaryDirectory() as d:
            desc = {"code": "MD", "graph_version": "2026-10-30"}
            if block is not None:
                block.setdefault("file", "md-2026-10-30.buslanes")
                desc["bus_lanes"] = block
                if file_text is not None:
                    open(os.path.join(d, block["file"]), "w", newline="\n").write(file_text)
            json.dump(desc, open(os.path.join(d, "md.package.json"), "w"))
            return B.gates(d)

    GOOD = "wedrive-buslanes 1\nw1 right\nw2 both\n"

    def test_valid_file_passes(self):
        self.assertEqual(self.run_gate(self.GOOD, {"entries": 2, "counts": {"right": 1, "left": 0, "both": 1}}), 0)

    def test_no_block_is_a_warning_not_a_failure(self):
        self.assertEqual(self.run_gate(None, None), 0)      # published before bus lanes existed

    def test_malformed_line_fails_the_run(self):
        self.assertEqual(self.run_gate("wedrive-buslanes 1\nw1 right\nw2 sideways\n", {"entries": 2}), 1)

    def test_missing_file_fails_the_run(self):
        self.assertEqual(self.run_gate(None, {"entries": 2}), 1)

    def test_entries_disagreeing_with_the_package_fail(self):
        self.assertEqual(self.run_gate(self.GOOD, {"entries": 3}), 1)

    def test_counts_disagreeing_with_the_package_fail(self):
        self.assertEqual(self.run_gate(self.GOOD, {"entries": 2, "counts": {"right": 2, "left": 0, "both": 0}}), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
