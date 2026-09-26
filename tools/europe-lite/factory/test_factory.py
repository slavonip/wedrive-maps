# -*- coding: utf-8 -*-
"""Tests for the factory glue: gates.json from real gate reports, and the manifest contract.

    python3 tools/europe-lite/factory/test_factory.py
"""
import json, os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gates_json, lite_manifest  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


class GatesJson(unittest.TestCase):
    """The fixtures are the real output of the three gates on final2 (2026-09-24)."""

    def test_real_reports_pass_with_their_numbers(self):
        s = gates_json.parse("struct", fixture("struct.txt"), 0)
        t = gates_json.parse("transit", fixture("transit.txt"), 0)
        r = gates_json.parse("routes", fixture("routes.txt"), 0)
        for g in (s, t, r):
            self.assertEqual(g["verdict"], "PASS", g["problems"])
        self.assertEqual(s["counts"]["connector_ways"], 5834)
        self.assertEqual(s["counts"]["lost_in_ferry_reclassification"], 18)
        self.assertEqual(s["counts"]["gained"], 0)
        self.assertEqual((t["counts"]["pairs"], t["counts"]["land_only"], t["counts"]["new_transits"]), (216, 105, 0))
        self.assertEqual((r["counts"]["pass"], r["counts"]["known"], r["counts"]["accepted"], r["counts"]["fail"]), (38, 5, 3, 0))

    def test_nonzero_exit_is_fail_even_with_pass_text(self):
        self.assertEqual(gates_json.parse("routes", fixture("routes.txt"), 1)["verdict"], "FAIL")

    def test_fail_verdict_line_is_fail(self):
        text = fixture("routes.txt").replace("ROUTES GATE: PASS", "ROUTES GATE: FAIL")
        self.assertEqual(gates_json.parse("routes", text, 0)["verdict"], "FAIL")

    def test_missing_summary_is_fail_not_zero(self):
        text = "\n".join(l for l in fixture("transit.txt").splitlines() if not l.startswith("pairs "))
        g = gates_json.parse("transit", text, 0)
        self.assertEqual(g["verdict"], "FAIL")
        self.assertNotIn("new_transits", g["counts"])

    def test_truncated_report_is_fail(self):
        text = fixture("struct.txt").splitlines()
        g = gates_json.parse("struct", "\n".join(text[:1]), 0)
        self.assertEqual(g["verdict"], "FAIL")

    def test_two_verdict_lines_is_fail(self):
        self.assertEqual(gates_json.parse("struct", fixture("struct.txt") + "STRUCT GATE: PASS\n", 0)["verdict"], "FAIL")

    def test_cli_writes_json_and_exit_code(self):
        d = tempfile.mkdtemp()
        try:
            args = [sys.executable, os.path.join(HERE, "gates_json.py")]
            for g in ("struct", "transit", "routes"):
                args += ["--" + g, os.path.join(FIX, g + ".txt"), "--%s-exit" % g, "0"]
            ok = subprocess.run(args + ["--baseline", "test", "--out", os.path.join(d, "g.json")], capture_output=True)
            self.assertEqual(ok.returncode, 0, ok.stdout)
            self.assertEqual(json.load(open(os.path.join(d, "g.json")))["verdict"], "PASS")
            args[args.index("--routes-exit") + 1] = "1"
            bad = subprocess.run(args + ["--baseline", "test", "--out", os.path.join(d, "g.json")], capture_output=True)
            self.assertEqual(bad.returncode, 1)
            self.assertEqual(json.load(open(os.path.join(d, "g.json")))["verdict"], "FAIL")
        finally:
            shutil.rmtree(d)


def gates_from(struct, transit, routes=None, lifted=None):
    res = {"struct": gates_json.parse("struct", struct, 0),
           "transit": gates_json.parse("transit", transit, 0),
           "routes": gates_json.parse("routes", routes or fixture("routes.txt"), 0)}
    path = None
    if lifted is not None:
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(lifted, f)
    try:
        gates_json.transit_coverage(res, path)
    finally:
        if path:
            os.remove(path)
    return res


