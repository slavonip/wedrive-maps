# wedrive-maps

Offline map and routing data for a personal car navigation app, built from OpenStreetMap by
GitHub Actions and published as release assets.

Map data © OpenStreetMap contributors, available under the
[Open Database License](https://opendatacommons.org/licenses/odbl/). Anything built here is a
derived database and carries the same licence.

---

## What this repository is for

No computer of ours stays in the chain. Adding a country is one entry in
[`regions.yml`](regions.yml); a scheduled workflow fetches the extracts, builds, **proves the
result routes**, publishes the artifacts to a rolling release, and only then updates
[`index.json`](index.json) — which is the only file the car reads.

```
regions.yml ──► Actions ──► build ──► probes ──► release assets
                                        │
                                    fail └──► index.json unchanged, cars stay on last month
```

The separation between *publishing* and *promoting* is the safety property: an asset can exist
in the release while no car has been told about it.

## Packages, not countries

A **package** is what a car installs: one routing graph, built in a single pass from one or more
country extracts.

That is not a preference. Measured on 2026-09-17, building Moldova and Romania separately and
unpacking both into one tile directory does **not** produce a graph covering both:

| | level 0 | level 1 | level 2 |
|---|---|---|---|
| moldova | 3 | 11 | 100 |
| romania | 7 | 48 | 581 |

**35 tiles exist in both extracts with the same path and different content — including all three
of Moldova's level-0 tiles — and none of the 35 are identical.** Whichever is unpacked second
wins, so the other country loses its upper hierarchy: the merged graph could not route *inside
Moldova*, a trip neither package has any trouble with alone. Merging is worse than either half.

So a car that crosses a border needs a package built from both PBFs at once, which is what
`scripts/build-graph.sh` does and what `regions.yml` describes.

## Europe Lite

One routing graph of Europe's arterial network (motorway … tertiary, car ferries, turn
restrictions, and the shortest road chains to every ferry berth), always installed in the car under
the country packages. Its pipeline lives in [`tools/europe-lite/`](tools/europe-lite/) — a copy of
the app repository's pipeline, revision in `tools/europe-lite/PIPELINE_REV` — and
`.github/workflows/europe-lite-factory.yml` rebuilds it from the current Geofabrik extract, runs the
structural, transit and route gates against a stock-Valhalla control graph of the same data, and publishes only on
PASS.

Each release is tagged `europe-lite-YYYY-MM-DD` and always carries the same asset names:
`europe_lite.tar.gz`, `europe_lite.osm.pbf`, `gates.json`, `europe-lite.json`, `SHA256SUMS`.
[`europe-lite.json`](europe-lite.json) at the root is the current pointer the app reads; it moves
only after a release has been uploaded as a draft, downloaded back, verified and published. Details
and the order of switching the factory on: [`tools/europe-lite/README.md`](tools/europe-lite/README.md), "Factory".

## Country packages (the regional factory)

FULL routing graphs, one per country, joined at the borders by portal tables. This is what the
app installs per country; [`manifest.json`](manifest.json) at the root is its pointer
(`kind: "regional"`). `.github/workflows/regional-factory.yml`:

```
precheck   which countries changed: Geofabrik MD5 vs the manifest, the engine (regional-engine.lock),
           the timezone database. Unchanged countries are CARRIED, not rebuilt. Nothing changed ->
           no release.
factory    container = the engine BY DIGEST; changed countries built (extract MD5-checked, pinned
           timezones); carried ones fetched and sha-checked; ALL portal tables; the regression on
           the assembled set (seams, no lost region, km); gzip -n; parts over 1.9 GiB; manifest.
publish    draft -> upload -> download back and check -> publish -> every manifest URL answers
           -> commit manifest.json. mode=verify does the same and deletes the draft instead.
report     an issue when a production run fails; manifest.json is then unchanged.
```

- **Packages** are `<cc>-<data-date>.tar.gz` (gzip -n, deterministic), cut into `.partNNN` only
  above 1.9 GiB. A release holds only what that run built plus every portal table; a carried
  country keeps pointing at the immutable asset of the release it came from. Old releases are
  never deleted.
- **region_id is permanent** (MD 1, RO 2, HU 3, AT 4, DE 5): it is written into the portal tables.
- **The manifest** keeps the app's fields unchanged and adds provenance per country — `source`
  (Geofabrik URL, MD5, sha256, replication), `engine_sha`, `engine_digest`, `timezones`,
  `release`, `portals` — plus `engine`, `timezones` and `pipeline` at the top. `map` and `search`
  are optional and survive a graph rebuild.
- **Modes:** `dry` (build and check), `verify` (also upload a draft, download it back, delete it),
  `publish` (production; only for the whole set of `regional.json`). Control runs on part of the
  set use `countries=MD` or `countries=MD,RO`.
- Tests: `python3 tests/test_regional.py` (run by the precheck of every run).

## Layout

```
regions.yml               what to build; packages, each a list of Geofabrik paths
index.json                what the car reads: urls, sizes, sha256, dates, versions
scripts/build-graph.sh    one package, one pass, several extracts
scripts/probe.sh          the gate: routes with known answers
.github/workflows/        monthly build, publish, promote
```

## The probes are the gate

A graph that builds is not a graph that routes, and the car's runtime has no version check — it
will read a graph built by the wrong Valhalla, or one missing half a country, and simply fail to
find routes later. So every package must answer three questions before it is promoted:

- **one-ways are obeyed** — the same two points on Strada Alexandr Pușkin cost ~322 m with the
  one-way and ~681 m against it;
- **a bridge is not a junction** — Bulevardul Renașterii Naționale over Calea Moșilor: 37 m apart
  in a straight line, ~3 km by road;
- **the border, for packages that claim one** — Chișinău → Iași.

## Adding a country

```yaml
packages:
  md-ro-ua:
    title: Moldova + Romania + Ukraine
    countries: [MD, RO, UA]
    regions:
      - europe/moldova
      - europe/romania
      - europe/ukraine
```

Then run the workflow. Combinations are curated rather than enumerated: thirty countries have a
billion subsets, and this builds the handful one car actually drives.

## Known limits

- **2 GiB per release asset.** Bigger packages ship as parts with a checksum each, plus the
  checksum of the whole, which is what gates activation.
- **The runner's disk is the ceiling**, not its CPU — roughly 14 GB free. Moldova is comfortable;
  a country the size of Germany may not fit at all, and that is measured rather than assumed.
- **Monthly means monthly.** New roads appear up to a month late. Anything time-critical —
  closures, jams, police — comes from a live feed in the app and is unaffected by this repository.
