# -*- coding: utf-8 -*-
"""The Europe Lite SETTLEMENTS CATALOGUE: cities, towns and villages of every published country
(except RU), searchable by name on a car that has only Europe Lite for that country.

    lite-places.py build --manifest manifest.json --out DIR [--previous europe-lite.json]
                         [--local DIR] [--exclude RU] [--min-rows N]
    lite-places.py probe places.sqlite

Source: the published /2 search databases of the regional manifest — their `addr_locality` table
(city / town / village; hamlets left out, owner 2026-11-25, variant B2), and for a /1 file (IS
today) the settlements of its `place` table (kind 0, class unknown: kept as 'town'). Names are the
country's own (OSM `name`); the other spellings ride in `aliases` and are searchable, exactly as
in the full search (owner's default: "names as today").

The schema is the full search's `place` + `place_fts` (FTS4 unicode61, content table) +
`place_cat_pos`, so the app's SearchDb reads it with the same queries, plus two columns per row:
`cc` (the country) and `district` (what tells two same-named villages apart). Rows go in by
population, biggest first, so docid order is population order.

    DIR/places.sqlite       the catalogue (the app reads it in place)
    DIR/places.sqlite.gz    what is published (gzip -9)
    DIR/places.json         rows, counts per country and per kind, sizes and sha256 of both

GATES (exit 1, nothing written to places.json):
  - rows >= --min-rows (default 300 000; 365 675 measured 2026-11-25 without RU);
  - per country, rows >= 90 % of the previous catalogue's (`places.counts` of --previous); a
    country that had rows and now has none fails the same way;
  - the excluded countries have no row;
  - PROBES: the first result (FTS prefix match, population first, as the app's pool) of München,
    Мюнхен and Munich is München in DE, of Thessaloniki and Салоники Θεσσαλονίκη in GR, of Tirana
    Tiranë in AL, of Pleven Плевен in BG.

Standard library only (the runner's python3; sqlite3 with FTS4, as Ubuntu ships it).
"""
import argparse, datetime, gzip, hashlib, json, os, shutil, sqlite3, sys, time, urllib.request

KEEP_KINDS = ("city", "town", "village")
FORMAT = "wedrive-lite-places/1"
MIN_SHARE = 0.90

# query, expected country, expected name (the row's own `name`)
PROBES = [
    ("München", "DE", "München"),
    ("Мюнхен", "DE", "München"),
    ("Munich", "DE", "München"),
    ("Thessaloniki", "GR", "Θεσσαλονίκη"),
    ("Салоники", "GR", "Θεσσαλονίκη"),
    ("Tirana", "AL", "Tiranë"),
    ("Pleven", "BG", "Плевен"),
]

SCHEMA = """
CREATE TABLE place(
    id       INTEGER PRIMARY KEY,
    kind     INTEGER NOT NULL,          -- always 0: a settlement
    name     TEXT    NOT NULL,          -- OSM name, the country's own script
    aliases  TEXT,                      -- JSON array of other spellings; NULL when none
    lat      REAL    NOT NULL,
    lon      REAL    NOT NULL,
    cat      TEXT,                      -- always NULL (no category button)
    detail   TEXT,                      -- city / town / village
    pop      INTEGER NOT NULL DEFAULT 0,
    cc       TEXT    NOT NULL,          -- ISO country of the row (the manifest's region code)
    district TEXT    NOT NULL           -- raion / judet / Landkreis; '' when the source has none
);
CREATE INDEX place_cat_pos ON place(cat, lat, lon);
CREATE INDEX place_cc ON place(cc);
CREATE VIRTUAL TABLE place_fts USING fts4(name, aliases, tokenize=unicode61, content='place');
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def fetch(url, dst, want_sha, attempts=3):
    """One file, streamed, sha256-checked; retried on a network error or a wrong sum."""
    for n in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=120) as r, open(dst, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            got = sha256(dst)
            if got == want_sha:
                return
            print("  %s: sha256 %s, expected %s (attempt %d)" % (os.path.basename(dst), got, want_sha, n))
        except Exception as e:  # noqa: BLE001 - any network failure is retried, then fatal
            print("  %s: %s (attempt %d)" % (os.path.basename(dst), e, n))
        time.sleep(15 * n)
    raise SystemExit("could not fetch %s" % url)


def rows_of(path, cc):
    """(name, aliases, kind, district, lat, lon, pop, cc) of one country's search database."""
    d = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        has = d.execute("SELECT count(*) FROM sqlite_master WHERE name = 'addr_locality'").fetchone()[0]
        if has:
            q = "SELECT name, aliases, kind, district, lat, lon, pop FROM addr_locality WHERE kind IN (%s)" % \
                ",".join("?" * len(KEEP_KINDS))
            return [(n, a, k, dist or "", lat, lon, pop or 0, cc) for n, a, k, dist, lat, lon, pop in d.execute(q, KEEP_KINDS)]
        # a /1 file: the settlements of the place table, class unknown
        return [(n, a, "town", "", lat, lon, pop or 0, cc)
                for n, a, lat, lon, pop in d.execute("SELECT name, aliases, lat, lon, pop FROM place WHERE kind = 0")]
    finally:
        d.close()


