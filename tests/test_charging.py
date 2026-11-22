# -*- coding: utf-8 -*-
"""Tests for the charging catalogue (wedrive-charging/1). Offline: every test runs on the fixtures in
tests/fixtures/charging/ or on a local fake HTTP server. Standard library only.

    python3 tests/test_charging.py

The fixtures: tests/fixtures/charging/ocm/ — 39 OCM records from the public openchargemap/ocm-export
(MD, RO, NL, DE; providers 1, 26, 29, 37, 38) plus hand-made ones (provider 15, status 200/210, a
broken connector, an unknown connection type, an EVSE id), and a referencedata.json trimmed to
what they use; tests/fixtures/charging/osm/ — every Moldovan charging station of the Geofabrik
extract of 2026-11-20 (68) plus hand-made lifecycle, socket:* and EVSE records;
dedup-reference.json — 30 hand-checked MD (OCM, OSM) pairs; gate-*.json — previous-release counts.
"""
import gzip, hashlib, http.server, importlib.util, json, os, shutil, socket, sqlite3, subprocess, sys, tempfile, threading, time, unittest
import urllib.parse
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = os.path.join(ROOT, "scripts")
FX = os.path.join(ROOT, "tests", "fixtures", "charging")
sys.path.insert(0, S)
import charging_db as cdb  # noqa: E402


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(S, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ocm = load("charging_ocm_fetch", "charging-ocm-fetch.py")
ovp = load("charging_osm_overpass", "charging-osm-overpass.py")
build = load("charging_build", "charging-build.py")
cman = load("charging_manifest", "charging-manifest.py")
CFG = json.load(open(os.path.join(ROOT, "regional.json"), encoding="utf-8"))
CONNECTORS = json.load(open(os.path.join(ROOT, "charging", "connectors.json"), encoding="utf-8"))
PROVIDERS = json.load(open(os.path.join(ROOT, "charging", "providers.json"), encoding="utf-8"))
SECRET = "k3y-th4t-must-n3ver-leak"
# a fake key with URL-reserved characters, so its url-encoded form differs from it (never a real key)
LEAKY = "Fk8+ocm/T3st=k3y-Zq9xW2Lp"
WINDOW = 8                      # any run of this many characters of the key counts as a leak


def leaks(haystack, key=SECRET):
    """-> every form of `key` found in `haystack` (str or bytes): the whole key, any WINDOW-long
    fragment of it, and the same for its url-encoded forms. Case-insensitive (%2B vs %2b)."""
    if isinstance(haystack, str):
        haystack = haystack.encode("utf-8", "surrogateescape")
    h = haystack.lower()
    found = []
    for form in sorted({key, urllib.parse.quote(key, safe=""), urllib.parse.quote_plus(key)}):
        for i in range(len(form) - WINDOW + 1):
            w = form[i:i + WINDOW]
            if w.lower().encode() in h and w not in found:
                found.append(w)
    return found


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def run_build(out):
    return build.build(os.path.join(ROOT, "regional.json"), os.path.join(FX, "ocm"), os.path.join(FX, "osm"), out)


# ─────────────────────────────── status, usage, connectors (AC-4, AC-5) ───────────────────────────────

class Status(unittest.TestCase):
    def test_every_ocm_status_code(self):
        want = {0: "UNKNOWN", 10: "OPERATIONAL", 20: "OPERATIONAL", 30: "NOT_OPERATIONAL", 50: "OPERATIONAL",
                75: "PARTLY", 100: "NOT_OPERATIONAL", 150: "PLANNED", 200: None, 210: None, None: "UNKNOWN"}
        for code, status in want.items():
            self.assertEqual(ocm.status_of(code)[0], status, code)
        self.assertEqual(ocm.status_of(30)[1], "ocm:30")          # the raw code tells 30 from 100
        self.assertEqual(ocm.status_of(100)[1], "ocm:100")
        self.assertEqual(ocm.status_of(None)[1], "ocm:none")

    def test_every_osm_lifecycle_tag(self):
        cs = {"amenity": "charging_station"}
        cases = [
            (cs, ("UNKNOWN", "osm:present")),
            ({**cs, "operational_status": "closed"}, ("NOT_OPERATIONAL", "osm:operational_status=closed")),
            ({**cs, "operational_status": "out_of_order"}, ("NOT_OPERATIONAL", "osm:operational_status=out_of_order")),
            ({**cs, "disused": "yes"}, ("NOT_OPERATIONAL", "osm:disused")),
            ({"disused:amenity": "charging_station"}, ("NOT_OPERATIONAL", "osm:disused")),
            ({"construction:amenity": "charging_station"}, ("PLANNED", "osm:construction")),
            ({"proposed:amenity": "charging_station"}, ("PLANNED", "osm:proposed")),
            ({"planned:amenity": "charging_station"}, ("PLANNED", "osm:planned")),
            ({**cs, "construction": "yes"}, ("PLANNED", "osm:construction")),
            ({"abandoned:amenity": "charging_station"}, (None, "osm:abandoned")),
            ({"demolished:amenity": "charging_station"}, (None, "osm:demolished")),
            ({"removed:amenity": "charging_station"}, (None, "osm:removed")),
            ({"razed:amenity": "charging_station"}, (None, "osm:razed")),
            ({"amenity": "parking"}, (None, None)),
        ]
        for tags, want in cases:
            self.assertEqual(cdb.osm_status(tags), want, tags)

    def test_a_station_whose_every_connector_is_broken_does_not_work(self):
        # owner, 2026-11-21: OPERATIONAL + every connector NOT_OPERATIONAL = NOT_OPERATIONAL;
        # OPERATIONAL + only some broken stays PARTLY
        ref = json.load(open(os.path.join(FX, "ocm", "referencedata.json"), encoding="utf-8"))

        def station(*codes):
            poi = {"ID": 1, "StatusTypeID": 50, "AddressInfo": {"Latitude": 47.0, "Longitude": 28.85},
                   "Connections": [{"ConnectionTypeID": 33, "StatusTypeID": c} for c in codes]}
            r = ocm.normalise(poi, ref, CONNECTORS, "MD", {})
            return r["status"], r["status_raw"], [c["status"] for c in r["connectors"]]

        self.assertEqual(station(100), ("NOT_OPERATIONAL", "ocm:50", ["NOT_OPERATIONAL"]))
        self.assertEqual(station(100, 30), ("NOT_OPERATIONAL", "ocm:50", ["NOT_OPERATIONAL"] * 2))
        self.assertEqual(station(100, 200)[0], "NOT_OPERATIONAL")         # a removed connector is not counted
        self.assertEqual(station(100, 50)[0], "PARTLY")
        self.assertEqual(station(30, 100, 50)[0], "PARTLY")
        self.assertEqual(station(100, None)[0], "PARTLY")                 # one of unknown status: not all broken
        self.assertEqual(station(50, 50)[0], "OPERATIONAL")
        self.assertEqual(station()[0], "OPERATIONAL")                    # no connectors: the station's own word

    def test_usage(self):
        for t, u in ((1, "PUBLIC"), (4, "PUBLIC_MEMBERSHIP"), (5, "PUBLIC_PAY"), (7, "PUBLIC"), (6, "CUSTOMERS"),
                     (2, "PRIVATE"), (3, "PRIVATE"), (0, "UNKNOWN")):
            self.assertEqual(ocm.USAGE[t], u)
        self.assertEqual(cdb.osm_usage({"access": "customers"}), "CUSTOMERS")
        self.assertEqual(cdb.osm_usage({"access": "private"}), "PRIVATE")
        self.assertEqual(cdb.osm_usage({}), "UNKNOWN")

    def test_every_ocm_connection_type_is_mapped(self):
        ref = json.load(open(os.path.join(FX, "ocm", "referencedata.json"), encoding="utf-8"))
        ids = {str(t["ID"]) for t in ref["ConnectionTypes"]}
        self.assertEqual(len(ids), 43)
        self.assertEqual(ids - set(CONNECTORS["ocm"]), set())
        for std in CONNECTORS["ocm"].values():
            self.assertIn(std, CONNECTORS["standards"])
        for std in CONNECTORS["osm"].values():
            self.assertIn(std, CONNECTORS["standards"])
        self.assertEqual(CONNECTORS["ocm"]["33"], "CCS2")
        self.assertEqual(CONNECTORS["ocm"]["1036"], "TYPE2_TETHERED")
        self.assertEqual(CONNECTORS["ocm"]["1040"], "GBT_DC")

    def test_osm_kw(self):
        self.assertEqual(cdb.osm_kw("22 kW"), 22)
        self.assertEqual(cdb.osm_kw("11 kW;22 kW"), 22)
        self.assertEqual(cdb.osm_kw("11000 W"), 11)
        self.assertEqual(cdb.osm_kw("3,7"), 3.7)
        self.assertIsNone(cdb.osm_kw("yes"))
        self.assertIsNone(cdb.osm_kw("5000 kW"))

    def test_zero_kw_is_null_not_zero(self):
        self.assertIsNone(cdb.osm_kw("0"))
        self.assertIsNone(cdb.osm_kw("0 kW"))
        self.assertIsNone(ocm.kw_of(0))
        self.assertIsNone(ocm.kw_of("0"))
        ref = json.load(open(os.path.join(FX, "ocm", "referencedata.json"), encoding="utf-8"))
        poi = {"ID": 1, "StatusTypeID": 50, "AddressInfo": {"Latitude": 47.0, "Longitude": 28.85},
               "Connections": [{"ConnectionTypeID": 33, "PowerKW": 0}]}
        r = ocm.normalise(poi, ref, CONNECTORS, "MD", {})
        self.assertEqual([c["kw"] for c in r["connectors"]], [None])
        self.assertIsNone(r["max_kw"])
        f = {"id": "n1", "geometry": {"type": "Point", "coordinates": [28.85, 47.0]},
             "properties": {"amenity": "charging_station", "socket:type2": "1", "socket:type2:output": "0 kW",
                            "charging_station:output": "0"}}
        r = cdb.osm_record(f, CONNECTORS, cdb.Countries(CFG, ROOT), {})
        self.assertEqual([c["kw"] for c in r["connectors"]], [None])
        self.assertIsNone(r["max_kw"])
        d = tempfile.mkdtemp()
        try:
            r.update(id=1, source=cdb.OSM_SOURCE_ID)
            p = os.path.join(d, "x.sqlite")
            cdb.write(p, {"format": cdb.FORMAT}, cdb.source_rows(PROVIDERS, set()), [r], {1: 1}, {}, CONNECTORS)
            db = sqlite3.connect(p)
            self.assertEqual(db.execute("SELECT max_kw FROM station").fetchall(), [(None,)])
            self.assertEqual(db.execute("SELECT kw FROM connector").fetchall(), [(None,)])
            db.close()
        finally:
            shutil.rmtree(d)

    def test_addr_country_wins_when_it_names_one_of_ours(self):
        countries = cdb.Countries(CFG, ROOT)
        lat, lon = 47.0245, 28.8323                                       # Chișinău
        self.assertEqual(countries.of(lat, lon), "MD")
        self.assertEqual(countries.of(lat, lon, "RO"), "RO")
        self.assertEqual(countries.of(lat, lon, " ro "), "RO")
        self.assertEqual(countries.of(lat, lon, "UK"), "GB")              # the alias
        self.assertEqual(countries.of(lat, lon, "ZZ"), "MD")              # not ours: the position decides
        f = {"id": "n2", "geometry": {"type": "Point", "coordinates": [lon, lat]},
             "properties": {"amenity": "charging_station", "addr:country": "RO"}}
        self.assertEqual(cdb.osm_record(f, CONNECTORS, countries, {})["country"], "RO")

    def test_an_evse_id_must_look_like_one(self):
        self.assertEqual(cdb.evse_norm("yes"), [])
        self.assertEqual(cdb.evse_norm("1;no; "), [])
        self.assertEqual(cdb.evse_norm("DE*ION*E12345; yes ;de ion e12345"), ["DEIONE12345"])
        self.assertEqual(cdb.evse_id("NL*ALL*E0001*1"), "NLALLE00011")
        self.assertIsNone(cdb.evse_id("yes"))
        # one rule for both sides: the fetcher keeps no copy of its own
        self.assertFalse(hasattr(ocm, "EVSE"))
        ref = json.load(open(os.path.join(FX, "ocm", "referencedata.json"), encoding="utf-8"))
        poi = {"ID": 1, "StatusTypeID": 50, "AddressInfo": {"Latitude": 47.0, "Longitude": 28.85},
               "Connections": [{"ConnectionTypeID": 33, "Reference": "yes"},
                               {"ConnectionTypeID": 33, "Reference": "de*ion*e777"}]}
        self.assertEqual(ocm.normalise(poi, ref, CONNECTORS, "MD", {})["evse_ids"], ["DEIONE777"])


# ─────────────────────────────── the polite fetcher (AC-2) ───────────────────────────────

class FakeOCM:
    """A local /v3 that pages like OCM: greaterthanid + maxresults, sorted by ID."""

    def __init__(self, records=None, script=None, break_order=False, ref=None):
        self.records = records or {}
        self.ref = ref
        self.script = list(script or [])     # status codes to answer before the real answer
        self.break_order = break_order
        self.seen = []                       # (path+query, headers)
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.seen.append((self.path, dict(self.headers)))
                if fake.script:
                    code = fake.script.pop(0)
                    self.send_response(code); self.end_headers(); self.wfile.write(b"no"); return
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                if u.path.endswith("/referencedata"):
                    body = fake.ref or {"Operators": [], "ConnectionTypes": []}
                else:
                    recs = [r for r in fake.records.get(q.get("countrycode"), []) if r["ID"] > int(q["greaterthanid"])]
                    body = recs[:int(q["maxresults"])]
                    if fake.break_order and len(body) > 1:
                        body = [body[1], body[0]] + body[2:]
                data = gzip.compress(json.dumps(body).encode())
                self.send_response(200)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d/v3" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown(); self.httpd.server_close()


class Clock:
    """Fake time: sleep advances it, nothing else does. Records every request's moment."""

    def __init__(self):
        self.t, self.sleeps = 1000.0, []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s); self.t += s


