# Address index — format `wedrive-address/2`

The house-number part of every country's `<cc>.search.sqlite`, built by
`scripts/search_addresses.py` (+ `scripts/search_localities.py`) from the SAME Geofabrik PBF the
country's routing graph is built from (its md5 is recorded in `addr_meta.source_md5`).

Status (2026-10-01): built and verified locally for MD, RO, HU, AT, DE. **Production still publishes
`/1`** until the rollout gates pass (see the end of this file).

## /1 and /2 in one sentence each

* **/1** tied a street to its locality by NAME: `addr_street.city` was the `addr:city` string or the
  nearest settlement of the tile place table. Measured on the published Moldova index, only 68.9 %
  of streets named exactly one settlement, and 166 of 2 559 settlements lay outside the country.
* **/2** gives every locality an id and every street a locality id, decided by GEOGRAPHY, cut to
  the country's own border, with a district and (where the country has one) a commune for context.

## The generic hierarchy

```
country -> district -> commune -> locality -> street -> house
```

One model for every country; only the OSM `admin_level` of each step is per country
(`DISTRICT_LEVEL`, `COMMUNE_LEVEL` in `search_localities.py`):

| country | district (level) | commune (level) | notes |
|---|---|---|---|
| MD | raion / municipiu (4) | — | |
| RO | județ / București (4) | comună / oraș / municipiu (8) | |
| HU | megye / Budapest (6) | — | |
| AT | Bezirk / Statutarstadt (6) | Gemeinde (8) | Wien: Land at 4, no 6 or 8 |
| DE | Landkreis / kreisfreie Stadt (6) | Gemeinde (8) | Berlin, Hamburg: Länder at 4 |
| other | 6 by default | none | until measured |

**`commune` is generic and optional.** It is the unit below the district that tells same-named
villages apart; the app labels it per country ("Comuna", "Gemeinde"). A country without that level
has none, and so do localities where OSM has none (AT statutory cities, DE kreisfreie Städte, Wien,
Berlin, Hamburg) — that is the data, not a gap to fill. The commune is **context and
disambiguation only, never a search step**: the driver still goes country -> locality -> street ->
house.

Choosing the levels for a new country is a measurement, not a copy: census the PBF's
`boundary=administrative` relations by `admin_level` and pick the levels that hold the expected
units by name and count (DE: 400 named level-6 areas = 294 Landkreise + 106 kreisfreie Städte).

## Schema

```sql
addr_locality(id PK, name, aliases JSON, kind city|town|village|hamlet, district TEXT,
              lat, lon, pop, houses, commune INTEGER NULL -> addr_commune.id)
addr_locality_fts   FTS4(name, aliases)            content = addr_locality
addr_commune(id PK, name, display, aliases JSON, district)
addr_commune_fts    FTS4(name, aliases)            content = addr_commune
addr_street(id PK, name, aliases JSON, city, lat, lon, houses,      -- /1 columns, unchanged
            locality INTEGER NULL -> addr_locality.id, city_alt JSON) -- /2
addr_street_fts     FTS4(name, aliases, city, city_alt)             content = addr_street
addr(street, key, osm, num, lat, lon)  PRIMARY KEY(street, key, osm)  WITHOUT ROWID
addr_meta(k, v): format, streets, houses, objects, localities, communes, built, source_md5
```

* `addr_commune.display` is what a context line shows: OSM `official_name`, else `name:prefix` +
  `name`, else `name` ("Comuna Albești", "Municipiul Iași"; AT Gemeinden carry no prefix in OSM).
* `addr_street.city_alt` carries the locality's aliases so "Bender Leningradskaya 52" finds the
  street of "Бендеры".

## Backward compatibility (a /1 app reading a /2 file)

* Every `/1` column of `addr_street` and `addr` is unchanged; `/2` only ADDS columns and tables.
* The app (v25) reads columns BY NAME, never `SELECT *`.
* **The commune is deliberately kept OUT of `addr_locality_fts` and `addr_street_fts`.** v25 matches
  those tables on every column with a candidate limit; a commune name there would pull every
  village of "Comuna X" into a search for X and could push the real answer out of the limit. It has
  its own `addr_commune_fts`, which v25 does not know and never opens.

## How a locality and a street are decided

1. **Country cut.** Localities and house objects outside the country's own admin_level=2 polygon
   (fallback: the Geofabrik polygon) are dropped — the Geofabrik extract overlaps its neighbours.
2. **Localities** are `place=city/town/village/hamlet` nodes and areas. An area joins the place
   point it contains (same name, else a shared alias, else the only point inside); a bare area
   with no point becomes an area-only locality.