def write_db(path, rows, created):
    if os.path.exists(path):
        os.remove(path)
    # big places first: docid order = population order, then a total order so the bytes are
    # reproducible from the same inputs
    rows = sorted(rows, key=lambda r: (-r[6], r[7], r[0], r[4], r[5]))
    d = sqlite3.connect(path)
    d.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;" + SCHEMA)
    d.executemany("INSERT INTO place(kind, name, aliases, lat, lon, cat, detail, pop, cc, district) "
                  "VALUES (0, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                  [(r[0], r[1], r[4], r[5], r[2], r[6], r[7], r[3]) for r in rows])
    d.execute("INSERT INTO place_fts(place_fts) VALUES ('rebuild')")
    d.executemany("INSERT INTO meta(k, v) VALUES (?, ?)", [("format", FORMAT), ("rows", str(len(rows))),
                                                           ("created", created)])
    d.commit()
    d.execute("VACUUM")
    d.close()


def fts_expression(query):
    """The app's SearchDb.ftsExpression, enough for the probes: every word a prefix."""
    words = [w for w in query.lower().replace(",", " ").replace("-", " ").split() if w]
    return " ".join(w + "*" for w in words)


def probe(path):
    """Every probe's first result; a list of problems (empty = PASS)."""
    d = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    problems = []
    try:
        for q, cc, name in PROBES:
            r = d.execute("SELECT p.name, p.cc, p.pop FROM place_fts f JOIN place p ON p.id = f.docid "
                          "WHERE place_fts MATCH ? ORDER BY p.pop DESC LIMIT 1", (fts_expression(q),)).fetchone()
            ok = r is not None and r[0] == name and r[1] == cc
            print("  probe %-14s -> %s %s" % (q, r and "%s (%s, pop %d)" % r, "OK" if ok else "FAIL"))
            if not ok:
                problems.append("probe %r: first result %r, expected %s in %s" % (q, r, name, cc))
    finally:
        d.close()
    return problems


def gates(counts, rows, previous, exclude, min_rows):
    p = []
    if rows < min_rows:
        p.append("%d rows, the gate is %d" % (rows, min_rows))
    for cc in exclude:
        if counts.get(cc):
            p.append("%s is excluded and has %d rows" % (cc, counts[cc]))
    for cc, before in sorted((previous or {}).items()):
        now = counts.get(cc, 0)
        if before > 0 and now < MIN_SHARE * before:
            p.append("%s: %d rows, the previous catalogue had %d (< %d %%)" % (cc, now, before, MIN_SHARE * 100))
    return p


def build(a):
    m = json.load(open(a.manifest, encoding="utf-8"))
    if m.get("kind") != "regional":
        raise SystemExit("%s is not a regional manifest" % a.manifest)
    exclude = sorted({c.strip().upper() for c in a.exclude.split(",") if c.strip()})
    previous = None
    if a.previous and os.path.exists(a.previous):
        previous = (json.load(open(a.previous, encoding="utf-8")).get("places") or {}).get("counts")
    os.makedirs(a.out, exist_ok=True)
    cache = os.path.join(a.out, ".src")
    os.makedirs(cache, exist_ok=True)
    rows, counts, sources = [], {}, {}
    for cc in sorted(m["regions"]):
        if cc in exclude:
            print("%s: excluded" % cc)
            continue
        s = m["regions"][cc].get("search")
        if not s:
            print("%s: no search database in the manifest" % cc)
            continue
        local = os.path.join(a.local, "%s.sqlite" % cc) if a.local else None
        if local and os.path.exists(local) and sha256(local) == s["sha256"]:
            path, temp = local, False
        else:
            path, temp = os.path.join(cache, "%s.sqlite" % cc), True
            fetch(s["url"], path, s["sha256"])
        got = rows_of(path, cc)
        if temp:
            os.remove(path)          # one country on disk at a time: DE alone is 1.2 GB
        rows += got
        counts[cc] = len(got)
        sources[cc] = {"sha256": s["sha256"], "format": (s.get("addresses") or {}).get("format")}
        print("%s: %d settlements" % (cc, len(got)))
    shutil.rmtree(cache, ignore_errors=True)
    created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    kinds = {}
    for r in rows:
        kinds[r[2]] = kinds.get(r[2], 0) + 1
    problems = gates(counts, len(rows), previous, exclude, a.min_rows)
    db = os.path.join(a.out, "places.sqlite")
    write_db(db, rows, created)
    problems += probe(db)
    for x in problems:
        print("GATE FAIL:", x)
    if problems:
        return 1
    gz = db + ".gz"
    with open(db, "rb") as src, gzip.GzipFile(gz, "wb", compresslevel=9, mtime=0) as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
    desc = {"format": FORMAT, "file": "places.sqlite.gz", "bytes": os.path.getsize(gz), "sha256": sha256(gz),
            "sqlite_file": "places.sqlite", "sqlite_bytes": os.path.getsize(db), "sqlite_sha256": sha256(db),
            "rows": len(rows), "counts": counts, "kinds": kinds, "excluded": exclude,
            "source": {"manifest_tag": m.get("tag"), "countries": sources}, "created": created}
    json.dump(desc, open(os.path.join(a.out, "places.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False,
              sort_keys=True)
    print("places.sqlite %d rows, %.1f MB, gz %.1f MB, %d countries; kinds %s"
          % (len(rows), desc["sqlite_bytes"] / 1e6, desc["bytes"] / 1e6, len(counts), kinds))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--manifest", required=True, help="the regional manifest.json (search databases)")
    b.add_argument("--out", required=True)
    b.add_argument("--previous", help="the current europe-lite.json: its places.counts are the 90 % gate")
    b.add_argument("--local", help="a directory of <CC>.sqlite already downloaded (used when the sha256 matches)")
    b.add_argument("--exclude", default="RU", help="comma-separated countries left out (default RU: no Lite graph)")
    b.add_argument("--min-rows", type=int, default=300_000)
    p = sub.add_parser("probe")
    p.add_argument("db")
    a = ap.parse_args()
    if a.cmd == "probe":
        problems = probe(a.db)
        return 1 if problems else 0
    return build(a)


if __name__ == "__main__":
    sys.exit(main())
