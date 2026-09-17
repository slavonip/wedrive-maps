"""Prove a basemap DRAWS, before anyone installs it. The other half of the gate.

    usage: python3 probe-basemap.py <package.pmtiles> <probes.json> [<pmtiles-binary>]

Exit code is the gate, same contract as probe.py.

WHY THIS EXISTS SEPARATELY. The routing probes call `valhalla_service route` and nothing else,
so every one of them is about the graph. A `.pmtiles` is not routed, it is rendered — and its
characteristic failure is the worst kind there is: **a style pointed at the wrong vector schema
renders NOTHING, silently.** No error, no warning, a blank screen with a working route line on
it. That was measured in §13 when a style written for the OpenMapTiles schema was pointed at
Protomaps v4, whose layers are `roads`/`water`/`places`, not `transportation`/`park`/`poi`.

So what is asserted is what a style actually depends on: that the archive opens, that it holds
the layers our styles name, that it covers the ground the package claims, and that a tile really
exists over each country in it — because an archive can carry a correct bounding box and no
tiles at all, and its header cannot tell the difference.
"""
import json
import math
import subprocess
import sys

# The source-layers `dhu.json`, `cluster.json` and `hud.json` reference. A basemap missing one of
# these does not fail; it draws everything EXCEPT that, which is why the list is checked rather
# than trusted. Keep it in step with the styles: this is the only place the coupling is written
# down, and the styles are in the other repository.
REQUIRED_LAYERS = ("earth", "water", "landuse", "roads", "buildings", "places", "boundaries")

# The zoom a probe tile is fetched at. Low enough that one tile covers a city and high enough
# that it carries real geometry rather than a continental generalisation.
PROBE_ZOOM = 10


def run(binary: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([binary, *args], capture_output=True, timeout=300)


def tile_xy(lat: float, lon: float, zoom: int) -> tuple:
    """Slippy-map tile containing a point. Standard Web Mercator, written out rather than
    imported so this stays stdlib-only inside whatever container runs it."""
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    radians = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(radians)) / math.pi) / 2.0 * n)
    return x, y


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def ok(self, name: str, detail: str) -> None:
        print(f"ok   {name}: {detail}")

    def fail(self, name: str, detail: str) -> None:
        print(f"FAIL {name}: {detail}")
        self.failed += 1


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    archive, plan_path = sys.argv[1], sys.argv[2]
    binary = sys.argv[3] if len(sys.argv) > 3 else "pmtiles"

    plan = json.load(open(plan_path, encoding="utf-8"))
    package = plan["package"]
    report = Report()

    print(f"== basemap probes for {package} ==")

    # ── it opens at all ─────────────────────────────────────────────────────────────────────
    header = run(binary, "show", archive)
    if header.returncode != 0:
        report.fail("archive opens", (header.stderr.decode(errors="replace").strip()
                                      or "pmtiles show refused it"))
        print(f"-- {package} basemap: BLOCKED (1 failed)")
        return 1
    report.ok("archive opens", header.stdout.decode(errors="replace").splitlines()[0][:80])

    # ── the schema a style will be pointed at ───────────────────────────────────────────────
    metadata = run(binary, "show", "--metadata", archive)
    try:
        meta = json.loads(metadata.stdout.decode(errors="replace"))
    except Exception:                              # noqa: BLE001
        meta = {}
    layers = sorted(layer.get("id") for layer in meta.get("vector_layers", []) if layer.get("id"))
    if not layers:
        report.fail("vector layers", "the archive declares none — a style would render nothing "
                                     "and report no error")
    else:
        missing = [name for name in REQUIRED_LAYERS if name not in layers]
        if missing:
            report.fail("vector layers", f"missing {', '.join(missing)} — every style that names "
                                         f"one draws nothing for it, silently. Present: "
                                         f"{', '.join(layers)}")
        else:
            report.ok("vector layers", f"{len(layers)} present, all {len(REQUIRED_LAYERS)} "
                                       f"required ones among them")

    # ── a tile really exists over each country ──────────────────────────────────────────────
    # An archive can carry a correct bounding box and no tiles inside it; the header cannot tell
    # the difference, and neither can a driver until the screen is blank.
    for probe in plan["probes"]:
        if probe["kind"] != "timezone":            # reuse its point: one per country, urban
            continue
        lat, lon = probe["at"]
        x, y = tile_xy(lat, lon, PROBE_ZOOM)
        name = probe["name"].split(":")[0] + ": tiles present"
        tile = run(binary, "tile", archive, str(PROBE_ZOOM), str(x), str(y))
        if tile.returncode != 0 or len(tile.stdout) == 0:
            report.fail(name, f"no tile at z{PROBE_ZOOM}/{x}/{y} over {lat}, {lon}")
        else:
            report.ok(name, f"z{PROBE_ZOOM}/{x}/{y} is {len(tile.stdout)} bytes")

    verdict = "PROMOTABLE" if report.failed == 0 else f"BLOCKED ({report.failed} failed)"
    print(f"-- {package} basemap: {verdict}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
