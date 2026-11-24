# -*- coding: utf-8 -*-
"""National access points (NAP) — the ONE factory file that reads national charging registries.

Six keyless static files (charging/nap-sources.json): FR IRVE (CSV), DE BNetzA (CSV), NL NDW
(OCPI JSON), FI Digitraffic (GeoJSON), CH BFE (OICP JSON), LT Via Lietuva (DATEX II XML). Each is
read into OUR station dicts (the same shape charging_db.osm_record and the OCM normaliser produce),
plus two things only a registry has:

  "evses"     one dict per PHYSICAL charging point: the id as the source wrote it (`raw`, None when
              the source names none), its standards, current and power;
  "live_keys" the ids a live feed uses for this station's points where they are not EVSE ids (LT's
              DATEX status names a point `<site id>#<connectorIndex>`);
  "refs"      registry ids another source may carry for the same place (OCM's DataProvidersReference
              of provider 28 = FR id_station_itinerance, of provider 32 = the LT site number).

Devices of one source at one car park become one site (`group`): the same operator (normalised,
aliases applied) within GROUP_M of the group's FIRST device — the seed, so a row of chargers never
chains into a street. A physical EVSE id appears on at most one site of a source.

Standard library only.
"""
import csv, gzip, io, json, math, re
import xml.etree.ElementTree as ET

import charging_db as cdb

GROUP_M = 25.0