class Fetcher(unittest.TestCase):
    def setUp(self):
        self.page = ocm.PAGE
        ocm.PAGE = 3
        self.clock = Clock()
        self.logs = []

    def tearDown(self):
        ocm.PAGE = self.page

    def client(self, base):
        return ocm.Client(SECRET, base=base, sleep=self.clock.sleep, clock=self.clock.clock, log=self.logs.append)

    def test_pages_by_greaterthanid_until_a_short_page(self):
        recs = [{"ID": i} for i in (5, 9, 12, 20, 21, 30, 31, 40)]
        srv = FakeOCM({"MD": recs})
        try:
            c = self.client(srv.base)
            got, pages = ocm.fetch_country(c, "MD")
        finally:
            srv.close()
        self.assertEqual([r["ID"] for r in got], [5, 9, 12, 20, 21, 30, 31, 40])
        self.assertEqual(pages, 3)                 # 3 + 3 + 2: only the last page is short
        after = [dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(p).query))["greaterthanid"] for p, _ in srv.seen]
        self.assertEqual(after, ["0", "12", "30"])
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(srv.seen[0][0]).query))
        self.assertEqual(q["sortby"], "id_asc"); self.assertEqual(q["compact"], "true")
        self.assertEqual(q["verbose"], "false"); self.assertEqual(q["maxresults"], "3")

    def test_an_exact_multiple_ends_on_an_empty_page(self):
        srv = FakeOCM({"MD": [{"ID": i} for i in (1, 2, 3)]})
        try:
            got, pages = ocm.fetch_country(self.client(srv.base), "MD")
        finally:
            srv.close()
        self.assertEqual((len(got), pages), (3, 2))

    def test_ids_out_of_order_fail(self):
        srv = FakeOCM({"MD": [{"ID": i} for i in (1, 2, 3, 4)]}, break_order=True)
        try:
            with self.assertRaises(ocm.FetchError):
                ocm.fetch_country(self.client(srv.base), "MD")
        finally:
            srv.close()

    def test_a_duplicate_id_across_pages_fails(self):
        srv = FakeOCM({"MD": [{"ID": i} for i in (1, 2, 3)]})
        try:
            c = self.client(srv.base)
            orig = c.get
            c.get = lambda path, params=None: [{"ID": 3}, {"ID": 4}] if dict(params)["greaterthanid"] == "3" else orig(path, params)
            with self.assertRaises(ocm.FetchError):
                ocm.fetch_country(c, "MD")
        finally:
            srv.close()

    def test_the_key_is_a_header_and_never_in_a_url(self):
        srv = FakeOCM({"MD": [{"ID": 1}]}, script=[503])      # one retry: its log line must not leak either
        try:
            c = self.client(srv.base)
            ocm.fetch_country(c, "MD")
        finally:
            srv.close()
        self.assertEqual(len(srv.seen), 2)
        for path, headers in srv.seen:
            self.assertNotIn(SECRET, path)
            self.assertNotIn("key=", path.lower())
            h = {k.lower(): v for k, v in headers.items()}
            self.assertEqual(h.get("x-api-key"), SECRET)
            self.assertEqual(h.get("user-agent"), ocm.USER_AGENT)
        for u in c.urls:
            self.assertNotIn(SECRET, u)
        self.assertTrue(any("retry in" in l for l in self.logs), self.logs)
        self.assertFalse(any(SECRET in l for l in self.logs))
        # no fragment and no url-encoded form either, anywhere the key could surface
        self.assertEqual(leaks("\n".join(p for p, _ in srv.seen)), [])
        self.assertEqual(leaks("\n".join(c.urls)), [])
        self.assertEqual(leaks("\n".join(self.logs)), [])

    def test_the_leak_check_sees_fragments_and_encoded_forms(self):
        # the guard itself: a check that cannot see a leak passes for the wrong reason
        self.assertEqual(leaks("nothing here"), [])
        self.assertTrue(leaks("x" + SECRET + "y"))
        self.assertTrue(leaks("...th4t-mus..."))                          # 8 characters out of the middle
        self.assertEqual(leaks("th4t-mu"), [])                           # 7 is below the window
        self.assertTrue(leaks(b"\x00\x01" + SECRET[5:15].encode() + b"\xff"))
        self.assertTrue(leaks(urllib.parse.quote(LEAKY, safe=""), LEAKY))
        self.assertTrue(leaks(urllib.parse.quote(LEAKY, safe="").lower(), LEAKY))
        self.assertTrue(leaks(urllib.parse.quote_plus(LEAKY)[6:16], LEAKY))

    def test_a_url_carrying_the_key_is_refused_before_any_request(self):
        srv = FakeOCM({"MD": [{"ID": 1}]})                    # the server is UP: only the guard can refuse
        try:
            c = self.client(srv.base)
            for params in ([("key", SECRET)], [("apikey", "x")], [("q", SECRET)]):
                with self.assertRaises(ocm.FetchError) as e:
                    c.get("/poi", params)
                self.assertIn("refusing", str(e.exception))
            self.assertEqual((srv.seen, c.requests), ([], 0))
        finally:
            srv.close()

    def test_max_requests_stops_a_runaway_paging(self):
        recs = [{"ID": i} for i in range(1, 31)]               # 10 full pages of 3
        srv = FakeOCM({"MD": recs})
        old = ocm.MAX_REQUESTS
        ocm.MAX_REQUESTS = 4
        try:
            with self.assertRaises(ocm.FetchError) as e:
                ocm.fetch_country(self.client(srv.base), "MD")
        finally:
            ocm.MAX_REQUESTS = old
            srv.close()
        self.assertIn("more than 4 requests", str(e.exception))
        self.assertEqual(len(srv.seen), 4)
        self.assertEqual(ocm.MAX_REQUESTS, 200)

    def test_at_least_a_second_between_requests(self):
        recs = [{"ID": i} for i in range(1, 11)]
        srv = FakeOCM({"MD": recs}, script=[503])
        stamps = []
        try:
            c = self.client(srv.base)
            orig_open = c.opener.open

            def open_(req, timeout=None):
                stamps.append(self.clock.t)
                return orig_open(req, timeout=timeout)
            c.opener.open = open_
            ocm.fetch_country(c, "MD")
        finally:
            srv.close()
        self.assertGreaterEqual(len(stamps), 5)
        for a, b in zip(stamps, stamps[1:]):
            self.assertGreaterEqual(b - a, 1.0)

    def test_retry_with_backoff_only_on_429_and_5xx(self):
        srv = FakeOCM({"MD": [{"ID": 1}]}, script=[429, 503, 500])
        try:
            got, _ = ocm.fetch_country(self.client(srv.base), "MD")
        finally:
            srv.close()
        self.assertEqual(len(got), 1)
        self.assertEqual(len(srv.seen), 4)
        self.assertEqual([s for s in self.clock.sleeps if s >= 2], [2, 4, 8])

    def test_403_fails_at_once(self):
        srv = FakeOCM({"MD": [{"ID": 1}]}, script=[403])
        try:
            with self.assertRaises(ocm.FetchError) as e:
                ocm.fetch_country(self.client(srv.base), "MD")
        finally:
            srv.close()
        self.assertEqual(len(srv.seen), 1)
        self.assertEqual([s for s in self.clock.sleeps if s >= 2], [])
        self.assertNotIn(SECRET, str(e.exception))
        self.assertEqual(leaks(str(e.exception)), [])

    def test_other_4xx_fail_at_once(self):
        srv = FakeOCM({"MD": [{"ID": 1}]}, script=[404])
        try:
            with self.assertRaises(ocm.FetchError):
                ocm.fetch_country(self.client(srv.base), "MD")
        finally:
            srv.close()
        self.assertEqual(len(srv.seen), 1)

    def test_persistent_5xx_gives_up_after_2_4_8_16_32(self):
        srv = FakeOCM({"MD": [{"ID": 1}]}, script=[502] * 10)
        try:
            with self.assertRaises(ocm.FetchError):
                ocm.fetch_country(self.client(srv.base), "MD")
        finally:
            srv.close()
        self.assertEqual(len(srv.seen), 6)
        self.assertEqual([s for s in self.clock.sleeps if s >= 2], [2, 4, 8, 16, 32])

    def test_network_errors_are_retried(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        with self.assertRaises(ocm.FetchError):
            ocm.fetch_country(self.client("http://127.0.0.1:%d/v3" % port), "MD")
        self.assertEqual([x for x in self.clock.sleeps if x >= 2], [2, 4, 8, 16, 32])

    def test_no_key_no_request(self):
        for k in ("", "test", "null", None):
            with self.assertRaises(ocm.FetchError):
                ocm.Client(k)

    def test_fetch_writes_sorted_files_and_a_report(self):
        cfg = {"countries": {"MD": {}, "XA": {}, "XG": {}}}
        srv = FakeOCM({"MD": [{"ID": 3, "StatusTypeID": 50, "DataProviderID": 1}, {"ID": 7, "StatusTypeID": 100}],
                       "JE": [{"ID": 11}], "GG": []})
        d = tempfile.mkdtemp()
        try:
            rep = ocm.fetch(cfg, d, self.client(srv.base))
        finally:
            srv.close()
        self.assertEqual(sorted(rep["countries"]), ["GG", "JE", "MD"])    # XA is inside PT
        self.assertEqual(rep["countries"]["MD"]["status"], {"100": 1, "50": 1})
        self.assertEqual((rep["countries"]["MD"]["min_id"], rep["countries"]["MD"]["max_id"]), (3, 7))
        self.assertEqual(rep["requests"], 4)
        self.assertEqual([r["ID"] for r in ocm.read_jsonl_gz(os.path.join(d, "MD.jsonl.gz"))], [3, 7])
        self.assertTrue(os.path.isfile(os.path.join(d, "referencedata.json")))
        self.assertNotIn(SECRET, open(os.path.join(d, "report.json")).read())
        shutil.rmtree(d)

    def test_write_jsonl_gz_is_the_same_bytes_whenever_it_runs(self):
        recs = [{"ID": 3, "b": [1, 2], "a": "Chișinău"}, {"ID": 7, "z": None}]
        d = tempfile.mkdtemp()
        real = time.time
        try:
            a, b = os.path.join(d, "a.jsonl.gz"), os.path.join(d, "b.jsonl.gz")
            ocm.write_jsonl_gz(a, recs)
            # "a day later", and the records' keys in another order: the bytes may not move
            with mock.patch("time.time", lambda: real() + 86400):
                ocm.write_jsonl_gz(b, [dict(reversed(list(r.items()))) for r in recs])
            self.assertEqual(open(a, "rb").read(), open(b, "rb").read())
            head = open(a, "rb").read(10)
            self.assertEqual(head[4:8], b"\0\0\0\0", "gzip mtime is not 0")
            self.assertEqual(head[3] & 0x08, 0, "a file name in the gzip header")
            self.assertEqual(ocm.read_jsonl_gz(a), recs)
        finally:
            shutil.rmtree(d)


# ─────────────────────────────── the build, on the fixtures ───────────────────────────────

class Built(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        cls.desc, cls.rep = run_build(cls.d)
        cls.db = sqlite3.connect(os.path.join(cls.d, "charging.sqlite"))

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        shutil.rmtree(cls.d)

    def q(self, sql, *a):
        return self.db.execute(sql, a).fetchall()

    def station(self, src_id):
        rows = self.q("SELECT id, cluster, status, status_raw, usage, operator, country FROM station WHERE src_id=?", src_id)
        return rows[0] if rows else None

    # AC-4
    def test_removed_and_unpublished_are_gone_and_counted(self):
        self.assertEqual(self.q("SELECT count(*) FROM station WHERE status_raw IN ('ocm:200','ocm:210')"), [(0,)])
        self.assertIsNone(self.station("9000200")); self.assertIsNone(self.station("9000210"))
        self.assertIsNone(self.station("9000001"))                       # SubmissionStatus 1000 (delisted)
        self.assertEqual(self.rep["dropped_removed"], 2)
        self.assertEqual(self.rep["dropped_unpublished"], 1)
        self.assertEqual(self.desc["counts"]["dropped_removed"], 2 + self.rep["dropped_removed_osm"])
        self.assertEqual(self.rep["dropped_removed_osm"], 4)              # abandoned/demolished/removed/razed

    def test_statuses_in_the_output(self):
        raw = dict(self.q("SELECT status_raw, status FROM station GROUP BY status_raw"))
        for code, st in (("ocm:0", "UNKNOWN"), ("ocm:75", "PARTLY"), ("ocm:100", "NOT_OPERATIONAL"),
                         ("ocm:150", "PLANNED"), ("osm:present", "UNKNOWN"), ("osm:disused", "NOT_OPERATIONAL"),
                         ("osm:construction", "PLANNED"), ("osm:proposed", "PLANNED"), ("osm:planned", "PLANNED"),
                         ("osm:operational_status=closed", "NOT_OPERATIONAL")):
            self.assertEqual(raw.get(code), st, code)
        self.assertIn(raw.get("ocm:50"), ("OPERATIONAL", "PARTLY"))

    def test_an_osm_station_without_lifecycle_tags_is_kept_as_unknown(self):
        n = self.q("SELECT count(*) FROM station WHERE status_raw='osm:present' AND status='UNKNOWN'")[0][0]
        self.assertGreaterEqual(n, 68)

    def test_a_broken_connector_keeps_its_status_and_makes_the_station_partly(self):
        sid, _, status, raw, *_ = self.station("9000075")
        self.assertEqual((status, raw), ("PARTLY", "ocm:50"))
        st = sorted(self.q("SELECT standard, status FROM connector WHERE station=?", sid))
        # the fourth connector (status 200, removed) is gone, the broken Type 2 keeps its own status
        self.assertEqual(st, [("CCS2", "OPERATIONAL"), ("OTHER", "OPERATIONAL"), ("TYPE2", "NOT_OPERATIONAL")])
        self.assertEqual(self.rep["connectors_removed"], 1)

    # AC-5
    def test_kw_and_quantity_carried(self):
        self.assertGreaterEqual(self.rep["kw_out"], 0.99 * (self.rep["kw_in"] - 1))   # one fixture kW is 5000
        self.assertEqual(self.rep["kw_in"] - self.rep["kw_out"], 1)
        self.assertGreaterEqual(self.rep["qty_out"], 0.99 * self.rep["qty_in"])
        self.assertGreater(self.rep["kw_in"], 30)

    def test_an_unknown_connection_type_is_other_and_reported(self):
        self.assertEqual(self.rep["unknown_connection_types"], {"9999": 1})
        sid = self.station("9000075")[0]
        self.assertIn(("OTHER",), self.q("SELECT standard FROM connector WHERE station=?", sid))

    def test_osm_sockets_are_carried(self):
        sid = self.station("n9100000001")[0]
        got = sorted(self.q("SELECT standard, current, kw, qty FROM connector WHERE station=?", sid))
        self.assertEqual(got, [("CCS2", "DC", 150.0, 2), ("OTHER", None, None, 1), ("TYPE2", None, 22.0, 4)])
        self.assertEqual(self.q("SELECT max_kw, has_dc, capacity FROM station WHERE id=?", sid), [(150.0, 1, 6)])
        self.assertEqual(self.rep["unknown_osm_sockets"], {"weird_plug": 1})

    # AC-6
    def test_every_station_has_a_licensed_source(self):
        bad = self.q("SELECT count(*) FROM station s LEFT JOIN source o ON o.id=s.source "
                     "WHERE o.id IS NULL OR o.licence='' OR o.attribution=''")
        self.assertEqual(bad, [(0,)])
        self.assertEqual(self.q("SELECT kind, licence FROM source WHERE id=1"), [("osm", "ODbL 1.0")])
        lic = dict(self.q("SELECT provider_id, licence FROM source WHERE kind='ocm'"))
        self.assertEqual(lic[1], "CC BY 4.0"); self.assertEqual(lic[26], "CC BY-NC-SA 3.0")
        self.assertEqual(lic[29], "CC BY 4.0"); self.assertEqual(lic[37], "CC0 1.0"); self.assertEqual(lic[38], "CC0 1.0")

    def test_the_provider_15_record_is_absent(self):
        self.assertIsNone(self.station("9000015"))
        self.assertNotIn(15, [r[0] for r in self.q("SELECT provider_id FROM source")])
        self.assertEqual(self.rep["dropped_licence"], 2)                  # provider 15 + an unknown provider 77
        self.assertEqual(self.rep["unknown_providers"], {"77": 1})

    def test_the_allowlist_is_the_plan(self):
        self.assertEqual(sorted(p["id"] for p in PROVIDERS["excluded"]), [8, 13, 14, 15, 17, 20, 21, 22, 23, 27])
        allow = {p["id"] for p in PROVIDERS["allow"]}
        self.assertTrue({1, 26, 29, 37, 38} <= allow)
        self.assertFalse(allow & {p["id"] for p in PROVIDERS["excluded"]})

    def test_private_is_kept(self):
        self.assertEqual(self.station("9000044")[4], "PRIVATE")           # Private Residence, usage unknown
        self.assertIsNone(self.station("9000044")[5])                    # placeholder operator -> NULL

    def test_placeholders_are_not_operators(self):
        self.assertEqual(self.q("SELECT count(*) FROM station WHERE operator_id IN (1,44,45)"), [(0,)])

    def test_country_of_an_osm_station_near_a_border(self):
        self.assertEqual(self.station("n9100000020")[6], "RO")           # Iași, inside MD's bbox, no addr:country
        self.assertEqual(self.station("n9100000021")[6], "MD")

    # AC-7
    def test_hand_reference_of_30_md_pairs(self):
        ref = json.load(open(os.path.join(FX, "dedup-reference.json"), encoding="utf-8"))["pairs"]
        self.assertEqual(len(ref), 30)
        self.assertEqual(sum(1 for p in ref if p["same"]), 5)
        self.assertGreaterEqual(sum(1 for p in ref if not p["same"] and 50 <= p["m"] <= 500), 10)
        errors = []
        for p in ref:
            a, b = self.station(str(p["ocm"])), self.station(p["osm"])
            self.assertIsNotNone(a, p); self.assertIsNotNone(b, p)
            if (a[1] == b[1]) != p["same"]:
                errors.append(p)
        self.assertLessEqual(len(errors), 1, errors)

    def test_an_evse_id_joins_records_whatever_the_distance(self):
        a, b = self.station("9000300"), self.station("n9100000030")
        self.assertEqual(a[1], b[1])

    def test_no_osm_record_in_two_clusters_and_clusters_are_min_ids(self):
        rows = self.q("SELECT id, cluster FROM station")
        self.assertEqual(len(rows), len({r[0] for r in rows}))
        for i, c in rows:
            self.assertLessEqual(c, i)
            self.assertEqual(self.q("SELECT cluster FROM station WHERE id=?", c), [(c,)])

    def test_gates_pass_on_the_fixture(self):
        problems, notes = cdb.validate(os.path.join(self.d, "charging.sqlite"), os.path.join(self.d, "charging.sqlite.gz"),
                                       CFG, None, {})
        self.assertEqual(problems, [])

    def test_moldova_needs_40_ocm_stations(self):
        problems, _ = cdb.validate(os.path.join(self.d, "charging.sqlite"), os.path.join(self.d, "charging.sqlite.gz"),
                                   CFG, None, cdb.MIN_OCM)
        self.assertTrue(any(p.startswith("MD:") for p in problems), problems)

    def test_descriptor(self):
        d = self.desc
        self.assertEqual(d["format"], "wedrive-charging/1")
        self.assertEqual(d["sha256"], sha(os.path.join(self.d, "charging.sqlite.gz")))
        self.assertEqual(d["sqlite_sha256"], sha(os.path.join(self.d, "charging.sqlite")))
        self.assertEqual(d["counts"]["stations"], d["counts"]["osm"] + d["counts"]["ocm"])
        self.assertEqual(d["sources"]["osm"][0]["md5"], "53adc01bba1f8fa1824a4d7a808b77f1")
        meta = dict(self.q("SELECT key, value FROM meta"))
        self.assertEqual(meta["format"], "wedrive-charging/1")
        self.assertEqual(meta["ocm_fetched"], "2026-11-21T06:00:00Z")
        self.assertEqual(len(self.q("SELECT id FROM ocm_connection")), 43)


class Dedup(unittest.TestCase):
    """Synthetic stations, metres apart along a meridian at Chișinău's latitude."""

    @staticmethod
    def s(i, kind, metres, operator=None, conns=(), name=None, evse=()):
        return {"id": i, "kind": kind, "lat": 47.0 + metres / 111195.0, "lon": 28.85, "operator": operator,
                "name": name, "connectors": [{"standard": c} for c in conns], "evse_ids": list(evse)}

    def test_near_with_different_operators_and_disjoint_connectors_is_two(self):
        c = cdb.cluster([self.s(1, "osm", 0, "Alpha", ["TYPE2"]), self.s(2, "ocm", 30, "Beta", ["CCS2"])])
        self.assertNotEqual(c[1], c[2])

    def test_near_with_different_operators_is_two(self):
        c = cdb.cluster([self.s(1, "osm", 0, "Alpha", ["TYPE2"]), self.s(2, "ocm", 30, "Beta", ["TYPE2"])])
        self.assertNotEqual(c[1], c[2])

    def test_near_with_disjoint_connectors_is_two(self):
        c = cdb.cluster([self.s(1, "osm", 0, "Alpha", ["TYPE2"]), self.s(2, "ocm", 30, "Alpha", ["CCS2"])])
        self.assertNotEqual(c[1], c[2])

    def test_near_without_conflict_is_one(self):
        c = cdb.cluster([self.s(1, "osm", 0, "IONITY GmbH", ["CCS2"]), self.s(2, "ocm", 30, "Ionity", ["CCS2", "TYPE2"])])
        self.assertEqual(c[1], c[2])

    def test_49_and_51_metres(self):
        self.assertEqual(len(set(cdb.cluster([self.s(1, "osm", 0), self.s(2, "ocm", 49)]).values())), 1)
        self.assertEqual(len(set(cdb.cluster([self.s(1, "osm", 0), self.s(2, "ocm", 51)]).values())), 2)

    def test_50_to_100_needs_the_same_operator_or_name(self):
        self.assertEqual(len(set(cdb.cluster([self.s(1, "osm", 0, "Alpha"), self.s(2, "ocm", 80, "Alpha")]).values())), 1)
        self.assertEqual(len(set(cdb.cluster([self.s(1, "osm", 0, name="Mall X"), self.s(2, "ocm", 80, name="mall x")]).values())), 1)
        self.assertEqual(len(set(cdb.cluster([self.s(1, "osm", 0), self.s(2, "ocm", 80)]).values())), 2)
        self.assertEqual(len(set(cdb.cluster([self.s(1, "osm", 0, "Alpha"), self.s(2, "ocm", 150, "Alpha")]).values())), 2)

    def test_mutual_nearest_one_to_one(self):
        # two OSM records 20 m and 30 m from one OCM record: only the nearer joins it
        c = cdb.cluster([self.s(1, "osm", 0), self.s(2, "osm", 50), self.s(3, "ocm", 20)])
        self.assertEqual(c[1], c[3]); self.assertNotEqual(c[2], c[3])
        # one OSM between two OCM: one pair, the other OCM alone; no chain
        c = cdb.cluster([self.s(1, "osm", 0), self.s(2, "ocm", -10), self.s(3, "ocm", 25)])
        self.assertEqual(c[1], c[2]); self.assertNotEqual(c[3], c[1])

    def test_ocm_ocm_and_osm_osm_are_never_joined_by_distance(self):
        self.assertEqual(len(set(cdb.cluster([self.s(1, "ocm", 0), self.s(2, "ocm", 5)]).values())), 2)
        self.assertEqual(len(set(cdb.cluster([self.s(1, "osm", 0), self.s(2, "osm", 5)]).values())), 2)

    def test_the_same_evse_id_is_one_place(self):
        c = cdb.cluster([self.s(1, "osm", 0, evse=["DEIONE1"]), self.s(2, "ocm", 900, evse=["DEIONE1"])])
        self.assertEqual(c[1], c[2])

    def test_osm_ref_eu_evse_yes_in_two_countries_is_two_places(self):
        countries = cdb.Countries(CFG, ROOT)
        feats = [{"id": "n1", "geometry": {"type": "Point", "coordinates": [28.85, 47.0]},          # MD
                  "properties": {"amenity": "charging_station", "ref:EU:EVSE": "yes"}},
                 {"id": "n2", "geometry": {"type": "Point", "coordinates": [8.68, 50.11]},           # DE
                  "properties": {"amenity": "charging_station", "ref:EU:EVSE": "yes"}}]
        recs = [cdb.osm_record(f, CONNECTORS, countries, {}) for f in feats]
        self.assertEqual([r["country"] for r in recs], ["MD", "DE"])
        self.assertEqual([r["evse_ids"] for r in recs], [[], []])
        for i, r in enumerate(recs, 1):
            r["id"] = i
        c = cdb.cluster(recs)
        self.assertNotEqual(c[1], c[2])

    def test_an_evse_id_never_joins_two_osm_or_two_ocm_records(self):
        e = ["DEIONE1"]
        c = cdb.cluster([self.s(1, "osm", 0, evse=e), self.s(2, "osm", 5000, evse=e)])
        self.assertNotEqual(c[1], c[2])
        c = cdb.cluster([self.s(1, "ocm", 0, evse=e), self.s(2, "ocm", 5000, evse=e)])
        self.assertNotEqual(c[1], c[2])
        # two OSM and two OCM sharing one id: two pairs, one to one, in id order
        c = cdb.cluster([self.s(1, "osm", 0, evse=e), self.s(2, "osm", 5000, evse=e),
                         self.s(3, "ocm", 9000, evse=e), self.s(4, "ocm", 20000, evse=e)])
        self.assertEqual(c[1], c[3]); self.assertNotEqual(c[2], c[1]); self.assertNotEqual(c[4], c[1])
        # one OSM with two ids, each held by another OCM record: only one of them joins it
        c = cdb.cluster([self.s(1, "osm", 0, evse=["DEIONE1", "DEIONE2"]),
                         self.s(2, "ocm", 9000, evse=["DEIONE1"]), self.s(3, "ocm", 20000, evse=["DEIONE2"])])
        self.assertEqual(c[1], c[2]); self.assertNotEqual(c[3], c[1])

    def test_an_evse_pair_takes_no_spatial_partner_too(self):
        # OSM 1 and OCM 2 are one place by EVSE; OSM 3 sits 10 m from OCM 2 — it must not join that cluster
        c = cdb.cluster([self.s(1, "osm", 0, evse=["DEIONE1"]), self.s(2, "ocm", 5000, evse=["DEIONE1"]),
                         self.s(3, "osm", 5010)])
        self.assertEqual(c[1], c[2]); self.assertNotEqual(c[3], c[2])
        # and the other way: OCM 3 10 m from OSM 1
        c = cdb.cluster([self.s(1, "osm", 0, evse=["DEIONE1"]), self.s(2, "ocm", 5000, evse=["DEIONE1"]),
                         self.s(3, "ocm", 10)])
        self.assertEqual(c[1], c[2]); self.assertNotEqual(c[3], c[1])


class Deterministic(unittest.TestCase):
    # AC-8
    def test_two_builds_are_the_same_bytes(self):
        a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
        real = time.time
        try:
            run_build(a)
            # the second build runs "a day later": nothing from the build's clock may reach the bytes
            with mock.patch("time.time", lambda: real() + 86400):
                run_build(b)
            for f in ("charging.sqlite", "charging.sqlite.gz", "charging.json"):
                self.assertEqual(sha(os.path.join(a, f)), sha(os.path.join(b, f)), f)
            for d in (a, b):
                head = open(os.path.join(d, "charging.sqlite.gz"), "rb").read(10)
                self.assertEqual(head[:3], b"\x1f\x8b\x08")
                self.assertEqual(head[3] & 0x08, 0, "a file name in the gzip header")
                self.assertEqual(head[4:8], b"\0\0\0\0", "gzip mtime is not 0")
        finally:
            shutil.rmtree(a); shutil.rmtree(b)


class Gates(unittest.TestCase):
    """Each validator gate, on a copy of the fixture build broken in exactly one way."""

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        run_build(cls.d)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d)

    def broken(self, *sql, schema=False):
        p = os.path.join(self.d, "broken.sqlite")
        shutil.copy(os.path.join(self.d, "charging.sqlite"), p)
        db = sqlite3.connect(p)
        if schema:
            db.execute("PRAGMA writable_schema=ON")
        for s in sql:
            db.execute(s)
        db.commit(); db.close()
        problems, _ = cdb.validate(p, os.path.join(self.d, "charging.sqlite.gz"), CFG, None, {})
        os.remove(p)
        return problems

    def assertOnly(self, problems, start):
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith(start), problems)

    def test_the_unbroken_copy_passes(self):
        self.assertEqual(self.broken(), [])

    def test_integrity(self):
        # the index's btree holds (lat, lon) while its schema now says (lon, lat): every row is missing from it
        self.assertOnly(self.broken("UPDATE sqlite_master SET sql='CREATE INDEX station_pos ON station(lon, lat)' "
                                    "WHERE name='station_pos'", schema=True), "integrity_check failed")

    def test_enums(self):
        self.assertOnly(self.broken("UPDATE station SET usage='FREE' WHERE id=1"), "station.usage outside the enum")
        self.assertOnly(self.broken("UPDATE station SET status='GONE' WHERE id=1"), "station.status outside the enum")
        self.assertOnly(self.broken("UPDATE connector SET status='GONE' WHERE rowid=1"), "connector.status outside the enum")

    def test_removed_ocm_records_are_never_stored(self):
        for code in ("200", "210"):
            self.assertOnly(self.broken("UPDATE station SET status_raw='ocm:%s' WHERE id=1" % code),
                            "1 removed (OCM 200/210)")

    def test_meta_format(self):
        self.assertOnly(self.broken("UPDATE meta SET value='wedrive-charging/0' WHERE key='format'"), "meta.format")
        self.assertOnly(self.broken("DELETE FROM meta WHERE key='format'"), "meta.format")

    def test_a_source_and_src_id_twice(self):
        # the table refuses a duplicate by its UNIQUE constraint, so the copy gets a table without it:
        # the gate must catch a file whose schema lost that guarantee
        db = sqlite3.connect(os.path.join(self.d, "charging.sqlite"))
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='station'").fetchone()[0]
        (a,), (b,) = db.execute("SELECT id FROM station WHERE source=1 ORDER BY id LIMIT 2").fetchall()
        db.close()
        loose = sql.replace(",\n  UNIQUE(source, src_id))", ")")
        self.assertNotEqual(loose, sql)
        self.assertOnly(self.broken(
            "CREATE TABLE s2 AS SELECT * FROM station", "DROP TABLE station", loose,
            "INSERT INTO station SELECT * FROM s2", "DROP TABLE s2",
            "CREATE INDEX station_pos ON station(lat, lon)", "CREATE INDEX station_cluster ON station(cluster)",
            "UPDATE station SET src_id=(SELECT src_id FROM station WHERE id=%d) WHERE id=%d" % (b, a)),
            "a (source, src_id) appears twice")

    def test_a_source_without_licence_or_attribution(self):
        src = "(SELECT min(id) FROM source WHERE kind='ocm')"
        for col in ("licence", "attribution"):
            for v in ("", "   "):
                self.assertOnly(self.broken("UPDATE source SET %s='%s' WHERE id=%s" % (col, v, src)),
                                "a source without licence or attribution")

    def test_the_osm_source_must_be_odbl(self):
        for v in ("CC BY 4.0", "ODbL", "odbl 1.0"):
            self.assertOnly(self.broken("UPDATE source SET licence='%s' WHERE id=%d" % (v, cdb.OSM_SOURCE_ID)),
                            "the OSM source row is not ODbL")

    def test_kw_range(self):
        for sql in ("UPDATE station SET max_kw=0 WHERE id=1", "UPDATE station SET max_kw=1000.5 WHERE id=1",
                    "UPDATE connector SET kw=0 WHERE rowid=1", "UPDATE connector SET kw=-3 WHERE rowid=1",
                    "UPDATE connector SET kw=5000 WHERE rowid=1"):
            self.assertOnly(self.broken(sql), "1 kW values outside")
        self.assertEqual(self.broken("UPDATE station SET max_kw=1000 WHERE id=1",
                                     "UPDATE connector SET kw=NULL WHERE rowid=1"), [])


