#!/usr/bin/env python3
"""Open Charge Map half of the charging catalogue (wedrive-charging/1).

    charging-ocm-fetch.py <regional.json> <OUT>       OCM_API_KEY in the environment

This file and nothing else in the factory knows what an OCM answer looks like: it FETCHES
(`fetch`) and it TRANSLATES one OCM record into our station (`normalise`). charging-build.py
imports both and never touches an OCM field itself — the same quarantine the app keeps for Waze.

Polite by construction, because the key is ours and one ban stops the whole catalogue:
  * one GET /v3/referencedata, then per country
    /v3/poi?countrycode=CC&sortby=id_asc&greaterthanid=N&maxresults=5000&compact=true
            &verbose=false&includecomments=false&excludecomputed=true
    — the paging OCM's own exporter uses (openchargemap/ocm-export): the next page starts after
    the largest ID of the last one, and a page shorter than maxresults is the last;
  * strictly sequential, at least PAUSE_S between any two requests;
  * a retry only for 429, 5xx and network errors, after 2, 4, 8, 16, 32 s; 403 (a bad or revoked
    key) and every other 4xx fail AT ONCE — hammering a refusal is what gets a key banned;
  * the key travels ONLY in the X-API-Key header, never in a URL, and is never printed;
  * every page is checked: IDs strictly increasing, inside the page and across pages.

Countries are regional.json's. XA (Azores) is inside OCM's PT; XG (Guernsey and Jersey) is two
OCM countries, JE and GG.

OUT/referencedata.json, OUT/<CC>.jsonl.gz (one record per line, sorted by ID, gzip mtime 0 so the
same answer is the same bytes), OUT/report.json (fetch time, requests, per-country count, ID
range and histograms of StatusTypeID, DataProviderID and SubmissionStatusTypeID).
"""
import datetime, gzip, io, json, os, sys, time, urllib.error, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import charging_db as cdb  # noqa: E402  (the shared EVSE id rule)

BASE = "https://api.openchargemap.io/v3"
PAGE = 5000
PAUSE_S = 1.0
BACKOFF_S = (2, 4, 8, 16, 32)
MAX_REQUESTS = 200
USER_AGENT = "charging-catalogue-builder/1"
# regional.json code -> the OCM country codes that make it up (absent = the same code)
OCM_COUNTRIES = {"XA": [], "XG": ["JE", "GG"]}
# and back: the regional.json country an OCM record is filed under
OUR_COUNTRY = {"JE": "XG", "GG": "XG"}


class FetchError(Exception):
    pass


