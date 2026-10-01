"""search_addresses.py: keys (shared vectors with the app) and one real build over a tiny OSM file.

The integration case builds a PBF with osmium, so it needs osmium on PATH (the precheck installs
it); it covers exactly the owner's list: a building+node duplicate, the same number on different
streets, the same street in two cities, 10A/10a, Alba Iulia/Alba-Iulia, associatedStreet, a number
with no street, and a Russian alias from the street way.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import search_addresses as sa  # noqa: E402
import search_db  # noqa: E402


def esc(v):
    return "".join(c if c.isalnum() or c in "-_.:/" else "%%%x%%" % ord(c) for c in v)


def tags(**kv):
    return "T" + ",".join("%s=%s" % (esc(k.replace("__", ":")), esc(v)) for k, v in kv.items())


class Keys(unittest.TestCase):
    def test_vectors_shared_with_the_app(self):
        v = json.load(open(os.path.join(HERE, "address-keys.json"), encoding="utf-8"))
        for a, b in v["street"]:
            self.assertEqual(sa.street_key(a), b, a)
        for a, b in v["house"]:
            got = sa.house_keys(a)
            self.assertEqual(got[0] if got else None, b, a)

    def test_a_listed_housenumber_is_several_houses(self):
        self.assertEqual(sa.house_keys("10;12"), ["10", "12"])
        self.assertEqual(sa.house_keys("10, 10A"), ["10", "10a"])
        self.assertEqual(sa.house_display("10;12A", "12a"), "12A")


@unittest.skipUnless(shutil.which("osmium"), "needs osmium")
class Build(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        lines = [
            # Chișinău: node + building of the same address -> one
            "n1 v1 x28.8300000 y47.0200000 " + tags(addr__street="Strada București", addr__housenumber="51", addr__city="Chișinău"),
            "n101 v1 x28.8301000 y47.0201000", "n102 v1 x28.8302000 y47.0201000",
            "n103 v1 x28.8302000 y47.0202000",
            # same street name and number in Bălți: a different street
            "n2 v1 x27.9300000 y47.7600000 " + tags(addr__street="Strada București", addr__housenumber="51", addr__city="Bălți"),
            # Alba Iulia / Alba-Iulia: one street
            "n3 v1 x28.7670000 y47.0410000 " + tags(addr__street="Strada Alba Iulia", addr__housenumber="200/5", addr__city="Chișinău"),
            "n4 v1 x28.7680000 y47.0415000 " + tags(addr__street="Strada Alba-Iulia", addr__housenumber="10A", addr__city="Chișinău"),
            # 10a and 10A 600 m apart on one street: two houses (not merged just for the number)
            "n5 v1 x28.8400000 y47.0300000 " + tags(addr__street="Strada Decebal", addr__housenumber="10a", addr__city="Chișinău"),
            "n6 v1 x28.8480000 y47.0300000 " + tags(addr__street="Strada Decebal", addr__housenumber="10A", addr__city="Chișinău"),
            # the same number on a different street: stays on its own street
            "n9 v1 x28.8350000 y47.0250000 " + tags(addr__street="Strada Kiev", addr__housenumber="51", addr__city="Chișinău"),
            # associatedStreet, no city -> the nearest settlement
            "n7 v1 x28.8330000 y47.0240000 " + tags(addr__housenumber="3A"),
            # a number with no street at all
            "n8 v1 x28.9000000 y47.1000000 " + tags(addr__housenumber="5"),
            "n201 v1 x28.8340000 y47.0240000", "n202 v1 x28.8360000 y47.0250000",
            "w10 v1 " + tags(building="yes", addr__street="Strada București", addr__housenumber="51", addr__city="Chișinău")
            + " Nn101,n102,n103,n101",
            "w20 v1 " + tags(highway="residential", name="Strada Kiev", name__ru="улица Киев") + " Nn201,n202",
            "r1 v1 " + tags(type="associatedStreet", name="Strada Kiev") + " Mn7@house,w20@street",
        ]
        opl = os.path.join(self.d, "t.opl")
        with open(opl, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.pbf = os.path.join(self.d, "t.osm.pbf")
        subprocess.run(["osmium", "sort", "-O", "-o", self.pbf, opl], check=True)
        self.db = os.path.join(self.d, "md.sqlite")
        c = sqlite3.connect(self.db)
        c.executescript(search_db.SCHEMA)
        c.executemany("INSERT INTO place(kind,name,lat,lon,pop) VALUES(0,?,?,?,?)",
                      [("Chișinău", 47.0245, 28.8323, 700000), ("Bălți", 47.7617, 27.9289, 100000)])
        c.commit(); c.close()
        self.stats = sa.build(self.pbf, self.db, self.d)
        self.c = sqlite3.connect(self.db)

    def tearDown(self):
        self.c.close()
        shutil.rmtree(self.d)

    def houses(self, street, city):
        return self.c.execute("SELECT a.key, a.osm, a.lat, a.lon FROM addr a JOIN addr_street s ON s.id=a.street "
                              "WHERE s.name=? AND s.city=? ORDER BY a.key, a.osm", (street, city)).fetchall()

    def test_the_owner_cases(self):
        self.assertEqual(self.stats["objects"], 10)
        self.assertEqual(self.stats["no_street"], 1)
        self.assertEqual(self.stats["via_associatedStreet"], 1)
        # node + building -> one house, and it is the NODE (osm = 1*4 + 0)
        self.assertEqual(self.houses("Strada București", "Chișinău"), [("51", 4, 470200000, 288300000)])
        self.assertEqual(len(self.houses("Strada București", "Bălți")), 1)
        # Alba Iulia and Alba-Iulia: one street with both houses
        alba = self.c.execute("SELECT id, name FROM addr_street WHERE name LIKE 'Strada Alba%'").fetchall()
        self.assertEqual(len(alba), 1)
        self.assertEqual([h[0] for h in self.houses(alba[0][1], "Chișinău")], ["10a", "200/5"])
        # 10a and 10A far apart: two houses, one key
        self.assertEqual([h[0] for h in self.houses("Strada Decebal", "Chișinău")], ["10a", "10a"])
        # the display keeps what OSM wrote
        self.assertEqual(sorted(r[0] for r in self.c.execute(
            "SELECT coalesce(num, key) FROM addr a JOIN addr_street s ON s.id=a.street WHERE s.name='Strada Decebal'")),
            ["10A", "10a"])
        # Kiev: its own 51, the associatedStreet house with the inferred city, the Russian alias
        kiev = self.c.execute("SELECT id, city, aliases FROM addr_street WHERE name='Strada Kiev'").fetchall()
        self.assertEqual(len(kiev), 1)
        self.assertEqual(kiev[0][1], "Chișinău")
        self.assertIn("улица Киев", json.loads(kiev[0][2]))
        self.assertEqual([h[0] for h in self.houses("Strada Kiev", "Chișinău")], ["3a", "51"])
        # FTS finds the street by name, by alias and by city
        for q in ("bucuresti", "киев", "chisinau alba"):
            self.assertTrue(self.c.execute("SELECT count(*) FROM addr_street_fts WHERE addr_street_fts MATCH ?",
                                           (q,)).fetchone()[0], q)

    def test_deterministic(self):
        again = os.path.join(self.d, "again.sqlite")
        c = sqlite3.connect(again); c.executescript(search_db.SCHEMA)
        c.executemany("INSERT INTO place(kind,name,lat,lon,pop) VALUES(0,?,?,?,?)",
                      [("Chișinău", 47.0245, 28.8323, 700000), ("Bălți", 47.7617, 27.9289, 100000)])
        c.commit(); c.close()
        sa.build(self.pbf, again, self.d)
        q = "SELECT * FROM addr ORDER BY street, key, osm"
        self.assertEqual(sqlite3.connect(again).execute(q).fetchall(), self.c.execute(q).fetchall())


@unittest.skipUnless(shutil.which("osmium"), "needs osmium")
class Build2(unittest.TestCase):
    """Format /2: localities from the PBF, cut to the country border, streets tied by geography."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        L = []
        # the country: a square 27.0-30.0 x 45.5-48.5 (admin_level=2, ISO MD), and a district
        # (admin_level=4) covering only its western half
        sq = [(1, 27.0, 45.5), (2, 30.0, 45.5), (3, 30.0, 48.5), (4, 27.0, 48.5)]
        dist = [(11, 27.0, 45.5), (12, 28.5, 45.5), (13, 28.5, 48.5), (14, 27.0, 48.5)]
        for i, x, y in sq + dist:
            L.append("n%d v1 x%.7f y%.7f" % (i, x, y))
        L.append("w1 v1 Nn1,n2,n3,n4,n1")
        L.append("w2 v1 Nn11,n12,n13,n14,n11")
        L.append("r1 v1 " + tags(type="boundary", boundary="administrative", admin_level="2", name="Moldova",
                                  **{"ISO3166-1": "MD"}) + " Mw1@outer")
        # the district lists a commune (admin_level=8) as a subarea: tags-filter pulls the member relation in
        # with it, and a commune must never be read as the district (r10 sorts before r2 on purpose)
        L.append("r2 v1 " + tags(type="boundary", boundary="administrative", admin_level="4", name="Raion Vest")
                 + " Mw2@outer,r10@subarea")
        for i, x, y in [(71, 27.8, 46.7), (72, 28.0, 46.7), (73, 28.0, 46.9), (74, 27.8, 46.9)]:
            L.append("n%d v1 x%.7f y%.7f" % (i, x, y))
        L.append("w7 v1 Nn71,n72,n73,n74,n71")
        L.append("r10 v1 " + tags(type="boundary", boundary="administrative", admin_level="8", name="Comuna Hîncești")
                 + " Mw7@outer")
        # places: two villages named Hîncești (west and east), a city Бендеры (ro Bender), a village with its
        # own area (Bubuieci, no node name clash), a village Chițcani, and a FOREIGN town beyond the border
        L += [
            "n20 v1 x27.9000000 y46.8000000 " + tags(place="village", name="Hîncești"),
            "n21 v1 x29.8000000 y46.2000000 " + tags(place="village", name="Hîncești"),
            "n22 v1 x29.4800000 y46.8200000 " + tags(place="city", name="Бендеры", name__ro="Bender", population="97027"),
            "n23 v1 x28.9500000 y47.0000000 " + tags(place="village", name="Bubuieci"),
            "n24 v1 x29.3000000 y46.6000000 " + tags(place="village", name="Chițcani"),
            "n25 v1 x30.5000000 y46.5000000 " + tags(place="town", name="Foreignville"),
            # a town whose point is Romanian and whose area is Russian (Transnistria), and a city with ё
            "n26 v1 x29.0000000 y47.7600000 " + tags(place="town", name="Rîbnița", name__ru="Рыбница"),
            "n27 v1 x28.8300000 y47.0200000 " + tags(place="city", name="Кишинёв"),
        ]
        for i, x, y in [(61, 28.98, 47.74), (62, 29.02, 47.74), (63, 29.02, 47.78), (64, 28.98, 47.78)]:
            L.append("n%d v1 x%.7f y%.7f" % (i, x, y))
        L.append("w5 v1 " + tags(place="town", name="Рыбница") + " Nn61,n62,n63,n64,n61")
        # Bubuieci's own area: 28.93-28.97 x 46.98-47.02
        for i, x, y in [(31, 28.93, 46.98), (32, 28.97, 46.98), (33, 28.97, 47.02), (34, 28.93, 47.02)]:
            L.append("n%d v1 x%.7f y%.7f" % (i, x, y))
        L.append("w3 v1 " + tags(place="village", name="Bubuieci") + " Nn31,n32,n33,n34,n31")
        # houses
        L += [
            # addr:city Hîncești near each village: two different localities
            "n40 v1 x27.9010000 y46.8010000 " + tags(addr__street="Strada Mare", addr__housenumber="1", addr__city="Hîncești"),
            "n41 v1 x29.8010000 y46.2010000 " + tags(addr__street="Strada Mare", addr__housenumber="1", addr__city="Hîncești"),
            # Bender, Cyrillic addr:city; the street way carries name:en
            "n42 v1 x29.4810000 y46.8210000 " + tags(addr__street="Ленинградская улица", addr__housenumber="52", addr__city="Бендеры"),
            "n50 v1 x29.4800000 y46.8200000", "n51 v1 x29.4830000 y46.8230000",
            "w4 v1 " + tags(highway="residential", name="Ленинградская улица", name__en="Leningradskaya Street") + " Nn50,n51",
            # no addr:city, inside Bubuieci's area (nearer to nothing else named)
            "n43 v1 x28.9600000 y46.9900000 " + tags(addr__street="Strada Florilor", addr__housenumber="7"),
            # Chițcani written in Cyrillic on three houses: not a name of it -> learned alias
            "n44 v1 x29.3010000 y46.6010000 " + tags(addr__street="Strada Nouă", addr__housenumber="1", addr__city="Кицканы"),
            "n45 v1 x29.3020000 y46.6010000 " + tags(addr__street="Strada Nouă", addr__housenumber="2", addr__city="Кицканы"),
            "n46 v1 x29.3030000 y46.6010000 " + tags(addr__street="Strada Nouă", addr__housenumber="3", addr__city="Кицканы"),
            # a house beyond the border
            "n47 v1 x30.5010000 y46.5010000 " + tags(addr__street="Foreign Street", addr__housenumber="9", addr__city="Foreignville"),
        ]
        opl = os.path.join(self.d, "t.opl")
        with open(opl, "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
        self.pbf = os.path.join(self.d, "t.osm.pbf")
        subprocess.run(["osmium", "sort", "-O", "-o", self.pbf, opl], check=True)
        self.db = os.path.join(self.d, "md.sqlite")
        c = sqlite3.connect(self.db)
        c.executescript(search_db.SCHEMA)
        c.commit(); c.close()
        self.stats = sa.build(self.pbf, self.db, self.d, country="MD")
        self.c = sqlite3.connect(self.db)

    def tearDown(self):
        self.c.close()
        shutil.rmtree(self.d)

    def street(self, name, locality_name):
        return self.c.execute("SELECT s.id, s.city, l.district FROM addr_street s JOIN addr_locality l ON l.id = s.locality "
                              "WHERE s.name = ? AND l.name = ?", (name, locality_name)).fetchall()

    def test_the_border_cuts_localities_and_houses(self):
        names = [r[0] for r in self.c.execute("SELECT name FROM addr_locality")]
        self.assertNotIn("Foreignville", names)
        self.assertEqual(self.stats["foreign_localities_dropped"], 1)
        self.assertEqual(self.stats["foreign_objects_dropped"], 1)
        self.assertFalse(self.c.execute("SELECT count(*) FROM addr_street WHERE name = 'Foreign Street'").fetchone()[0])

    def test_same_named_localities_are_told_apart_by_geography(self):
        rows = self.c.execute("SELECT s.locality, l.lon, l.district FROM addr_street s JOIN addr_locality l "
                              "ON l.id = s.locality WHERE s.name = 'Strada Mare' ORDER BY l.lon").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0][0], rows[1][0])
        self.assertEqual(rows[0][2], "Raion Vest")      # the western Hîncești has the district
        self.assertEqual(rows[1][2], "")

    def test_a_house_inside_a_locality_area_belongs_to_it(self):
        self.assertEqual(len(self.street("Strada Florilor", "Bubuieci")), 1)
        self.assertGreaterEqual(self.stats["streets_by_area"], 1)

    def test_latin_city_alias_finds_the_street(self):
        q = ("SELECT s.name, s.city FROM addr_street_fts f JOIN addr_street s ON s.id = f.docid "
             "WHERE addr_street_fts MATCH ?")
        self.assertEqual(self.c.execute(q, ("bender* leningradskaya*",)).fetchall(), [("Ленинградская улица", "Бендеры")])
        self.assertEqual(self.c.execute(q, ("бендеры* ленинградская*",)).fetchall(), [("Ленинградская улица", "Бендеры")])

    def test_a_consistent_addr_city_spelling_becomes_an_alias(self):
        aliases = json.loads(self.c.execute("SELECT aliases FROM addr_locality WHERE name = 'Chițcani'").fetchone()[0])
        self.assertIn("Кицканы", aliases)
        self.assertEqual(self.c.execute("SELECT count(*) FROM addr_locality_fts WHERE addr_locality_fts MATCH 'кицканы'")
                         .fetchone()[0], 1)

    def test_an_area_named_in_another_language_is_the_same_place(self):
        rows = self.c.execute("SELECT name, aliases FROM addr_locality WHERE name IN ('Rîbnița', 'Рыбница')").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Rîbnița")
        self.assertIn("Рыбница", json.loads(rows[0][1]))

    def test_yo_is_also_found_as_ye(self):
        self.assertIn("Кишинев", json.loads(self.c.execute("SELECT aliases FROM addr_locality WHERE name = 'Кишинёв'").fetchone()[0]))
        self.assertEqual(self.c.execute("SELECT count(*) FROM addr_locality_fts WHERE addr_locality_fts MATCH 'кишинев*'")
                         .fetchone()[0], 1)

    def test_format_and_the_v1_columns_stay(self):
        meta = dict(self.c.execute("SELECT k, v FROM addr_meta"))
        self.assertEqual(meta["format"], "wedrive-address/2")
        self.assertEqual(int(meta["localities"]), 7)   # two Hîncești, Бендеры, Bubuieci, Chițcani, Rîbnița, Кишинёв
        # a /1 app reads exactly these, by name
        self.c.execute("SELECT id, name, aliases, city, lat, lon, houses FROM addr_street LIMIT 1").fetchall()
        self.assertFalse(self.c.execute("SELECT count(*) FROM addr_street WHERE locality IS NULL").fetchone()[0])