# The structural summary with nothing lifted, and the transit summary of a run with nothing to do
# (the exact lines B1 produced on final2 on 2026-09-25).
STRUCT_NONE = fixture("struct.txt").replace(
    "destonly True->False: inferred service 168; explicit access in ferry reclassification 18;",
    "destonly True->False: inferred service 0; explicit access in ferry reclassification 0;")
TRANSIT_NONE = "pairs 0, land-only 0, new transits through lifted destonly: 0\nTRANSIT GATE: PASS\n"


class TransitCoverage(unittest.TestCase):
    """A transit PASS counts only if it examined every way the structural gate found lifted."""

    def test_final2_real_reports_are_covered(self):
        r = gates_from(fixture("struct.txt"), fixture("transit.txt"))
        self.assertEqual(r["transit"]["verdict"], "PASS", r["transit"]["problems"])
        self.assertEqual(r["transit"]["counts"]["lifted_ways"], 18)

    def test_nothing_lifted_and_nothing_examined_is_pass(self):
        r = gates_from(STRUCT_NONE, TRANSIT_NONE, lifted=[])
        self.assertEqual(r["transit"]["verdict"], "PASS", r["transit"]["problems"])

    def test_lifted_ways_but_zero_pairs_is_fail(self):
        # The vacuous run: something to examine, nothing examined. Must not pass.
        r = gates_from(fixture("struct.txt"), TRANSIT_NONE)
        self.assertEqual(r["transit"]["verdict"], "FAIL")
        self.assertTrue(any("need 216" in p for p in r["transit"]["problems"]))

    def test_fewer_pairs_than_lifted_ways_need_is_fail(self):
        t = fixture("transit.txt").replace("pairs 216,", "pairs 204,")
        self.assertEqual(gates_from(fixture("struct.txt"), t)["transit"]["verdict"], "FAIL")

    def test_lifted_json_agreeing_with_the_report_is_pass(self):
        r = gates_from(fixture("struct.txt"), fixture("transit.txt"), lifted=[str(i) for i in range(18)])
        self.assertEqual(r["transit"]["verdict"], "PASS", r["transit"]["problems"])

    def test_lifted_json_disagreeing_with_the_report_is_fail(self):
        r = gates_from(fixture("struct.txt"), fixture("transit.txt"), lifted=[str(i) for i in range(17)])
        self.assertEqual(r["transit"]["verdict"], "FAIL")


class BaselineUndo(unittest.TestCase):
    """B1': the baseline side undoes exactly promote.py's rename, and nothing else."""

    def setUp(self):
        import baseline_pbf
        sys.path.insert(0, os.path.dirname(HERE))
        import promote
        self.b, self.p = baseline_pbf, promote

    def test_promote_then_undo_restores_the_tag_bytes(self):
        for v in ("parking_aisle", "driveway", "drive-through"):
            src = "w167792665 v3 dV c0 t2020-01-01T00:00:00Z i0 u Taccess=yes,highway=service,service=%s Nn1,n2\n" % v
            promoted, _, renamed = self.p.promote_line(src)
            self.assertTrue(renamed)
            back, changed, _ = self.b.undo_line(promoted.encode(), {b"167792665"})
            self.assertTrue(changed)
            # highway stays promoted: only the service key goes back
            self.assertEqual(back.decode(), promoted.replace("wedrive:service=", "service="))
            self.assertIn("highway=tertiary", back.decode())

    def test_only_connectors_are_touched(self):
        line = b"w1 v1 Thighway=tertiary,wedrive:service=driveway Nn1,n2\n"
        out, changed, foreign = self.b.undo_line(line, {b"2"})
        self.assertEqual((out, changed, foreign), (line, False, True))

    def test_other_lines_pass_through_byte_for_byte(self):
        for line in (b"n1 v1 x1.0 y2.0\n", b"r5 v1 Ttype=restriction Mw1@from\n",
                     b"w3 v1 Thighway=primary,name=Stra%c3%9fe Nn1,n2\n"):
            self.assertEqual(self.b.undo_line(line, {b"3"})[0], line)

    def test_a_value_promote_never_writes_is_refused(self):
        with self.assertRaises(ValueError):
            self.b.undo_line(b"w1 v1 Thighway=tertiary,wedrive:service=alley Nn1\n", {b"1"})