def unzip(data):
    """Bytes as served: some hosts gzip the body without a Content-Encoding the client undid."""
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def num(x):
    try:
        v = float(str(x).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return v if 0 < v <= 1000 else None


def text_standard(text):
    """A plug as a registry spells it (`AC Typ 2 Steckdose`, `DC Kupplung Combo, CCS`, `Type 2
    Outlet`, `CHAdeMO`) -> (our standard, current) or (None, None) when it names nothing."""
    t = (text or "").strip().lower().replace("_", " ")
    if not t:
        return None, None
    dc = t.startswith("dc") or "dc " in t
    if "chademo" in t:
        return "CHADEMO", "DC"
    if "combo" in t or "ccs" in t:
        return ("CCS1", "DC") if "typ 1" in t or "type 1" in t or "t1" in t.split() else ("CCS2", "DC")
    if "gb/t" in t or "gbt" in t or "gb t" in t:
        return ("GBT_DC", "DC") if dc else ("GBT_AC", "AC")
    if "tesla" in t or "nacs" in t:
        return "NACS", "DC" if dc else None
    if "typ 2" in t or "type 2" in t or "type2" in t or "62196" in t or t == "t2":
        tethered = "kupplung" in t or "cable" in t or "kabel" in t
        return ("TYPE2_TETHERED" if tethered else "TYPE2"), ("DC" if dc else "AC")
    if "typ 1" in t or "type 1" in t or "j1772" in t:
        return "TYPE1", "AC"
    if "schuko" in t or "domestic" in t or "cee 7" in t or "haushalt" in t or t == "ef":
        return "SCHUKO", "AC"
    if "cee" in t or "60309" in t:
        return "CEE", "AC"
    return "OTHER", ("DC" if dc else None)


OCPI = {"IEC_62196_T2_COMBO": "CCS2", "IEC_62196_T2": "TYPE2", "CHADEMO": "CHADEMO", "IEC_62196_T1": "TYPE1",
        "IEC_62196_T1_COMBO": "CCS1", "DOMESTIC_F": "SCHUKO", "DOMESTIC_E": "SCHUKO", "TESLA_S": "NACS",
        "TESLA_R": "NACS", "NACS": "NACS", "GBT_DC": "GBT_DC", "GBT_AC": "GBT_AC",
        "IEC_60309_2_single_16": "CEE", "IEC_60309_2_three_16": "CEE", "IEC_60309_2_three_32": "CEE",
        "IEC_60309_2_three_64": "CEE"}


def ocpi_connector(standard, fmt, power_type, watts):
    std = OCPI.get(standard or "", "OTHER")
    if std == "TYPE2" and (fmt or "").upper() == "CABLE":
        std = "TYPE2_TETHERED"
    cur = "DC" if (power_type or "").upper() == "DC" or std in ("CCS2", "CCS1", "CHADEMO", "GBT_DC") else \
        ("AC" if (power_type or "").upper().startswith("AC") else None)
    kw = num((watts or 0) / 1000.0) if isinstance(watts, (int, float)) else None
    return {"standard": std, "current": cur, "kw": kw}


# ─────────────────────────────── one record per source ───────────────────────────────

def evse(raw, conns):
    raw = (raw or "").strip() or None
    kws = [c["kw"] for c in conns if c.get("kw")]
    curs = sorted({c["current"] for c in conns if c.get("current")})
    return {"raw": raw, "standards": sorted({c["standard"] for c in conns if c.get("standard")}),
            "current": "DC" if "DC" in curs else (curs[0] if curs else None),
            "kw": max(kws) if kws else None, "conns": conns}


def device(src, sid, cc, lat, lon, name=None, operator=None, address=None, evses=(), usage="UNKNOWN",
           status="UNKNOWN", refs=(), live_keys=(), opening_hours=None):
    return {"src": src, "sid": str(sid), "country": cc, "lat": lat, "lon": lon, "name": (name or "").strip() or None,
            "operator": (operator or "").strip() or None, "address": (address or "").strip() or None,
            "evses": list(evses), "usage": usage, "status": status, "refs": list(refs),
            "live_keys": list(live_keys), "opening_hours": opening_hours}


def ref_key(text):
    """A registry id compared across sources: upper case, letters and digits only."""
    return "".join(ch for ch in (text or "").upper() if ch.isalnum()) or None


def read_fr(data):
    """FR IRVE static (transport.data.gouv.fr 84013): one row per charging point (pdc); the
    station is `id_station_itinerance`."""
    by = {}
    rows = csv.DictReader(io.StringIO(unzip(data).decode("utf-8-sig")))
    for r in rows:
        sid = (r.get("id_station_itinerance") or r.get("id_station_local") or "").strip()
        try:
            lon, lat = float(r["consolidated_longitude"]), float(r["consolidated_latitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not sid:
            continue
        d = by.get(sid)
        if d is None:
            usage = "CUSTOMERS" if "réservé" in (r.get("condition_acces") or "").lower() else "PUBLIC"
            d = by[sid] = device("FR-irve", sid, "FR", lat, lon, r.get("nom_station"),
                                 r.get("nom_operateur") or r.get("nom_amenageur"), r.get("adresse_station"),
                                 usage=usage, refs=[("FR-station", ref_key(sid))], opening_hours=r.get("horaires"))
        kw = num(r.get("puissance_nominale"))
        if kw is None:           # a few rows give watts
            w = num((float(str(r.get("puissance_nominale")).replace(",", ".")) / 1000.0)
                    if re.match(r"^\s*\d+(?:[.,]\d+)?\s*$", str(r.get("puissance_nominale") or "")) else None)
            kw = w
        conns = []
        for col, std, cur in (("prise_type_combo_ccs", "CCS2", "DC"), ("prise_type_chademo", "CHADEMO", "DC"),
                              ("prise_type_2", "TYPE2", "AC"), ("prise_type_ef", "SCHUKO", "AC"),
                              ("prise_type_autre", "OTHER", None)):
            if (r.get(col) or "").strip().lower() in ("true", "1", "vrai"):
                if std == "TYPE2" and (r.get("cable_t2_attache") or "").strip().lower() in ("true", "1", "vrai"):
                    std = "TYPE2_TETHERED"
                conns.append({"standard": std, "current": cur, "kw": kw})
        d["evses"].append(evse(r.get("id_pdc_itinerance"), conns))
    return list(by.values())


def read_de(data):
    """DE BNetzA Ladesäulenregister: `;`-separated, a preamble before the header, latin-1; one row
    per charging device (Ladeeinrichtung) with up to six points, each with its own EVSE id."""
    text = unzip(data).decode("latin-1")
    lines = text.split("\n")          # never splitlines(): latin-1 0x85 is U+0085, a "line break" to it
    head = next((i for i, l in enumerate(lines) if l.startswith("Ladeeinrichtungs-ID")), None)
    if head is None:
        raise ValueError("BNetzA CSV: no header line")
    rd = csv.reader(io.StringIO("\n".join(lines[head:])), delimiter=";")
    cols = next(rd)
    ix = {h: i for i, h in enumerate(cols)}
    col = lambda row, name: row[ix[name]].strip() if name in ix and ix[name] < len(row) else ""
    out = []
    for row in rd:
        if len(row) < 17:
            continue
        try:
            lat, lon = float(col(row, "Breitengrad").replace(",", ".")), float(col(row, "Längengrad").replace(",", "."))
        except ValueError:
            continue
        st = col(row, "Status").lower()
        status = "NOT_OPERATIONAL" if "außer" in st or "ausser" in st else ("OPERATIONAL" if "betrieb" in st else "UNKNOWN")
        evses = []
        for k in range(1, 7):
            plugs = col(row, "Steckertypen%d" % k)
            eid = col(row, "EVSE-ID%d" % k)
            if not plugs and not eid:
                continue
            kw = num(col(row, "Nennleistung Stecker%d" % k))
            conns = []
            for p in re.split(r"[;,](?![^()]*\))", plugs) if plugs else ():
                std, cur = text_standard(p)
                if std and not (std == "OTHER" and p.strip().lower() in ("ccs", "combo")):
                    conns.append({"standard": std, "current": cur, "kw": kw})
            # `DC Kupplung Combo, CCS` is ONE plug written with a comma: keep one CCS2 per point
            seen, uniq = set(), []
            for c in conns:
                if c["standard"] not in seen:
                    seen.add(c["standard"]); uniq.append(c)
            evses.append(evse(eid, uniq))
        addr = "%s %s, %s %s" % (col(row, "Straße"), col(row, "Hausnummer"), col(row, "Postleitzahl"), col(row, "Ort"))
        out.append(device("DE-bnetza", col(row, "Ladeeinrichtungs-ID"), "DE", lat, lon, col(row, "Anzeigename (Karte)"),
                          col(row, "Betreiber"), addr.strip(" ,"), evses, usage="PUBLIC", status=status,
                          opening_hours=None))
    return out


def read_nl(data):
    """NL NDW: OCPI locations (one location = one site), EVSEs with eMI3 ids; REMOVED EVSEs dropped."""
    out = []
    for loc in json.loads(unzip(data)):
        try:
            lat, lon = float(loc["coordinates"]["latitude"]), float(loc["coordinates"]["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        evses = []
        for e in loc.get("evses") or []:
            if (e.get("status") or "").upper() == "REMOVED":
                continue
            conns = [ocpi_connector(c.get("standard"), c.get("format"), c.get("power_type"), c.get("max_electric_power"))
                     for c in e.get("connectors") or []]
            evses.append(evse(e.get("evse_id"), conns))
        if not evses:
            continue
        sid = "%s-%s-%s" % (loc.get("country_code", ""), loc.get("party_id", ""), loc.get("id"))
        op = (loc.get("operator") or {}).get("name") if isinstance(loc.get("operator"), dict) else None
        addr = ", ".join(x for x in (loc.get("address"), loc.get("city"), loc.get("postal_code")) if isinstance(x, str) and x.strip())
        out.append(device("NL-ndw", sid, "NL", lat, lon, loc.get("name"), op, addr, evses, usage="PUBLIC"))
    return out


def read_fi(data):
    """FI Digitraffic AFIR locations (GeoJSON), one feature = one site."""
    out = []
    for f in json.loads(unzip(data)).get("features") or []:
        p = f.get("properties") or {}
        try:
            lon, lat = (f.get("geometry") or {})["coordinates"][:2]
            lat, lon = float(lat), float(lon)
        except (KeyError, TypeError, ValueError):
            continue
        evses = []
        for e in p.get("evses") or []:
            if (e.get("status") or "").upper() == "REMOVED":
                continue
            conns = [ocpi_connector(c.get("standard"), c.get("format"), c.get("powerType"),
                                    c.get("maxElectricPower") or c.get("max_electric_power"))
                     for c in e.get("connectors") or []]
            evses.append(evse(e.get("id") or e.get("evseId"), conns))
        if not evses:
            continue
        op = ((p.get("operator") or {}).get("details") or {}).get("name")
        a = p.get("address") or {}
        addr = ", ".join(x for x in (a.get("street"), a.get("city"), a.get("postalCode")) if isinstance(x, str) and x.strip())
        out.append(device("FI-digitraffic", p.get("id"), "FI", lat, lon, p.get("name"), op, addr, evses, usage="PUBLIC"))
    return out


def _ch_name(r):
    n = r.get("ChargingStationNames")
    if isinstance(n, list) and n:
        return n[0].get("value")
    return n.get("value") if isinstance(n, dict) else None


def read_ch(data):
    """CH BFE OICP static: one record per EVSE; grouped into sites later (`group`)."""
    out = []
    for blk in json.loads(unzip(data)).get("EVSEData") or []:
        opn = blk.get("OperatorName")
        for r in blk.get("EVSEDataRecord") or []:
            try:
                la, lo = r["GeoCoordinates"]["Google"].split()
                lat, lon = float(la), float(lo)
            except (KeyError, AttributeError, ValueError):
                continue
            kws = [num(f.get("power")) for f in r.get("ChargingFacilities") or [] if isinstance(f, dict)]
            kw = max([k for k in kws if k] or [None]) if any(kws) else None
            conns = []
            for pl in r.get("Plugs") or []:
                std, cur = text_standard(pl)
                if std:
                    conns.append({"standard": std, "current": cur, "kw": kw})
            a = r.get("Address") or {}
            addr = ", ".join(x for x in (a.get("Street"), a.get("City"), a.get("PostalCode")) if isinstance(x, str) and x.strip())
            out.append(device("CH-bfe", r.get("EvseID"), "CH", lat, lon, _ch_name(r), opn, addr,
                              [evse(r.get("EvseID"), conns)], usage="PUBLIC"))
    return out


EGI = "{https://datex2.eu/schema/3/energyInfrastructure}"
LOC = "{https://datex2.eu/schema/3/locationReferencing}"
FAC = "{https://datex2.eu/schema/3/facilities}"
COM = "{https://datex2.eu/schema/3/common}"


def lt_status_keys(data):
    """LT DATEX status publication -> {site id: ['<site>#<connectorIndex>', …]} for every point
    the status feed addresses, REMOVED ones left out. Those keys are what LtLiveSource matches by:
    the static table's refill points do not line up with the status points one to one."""
    keys = {}
    if not data:
        return keys
    for s in ET.fromstring(unzip(data)).iter(EGI + "energyInfrastructureSiteStatus"):
        for p in s.iter(EGI + "refillPointStatus"):
            if (p.findtext(EGI + "status") or "").strip().upper() == "REMOVED":
                continue
            idx = p.get("connectorIndex") or p.get("id")
            keys.setdefault(s.get("id"), []).append("%s#%s" % (s.get("id"), idx))
    return keys


def read_lt(data, status=None):
    """LT Via Lietuva DATEX II table: one site per energyInfrastructureSite, its refill points as
    EVSEs (their ids are registry ids, not eMI3)."""
    keys = lt_status_keys(status)
    out = []
    for s in ET.fromstring(unzip(data)).iter(EGI + "energyInfrastructureSite"):
        lat, lon = s.find(".//" + LOC + "latitude"), s.find(".//" + LOC + "longitude")
        try:
            lat, lon = float(lat.text), float(lon.text)
        except (AttributeError, TypeError, ValueError):
            continue
        prov = s.find(".//" + EGI + "energyProvider")
        opn = None
        if prov is not None:
            v = prov.find(".//" + FAC + "name//" + COM + "value")
            opn = v.text if v is not None else None
        evses = []
        for rp in s.iter(EGI + "refillPoint"):
            conns = []
            for c in rp.iter(EGI + "connector"):
                std, cur = text_standard(c.findtext(EGI + "connectorType"))
                if std:
                    conns.append({"standard": std, "current": cur, "kw": num(c.findtext(EGI + "maxPowerAtSocket"))})
            evses.append(evse(rp.get("id"), conns))
        sid = s.get("id")
        m = re.search(r"(\d+)$", sid or "")
        out.append(device("LT-viali", sid, "LT", lat, lon, None, opn, None, evses, usage="PUBLIC",
                          refs=[("LT-site", m.group(1))] if m else [], live_keys=keys.get(sid, [])))
    return out


READERS = {"irve-csv": read_fr, "bnetza-csv": read_de, "ocpi-json": read_nl, "digitraffic-geojson": read_fi,
           "oicp-json": read_ch, "datex-table": read_lt}


# ─────────────────────────────── grouping into sites ───────────────────────────────

def _metres(a, b):
    x = math.radians(b["lon"] - a["lon"]) * math.cos(math.radians((a["lat"] + b["lat"]) / 2))
    return 6371008.8 * math.hypot(x, math.radians(b["lat"] - a["lat"]))


def group(devices, aliases, radius=GROUP_M):
    """Devices of one source at one car park -> one site: the same operator (normalised, aliases
    applied; both unknown also counts) within `radius` of the group's seed. Deterministic: input
    order, which every reader keeps as the file's."""
    cell = 0.0005
    grid, out = {}, []
    for d in devices:
        op = cdb.operator_key(d["operator"], aliases)
        k = (math.floor(d["lat"] / cell), math.floor(d["lon"] / cell))
        hit = None
        for i in (k[0] - 1, k[0], k[0] + 1):
            for j in (k[1] - 1, k[1], k[1] + 1):
                for seed in grid.get((i, j), ()):
                    if seed["_opk"] == op and _metres(seed, d) <= radius:
                        hit = seed
                        break
                if hit:
                    break
            if hit:
                break
        if hit:
            hit["evses"] += d["evses"]
            hit["live_keys"] += d["live_keys"]
            hit["refs"] += d["refs"]
            hit["_ids"].append(d["sid"])
            if hit["status"] != d["status"]:
                hit["status"] = "PARTLY" if "OPERATIONAL" in (hit["status"], d["status"]) else "UNKNOWN"
        else:
            d["_opk"] = op
            d["_ids"] = [d["sid"]]
            grid.setdefault(k, []).append(d)
            out.append(d)
    for d in out:
        d.pop("_opk", None)
        ids = d.pop("_ids")
        if len(ids) > 1:          # a grouped site is named by its smallest member id
            d["sid"] = min(ids, key=lambda x: (len(x), x))
    return out


# ─────────────────────────────── to our station dict ───────────────────────────────

def station(d, source_id, report):
    """A site -> our station dict (kind "nap"), its EVSE ids deduplicated within the site."""
    seen, evses = set(), []
    for e in d["evses"]:
        key = cdb.evse_key(e["raw"]) if e["raw"] else None
        if key and key in seen:
            report["nap_evse_repeated_in_site"] = report.get("nap_evse_repeated_in_site", 0) + 1
            continue
        if key:
            seen.add(key)
        evses.append(e)
    agg = {}
    for e in evses:
        for c in e["conns"]:
            k = (c["standard"], c.get("current"))
            a = agg.setdefault(k, {"standard": c["standard"], "current": c.get("current"), "kw": None, "qty": 0, "status": None})
            a["qty"] += 1
            if c.get("kw") and (a["kw"] is None or c["kw"] > a["kw"]):
                a["kw"] = c["kw"]
    conns = [agg[k] for k in sorted(agg, key=lambda k: (k[0], k[1] or ""))]
    kws = [c["kw"] for c in conns if c["kw"]]
    return {
        "kind": "nap", "provider_id": None, "src_id": d["sid"], "source": source_id, "country": d["country"],
        "lat": d["lat"], "lon": d["lon"], "name": d["name"], "address": d["address"], "operator": d["operator"],
        "operator_id": None, "usage": d["usage"], "status": d["status"], "status_raw": "nap:" + d["src"],
        "status_date": None, "verified_date": None, "created_date": None,
        "points": len(evses) or None, "capacity": None, "max_kw": max(kws) if kws else None,
        "has_dc": int(any(c["current"] == "DC" for c in conns)), "cost": None,
        "opening_hours": (d.get("opening_hours") or "").strip() or None,
        "evse_ids": sorted({cdb.evse_id(e["raw"]) for e in evses if e["raw"] and cdb.evse_id(e["raw"])}),
        "evses": [{"raw": e["raw"], "standards": e["standards"], "current": e["current"], "kw": e["kw"]} for e in evses],
        "evse_raw": {},
        "live_keys": sorted(set(d["live_keys"])),
        "refs": sorted({(s, r) for s, r in d["refs"] if r}),
        "connectors": conns}


def load(src, data, aliases, report, extra=None):
    """One NAP source's downloaded bytes -> station dicts (ids not yet assigned)."""
    reader = READERS[src["format"]]
    devices = reader(data, extra) if src["format"] == "datex-table" else reader(data)
    report.setdefault("nap_devices", {})[src["id"]] = len(devices)
    if src.get("group"):
        devices = group(devices, aliases)
    # a physical EVSE id on two sites of one source: the first site (file order) keeps it
    owner = {}
    for d in devices:
        keep = []
        for e in d["evses"]:
            key = cdb.evse_key(e["raw"]) if e["raw"] else None
            if key and owner.setdefault(key, d["sid"]) != d["sid"]:
                report["nap_evse_on_two_sites"] = report.get("nap_evse_on_two_sites", 0) + 1
                continue
            keep.append(e)
        d["evses"] = keep
    # two sites sharing a src_id (a registry that repeats a station id): the first keeps it
    seen, out = set(), []
    for d in devices:
        if d["sid"] in seen or not d["sid"]:
            report["nap_dropped_dup_id"] = report.get("nap_dropped_dup_id", 0) + 1
            continue
        seen.add(d["sid"])
        out.append(station(d, src["source_id"], report))
    report.setdefault("nap_sites", {})[src["id"]] = len(out)
    return out