3. **District / commune** of a locality: the polygon of that admin level containing its point,
   taking only relations OF that level (`osmium tags-filter` also returns member relations; a
   county's member communes were once read as its district).
4. **A street's locality**, in order: `addr:city` naming a locality (name or alias) within 25 km ->
   the place area containing the house -> a same-named street already resolved within 3 km -> the
   nearest locality within 15 km. `addr:city` spellings seen on >= 3 houses become aliases.

## Duplicate localities (one village mapped twice in OSM)

Conservative, and **only inside one commune id** — different communes never merge, and countries
without a commune level never merge. A same-named locality merges into the most informative one
(wikidata, then population, then a point over an area) only when:

1. both carry the same `wikidata`; or
2. it has neither wikidata nor population and lies within 2 km of an informative one; or
3. it is a bare area-only locality of that name (the area is the territory of the place).

Anything else stays apart, however close. Names and population move to the survivor; streets and
houses follow because the merge happens before association, and the removed copy's point stays an
anchor of the survivor for the "nearest" step. A conflicting `is_in` is merged and reported
(`diagnostic:` on stderr, `merged_with_conflicting_is_in`). Measured: RO 7, AT 41, DE 59 merges.

## Names and normalisation

* street key: case, diacritics (ș/ş), punctuation -> one space; words are kept.
* house key: lower case, spaces out, Cyrillic look-alikes to Latin (10А -> 10a); "10/1", "26D" are
  text. Factory and app share one vector file (`tests/address-keys.json`).
* Aliases: `name:ro/ru/en/uk/hu/de`, alt/old/official/short/int names; a name with ё also gets its
  е spelling (SQLite's unicode61 does not fold Cyrillic). The app additionally asks FTS for both
  spellings of a word with й/ё.

## Houses (what the app shows)

* One button per house KEY in a street, natural order: 2 < 7 < 7a < 7б < 10 < 10/1 < 10a < 11.
  Objects of one key closer than 50 m (`DEDUP_M`) are one house; farther apart they stay separate objects but
  the grid still shows the key once.
* A small letter after a digit is displayed raised ("7Б"), so 7б never reads as 76.
* A number the street does not have: the nearest existing numbers are offered, plus "drive to the
  street" (the street's median point).

## Geometry and performance

Point-in-polygon is a cell grid per polygon set (country, district, commune, place areas): a cell no
boundary segment touches is classified once by its centre; a point in an edge cell is tested
exactly, reading only the segments of its 0.002-degree latitude band (`Bands`). Edge cells are found
by clipping every segment against the cells of its bounding box (an earlier sampling could miss a
segment clipping a cell corner).

| | AT before | AT after |
|---|---|---|
| build | 66 min | 4.3 min |
| country test, all 4 690 140 AT points | 879 s on 16 processes | 3.8 s on one |
| different answers | | 0 |
| index content | | byte-identical |

Scale, all built from the exact published Geofabrik sources (2026-09-27), one desktop core:

| | MD | RO | HU | AT | DE |
|---|---|---|---|---|---|
| localities | 1 682 | 13 870 | 4 681 | 22 143 | 85 635 |
| districts | 37 | 42 | 20 | 93 | 399 |
| communes | — | 3 179 | — | 2 077 | 10 641 |
| streets | 37 261 | 43 365 | 42 544 | 139 339 | 1 101 497 |
| houses | 914 187 | 997 940 | 726 929 | 2 376 267 | 20 078 123 |
| streets without locality | 0 | 0 | 0 | 0 | 0 |
| foreign localities dropped | 57 | 161 | 77 | 102 | 268 |
| same name in one district | 15 | 394 | 29 | 909 | 2 545 |
| same name in one commune | — | 1 | — | 30 | 274 |
| index (whole search file) | 53.3 MB | 75.8 MB | 69.7 MB | 153.8 MB | 1 172 MB |
| build | 50 s | 87 s | 58 s | 4.9 min | 31 min |
| peak RSS | 2.2 GB | 2.4 GB | 2.3 GB | 2.4 GB | 2.7 GB |

## Rollout gates (owner, 2026-10-01)

`/2` replaces `/1` the moment this code is on `main`: the factory has no format switch, and the
scheduled regional run publishes whatever `main` builds. So it stays off `main` until all pass:
final diff reviewed · this document · the released v25 against final `/2` files · RU built locally
· a GitHub Actions dry run that publishes nothing.

## Status 2026-10-02

The five gates above are passed: diff reviewed, this document, released v25 against final `/2`
files (20/20; RU `/2` on v25 PASS), RU built locally (19.5 min, 3.38 GB peak, content identical
after the memory fix), and two GitHub dry runs on `factory-v2-dryrun` (MD, `mode=dry`, publish
skipped): 36977623626 and 36984051594 PASS.

The app side is done: v26 (`apk/2026.10.02-v26`, app `main` `a422947`) reads both `/1` and `/2`
(format detected by the `addr_locality` table), so production can move `/1` -> `/2` with no new
APK. Production is still `/1`: the manifest on `origin/main` (`29065b3`) is
`regional-2026-09-28-23`, all 50 countries `wedrive-address/1`.

What remains before `main` is pushed (and with it the cron starts publishing `/2`):
- the owner's decision to push `main`;
- one `mode=dry` run on GitHub with the heavy countries (DE, RU, and the largest of FR/PL/IT) —
  only MD has run in CI so far, so the runner's time and memory for those are not yet measured;
- a short v26 x `/2` check on that run's artifacts (search code is unchanged since v25).