class Client:
    """Sequential, paced, retrying GET. `sleep` and `clock` are injectable for the tests."""

    def __init__(self, key, base=BASE, sleep=time.sleep, clock=time.monotonic, opener=None, log=print):
        if not key or key.strip().lower() in ("test", "null"):
            raise FetchError("no OCM API key (OCM_API_KEY): OCM answers 403 without one")
        self._key = key
        self.base = base.rstrip("/")
        self.sleep, self.clock, self.log = sleep, clock, log
        self.opener = opener or urllib.request.build_opener()
        self.requests = 0
        self.urls = []
        self._last = None

    def _pace(self):
        if self._last is not None:
            wait = PAUSE_S - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)

    def get(self, path, params=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if "key" in q or "apikey" in q or self._key in url:
            raise FetchError("refusing to put the API key into a URL")
        for attempt in range(len(BACKOFF_S) + 1):
            if self.requests >= MAX_REQUESTS:
                raise FetchError("more than %d requests: refusing to go on (paging is broken?)" % MAX_REQUESTS)
            self._pace()
            self.requests += 1
            self.urls.append(url)
            req = urllib.request.Request(url, headers={
                "X-API-Key": self._key, "Accept": "application/json",
                "Accept-Encoding": "gzip", "User-Agent": USER_AGENT})
            status, why = None, None
            try:
                with self.opener.open(req, timeout=120) as r:
                    body = r.read()
                    if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                        body = gzip.decompress(body)
                    self._last = self.clock()
                    return json.loads(body.decode("utf-8"))
            except urllib.error.HTTPError as e:
                self._last = self.clock()
                status = e.code
                if status != 429 and status < 500:
                    raise FetchError("%s -> HTTP %d%s" % (url, status,
                                     " (the key is missing, wrong or revoked)" if status == 403 else ""))
                why = "HTTP %d" % status
            except (urllib.error.URLError, OSError, ValueError) as e:
                self._last = self.clock()
                why = "%s: %s" % (type(e).__name__, getattr(e, "reason", e))
            if attempt == len(BACKOFF_S):
                raise FetchError("%s: %s, gave up after %d attempts" % (url, why, attempt + 1))
            self.log("   %s: %s, retry in %d s" % (url, why, BACKOFF_S[attempt]))
            self.sleep(BACKOFF_S[attempt])


def poi_params(cc, after):
    return [("countrycode", cc), ("sortby", "id_asc"), ("greaterthanid", str(after)),
            ("maxresults", str(PAGE)), ("compact", "true"), ("verbose", "false"),
            ("includecomments", "false"), ("excludecomputed", "true")]


def fetch_country(client, cc):
    """-> (records sorted by ID, pages). Fails on any ID that does not strictly increase."""
    out, after, pages = [], 0, 0
    while True:
        page = client.get("/poi", poi_params(cc, after))
        pages += 1
        if not isinstance(page, list):
            raise FetchError("%s: /poi answered %s, not a list" % (cc, type(page).__name__))
        for p in page:
            i = p.get("ID")
            if not isinstance(i, int) or i <= after:
                raise FetchError("%s: ID %r after %d — the order is broken, a record would be lost" % (cc, i, after))
            after = i
            out.append(p)
        if len(page) < PAGE:
            return out, pages


def histogram(records, key):
    h = {}
    for r in records:
        k = str(r.get(key))
        h[k] = h.get(k, 0) + 1
    return dict(sorted(h.items()))


def write_jsonl_gz(path, records):
    raw = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as g:
        for r in records:
            g.write((json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
    with open(path, "wb") as f:
        f.write(raw.getvalue())


def read_jsonl_gz(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def ocm_codes(cfg):
    codes = []
    for c in sorted(cfg["countries"]):
        codes += OCM_COUNTRIES.get(c, [c])
    return codes


def fetch(cfg, out, client, now=None):
    os.makedirs(out, exist_ok=True)
    fetched = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ref = client.get("/referencedata")
    with open(os.path.join(out, "referencedata.json"), "w", encoding="utf-8") as f:
        json.dump(ref, f, sort_keys=True, ensure_ascii=False, indent=1)
    report = {"fetched": fetched, "countries": {}}
    for cc in ocm_codes(cfg):
        recs, pages = fetch_country(client, cc)
        write_jsonl_gz(os.path.join(out, "%s.jsonl.gz" % cc), recs)
        ids = [r["ID"] for r in recs]
        report["countries"][cc] = {
            "count": len(recs), "pages": pages,
            "min_id": ids[0] if ids else None, "max_id": ids[-1] if ids else None,
            "status": histogram(recs, "StatusTypeID"),
            "provider": histogram(recs, "DataProviderID"),
            "submission": histogram(recs, "SubmissionStatusTypeID")}
        client.log("   %-3s %6d POI, %d page(s)" % (cc, len(recs), pages))
    report["requests"] = client.requests
    report["total"] = sum(c["count"] for c in report["countries"].values())
    with open(os.path.join(out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, sort_keys=True, indent=1)
    return report


# ─────────────────────────────── one OCM record -> our station ───────────────────────────────

# OCM StatusTypeID -> our status. 200/210 (removed, duplicate) are DROPPED, never stored.
STATUS = {0: "UNKNOWN", 10: "OPERATIONAL", 20: "OPERATIONAL", 30: "NOT_OPERATIONAL", 50: "OPERATIONAL",
          75: "PARTLY", 100: "NOT_OPERATIONAL", 150: "PLANNED"}
REMOVED = (200, 210)
# OCM UsageTypeID -> our usage
USAGE = {0: "UNKNOWN", 1: "PUBLIC", 4: "PUBLIC_MEMBERSHIP", 5: "PUBLIC_PAY", 7: "PUBLIC",
         6: "CUSTOMERS", 2: "PRIVATE", 3: "PRIVATE"}
# OCM operator placeholders: Unknown, Private Residence, Business Owner — not an operator's name
PLACEHOLDER_OPERATORS = (1, 44, 45)
PRIVATE_RESIDENCE = 44


def status_of(code):
    """-> (status, status_raw); status None = dropped (200/210)."""
    if code is None:
        return "UNKNOWN", "ocm:none"
    if code in REMOVED:
        return None, "ocm:%d" % code
    return STATUS.get(code, "UNKNOWN"), "ocm:%d" % code


def live_submissions(ref):
    return {s["ID"] for s in ref.get("SubmissionStatusTypes", []) if s.get("IsLive")}


def operators(ref):
    return {o["ID"]: o["Title"] for o in ref.get("Operators", []) if o.get("Title")}


def kw_of(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if 0 < v <= 1000 else None


def normalise(poi, ref, connectors, country, stats):
    """One OCM POI -> our station dict, or None (dropped, the reason counted in `stats`).

    Provider licence is NOT decided here: the record keeps its DataProviderID and the build
    checks it against charging/providers.json."""
    sub = poi.get("SubmissionStatusTypeID")
    if sub is not None and sub not in stats.setdefault("_live", live_submissions(ref)):
        stats["dropped_unpublished"] = stats.get("dropped_unpublished", 0) + 1
        return None
    status, raw = status_of(poi.get("StatusTypeID"))
    if status is None:
        stats["dropped_removed"] = stats.get("dropped_removed", 0) + 1
        return None
    a = poi.get("AddressInfo") or {}
    lat, lon = a.get("Latitude"), a.get("Longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)) or (lat == 0 and lon == 0):
        stats["dropped_nocoords"] = stats.get("dropped_nocoords", 0) + 1
        return None
    ops = stats.setdefault("_ops", operators(ref))
    op_id = poi.get("OperatorID")
    operator = None if op_id in PLACEHOLDER_OPERATORS else ops.get(op_id)
    usage = USAGE.get(poi.get("UsageTypeID"), "UNKNOWN")
    if op_id == PRIVATE_RESIDENCE and usage == "UNKNOWN":
        usage = "PRIVATE"
    conns, evse = [], set()
    for c in poi.get("Connections") or []:
        stats["connectors_in"] = stats.get("connectors_in", 0) + 1
        cstatus = None
        if c.get("StatusTypeID") is not None:
            cstatus, _ = status_of(c.get("StatusTypeID"))
            if cstatus is None:
                stats["connectors_removed"] = stats.get("connectors_removed", 0) + 1
                continue
        tid = c.get("ConnectionTypeID")
        std = connectors["ocm"].get(str(tid))
        if std is None:
            std = "OTHER"
            stats.setdefault("unknown_connection_types", {})
            stats["unknown_connection_types"][str(tid)] = stats["unknown_connection_types"].get(str(tid), 0) + 1
        cur = connectors["ocm_current"].get(str(c.get("CurrentTypeID")))
        if cur is None and std in connectors["dc_standards"]:
            cur = "DC"
        if c.get("PowerKW") is not None:
            stats["kw_in"] = stats.get("kw_in", 0) + 1
        kw = kw_of(c.get("PowerKW"))
        if kw is not None:
            stats["kw_out"] = stats.get("kw_out", 0) + 1
        qty = c.get("Quantity") if isinstance(c.get("Quantity"), int) and c.get("Quantity") > 0 else None
        if c.get("Quantity") is not None:
            stats["qty_in"] = stats.get("qty_in", 0) + 1
            if qty is not None:
                stats["qty_out"] = stats.get("qty_out", 0) + 1
        e = cdb.evse_id(c.get("Reference"))
        if e:
            evse.add(e)
        conns.append({"standard": std, "current": cur, "kw": kw, "qty": qty, "status": cstatus})
    # an operational station whose every connector is broken does not work (owner, 2026-11-21);
    # only some broken -> PARTLY
    if status == "OPERATIONAL" and any(c["status"] == "NOT_OPERATIONAL" for c in conns):
        status = "NOT_OPERATIONAL" if all(c["status"] == "NOT_OPERATIONAL" for c in conns) else "PARTLY"
    addr = ", ".join(x for x in (a.get("AddressLine1"), a.get("AddressLine2"), a.get("Town"), a.get("Postcode"))
                     if isinstance(x, str) and x.strip())
    kws = [c["kw"] for c in conns if c["kw"] is not None]
    pts = poi.get("NumberOfPoints")
    return {
        "kind": "ocm", "provider_id": poi.get("DataProviderID"), "src_id": str(poi["ID"]),
        "country": OUR_COUNTRY.get(country, country), "lat": float(lat), "lon": float(lon),
        "name": (a.get("Title") or "").strip() or None, "address": addr or None,
        "operator": operator, "operator_id": None if op_id in PLACEHOLDER_OPERATORS else op_id,
        "usage": usage, "status": status, "status_raw": raw,
        "status_date": poi.get("DateLastStatusUpdate"), "verified_date": poi.get("DateLastVerified"),
        "created_date": poi.get("DateCreated"),
        "points": pts if isinstance(pts, int) and pts > 0 else None, "capacity": None,
        "max_kw": max(kws) if kws else None,
        "has_dc": int(any(c["current"] == "DC" for c in conns)),
        "cost": (poi.get("UsageCost") or "").strip() or None, "opening_hours": None,
        "evse_ids": sorted(evse), "connectors": conns}


def main():
    if len(sys.argv) != 3:
        print(__doc__); return 2
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
    key = os.environ.get("OCM_API_KEY", "")
    client = Client(key, base=os.environ.get("OCM_BASE", BASE))
    try:
        rep = fetch(cfg, sys.argv[2], client)
    except FetchError as e:
        print("::error::OCM fetch failed: %s" % e)
        return 1
    print("OCM: %d POI in %d countries, %d requests, fetched %s"
          % (rep["total"], len(rep["countries"]), rep["requests"], rep["fetched"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