class PublishRefuses(unittest.TestCase):
    """publish.sh must stop before its first gh call when the gates did not pass."""

    def test_no_gh_call_when_gates_fail(self):
        d = tempfile.mkdtemp()
        try:
            rel, bin_, ptr = (os.path.join(d, x) for x in ("rel", "bin", "ptr"))
            for p in (rel, bin_, ptr):
                os.makedirs(p)
            log = os.path.join(d, "gh-calls.log")
            with open(os.path.join(bin_, "gh"), "w") as f:
                f.write("#!/bin/sh\necho \"$@\" >> %s\nexit 0\n" % log)
            os.chmod(os.path.join(bin_, "gh"), 0o755)
            for name, data in (("europe_lite.tar.gz", b"g"), ("europe_lite.osm.pbf", b"p")):
                open(os.path.join(rel, name), "wb").write(data)
            # A VALID manifest (made while the gates passed) ...
            g = {"verdict": "PASS", "baseline": {}}
            for k in ("struct", "transit", "routes"):
                g[k] = gates_json.parse(k, fixture(k + ".txt"), 0)
            json.dump(g, open(os.path.join(rel, "gates.json"), "w"))
            src = os.path.join(d, "source.json")
            json.dump({"url": "u", "md5": "0" * 32, "sha256": "a" * 64, "bytes": 1, "fetched": "x",
                       "replication": "2026-09-21T00:00:00Z", "sequence": 1}, open(src, "w"))
            mk = subprocess.run([sys.executable, os.path.join(HERE, "lite_manifest.py"), "make",
                                 "--repo", "owner/repo", "--dir", rel, "--source", src,
                                 "--gates", os.path.join(rel, "gates.json"), "--lite-image", "x@sha256:" + "b" * 64,
                                 "--tiles", "1", "--tar-bytes", "1", "--run-id", "1", "--run-url", "u",
                                 "--out", os.path.join(rel, "europe-lite.json")], capture_output=True, text=True)
            self.assertEqual(mk.returncode, 0, mk.stdout)
            # ... and then a gate report that FAILS: only gates.json can stop publication now.
            g["routes"] = gates_json.parse("routes", fixture("routes.txt"), 1)
            g["verdict"] = "FAIL"
            json.dump(g, open(os.path.join(rel, "gates.json"), "w"))
            env = dict(os.environ, PATH=bin_ + os.pathsep + os.environ["PATH"])
            r = subprocess.run(["bash", os.path.join(HERE, "publish.sh"), rel, "owner/repo", ptr],
                               capture_output=True, text=True, env=env)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("gates.json is not PASS", r.stdout + r.stderr)
            self.assertFalse(os.path.exists(log), "gh was called: %s" % (open(log).read() if os.path.exists(log) else ""))
        finally:
            shutil.rmtree(d)