class NoKeyAnywhere(unittest.TestCase):
    """AC-2 end to end, through the same CLIs the workflow runs: fetch (key in the environment) ->
    build -> validate -> manifest. Every artifact (plain and gunzipped), every log line and every
    URL the server saw is searched for the key, any 8-character fragment of it and its
    url-encoded forms. The key is a fake one; a real key is never read here."""

    def test_no_fragment_of_the_key_reaches_any_artifact_or_log(self):
        recs = {}
        for cc in ("MD", "RO", "NL", "DE"):
            rs = [json.loads(l) for l in open(os.path.join(FX, "ocm", cc + ".jsonl"), encoding="utf-8") if l.strip()]
            recs[cc] = sorted(rs, key=lambda r: r["ID"])
        ref = json.load(open(os.path.join(FX, "ocm", "referencedata.json"), encoding="utf-8"))
        srv = FakeOCM(recs, script=[503], ref=ref)                      # one retry: its log line too
        d = tempfile.mkdtemp()
        logs = []
        try:
            cfg = os.path.join(d, "cfg.json")
            json.dump({"countries": {cc: {} for cc in recs}}, open(cfg, "w"))
            env = dict(os.environ, OCM_API_KEY=LEAKY, OCM_BASE=srv.base, PYTHONIOENCODING="utf-8")

            def run(*args, ok=True):
                p = subprocess.run([sys.executable] + list(args), env=env, capture_output=True, cwd=ROOT)
                logs.append(p.stdout + p.stderr)
                if ok:
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

            run(os.path.join(S, "charging-ocm-fetch.py"), cfg, os.path.join(d, "ocm"))
            out = os.path.join(d, "out")
            run(os.path.join(S, "charging-build.py"), "build", os.path.join(ROOT, "regional.json"),
                os.path.join(d, "ocm"), os.path.join(FX, "osm"), out)
            run(os.path.join(S, "charging-build.py"), "validate", out, os.path.join(ROOT, "regional.json"), "-",
                "--no-min-ocm", ok=False)
            man = os.path.join(d, "manifest.json")
            json.dump({"kind": "regional", "tag": "regional-x", "regions": {}}, open(man, "w"))
            run(os.path.join(S, "charging-manifest.py"), man, os.path.join(out, "charging.json"),
                "https://example.invalid/charging.sqlite.gz")
            self.assertTrue(json.load(open(man))["charging"])
        finally:
            srv.close()
        try:
            self.assertGreaterEqual(len(srv.seen), 6)                    # referencedata, a retry, 4 countries
            self.assertTrue(any(b"retry in" in l for l in logs), logs)
            hits = {}
            for i, l in enumerate(logs):
                if leaks(l, LEAKY):
                    hits["log %d" % i] = leaks(l, LEAKY)
            seen = "\n".join(path for path, _ in srv.seen)
            if leaks(seen, LEAKY):
                hits["request URLs"] = leaks(seen, LEAKY)
            names = set()
            for base, _, files in os.walk(d):
                for n in files:
                    p = os.path.join(base, n)
                    names.add(os.path.relpath(p, d).replace(os.sep, "/"))
                    data = open(p, "rb").read()
                    if n.endswith(".gz"):
                        data += gzip.decompress(data)
                    if n.endswith(".sqlite"):
                        db = sqlite3.connect(p)
                        data += "\n".join(db.iterdump()).encode("utf-8")
                        db.close()
                    if leaks(data, LEAKY):
                        hits[os.path.relpath(p, d)] = leaks(data, LEAKY)
            # the artifacts this gate is about must all have been produced and searched
            for want in ("ocm/MD.jsonl.gz", "ocm/DE.jsonl.gz", "ocm/report.json", "ocm/referencedata.json",
                         "out/charging.sqlite", "out/charging.sqlite.gz", "out/charging.json", "out/report.json",
                         "manifest.json"):
                self.assertIn(want, names)
            self.assertEqual(hits, {})
        finally:
            shutil.rmtree(d)


