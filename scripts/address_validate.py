#!/usr/bin/env python3
"""Gate on a built address index (format wedrive-address/2) before anything describes or publishes it.

    usage: address_validate.py <cc.search.sqlite> [--max-unresolved 0.01]

Fails (exit 1) when the file is not what the app and docs/address-format.md promise:
  * SQLite integrity; format /2 in addr_meta;
  * every /1 column a released app reads by name still exists, and the /2 tables and columns too;
  * houses and streets present; the counts in addr_meta equal the tables;
  * every street's locality and every locality's commune points at an existing row;
  * the FTS tables index exactly their content tables (a stale or half-built FTS finds nothing);
  * streets without a locality at most --max-unresolved of all streets (measured 0 on MD..DE).
Prints one JSON line of counts either way, so a CI log shows what was checked.
"""
import argparse
import json
import sqlite3
import sys

V1_STREET = ["id", "name", "aliases", "city", "lat", "lon", "houses"]
V1_ADDR = ["street", "key", "osm", "num", "lat", "lon"]
V2_STREET = ["locality", "city_alt"]
V2_LOCALITY = ["id", "name", "aliases", "kind", "district", "lat", "lon", "pop", "houses", "commune"]
V2_COMMUNE = ["id", "name", "display", "aliases", "district"]


def columns(c, table):
    return [r[1] for r in c.execute('PRAGMA table_info("%s")' % table)]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--max-unresolved", type=float, default=0.01)
    a = ap.parse_args(argv)
    c = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    q = lambda s: c.execute(s).fetchone()[0]
    problems = []

    def need(ok, what):
        if not ok:
            problems.append(what)

    need(q("PRAGMA integrity_check") == "ok", "integrity_check failed")
    meta = dict(c.execute("SELECT k, v FROM addr_meta")) if "addr_meta" in [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")] else {}
    need(meta.get("format") == "wedrive-address/2", "addr_meta.format is %r" % meta.get("format"))
    for table, want in (("addr_street", V1_STREET + V2_STREET), ("addr", V1_ADDR),
                        ("addr_locality", V2_LOCALITY), ("addr_commune", V2_COMMUNE)):
        missing = [x for x in want if x not in columns(c, table)]
        need(not missing, "%s lacks %s" % (table, missing))
    if problems:                                  # the rest would only fail on the same cause
        print(json.dumps({"db": a.db, "problems": problems}, ensure_ascii=False))
        return 1

    n = {"streets": q("SELECT count(*) FROM addr_street"), "houses": q("SELECT count(*) FROM addr"),
         "localities": q("SELECT count(*) FROM addr_locality"), "communes": q("SELECT count(*) FROM addr_commune"),
         "unresolved_streets": q("SELECT count(*) FROM addr_street WHERE locality IS NULL")}
    need(n["streets"] > 0 and n["houses"] > 0 and n["localities"] > 0, "empty index %s" % n)
    for k in ("streets", "houses", "localities", "communes"):
        if k in meta:
            need(int(meta[k]) == n[k], "addr_meta.%s=%s but the table has %d" % (k, meta[k], n[k]))
    need(q("SELECT count(*) FROM addr_street s WHERE locality IS NOT NULL AND NOT EXISTS "
           "(SELECT 1 FROM addr_locality l WHERE l.id = s.locality)") == 0, "streets point at missing localities")
    need(q("SELECT count(*) FROM addr_locality l WHERE commune IS NOT NULL AND NOT EXISTS "
           "(SELECT 1 FROM addr_commune m WHERE m.id = l.commune)") == 0, "localities point at missing communes")
    need(q("SELECT count(*) FROM addr a WHERE NOT EXISTS (SELECT 1 FROM addr_street s WHERE s.id = a.street)") == 0,
         "houses point at missing streets")
    for fts, table in (("addr_street_fts", "addr_street"), ("addr_locality_fts", "addr_locality"),
                       ("addr_commune_fts", "addr_commune")):
        need(q("SELECT count(*) FROM %s_docsize" % fts) == q("SELECT count(*) FROM %s" % table),
             "%s does not index all of %s" % (fts, table))
    need(n["unresolved_streets"] <= a.max_unresolved * n["streets"],
         "%d of %d streets have no locality" % (n["unresolved_streets"], n["streets"]))
    print(json.dumps(dict(n, db=a.db, format=meta.get("format"), source_md5=meta.get("source_md5"),
                          problems=problems), ensure_ascii=False))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