class Manifest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        with open(os.path.join(self.d, "europe_lite.tar.gz"), "wb") as f:
            f.write(b"graph bytes")
        with open(os.path.join(self.d, "europe_lite.osm.pbf"), "wb") as f:
            f.write(b"pbf bytes")
        g = {"verdict": "PASS", "baseline": {"description": "B1", "image": "stock@sha256:x"}}
        for k, t in (("struct", "struct.txt"), ("transit", "transit.txt"), ("routes", "routes.txt")):
            g[k] = gates_json.parse(k, fixture(t), 0)
        self.gates = os.path.join(self.d, "gates.json")
        json.dump(g, open(self.gates, "w"))
        self.source = os.path.join(self.d, "source.json")
        json.dump({"url": "https://download.geofabrik.de/europe-latest.osm.pbf", "md5": "0" * 32,
                   "sha256": "a" * 64, "bytes": 35020804941, "fetched": "2026-09-22",
                   "replication": "2026-09-21T20:21:51Z", "sequence": 4920}, open(self.source, "w"))
        self.out = os.path.join(self.d, "europe-lite.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def make(self, **kw):
        args = ["make", "--repo", "slavonip/wedrive-maps", "--dir", self.d, "--source", self.source,
                "--gates", self.gates, "--lite-image", "ghcr.io/x/lite@sha256:" + "b" * 64,
                "--tiles", "23746", "--tar-bytes", "2710016000", "--run-id", "42",
                "--run-url", "https://example/run/42", "--out", self.out]
        return subprocess.run([sys.executable, os.path.join(HERE, "lite_manifest.py")] + args,
                              capture_output=True, text=True)

    def files(self):
        r = subprocess.run([sys.executable, os.path.join(HERE, "lite_manifest.py"), "files", self.out],
                           capture_output=True, text=True)
        return r.stdout.split()

    def test_without_features_the_release_is_the_contract_five(self):
        self.assertEqual(self.make().returncode, 0)
        self.assertNotIn("features", json.load(open(self.out)))
        self.assertEqual(self.files(), ["europe_lite.tar.gz", "europe_lite.osm.pbf", "gates.json",
                                        "europe-lite.json", "SHA256SUMS"])

    def test_features_ride_in_the_manifest_with_their_counts(self):
        with open(os.path.join(self.d, "europe_lite.features"), "w") as f:
            f.write("wedrive-features 1\ncountry EU\ngraph_version 2026-09-21\nsource x\nentries 0\n"
                    "counts camera_speed=0,camera_red_light=0,camera_section=0,level_crossing=0,"
                    "tram_level_crossing=0,traffic_signals=0\nid type lat_e7 lon_e7 bearing maxspeed way_ids attrs\n")
        r = self.make()
        self.assertEqual(r.returncode, 0, r.stdout)
        fe = json.load(open(self.out))["features"]
        self.assertEqual((fe["file"], fe["format"], fe["counts"]["camera_speed"]),
                         ("europe_lite.features", "wedrive-features/1", 0))
        self.assertTrue(fe["url"].endswith("/europe-lite-2026-09-21/europe_lite.features"))
        self.assertIn("europe_lite.features", self.files())
        self.assertEqual(len(self.files()), 6)

    def test_make_gives_the_contract(self):
        r = self.make()
        self.assertEqual(r.returncode, 0, r.stdout)
        m = json.load(open(self.out))
        self.assertEqual((m["schema"], m["kind"], m["version"], m["tag"]), (1, "europe-lite", "2026-09-21", "europe-lite-2026-09-21"))
        self.assertEqual(m["graph"]["file"], "europe_lite.tar.gz")
        self.assertEqual(m["pbf"]["file"], "europe_lite.osm.pbf")
        self.assertTrue(m["graph"]["url"].endswith("/releases/download/europe-lite-2026-09-21/europe_lite.tar.gz"))
        self.assertEqual(m["graph"]["bytes"], len(b"graph bytes"))
        self.assertEqual(set(m["engine"]["patches"]), {"B2", "C"})
        self.assertEqual(m["pipeline"]["revision"], "3eac2bcd744f76502c7f8156bad977831a7c35f0")
        self.assertEqual(m["gates"]["transit"]["new_transits"], 0)
        self.assertEqual(m["run"]["id"], 42)
        self.assertEqual(lite_manifest.validate(m, self.d), [])

    def test_gates_fail_refuses_a_manifest(self):
        g = json.load(open(self.gates)); g["verdict"] = "FAIL"; json.dump(g, open(self.gates, "w"))
        self.assertEqual(self.make().returncode, 1)
        self.assertFalse(os.path.exists(self.out))

    def test_check_catches_a_changed_file(self):
        self.make()
        with open(os.path.join(self.d, "europe_lite.tar.gz"), "ab") as f:
            f.write(b"!")
        m = json.load(open(self.out))
        self.assertTrue(any("does not match" in p for p in lite_manifest.validate(m, self.d)))

    def test_check_catches_a_versioned_file_name(self):
        self.make()
        m = json.load(open(self.out))
        m["graph"]["file"] = "europe_lite-2026-09-21.tar.gz"
        self.assertTrue(any("contract" in p for p in lite_manifest.validate(m)))

    def test_check_catches_tag_version_mismatch(self):
        self.make()
        m = json.load(open(self.out))
        m["tag"] = "europe-lite-2026-09-22"
        self.assertTrue(lite_manifest.validate(m))


if __name__ == "__main__":
    unittest.main(verbosity=1)
