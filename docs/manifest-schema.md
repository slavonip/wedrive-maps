# Manifests the app reads

Two pointers at the repository root, each read by the car over `raw.githubusercontent.com`:

| file | kind | written by | what it points at |
|---|---|---|---|
| `manifest.json` | `regional` | regional factory, world-basemap, charging factory | every country package (graph, frontier, features, bus lanes, map, search), the WORLD overview, the charging catalogue |
| `europe-lite.json` | `europe-lite` | Europe Lite factory (`tools/europe-lite/factory/lite_manifest.py`), **lite-overlay** (`scripts/lite-overlay-manifest.py`) | the Lite routing graph and its features, and the Lite overlay below |

A pointer moves only by a commit made AFTER its release was published and every public URL answered
with the stated size. Unknown keys are ignored by the app, so a section added here never breaks an
older build; a section the app needs is guarded by `min_app` instead.

## `europe-lite.json`

The graph part (`graph`, `pbf`, `features`, `gates`, `engine`, `source`, `run`, `tag`, `version`) is
described in `tools/europe-lite/factory/lite_manifest.py`: its files are the assets of the release
`europe-lite-<version>`, under contract names that never carry the version.

### The overlay: `overview` and `places` (2026-11-25)

What a car sees and finds in a country it drives on Europe Lite alone, with no full package. Both
files are the assets of their OWN release, `lite-overlay-<YYYY-MM-DD>[-N]`, built by
`.github/workflows/lite-overlay.yml`; each section names that release. The Lite factory carries both
sections forward verbatim when it publishes a new graph (`lite_manifest.py make --previous`, by
default the current pointer), so a monthly graph never drops them, and they are not files of the
graph's release (`lite_manifest.py files` does not list them).

```json
"overview": {
  "file": "overview.pmtiles",                 // contract name
  "url": ".../releases/download/lite-overlay-2026-11-25/overview.pmtiles",
  "bytes": 98400000, "sha256": "…",
  "release": "lite-overlay-2026-11-25",
  "build": "20261124", "date": "2026-11-24",  // Protomaps planet build
  "maxzoom": 9, "schema": "protomaps-v4",
  "layers": ["boundaries", "earth", "places", "roads", "water"]
},
"places": {
  "format": "wedrive-lite-places/1",
  "file": "places.sqlite.gz",                 // what is downloaded
  "url": ".../releases/download/lite-overlay-2026-11-25/places.sqlite.gz",
  "bytes": 28013979, "sha256": "…",           // of the .gz
  "sqlite_bytes": 56709120, "sqlite_sha256": "…",   // of the unpacked places.sqlite
  "release": "lite-overlay-2026-11-25",
  "rows": 365675, "counts": {"AD": 9, "AL": 2617, …}, "kinds": {"city": 1314, "town": 16356, "village": 348005},
  "excluded": ["RU"]
}
```

**overview.pmtiles** — `scripts/lite-overview.sh`: Protomaps planet, cut by
`polygons/europe-lite.geojson` (Geofabrik `europe.poly`, the polygon the Lite graph is cut from;
RU is not in it), z0–9, only `earth water boundaries places roads` (no landuse: 436 of 555 MB at
z0–9). Held to the PMTiles contract (`scripts/pmtiles_contract.py`, the app's `PmtilesContract`) and
to ≤ 120 MB. The app serves it through its parallel tile reader and drops a tile wherever an
installed country covers it completely, so a country's own map is never drawn twice.

**places.sqlite** — `scripts/lite-places.py`: the `addr_locality` rows of kind city / town / village
of every country of `manifest.json` except RU (a `/1` search file — IS today — gives its `place`
settlements, kind unknown → `town`). The schema is the full search's `place` + `place_fts` (FTS4
unicode61, content table) + `place_cat_pos`, so the app's `SearchDb` reads it with its own queries,
plus `cc` (the row's country) and `district` per row, and a `meta` table (`format`, `rows`,
`created`). Rows are inserted biggest population first. Names are the country's own; other
spellings are in `aliases` and searchable. Gates: ≥ 300 000 rows, every country ≥ 90 % of the
previous catalogue's `counts`, no row of an excluded country, and the first result of München /
Мюнхен / Munich, Thessaloniki / Салоники, Tirana and Pleven.

The app installs both with Europe Lite, under its one "Europe" action, into the Lite asset
directory (`overview.pmtiles`, `places.sqlite`, `overlay.version`), and reads the catalogue only for
countries whose own search is not installed.