class PreviousRelease(unittest.TestCase):
    # AC-10
    def gate(self, name):
        g = json.load(open(os.path.join(FX, name), encoding="utf-8"))
        return cdb.previous_gate(g["current"], g["previous"])

    def test_minus_30_percent_fails(self):
        self.assertTrue(self.gate("gate-minus30.json"))

    def test_minus_10_percent_passes(self):
        self.assertEqual(self.gate("gate-minus10.json"), [])

    def test_small_countries_may_swing(self):
        self.assertEqual(cdb.previous_gate({"MD": 20, "DE": 1000}, {"MD": 40, "DE": 1000}), [])

    def test_the_total_may_not_fall_below_90_percent(self):
        prev = {"C%d" % i: 40 for i in range(10)}
        cur = dict(prev, C0=0, C1=0)                                    # 320/400 = 80 %
        self.assertTrue(cdb.previous_gate(cur, prev))
        self.assertEqual(cdb.previous_gate(dict(prev, C0=4), prev), [])     # 364/400 = 91 %

    def test_validate_reads_the_previous_manifest(self):
        d = tempfile.mkdtemp()
        try:
            desc, _ = run_build(d)
            nl = desc["counts"]["ocm_by_country"]["NL"]
            for share, fails in ((0.7, True), (0.9, False)):
                prev = {"charging": {"counts": {"ocm_by_country": dict(desc["counts"]["ocm_by_country"])}}}
                # previous NL = what the current NL count is `share` of; small countries get a floor
                prev["charging"]["counts"]["ocm_by_country"]["NL"] = int(round(max(nl, 50) / share))
                cur_nl = max(nl, 50)
                p = os.path.join(d, "prev.json")
                json.dump(prev, open(p, "w"))
                got = cdb.previous_gate(dict(desc["counts"]["ocm_by_country"], NL=cur_nl),
                                        build.previous_counts(p)[0])
                self.assertEqual(bool(got), fails, (share, got))
        finally:
            shutil.rmtree(d)

    def test_size_limits_stop_the_run(self):
        d = tempfile.mkdtemp()
        old = cdb.MAX_GZ
        try:
            run_build(d)
            cdb.MAX_GZ = 10
            problems, _ = cdb.validate(os.path.join(d, "charging.sqlite"), os.path.join(d, "charging.sqlite.gz"), CFG, None, {})
            self.assertTrue(any(p.startswith("STOP") for p in problems))
        finally:
            cdb.MAX_GZ = old
            shutil.rmtree(d)


