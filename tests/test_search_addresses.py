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


if __name__ == "__main__":
    unittest.main()
