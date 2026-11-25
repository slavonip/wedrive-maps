# -*- coding: utf-8 -*-
"""The Europe Lite overlay: the settlements catalogue (lite-places.py) and its manifest sections
(lite-overlay-manifest.py).

    python3 tests/test_lite_overlay.py
"""
import importlib.util, json, os, shutil, sqlite3, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), os.path.join(ROOT, "scripts", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


places = load("lite-places")
overlay = load("lite-overlay-manifest")


def v2_db(path, rows):
    """A minimal /2 country search database: the addr_locality columns lite-places reads."""
    d = sqlite3.connect(path)
    d.execute("CREATE TABLE place(id INTEGER PRIMARY KEY, kind INTEGER, name TEXT, aliases TEXT, lat REAL, lon REAL, "
              "cat TEXT, detail TEXT, pop INTEGER)")
    d.execute("CREATE TABLE addr_locality(id INTEGER PRIMARY KEY, name TEXT, aliases TEXT, kind TEXT, district TEXT, "
              "lat REAL, lon REAL, pop INTEGER, houses INTEGER, commune INTEGER)")
    d.executemany("INSERT INTO addr_locality(name, aliases, kind, district, lat, lon, pop, houses) VALUES (?,?,?,?,?,?,?,0)", rows)
    d.commit(); d.close()


def v1_db(path, rows):
    d = sqlite3.connect(path)
    d.execute("CREATE TABLE place(id INTEGER PRIMARY KEY, kind INTEGER, name TEXT, aliases TEXT, lat REAL, lon REAL, "
              "cat TEXT, detail TEXT, pop INTEGER)")
    d.executemany("INSERT INTO place(kind, name, aliases, lat, lon, pop) VALUES (?,?,?,?,?,?)", rows)
    d.commit(); d.close()


class Catalogue(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.src = os.path.join(self.d, "src"); os.makedirs(self.src)
        v2_db(os.path.join(self.src, "DE.sqlite"), [
            ("München", '["Munich", "Мюнхен"]', "city", "Oberbayern", 48.137, 11.575, 1484226),
            ("Münchenbernsdorf", None, "town", "Greiz", 50.82, 11.93, 2800),
            ("Kleinweiler", None, "hamlet", "Oberallgäu", 47.6, 10.2, 0),          # hamlets are left out
        ])
        v2_db(os.path.join(self.src, "GR.sqlite"), [
            ("Θεσσαλονίκη", '["Thessaloniki", "Салоники"]', "city", "Θεσσαλονίκη", 40.64, 22.94, 790824)])
        v2_db(os.path.join(self.src, "AL.sqlite"), [("Tiranë", '["Tirana"]', "city", "Tiranë", 41.33, 19.82, 557422)])
        v2_db(os.path.join(self.src, "BG.sqlite"), [("Плевен", '["Pleven"]', "city", "Плевен", 43.42, 24.61, 111567),
                                                     ("Плевня", None, "village", "Драма", 41.2, 24.0, 300)])
        v2_db(os.path.join(self.src, "RU.sqlite"), [("Москва", '["Moscow"]', "city", "Москва", 55.75, 37.62, 13000000)])
        v1_db(os.path.join(self.src, "IS.sqlite"), [(0, "Reykjavík", None, 64.14, -21.94, 140000),
                                                     (1, "Laugavegur", None, 64.14, -21.93, 0)])   # streets are not settlements
        regions = {}
        for cc in ("DE", "GR", "AL", "BG", "RU", "IS"):
            f = os.path.join(self.src, cc + ".sqlite")
            regions[cc] = {"search": {"url": "file://nowhere/" + cc, "sha256": places.sha256(f),
                                      "addresses": {"format": "wedrive-address/1" if cc == "IS" else "wedrive-address/2"}}}
        self.manifest = os.path.join(self.d, "manifest.json")
        json.dump({"kind": "regional", "tag": "regional-test", "regions": regions}, open(self.manifest, "w"))
        self.out = os.path.join(self.d, "out")

    def tearDown(self):
        shutil.rmtree(self.d)

    def run_build(self, *extra):
        return subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "lite-places.py"), "build",
                               "--manifest", self.manifest, "--out", self.out, "--local", self.src,
                               "--min-rows", "1"] + list(extra), capture_output=True, text=True)

    def test_builds_the_catalogue_without_ru_and_without_hamlets(self):
        r = self.run_build()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        desc = json.load(open(os.path.join(self.out, "places.json"), encoding="utf-8"))
        self.assertEqual(desc["counts"], {"AL": 1, "BG": 2, "DE": 2, "GR": 1, "IS": 1})
        self.assertEqual(desc["excluded"], ["RU"])
        self.assertEqual(desc["sha256"], places.sha256(os.path.join(self.out, "places.sqlite.gz")))
        self.assertEqual(desc["sqlite_sha256"], places.sha256(os.path.join(self.out, "places.sqlite")))
        d = sqlite3.connect(os.path.join(self.out, "places.sqlite"))
        # population order = docid order, every row a settlement with its country and district
        first = d.execute("SELECT name, cc, district, kind, detail FROM place ORDER BY id LIMIT 1").fetchone()
        self.assertEqual(first, ("München", "DE", "Oberbayern", 0, "city"))
        self.assertEqual(d.execute("SELECT count(*) FROM place WHERE cc = 'RU'").fetchone()[0], 0)
        self.assertEqual(d.execute("SELECT detail FROM place WHERE cc = 'IS'").fetchone()[0], "town")
        # the app's query: an alias in another script finds the row
        hit = d.execute("SELECT p.name FROM place_fts f JOIN place p ON p.id = f.docid WHERE place_fts MATCH 'салоники*'").fetchall()
        self.assertEqual(hit, [("Θεσσαλονίκη",)])

    def test_a_country_that_shrank_below_90_percent_fails_the_build(self):
        prev = os.path.join(self.d, "europe-lite.json")
        json.dump({"places": {"counts": {"DE": 3, "BG": 2}}}, open(prev, "w"))
        r = self.run_build("--previous", prev)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("DE: 2 rows, the previous catalogue had 3", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.out, "places.json")))

    def test_a_failing_probe_fails_the_build(self):
        os.remove(os.path.join(self.src, "AL.sqlite"))
        v2_db(os.path.join(self.src, "AL.sqlite"), [("Durrës", None, "city", "Durrës", 41.3, 19.4, 113000)])
        m = json.load(open(self.manifest))
        m["regions"]["AL"]["search"]["sha256"] = places.sha256(os.path.join(self.src, "AL.sqlite"))
        json.dump(m, open(self.manifest, "w"))
        r = self.run_build()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("probe 'Tirana'", r.stdout)

    def test_gates(self):
        self.assertEqual(places.gates({"MD": 100}, 100, {"MD": 100}, ["RU"], 1), [])
        self.assertTrue(places.gates({"MD": 89}, 89, {"MD": 100}, ["RU"], 1))
        self.assertTrue(places.gates({}, 0, {"MD": 100}, ["RU"], 0))              # a country gone entirely
        self.assertTrue(places.gates({"RU": 5}, 5, None, ["RU"], 1))             # an excluded country present
        self.assertTrue(places.gates({"MD": 10}, 10, None, ["RU"], 300_000))     # too few rows overall


