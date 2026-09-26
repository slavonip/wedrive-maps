# -*- coding: utf-8 -*-
"""Tests for the regional factory scripts. Standard library only (the factory runs them in the
engine container and on a bare runner, neither of which has pytest).

    python3 tests/test_regional.py
"""
import gzip, hashlib, importlib.util, io, json, os, shutil, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = os.path.join(ROOT, "scripts")


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(S, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


precheck = load("regional_precheck", "regional-precheck.py")
carry = load("regional_carry", "regional-carry.py")
package = load("regional_package", "regional-package.py")
manifest = load("regional_manifest", "regional-manifest.py")

CFG = json.load(open(os.path.join(ROOT, "regional.json"), encoding="utf-8"))
LOCK = {"engine_ref": "multi-region-runtime-v2", "engine_sha": "e" * 40,
        "image": "ghcr.io/x/engine@sha256:" + "d" * 64,
        "timezones_url": "https://x/timezones.sqlite", "timezones_sha256": "t" * 64}


def sha(b):
    return hashlib.sha256(b).hexdigest()


def region(code, md5="m" * 32, **kw):
    r = {"region_id": CFG["countries"][code]["region_id"], "graph_version": "2026-09-19",
         "package": "%s-2026-09-19.tar.gz" % code.lower(),
         "url": "https://github.com/o/r/releases/download/regional-old/%s-2026-09-19.tar.gz" % code.lower(),
         "bytes": 3, "sha256": "s", "tiles": 1, "engine": "multi-region-runtime-v2",
         "engine_sha": LOCK["engine_sha"], "source": {"md5": md5}, "timezones": {"sha256": LOCK["timezones_sha256"]}}
    r.update(kw)
    return r


class Precheck(unittest.TestCase):
    ALL = list(CFG["countries"])

    def decide(self, man, md5s, countries=None, rebuild="auto"):
        return precheck.decide(CFG, LOCK, man, countries or self.ALL, rebuild, md5s)

    def test_first_run_rebuilds_everything(self):
        rb, ca, why = self.decide(None, {})
        self.assertEqual(rb, self.ALL); self.assertEqual(ca, [])

    def test_unchanged_sources_rebuild_nothing(self):
        man = {"regions": {c: region(c) for c in self.ALL}}
        rb, ca, _ = self.decide(man, {c: "m" * 32 for c in self.ALL})
        self.assertEqual(rb, []); self.assertEqual(ca, self.ALL)

    def test_only_the_changed_country_is_rebuilt(self):
        man = {"regions": {c: region(c) for c in self.ALL}}
        md5s = {c: "m" * 32 for c in self.ALL}; md5s["RO"] = "n" * 32
        rb, ca, why = self.decide(man, md5s)
        self.assertEqual(rb, ["RO"]); self.assertNotIn("RO", ca)
        self.assertIn("changed", why["RO"])

    def test_old_manifest_without_provenance_rebuilds(self):
        man = {"regions": {c: {"region_id": 1} for c in self.ALL}}          # today's production manifest
        rb, _, why = self.decide(man, {c: "m" * 32 for c in self.ALL})
        self.assertEqual(rb, self.ALL)
        self.assertTrue(all("MD5" in w for w in why.values()))

    def test_engine_or_timezones_change_rebuilds(self):
        man = {"regions": {c: region(c) for c in self.ALL}}
        man["regions"]["MD"]["engine_sha"] = "other"
        man["regions"]["HU"]["timezones"] = {"sha256": "other"}
        rb, _, why = self.decide(man, {c: "m" * 32 for c in self.ALL})
        self.assertEqual(rb, ["MD", "HU"])

    def test_control_subset_and_forced(self):
        rb, ca, _ = self.decide(None, {}, countries=["MD", "RO"])
        self.assertEqual((rb, ca), (["MD", "RO"], []))
        man = {"regions": {c: region(c) for c in self.ALL}}
        rb, ca, _ = self.decide(man, {c: "m" * 32 for c in self.ALL}, rebuild="DE")
        self.assertEqual(rb, ["DE"])
        with self.assertRaises(SystemExit):
            self.decide(man, {}, countries=["MD"], rebuild="DE")


class Carry(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def fake_fetch(self, files):
        def fetch(url, dst):
            with open(dst, "wb") as f:
                f.write(files[url.rsplit("/", 1)[1]])
        return fetch

    def test_single_file_is_verified_and_described(self):
        data = b"tiles!"
        man = {"regions": {"MD": region("MD", bytes=len(data), sha256=sha(data), map={"available": True})}}
        d = carry.carry(man, "MD", self.d, fetch=self.fake_fetch({"md-2026-09-19.tar.gz": data}))
        self.assertTrue(d["carried"]); self.assertEqual(d["url"], man["regions"]["MD"]["url"])
        self.assertEqual(d["map"], {"available": True})
        self.assertEqual(open(os.path.join(self.d, "md-2026-09-19.tar.gz"), "rb").read(), data)

    def test_parts_are_joined_and_checked(self):
        a, b = b"abc", b"def"
        man = {"regions": {"DE": region("DE", bytes=6, sha256=sha(a + b), parts=[
            {"name": "de.part001", "bytes": 3, "sha256": sha(a)}, {"name": "de.part002", "bytes": 3, "sha256": sha(b)}])}}
        carry.carry(man, "DE", self.d, fetch=self.fake_fetch({"de.part001": a, "de.part002": b}))
        self.assertEqual(open(os.path.join(self.d, man["regions"]["DE"]["package"]), "rb").read(), a + b)

    def test_corrupt_download_is_refused(self):
        man = {"regions": {"MD": region("MD", bytes=6, sha256=sha(b"tiles!"))}}
        with self.assertRaises(SystemExit):
            carry.carry(man, "MD", self.d, fetch=self.fake_fetch({"md-2026-09-19.tar.gz": b"TILES!"}))
        self.assertFalse(os.path.exists(os.path.join(self.d, "md-2026-09-19.tar.gz")))

    def test_bad_part_is_refused(self):
        man = {"regions": {"DE": region("DE", bytes=6, sha256=sha(b"abcdef"), parts=[
            {"name": "de.part001", "bytes": 3, "sha256": sha(b"abc")}, {"name": "de.part002", "bytes": 3, "sha256": sha(b"def")}])}}
        with self.assertRaises(SystemExit):
            carry.carry(man, "DE", self.d, fetch=self.fake_fetch({"de.part001": b"abc", "de.part002": b"XYZ"}))

    def test_frontier_travels_with_the_package_and_is_checked(self):
        data, fr = b"tiles!", b"wedrive-frontier 1\n"
        f = {"format": "wedrive-frontier/1", "file": "md-2026-09-19.frontier", "bytes": len(fr), "sha256": sha(fr),
             "url": "https://github.com/o/r/releases/download/regional-old/md-2026-09-19.frontier"}
        man = {"regions": {"MD": region("MD", bytes=len(data), sha256=sha(data), frontier=f)}}
        files = {"md-2026-09-19.tar.gz": data, "md-2026-09-19.frontier": fr}
        d = carry.carry(man, "MD", self.d, fetch=self.fake_fetch(files))
        self.assertEqual(d["frontier"], f)
        self.assertEqual(open(os.path.join(self.d, "md-2026-09-19.frontier"), "rb").read(), fr)
        files["md-2026-09-19.frontier"] = b"tampered"
        os.remove(os.path.join(self.d, "md-2026-09-19.frontier"))
        with self.assertRaises(SystemExit):
            carry.carry(man, "MD", self.d, fetch=self.fake_fetch(files))

    def test_country_not_in_manifest_cannot_be_carried(self):
        with self.assertRaises(SystemExit):
            carry.carry({"regions": {}}, "MD", self.d)


class Package(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)
        package.split_assets.PART_BYTES = 1_900_000_000

    def desc(self, code, name, data, carried=False):
        open(os.path.join(self.d, name), "wb").write(data)
        json.dump({"code": code, "package": name, "bytes": len(data), "sha256": sha(data), "carried": carried,
                   "graph_version": "2026-09-26", "tiles": 1}, open(os.path.join(self.d, "%s.package.json" % code.lower()), "w"))

    def test_gzip_n_is_deterministic_and_described(self):
        data = os.urandom(1000) * 50
        self.desc("MD", "md-2026-09-26.tar", data)
        package.package(self.d)
        d = json.load(open(os.path.join(self.d, "md.package.json")))
        gz = open(os.path.join(self.d, "md-2026-09-26.tar.gz"), "rb").read()
        self.assertEqual(d["package"], "md-2026-09-26.tar.gz")
        self.assertEqual((d["bytes"], d["sha256"]), (len(gz), sha(gz)))
        self.assertEqual((d["tar_bytes"], d["tar_sha256"]), (len(data), sha(data)))
        self.assertEqual(gzip.decompress(gz), data)
        self.assertEqual(gz[4:8], b"\0\0\0\0")                       # -n: no timestamp in the header
        self.assertFalse(os.path.exists(os.path.join(self.d, "md-2026-09-26.tar")))
        # the same tar again gives the same bytes
        self.desc("MD", "md-2026-09-26.tar", data)
        package.package(self.d)
        self.assertEqual(open(os.path.join(self.d, "md-2026-09-26.tar.gz"), "rb").read(), gz)

    def test_over_the_limit_is_cut_into_parts(self):
        self.desc("DE", "de-2026-09-26.tar", os.urandom(40000))
        package.package(self.d, part_bytes=15000)
        d = json.load(open(os.path.join(self.d, "de.package.json")))
        self.assertEqual(len(d["parts"]), 3)
        joined = b"".join(open(os.path.join(self.d, p["name"]), "rb").read() for p in d["parts"])
        self.assertEqual((len(joined), sha(joined)), (d["bytes"], d["sha256"]))

    def test_carried_packages_are_not_touched(self):
        self.desc("RO", "ro-2026-09-19.tar.gz", b"published", carried=True)
        self.assertEqual(package.package(self.d), [])
        self.assertEqual(json.load(open(os.path.join(self.d, "ro.package.json")))["package"], "ro-2026-09-19.tar.gz")


class Manifest(unittest.TestCase):
    TAG = "regional-2026-09-26-9"
    BASE = "https://github.com/o/r/releases/download/" + TAG

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cfg = os.path.join(ROOT, "regional.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def put(self, name, data):
        open(os.path.join(self.d, name), "wb").write(data)

    def built(self, code, data, **kw):
        self.put("%s-2026-09-26.tar.gz" % code.lower(), data)
        d = {"code": code, "graph_version": "2026-09-26", "tiles": 7, "package": "%s-2026-09-26.tar.gz" % code.lower(),
             "bytes": len(data), "sha256": sha(data), "engine": "multi-region-runtime-v2", "engine_sha": LOCK["engine_sha"],
             "source": {"md5": "m" * 32, "sha256": "p" * 64, "replication": "2026-09-26T20:00:00Z"},
             "timezones": {"sha256": LOCK["timezones_sha256"]}}
        d.update(kw)
        json.dump(d, open(os.path.join(self.d, "%s.package.json" % code.lower()), "w"))

    def carried(self, code, data, url):
        self.put("%s-2026-09-19.tar.gz" % code.lower(), data)
        json.dump({"code": code, "carried": True, "graph_version": "2026-09-19", "tiles": 5,
                   "package": "%s-2026-09-19.tar.gz" % code.lower(), "bytes": len(data), "sha256": sha(data),
                   "url": url, "parts": [], "engine": "multi-region-runtime-v2", "engine_sha": LOCK["engine_sha"],
                   "source": {"md5": "m" * 32, "sha256": "p" * 64, "replication": "2026-09-19T20:00:00Z"},
                   "timezones": {"sha256": LOCK["timezones_sha256"]}},
                  open(os.path.join(self.d, "%s.package.json" % code.lower()), "w"))

    def m(self, previous=None):
        return manifest.build(self.cfg, self.d, self.BASE, previous, self.TAG, LOCK, "abc123", 42, "https://run")

    def test_new_and_carried_countries(self):
        old = "https://github.com/o/r/releases/download/regional-2026-09-20-8/ro-2026-09-19.tar.gz"
        self.built("MD", b"md-new")
        self.carried("RO", b"ro-old", old)
        self.put("md-ro.portals", b"1 2 3\n4 5 6\n")
        prev = {"regions": {"RO": {"engine_digest": "sha256:old", "search": {"available": False}},
                            "MD": {"map": {"available": True, "url": "u"}}}}
        m = self.m(prev)
        self.assertEqual(m["regions"]["MD"]["url"], self.BASE + "/md-2026-09-26.tar.gz")
        self.assertEqual(m["regions"]["RO"]["url"], old)
        self.assertEqual(m["regions"]["RO"]["release"], "regional-2026-09-20-8")
        self.assertEqual(m["regions"]["MD"]["map"], {"available": True, "url": "u"})     # kept across a graph rebuild
        self.assertEqual(m["regions"]["RO"]["search"], {"available": False})
        self.assertEqual(m["regions"]["MD"]["engine_digest"], "sha256:" + "d" * 64)
        self.assertEqual(m["regions"]["RO"]["engine_digest"], "sha256:old")
        self.assertEqual(m["regions"]["MD"]["portals"], ["MD-RO"])
        self.assertEqual(m["portals"]["MD-RO"]["versions"], ["MD-2026-09-26", "RO-2026-09-19"])
        self.assertEqual(m["engine"]["digest"], "sha256:" + "d" * 64)
        self.assertEqual(m["pipeline"], {"revision": "abc123", "run_id": 42, "run_url": "https://run"})
        self.assertEqual(manifest.assets(m), ["md-2026-09-26.tar.gz", "md-ro.portals"])
        self.assertEqual(manifest.check(m, self.d, provenance=True), 0)
        self.assertEqual(manifest.verify_assets(m, self.d), [])
        urls = dict(manifest.urls(m))
        self.assertIn(old, urls)

    def test_app_contract_fields_are_all_there(self):
        self.built("MD", b"md")
        r = self.m()["regions"]["MD"]
        for k in ("region_id", "graph_version", "package", "url", "bytes", "sha256", "tiles", "title"):
            self.assertIn(k, r)
        self.assertNotIn("parts", r)                   # absent, not empty: the app reads "one file"

    def test_check_catches_changed_file_and_bad_versions(self):
        self.built("MD", b"md"); self.built("RO", b"ro"); self.put("md-ro.portals", b"1\n")
        m = self.m()
        self.put("md-2026-09-26.tar.gz", b"MD")
        self.assertGreater(manifest.check(m, self.d), 0)
        self.put("md-2026-09-26.tar.gz", b"md")
        m["portals"]["MD-RO"]["versions"] = ["MD-x", "RO-2026-09-26"]
        self.assertGreater(manifest.check(m, self.d), 0)

    def test_provenance_is_required_when_asked(self):
        self.built("MD", b"md", source={})
        self.assertGreater(manifest.check(self.m(), self.d, provenance=True), 0)

    def frontier(self, code, text=b"wedrive-frontier 1\n", url=None):
        name = "%s-2026-09-26.frontier" % code.lower()
        self.put(name, text)
        fb = {"format": "wedrive-frontier/1", "file": name, "bytes": len(text), "sha256": sha(text),
              "entries": 3, "osm_resolved": 2}
        if url:
            fb["url"] = url
        return fb

    def test_frontier_of_a_built_country_is_in_this_release(self):
        self.built("MD", b"md", frontier=self.frontier("MD"))
        m = self.m()
        f = m["regions"]["MD"]["frontier"]
        self.assertEqual(m["schema"], 2)
        self.assertEqual(f["url"], self.BASE + "/md-2026-09-26.frontier")
        self.assertEqual((f["format"], f["entries"], f["osm_resolved"]), ("wedrive-frontier/1", 3, 2))
        self.assertIn("md-2026-09-26.frontier", manifest.assets(m))
        self.assertIn((f["url"], f["bytes"]), manifest.urls(m))
        self.assertEqual(manifest.check(m, self.d), 0)
        self.assertEqual(manifest.verify_assets(m, self.d), [])

    def test_carried_frontier_stays_in_its_release_backfilled_one_is_new(self):
        old = "https://github.com/o/r/releases/download/regional-2026-09-20-8/"
        self.carried("RO", b"ro-old", old + "ro-2026-09-19.tar.gz")
        d = json.load(open(os.path.join(self.d, "ro.package.json")))
        d["frontier"] = self.frontier("RO", url=old + "ro-2026-09-26.frontier")
        json.dump(d, open(os.path.join(self.d, "ro.package.json"), "w"))
        self.carried("HU", b"hu-old", old + "hu-2026-09-19.tar.gz")
        d = json.load(open(os.path.join(self.d, "hu.package.json")))
        d["frontier"] = dict(self.frontier("HU"), backfilled=True)          # made in this run
        json.dump(d, open(os.path.join(self.d, "hu.package.json"), "w"))
        m = self.m()
        self.assertEqual(m["regions"]["RO"]["frontier"]["url"], old + "ro-2026-09-26.frontier")
        self.assertEqual(m["regions"]["HU"]["frontier"]["url"], self.BASE + "/hu-2026-09-26.frontier")
        self.assertEqual(manifest.assets(m), ["hu-2026-09-26.frontier"])  # neither package is re-uploaded

    def test_changed_frontier_is_caught(self):
        self.built("MD", b"md", frontier=self.frontier("MD"))
        m = self.m()
        self.put("md-2026-09-26.frontier", b"wedrive-frontier 1\nX\n")
        self.assertGreater(manifest.check(m, self.d), 0)

    def test_completeness_needs_a_frontier_per_country(self):
        for code in CFG["countries"]:
            self.built(code, code.encode())
        for b in CFG["borders"]:
            a, c = b["between"]
            self.put("%s-%s.portals" % (a.lower(), c.lower()), b"1\n")
        self.assertIn("incomplete: MD has no frontier", manifest.complete(self.m(), self.cfg))

    def test_completeness(self):
        self.built("MD", b"md")
        self.assertTrue(manifest.complete(self.m(), self.cfg))
        for code in CFG["countries"]:
            self.built(code, code.encode(), frontier=self.frontier(code))
        for b in CFG["borders"]:
            a, c = b["between"]
            self.put("%s-%s.portals" % (a.lower(), c.lower()), b"1\n")
        self.assertEqual(manifest.complete(self.m(), self.cfg), [])

    def test_region_id_is_the_config_one(self):
        self.built("DE", b"de")
        self.assertEqual(self.m()["regions"]["DE"]["region_id"], 5)


class Portals(unittest.TestCase):
    def test_a_set_without_any_border_pair_needs_no_table(self):
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "in", "MD", "tiles"))
            r = subprocess.run([sys.executable, os.path.join(S, "regional-portals.py"),
                                os.path.join(ROOT, "regional.json"), os.path.join(d, "in"), os.path.join(d, "out")],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        finally:
            shutil.rmtree(d)


class Regress(unittest.TestCase):
    """regional-regress.py against a fake valhalla_service: seams, lost regions and km decide."""

    def run_with(self, km, seams, lost=0, countries=("MD",), engine_line=None):
        d = tempfile.mkdtemp()
        try:
            fake = os.path.join(d, "valhalla_service")
            with open(fake, "w") as f:
                f.write("#!/bin/sh\n")
                if engine_line:
                    f.write("echo '%s' >&2\n" % engine_line)
                for _ in range(seams):
                    f.write("echo 'WEDRIVE SEAM x' >&2\n")
                for _ in range(lost):
                    f.write("echo 'WEDRIVE REGION LOST' >&2\n")
                f.write("echo '{\"trip\":{\"summary\":{\"length\":%s}}}'\n" % km)
            os.chmod(fake, 0o755)
            env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"])
            return subprocess.run([sys.executable, os.path.join(S, "regional-regress.py"),
                                   os.path.join(ROOT, "regional.json"), "cfg"] + list(countries),
                                  capture_output=True, text=True, env=env)
        finally:
            shutil.rmtree(d)

    def test_md_internal_probe_passes(self):
        self.assertEqual(self.run_with(138.0, 0).returncode, 0)

    def test_a_seam_inside_a_country_fails(self):
        self.assertEqual(self.run_with(138.0, 1).returncode, 1)

    def test_a_lost_region_fails(self):
        self.assertEqual(self.run_with(138.0, 0, lost=1).returncode, 1)

    def test_distance_out_of_tolerance_fails(self):
        self.assertEqual(self.run_with(500.0, 0).returncode, 1)

    def test_no_probe_for_the_set_fails(self):
        self.assertEqual(self.run_with(1.0, 0, countries=("XX",)).returncode, 1)

    def test_a_portal_rejected_by_the_engine_fails(self):
        r = self.run_with(138.0, 0, engine_line="[INFO] WEDRIVE: регионов 2, порталов 30, отвергнуто 2")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("отверг 2", r.stdout)
        r = self.run_with(138.0, 0, engine_line="[INFO] WEDRIVE: регионов 2, порталов 32, отвергнуто 0")
        self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=1)