class Manifest(unittest.TestCase):
    # AC-11 (the script side; the published URL is checked by the workflow)
    DESC = {"format": "wedrive-charging/1", "build": "2026-12-06", "file": "charging.sqlite.gz", "bytes": 5,
            "sha256": "a" * 64, "sqlite_bytes": 9, "sqlite_sha256": "b" * 64, "counts": {"stations": 1},
            "sources": {"ocm": {}, "osm": []}}

    def test_only_the_charging_entry_changes(self):
        m = {"kind": "regional", "tag": "regional-x", "min_app": 30, "regions": {"MD": {"url": "u"}},
             "portals": {}, "world": {"url": "w"}}
        url = "https://github.com/o/r/releases/download/charging-2026-12-06/charging.sqlite.gz"
        out = cman.apply(json.loads(json.dumps(m)), self.DESC, url)
        self.assertEqual({k: v for k, v in out.items() if k != "charging"}, m)
        self.assertEqual(out["charging"]["url"], url)
        self.assertEqual(sorted(out["charging"]), sorted(list(cman.REQUIRED) + ["url"]))

    def test_an_incomplete_or_foreign_descriptor_is_refused(self):
        with self.assertRaises(SystemExit):
            cman.apply({"kind": "regional"}, {"format": "wedrive-charging/1"}, "https://x")
        with self.assertRaises(SystemExit):
            cman.apply({"kind": "regional"}, dict(self.DESC, format="other/1"), "https://x")
        with self.assertRaises(SystemExit):
            cman.apply({"kind": "regional"}, self.DESC, "http://x")



