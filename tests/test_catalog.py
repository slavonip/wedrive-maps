# -*- coding: utf-8 -*-
"""The European catalog in regional.json and the matrix bookkeeping (regional-effective.py).
Standard library only.

    python3 tests/test_catalog.py            offline checks
    ONLINE=1 python3 tests/test_catalog.py   also asks Geofabrik for every country's .md5
"""
import importlib.util, json, os, shutil, tempfile, unittest, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "regional.json"), encoding="utf-8"))
spec = importlib.util.spec_from_file_location("eff", os.path.join(ROOT, "scripts", "regional-effective.py"))
eff = importlib.util.module_from_spec(spec); spec.loader.exec_module(eff)


class Catalog(unittest.TestCase):
    def test_ids_unique_and_in_range(self):
        ids = [c["region_id"] for c in CFG["countries"].values()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(1 <= i <= 255 for i in ids))

    def test_first_five_keep_their_ids(self):
        self.assertEqual({k: CFG["countries"][k]["region_id"] for k in ("MD", "RO", "HU", "AT", "DE")},
                         {"MD": 1, "RO": 2, "HU": 3, "AT": 4, "DE": 5})

    def test_every_country_has_a_polygon_and_a_path(self):
        for code, c in CFG["countries"].items():
            self.assertTrue(os.path.isfile(os.path.join(ROOT, c["polygon"])), code)
            g = json.load(open(os.path.join(ROOT, c["polygon"]), encoding="utf-8"))
            self.assertEqual(g["geometry"]["type"], "MultiPolygon", code)
            self.assertRegex(c["geofabrik"], r"^[a-z-]+(/[a-z-]+)?$", code)
            w, s, e, n = c["bbox"]
            self.assertTrue(-180 <= w < e <= 180 and -90 <= s < n <= 90, code)

    def test_russia_and_the_special_cases(self):
        self.assertEqual(CFG["countries"]["RU"]["geofabrik"], "russia")
        self.assertEqual(CFG["countries"]["GB"]["geofabrik"], "europe/great-britain")
        self.assertEqual(CFG["countries"]["IE"]["geofabrik"], "europe/ireland-and-northern-ireland")
        paths = [c["geofabrik"] for c in CFG["countries"].values()]
        for agg in ("europe/alps", "europe/dach", "europe/britain-and-ireland", "europe/united-kingdom"):
            self.assertNotIn(agg, paths)
        self.assertEqual(len(paths), len(set(paths)))

    def test_borders_name_known_countries_and_legacy_four(self):
        for b in CFG["borders"]:
            for c in b["between"]:
                self.assertIn(c, CFG["countries"])
        legacy = sorted("-".join(b["between"]) for b in CFG["borders"] if b.get("legacy"))
        self.assertEqual(legacy, ["AT-DE", "HU-AT", "MD-RO", "RO-HU"])
        self.assertTrue(all(b.get("required") for b in CFG["borders"] if b.get("legacy")))

    @unittest.skipUnless(os.environ.get("ONLINE"), "ONLINE=1 to ask Geofabrik")
    def test_every_source_exists(self):
        for code, c in CFG["countries"].items():
            url = "https://download.geofabrik.de/%s-latest.osm.pbf.md5" % c["geofabrik"]
            with urllib.request.urlopen(url, timeout=60) as r:
                self.assertEqual(len(r.read().decode().split()[0]), 32, code)


class Effective(unittest.TestCase):
    def setUp(self):
        self.w = tempfile.mkdtemp(); self.b = os.path.join(self.w, "basemaps"); os.makedirs(self.b)

    def tearDown(self):
        shutil.rmtree(self.w)

    def put(self, code, graph=True, base=True):
        low = code.lower()
        if graph:
            open(os.path.join(self.w, "%s.tar.gz" % low), "w").write("x")
            open(os.path.join(self.w, "%s.frontier" % low), "w").write("x")
            open(os.path.join(self.w, "%s.features" % low), "w").write("x")
            json.dump({"code": code, "package": "%s.tar.gz" % low, "frontier": {"file": "%s.frontier" % low},
                       "features": {"file": "%s.features" % low}},
                      open(os.path.join(self.w, "%s.package.json" % low), "w"))
        if base:
            open(os.path.join(self.b, "%s.basemap-desc.json" % low), "w").write("{}")

    def test_both_halves_rebuild_else_carry_or_absent(self):
        self.put("MD"); self.put("RO", base=False); self.put("IT", graph=False); self.put("FR", base=False)
        man = {"regions": {"RO": {"graph_version": "2026-09-25"}}}
        ok, carried, absent = eff.effective(self.w, self.b, man, ["MD", "RO", "IT", "FR"], ["HU"])
        self.assertEqual(ok, ["MD"])
        self.assertEqual(carried, ["HU", "RO"])          # RO failed but was published: carried
        self.assertEqual(sorted(absent), ["FR", "IT"])  # never published: absent
        # the half that succeeded is gone, so nothing picks it up by mistake
        self.assertFalse(os.path.exists(os.path.join(self.w, "ro.package.json")))
        self.assertFalse(os.path.exists(os.path.join(self.w, "ro.tar.gz")))
        self.assertFalse(os.path.exists(os.path.join(self.b, "it.basemap-desc.json")))
        self.assertTrue(os.path.exists(os.path.join(self.w, "md.package.json")))


if __name__ == "__main__":
    unittest.main()