class Build2Commune(unittest.TestCase):
    """Romania's commune level (owner, 2026-10-01): the real Vaslui case "Albești" — one village in
    Comuna Albești, another in Comuna Delești, same județ — told apart by commune, which is context
    only and stays out of the two FTS tables v25 already reads."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        L = []
        box = lambda base, x0, y0, x1, y1: [(base + 1, x0, y0), (base + 2, x1, y0), (base + 3, x1, y1), (base + 4, x0, y1)]
        shapes = {"country": box(100, 27.0, 46.0, 28.0, 47.0), "judet": box(110, 27.0, 46.0, 28.0, 47.0),
                  "albesti": box(120, 27.0, 46.0, 27.5, 47.0), "delesti": box(130, 27.5, 46.0, 28.0, 47.0)}
        for i, (name, pts) in enumerate(shapes.items(), 1):
            for n, x, y in pts:
                L.append("n%d v1 x%.7f y%.7f" % (n, x, y))
            L.append("w%d v1 N%s" % (i, ",".join("n%d" % n for n, _, _ in pts + pts[:1])))
        L.append("r1 v1 " + tags(type="boundary", boundary="administrative", admin_level="2", name="România",
                                  **{"ISO3166-1": "RO"}) + " Mw1@outer")
        # the județ lists its communes as subareas: they must become communes, never the district
        L.append("r2 v1 " + tags(type="boundary", boundary="administrative", admin_level="4", name="Vaslui")
                 + " Mw2@outer,r3@subarea,r4@subarea")
        L.append("r3 v1 " + tags(type="boundary", boundary="administrative", admin_level="8", name="Albești",
                                  official_name="Comuna Albești") + " Mw3@outer")
        L.append("r4 v1 " + tags(type="boundary", boundary="administrative", admin_level="8", name="Delești",
                                  name__prefix="Comuna") + " Mw4@outer")      # no official_name: prefix + name
        L += [
            "n20 v1 x27.2000000 y46.5000000 " + tags(place="village", name="Albești"),
            "n21 v1 x27.8000000 y46.5000000 " + tags(place="village", name="Albești"),
            "n22 v1 x27.3000000 y46.7000000 " + tags(place="village", name="Rădeni"),
            "n40 v1 x27.2010000 y46.5010000 " + tags(addr__street="Strada Principală", addr__housenumber="1", addr__city="Albești"),
            "n41 v1 x27.8010000 y46.5010000 " + tags(addr__street="Strada Principală", addr__housenumber="1", addr__city="Albești"),
            "n42 v1 x27.3010000 y46.7010000 " + tags(addr__street="Strada Școlii", addr__housenumber="2", addr__city="Rădeni"),
            # the real duplicate cases, all in Comuna Albești unless said otherwise (owner, 2026-10-01):
            # Pietrăria - the same wikidata twice, 3 km apart: one place
            "n50 v1 x27.1000000 y46.1000000 " + tags(place="village", name="Pietrăria", wikidata="Q12137206", population="463",
                                                     is_in="Albești;Vaslui;România"),
            "n51 v1 x27.1000000 y46.1270000 " + tags(place="village", name="Pietrăria", wikidata="Q12137206", is_in="Vaslui"),
            # Salcea - an informative point and a bare one 0.5 km away (carrying a Russian name)
            "n52 v1 x27.2000000 y46.2000000 " + tags(place="village", name="Salcea", population="9513"),
            "n53 v1 x27.2000000 y46.2045000 " + tags(place="village", name="Salcea", name__ru="Сэлча"),
            # Poiu - a bare point 5 km from the real hamlet: no evidence either way, stays apart
            "n54 v1 x27.3000000 y46.3000000 " + tags(place="hamlet", name="Poiu", wikidata="Q10816090", population="63"),
            "n55 v1 x27.3000000 y46.3450000 " + tags(place="hamlet", name="Poiu"),
            # Dealu - 1.5 km apart but across the commune border: two places, never merged
            "n56 v1 x27.4900000 y46.4000000 " + tags(place="village", name="Dealu", population="100"),
            "n57 v1 x27.5100000 y46.4000000 " + tags(place="village", name="Dealu"),
            # Câmpulung - the city's point and a bare area of its name whose centroid is 5.8 km off
            "n58 v1 x27.1000000 y46.6000000 " + tags(place="city", name="Câmpulung", wikidata="Q736296", population="43552"),
            # Valea Ștefanului - a near duplicate whose is_in names another commune: merged, reported
            "n59 v1 x27.4000000 y46.8000000 " + tags(place="village", name="Valea Ștefanului", wikidata="Q12087667",
                                                     is_in="Albești;Vaslui;România"),
            "n63 v1 x27.4000000 y46.8100000 " + tags(place="village", name="Valea Ștefanului", is_in="Cozieni;Vaslui;România"),
            # houses: one at the bare Salcea point, one inside the Câmpulung area with no addr:city
            # Vecina sits nearer to the bare Salcea copy than the real Salcea does: a street that was
            # nearest to the copy must follow it to Salcea, not drift to Vecina
            "n64 v1 x27.2000000 y46.2120000 " + tags(place="village", name="Vecina"),
            "n65 v1 x27.2000000 y46.2075000 " + tags(addr__street="Strada Morii", addr__housenumber="3"),
            "n60 v1 x27.2001000 y46.2046000 " + tags(addr__street="Strada Gării", addr__housenumber="1", addr__city="Salcea"),
            "n61 v1 x27.2001000 y46.2001000 " + tags(addr__street="Strada Gării", addr__housenumber="2", addr__city="Salcea"),
            "n62 v1 x27.1500000 y46.6400000 " + tags(addr__street="Strada Mare", addr__housenumber="5"),
        ]
        for n, x, y in box(140, 27.13, 46.62, 27.17, 46.66):
            L.append("n%d v1 x%.7f y%.7f" % (n, x, y))
        L.append("w10 v1 " + tags(place="city", name="Câmpulung") + " Nn141,n142,n143,n144,n141")
        opl = os.path.join(self.d, "t.opl")
        with open(opl, "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
        self.pbf = os.path.join(self.d, "t.osm.pbf")
        subprocess.run(["osmium", "sort", "-O", "-o", self.pbf, opl], check=True)
        self.db = os.path.join(self.d, "ro.sqlite")
        c = sqlite3.connect(self.db)
        c.executescript(search_db.SCHEMA)
        c.commit(); c.close()
        self.stats = sa.build(self.pbf, self.db, self.d, country="RO")
        self.c = sqlite3.connect(self.db)

    def tearDown(self):
        self.c.close()
        shutil.rmtree(self.d)

    def test_same_name_same_district_told_apart_by_commune(self):
        rows = self.c.execute("SELECT l.district, c.display FROM addr_locality l JOIN addr_commune c ON c.id = l.commune "
                              "WHERE l.name = 'Albești' ORDER BY l.lon").fetchall()
        self.assertEqual(rows, [("Vaslui", "Comuna Albești"), ("Vaslui", "Comuna Delești")])

    def test_each_street_belongs_to_its_own_albesti(self):
        rows = self.c.execute("SELECT c.display FROM addr_street s JOIN addr_locality l ON l.id = s.locality "
                              "JOIN addr_commune c ON c.id = l.commune WHERE s.name = 'Strada Principală' "
                              "ORDER BY s.lon").fetchall()
        self.assertEqual(rows, [("Comuna Albești",), ("Comuna Delești",)])

    def test_commune_is_findable_but_stays_out_of_the_v25_fts(self):
        self.assertEqual(self.c.execute("SELECT name FROM addr_commune_fts WHERE addr_commune_fts MATCH 'deles*'")
                         .fetchall(), [("Delești",)])
        # Rădeni lies in Comuna Albești: searching "Albești" must not drag it in (v25 matches every column)
        names = {r[0] for r in self.c.execute("SELECT l.name FROM addr_locality_fts f JOIN addr_locality l "
                                              "ON l.id = f.docid WHERE addr_locality_fts MATCH 'albesti*'")}
        self.assertEqual(names, {"Albești"})
        self.assertFalse(self.c.execute("SELECT count(*) FROM addr_street_fts WHERE addr_street_fts MATCH 'deles*'")
                         .fetchone()[0])

    def count(self, name):
        return self.c.execute("SELECT count(*) FROM addr_locality WHERE name = ?", (name,)).fetchone()[0]

    def test_same_wikidata_merges_beyond_the_distance(self):
        self.assertEqual(self.count("Pietrăria"), 1)
        self.assertEqual(self.c.execute("SELECT pop FROM addr_locality WHERE name = 'Pietrăria'").fetchone()[0], 463)

    def test_a_bare_duplicate_within_2_km_merges_and_brings_its_names_and_houses(self):
        self.assertEqual(self.count("Salcea"), 1)
        lid, pop, aliases = self.c.execute("SELECT id, pop, aliases FROM addr_locality WHERE name = 'Salcea'").fetchone()
        self.assertEqual(pop, 9513)
        self.assertIn("Сэлча", json.loads(aliases))
        rows = self.c.execute("SELECT s.locality, s.houses FROM addr_street s WHERE s.name = 'Strada Gării'").fetchall()
        self.assertEqual(rows, [(lid, 2)])               # one street, both houses, on the surviving id
        fts = [r[0] for r in self.c.execute("SELECT docid FROM addr_locality_fts WHERE addr_locality_fts MATCH 'сэлча'")]
        self.assertEqual(fts, [lid])                     # no stale duplicate left in the FTS

    def test_same_name_same_commune_beyond_2_km_without_evidence_stays_apart(self):
        self.assertEqual(self.count("Poiu"), 2)

    def test_same_name_in_different_communes_never_merges(self):
        self.assertEqual(self.count("Dealu"), 2)
        self.assertEqual(self.count("Albești"), 2)

    def test_a_bare_area_of_the_name_is_the_place_however_far_its_centroid(self):
        self.assertEqual(self.count("Câmpulung"), 1)
        lid = self.c.execute("SELECT id FROM addr_locality WHERE name = 'Câmpulung'").fetchone()[0]
        self.assertEqual(self.c.execute("SELECT locality FROM addr_street WHERE name = 'Strada Mare'").fetchone()[0], lid)

    def test_a_street_nearest_to_the_removed_copy_follows_it_to_the_survivor(self):
        self.assertEqual(self.c.execute("SELECT l.name FROM addr_street s JOIN addr_locality l ON l.id = s.locality "
                                        "WHERE s.name = 'Strada Morii'").fetchone()[0], "Salcea")

    def test_conflicting_is_in_is_merged_and_reported(self):
        # Valea Ștefanului (Albești vs Cozieni) is a conflict; Pietrăria ("Vaslui" inside
        # "Albești;Vaslui;România") is only coarser and is not

        self.assertEqual(self.count("Valea Ștefanului"), 1)
        self.assertEqual(self.stats["merged_with_conflicting_is_in"], 1)

    def test_the_locality_count_moves_exactly_by_the_merges(self):
        # Albești 2, Rădeni, Pietrăria, Salcea, Vecina, Poiu 2, Dealu 2, Câmpulung, Valea Ștefanului = 12
        self.assertEqual(self.c.execute("SELECT count(*) FROM addr_locality").fetchone()[0], 12)
        self.assertEqual((self.stats["duplicates_merged_wikidata"], self.stats["duplicates_merged_near"],
                          self.stats["duplicates_merged_area"]), (1, 2, 1))

    def test_no_commune_level_outside_romania_and_v25_columns_stay(self):
        meta = dict(self.c.execute("SELECT k, v FROM addr_meta"))
        self.assertEqual(int(meta["communes"]), 2)
        self.c.execute("SELECT id, name, aliases, kind, district, lat, lon, pop, houses FROM addr_locality").fetchall()


if __name__ == "__main__":
    unittest.main()