class OsmIds(unittest.TestCase):
    """osmium --add-unique-id=type_id with polygon output also emits AREAS ('a' ids): 2*way for a
    closed way, 2*relation+1 for a multipolygon. First real Europe run 2026-11-21 died on them."""

    def test_area_ids_map_back_to_their_osm_object(self):
        self.assertEqual(cdb.osm_id("n5"), "n5")
        self.assertEqual(cdb.osm_id("w45"), "w45")
        self.assertEqual(cdb.osm_id("r6"), "r6")
        self.assertEqual(cdb.osm_id("a90"), "w45")
        self.assertEqual(cdb.osm_id("a13"), "r6")
        for bad in ("", "x1", "a", "w", "n1a", None):
            with self.assertRaises(ValueError):
                cdb.osm_id(bad)

    def test_a_closed_way_seen_as_line_and_as_area_is_one_station_with_the_area(self):
        line = {"type": "Feature", "id": "w45", "properties": {"amenity": "charging_station"},
                "geometry": {"type": "LineString", "coordinates": [[0, 0], [0, 1], [1, 1], [0, 0]]}}
        area = {"type": "Feature", "id": "a90", "properties": {"amenity": "charging_station"},
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [0, 0]]]}}
        node = {"type": "Feature", "id": "n7", "properties": {"amenity": "charging_station"},
                "geometry": {"type": "Point", "coordinates": [2, 2]}}
        rel = {"type": "Feature", "id": "a13", "properties": {"amenity": "charging_station"},
               "geometry": {"type": "MultiPolygon", "coordinates": [[[[3, 3], [3, 4], [4, 4], [3, 3]]]]}}
        d = tempfile.mkdtemp()
        try:
            for order in ([line, area, node, rel], [area, line, node, rel]):
                path = os.path.join(d, "osm.geojsonseq")
                with open(path, "w", encoding="utf-8") as fh:
                    for f in order:
                        fh.write("\x1e" + json.dumps(f) + "\n")
                got = build.read_osm(path)
                self.assertEqual(sorted(f["id"] for f in got), ["n7", "r6", "w45"])
                w = [f for f in got if f["id"] == "w45"][0]
                self.assertEqual(w["geometry"]["type"], "Polygon")
                for f in got:
                    cdb.osm_sort_key(f["id"])        # every id sorts
        finally:
            shutil.rmtree(d, ignore_errors=True)