class OverlayManifest(unittest.TestCase):
    OV = {"size": 98400000, "sha256": "a" * 64, "build": "20261124", "date": "2026-11-24", "maxzoom": 9,
          "layers": ["boundaries", "earth", "places", "roads", "water"], "schema": "protomaps-v4"}
    PL = {"bytes": 28013979, "sha256": "b" * 64, "sqlite_bytes": 56709120, "sqlite_sha256": "c" * 64, "rows": 365675,
          "counts": {"MD": 1681}, "kinds": {"city": 1314}, "excluded": ["RU"]}

    def test_sections_point_into_their_own_release_and_leave_the_graph_alone(self):
        d = tempfile.mkdtemp()
        try:
            m = {"kind": "europe-lite", "tag": "europe-lite-2026-09-25", "graph": {"sha256": "f" * 64}}
            paths = [os.path.join(d, n) for n in ("europe-lite.json", "overview.json", "places.json")]
            for p, x in zip(paths, (m, self.OV, self.PL)):
                json.dump(x, open(p, "w"))
            r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "lite-overlay-manifest.py")] + paths +
                               ["slavonip/wedrive-maps", "lite-overlay-2026-11-25"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            out = json.load(open(paths[0]))
            self.assertEqual(out["graph"], m["graph"])
            self.assertEqual(out["tag"], "europe-lite-2026-09-25")
            self.assertEqual(out["overview"]["url"],
                             "https://github.com/slavonip/wedrive-maps/releases/download/lite-overlay-2026-11-25/overview.pmtiles")
            self.assertEqual((out["places"]["file"], out["places"]["sqlite_sha256"], out["places"]["format"]),
                             ("places.sqlite.gz", "c" * 64, "wedrive-lite-places/1"))
            self.assertEqual(overlay.validate(out), [])
        finally:
            shutil.rmtree(d)

    def test_a_bad_tag_is_refused(self):
        with self.assertRaises(SystemExit):
            overlay.sections(self.OV, self.PL, "o/r", "europe-lite-2026-11-25")

    def test_validate_names_what_is_wrong(self):
        ov, pl = overlay.sections(self.OV, self.PL, "o/r", "lite-overlay-2026-11-25")
        pl["sqlite_sha256"] = "x"
        ov["url"] = ov["url"].replace("lite-overlay-2026-11-25", "europe-lite-2026-09-25")
        p = overlay.validate({"overview": ov, "places": pl})
        self.assertTrue(any("places.sqlite_sha256" in x for x in p), p)
        self.assertTrue(any("overview.url" in x for x in p), p)


if __name__ == "__main__":
    unittest.main(verbosity=1)