# ─────────────────────────── OSM through Overpass, per country (option C) ───────────────────────────

NINE = {"amenity", "disused:amenity", "construction:amenity", "proposed:amenity", "planned:amenity",
        "abandoned:amenity", "demolished:amenity", "removed:amenity", "razed:amenity"}
GOOD = (b'<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6" generator="Overpass API">\n'
        b'<meta osm_base="2026-11-22T08:00:00Z"/>\n'
        b'<node id="1" lat="47.0" lon="28.8" version="3" timestamp="2026-01-01T00:00:00Z">'
        b'<tag k="amenity" v="charging_station"/></node>\n'
        b'<node id="2" lat="47.1" lon="28.9" version="1" timestamp="2026-01-01T00:00:00Z"/>\n'
        b'<node id="3" lat="47.2" lon="28.9" version="1" timestamp="2026-01-01T00:00:00Z">'
        b'<tag k="razed:amenity" v="charging_station"/></node>\n'
        b'<way id="4" version="2" timestamp="2026-01-01T00:00:00Z"><nd ref="2"/>'
        b'<tag k="disused:amenity" v="charging_station"/></way>\n</osm>\n')
REMARK = GOOD.replace(b"</osm>", b'<remark> runtime error: Query timed out in "query" at line 3 after 601 seconds. </remark>\n</osm>')
TRUNCATED = GOOD[:GOOD.index(b"<way")]


class FakeOverpass:
    """A local /api/interpreter answering from a script of (status, body), then the last one."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                fake.seen.append((dict(self.headers), urllib.parse.parse_qs(self.rfile.read(n).decode())))
                code, body = fake.script.pop(0) if len(fake.script) > 1 else fake.script[0]
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/api/interpreter" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown(); self.httpd.server_close()


def quiet(*a, **k):
    pass


class OverpassQuery(unittest.TestCase):
    # AC-1
    def keys(self, q):
        import re
        got = set(re.findall(r"nwr\[([a-z:]+)=charging_station\]", q))
        for alt in re.findall(r'nwr\[~"\^\(([^)]*)\):amenity\$"~"\^charging_station\$"\]', q):
            got |= {p + ":amenity" for p in alt.split("|")}
        return got

    def test_the_query_asks_for_exactly_the_nine_keys(self):
        q = ovp.query(ovp.area_clauses("MD", CFG))
        self.assertEqual(self.keys(q), NINE)
        self.assertEqual(set(ovp.KEYS), NINE)
        self.assertTrue(q.startswith("[out:xml][timeout:600];"))
        self.assertIn("(._;>;);", q)
        self.assertIn("out meta;", q)
        self.assertIn("(45.46,26.61,48.49,30.19)", q)           # MD's bbox as (S,W,N,E)
        self.assertNotIn("poly:", q)

    def test_the_osmium_fallback_filter_is_the_same_nine_keys(self):
        self.assertEqual({f.split("/", 1)[1].split("=")[0] for f in ovp.FILTER}, NINE)
        self.assertTrue(all(f.startswith("nwr/") and f.endswith("=charging_station") for f in ovp.FILTER))
        sh = open(os.path.join(S, "charging-osm-extract.sh"), encoding="utf-8").read()
        for k in NINE:
            self.assertIn("nwr/%s=charging_station" % k, sh)

    def test_russia_goes_by_polygon_never_by_bbox(self):
        clauses = ovp.area_clauses("RU", CFG)
        self.assertTrue(clauses)
        self.assertTrue(all(c.startswith('(poly:"') for c in clauses))
        q = ovp.query(clauses)
        self.assertNotIn("-180", q.split("poly:")[0])
        self.assertNotIn("(35.61,-180.0,83.83,180.0)", q)
        self.assertEqual(self.keys(q), NINE)
        for c in clauses:                                      # "lat lon" pairs, lat in range
            nums = [float(x) for x in c[len('(poly:"'):-2].split()]
            self.assertEqual(len(nums) % 2, 0)
            self.assertTrue(all(-90 <= lat <= 90 for lat in nums[0::2]))
            self.assertGreaterEqual(len(nums) // 2, 3)


class OverpassFetch(unittest.TestCase):
    # AC-2
    def test_a_good_answer_is_taken_and_counted(self):
        srv = FakeOverpass([(200, GOOD)])
        d = tempfile.mkdtemp()
        try:
            clock = Clock()
            row = ovp.one("MD", CFG, d, ROOT,
                          overpass_fn=lambda cc, q: ovp.overpass(cc, q, endpoints=(srv.url,), sleep=clock.sleep, log=quiet))
            self.assertEqual(row["url"], srv.url)
            self.assertEqual(row["replication"], "2026-11-22T08:00:00Z")
            self.assertEqual(row["stations"], 3)                     # n1, n3 (razed:), w4 (disused:); not n2
            self.assertIsNone(row["sequence"])
            self.assertEqual(row["bytes"], len(GOOD))
            self.assertEqual(row["md5"], hashlib.md5(GOOD).hexdigest())
            self.assertEqual(row["sha256"], hashlib.sha256(GOOD).hexdigest())
            self.assertEqual(open(os.path.join(d, "MD.osm"), "rb").read(), GOOD)
            headers, form = srv.seen[0]
            self.assertEqual(headers.get("User-Agent"), ovp.USER_AGENT)
            self.assertIn("nwr[amenity=charging_station]", form["data"][0])
            self.assertEqual(clock.sleeps, [])
        finally:
            srv.close(); shutil.rmtree(d)

    def test_remark_504_and_truncated_are_failures_then_the_other_endpoint(self):
        a = FakeOverpass([(200, REMARK), (504, b"busy"), (200, TRUNCATED)])
        b = FakeOverpass([(200, GOOD)])
        clock = Clock()
        try:
            ep, body, base = ovp.overpass("MD", "q", endpoints=(a.url, b.url), attempts=3, sleep=clock.sleep, log=quiet)
            self.assertEqual((ep, body, base), (b.url, GOOD, "2026-11-22T08:00:00Z"))
            self.assertEqual(len(a.seen), 3)
            self.assertEqual(len(b.seen), 1)
            self.assertEqual(clock.sleeps, [ovp.BACKOFF, ovp.BACKOFF * 2])          # exponential, per endpoint
        finally:
            a.close(); b.close()

    def test_a_transfer_cut_short_is_retried_not_a_traceback(self):
        # IncompleteRead / BadStatusLine are http.client.HTTPException, not OSError (verifier, 2026-11-22)
        import http.client
        raised = [http.client.IncompleteRead(b"<osm"), http.client.BadStatusLine("garbage")]

        def post(ep, q):
            if raised:
                raise raised.pop(0)
            return GOOD
        clock = Clock()
        ep, body, base = ovp.overpass("MD", "q", endpoints=("a",), attempts=3, post=post, sleep=clock.sleep, log=quiet)
        self.assertEqual(body, GOOD)
        self.assertEqual(len(clock.sleeps), 2)

    def test_malformed_xml_that_ends_in_osm_is_a_failure(self):
        bad = GOOD.replace(b"<osm", b"<osm <<", 1)
        self.assertTrue(bad.rstrip().endswith(b"</osm>"))
        with self.assertRaises(ovp.Failure):
            ovp.check_body(bad)

    def test_each_bad_answer_alone_is_a_failure(self):
        for body in (REMARK, TRUNCATED, GOOD.replace(b'<meta osm_base="2026-11-22T08:00:00Z"/>', b"")):
            with self.assertRaises(ovp.Failure):
                ovp.check_body(body)
        self.assertEqual(ovp.check_body(GOOD), "2026-11-22T08:00:00Z")

    def test_both_endpoints_down_fall_back_to_geofabrik(self):
        a = FakeOverpass([(429, b"slow down")])
        b = FakeOverpass([(503, b"down")])
        d = tempfile.mkdtemp()
        calls = []

        def gf(cc, cfg, out_dir):
            calls.append(cc)
            return {"name": "md", "url": "https://download.geofabrik.de/europe/moldova-261121.osm.pbf",
                    "md5": "m", "sha256": "s", "bytes": 1, "replication": "r", "sequence": None, "stations": 68}
        try:
            row = ovp.one("MD", CFG, d, ROOT, overpass_fn=lambda cc, q: ovp.overpass(
                cc, q, endpoints=(a.url, b.url), attempts=2, sleep=Clock().sleep, log=quiet),
                geofabrik_fn=gf, log=quiet)
            self.assertEqual(calls, ["MD"])
            self.assertEqual(row["stations"], 68)
            self.assertEqual((len(a.seen), len(b.seen)), (2, 2))
            self.assertFalse(os.path.exists(os.path.join(d, "MD.osm")))
        finally:
            a.close(); b.close(); shutil.rmtree(d)

    def test_overpass_and_geofabrik_both_failing_fails_the_run(self):
        a = FakeOverpass([(504, b"timeout")])
        d = tempfile.mkdtemp()

        def gf(cc, cfg, out_dir):
            raise SystemExit("Geofabrik serves none of the dated files")

        def one(cc, cfg, out_dir, root):
            return ovp.one(cc, cfg, out_dir, root, overpass_fn=lambda c, q: ovp.overpass(
                c, q, endpoints=(a.url,), attempts=2, sleep=Clock().sleep, log=quiet), geofabrik_fn=gf, log=quiet)
        try:
            prov = os.path.join(d, "prov.jsonl")
            cfg = os.path.join(d, "cfg.json")
            json.dump({"countries": {"MD": CFG["countries"]["MD"], "RO": CFG["countries"]["RO"]}}, open(cfg, "w"))
            import contextlib, io as _io
            err = _io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(_io.StringIO()):
                rc = ovp.main([cfg, os.path.join(d, "out"), prov], root=ROOT, one_fn=one, sleep=lambda s: None)
            self.assertNotEqual(rc, 0)
            self.assertIn("FAIL", err.getvalue())
            self.assertFalse(os.path.exists(prov) and open(prov).read().strip())     # MD failed first: no row
        finally:
            a.close(); shutil.rmtree(d)

    def test_the_geofabrik_fallback_checks_md5_and_filters_the_nine_keys(self):
        d = tempfile.mkdtemp()
        data = b"pbf-bytes"
        served = [b"corrupt", data]
        cmds = []

        def fetch(url, dst):
            open(dst, "wb").write(served.pop(0))

        def run(*args):
            cmds.append(args)
            if args[:3] == ("fileinfo", "-g", "header.option.osmosis_replication_timestamp"):
                return "2026-11-21T20:21:00Z"
            if args[0] == "fileinfo":
                return {"data.count.nodes": "5", "data.count.ways": "2", "data.count.relations": "0"}[args[3]]
            return ""
        try:
            src = lambda p: {"url": "https://download.geofabrik.de/europe/moldova-261121.osm.pbf",
                             "md5": hashlib.md5(data).hexdigest()}
            row = ovp.geofabrik("MD", CFG, d, source=src, fetch=fetch, run=run, log=quiet)
            self.assertEqual(row["stations"], 7)
            self.assertEqual(row["replication"], "2026-11-21T20:21:00Z")
            self.assertEqual(row["md5"], hashlib.md5(data).hexdigest())
            self.assertEqual(row["sha256"], hashlib.sha256(data).hexdigest())
            self.assertIsNone(row["sequence"])
            filt = [c for c in cmds if c[0] == "tags-filter"]
            self.assertEqual(len(filt), 2)
            for c in filt:
                self.assertEqual({a.split("/", 1)[1].split("=")[0] for a in c if a.startswith("nwr/")}, NINE)
            self.assertFalse(os.path.exists(os.path.join(d, "MD-geofabrik.osm.pbf")))     # the source goes
            with self.assertRaises(ovp.Failure):                                          # undated: refused
                ovp.geofabrik("MD", CFG, d, source=lambda p: {"url": "https://x/moldova-latest.osm.pbf", "md5": "x"},
                              fetch=fetch, run=run, log=quiet)
        finally:
            shutil.rmtree(d)


class Coverage(unittest.TestCase):
    # AC-3
    def station(self, lat, lon, **tags):
        return {"id": "n9", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": dict({"amenity": "charging_station"}, **tags)}

    def test_stations_outside_every_polygon_are_dropped(self):
        countries = cdb.Countries(CFG, ROOT)
        for lat, lon, where in ((40.15, 44.04, "Armenia, inside GE's and TR's bboxes"),
                                (35.68, 139.69, "Tokyo, inside RU's bbox")):
            self.assertFalse(countries.covers(lat, lon), where)
            rep = {}
            self.assertIsNone(cdb.osm_record(self.station(lat, lon), CONNECTORS, countries, rep), where)
            self.assertEqual(rep, {"dropped_outside": 1}, where)
            rep = {}                                              # addr:country naming ours does not save it
            self.assertIsNone(cdb.osm_record(self.station(lat, lon, **{"addr:country": "GE"}), CONNECTORS, countries, rep))
            self.assertEqual(rep, {"dropped_outside": 1})

    def test_border_stations_keep_their_country(self):
        countries = cdb.Countries(CFG, ROOT)
        for lat, lon, cc in ((47.2112, 27.8000, "MD"),            # Ungheni
                             (47.1585, 27.6014, "RO"),            # Iași
                             (47.0245, 28.8323, "MD"),            # Chișinău
                             (45.9050, 28.1950, "MD"),            # Cahul
                             (45.4353, 28.0080, "RO")):           # Galați
            self.assertTrue(countries.covers(lat, lon), cc)
            self.assertEqual(cdb.osm_record(self.station(lat, lon), CONNECTORS, countries, {})["country"], cc)
            self.assertEqual(countries.of(lat, lon), cc)

    def test_every_fixture_station_is_still_built(self):
        d = tempfile.mkdtemp()
        try:
            desc, rep = run_build(d)
            self.assertEqual(rep.get("dropped_outside", 0), 0)
        finally:
            shutil.rmtree(d)


class OsmGate(unittest.TestCase):
    # AC-6
    def test_a_country_losing_more_than_20_percent_of_osm_fails(self):
        self.assertTrue(cdb.osm_gate({"DE": 500}, {"DE": 1000}))
        self.assertEqual(cdb.osm_gate({"DE": 850}, {"DE": 1000}), [])
        self.assertEqual(cdb.osm_gate({"MD": 10}, {"MD": 40}), [])               # below the floor: may swing
        self.assertTrue(cdb.osm_gate({}, {"DE": 1000}))                           # gone entirely

    def test_validate_fails_on_halved_osm_against_the_previous_manifest(self):
        d = tempfile.mkdtemp()
        try:
            desc, _ = run_build(d)
            cur = desc["counts"]
            self.assertGreaterEqual(cur["osm_by_country"]["MD"], cdb.COUNTRY_LOSS_FLOOR)
            outs = {}
            for name, prev_osm in (("same", dict(cur["osm_by_country"])),
                                   ("halved", dict(cur["osm_by_country"], DE=2 * max(cur["osm_by_country"].get("DE", 0), 50),
                                                   MD=2 * cur["osm_by_country"]["MD"]))):
                p = os.path.join(d, name + ".json")
                json.dump({"charging": {"counts": {"ocm_by_country": cur["ocm_by_country"],
                                                   "osm_by_country": prev_osm}}}, open(p, "w"))
                import contextlib, io as _io
                o = _io.StringIO()
                with contextlib.redirect_stdout(o):
                    rc = build.main(["validate", d, os.path.join(ROOT, "regional.json"), p, "--no-min-ocm"])
                outs[name] = (rc, o.getvalue())
            self.assertEqual(outs["same"][0], 0, outs["same"][1])
            self.assertIn("CHARGING GATES: PASS", outs["same"][1])
            self.assertEqual(outs["halved"][0], 1)
            self.assertIn("CHARGING GATES: FAIL", outs["halved"][1])
            self.assertIn("DE: %d OSM stations" % cur["osm_by_country"].get("DE", 0), outs["halved"][1])
            self.assertIn("MD: %d OSM stations" % cur["osm_by_country"]["MD"], outs["halved"][1])
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main(verbosity=1)
