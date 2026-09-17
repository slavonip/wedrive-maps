"""Run a package's probe plan against its built graph. THE promotion gate.

    usage: python3 probe.py <valhalla-config.json> <probes.json>

Exit code is the gate: non-zero means the package must NOT be promoted, whatever else passed.
Stdlib only — this runs inside the Valhalla container, which has no PyYAML and no network.

Replaces the bash version, which had three defects worth remembering because each one made the
gate lie rather than fail:

  1. `set -e` with `-o pipefail` killed the script at the first command substitution whose
     router exited non-zero — which is exactly what a FAILING probe does. The bridge, timezone
     and border probes never ran, and the run still exited 0.
  2. Probe coordinates lived in the script and were typed from memory. The Moldova bridge pair
     pointed at a junction that is genuinely connected, so a correct graph read as broken.
  3. Expectations were absolute metres carried over from an August 2026 measurement, so an
     ordinary OSM edit failed a healthy graph.

What survives from it: every probe runs even after one fails, because "which of them failed" is
the only useful output from a gate.
"""
import json
import subprocess
import sys

# Going against a one-way must cost at least this much more than going with it. 1.4 is the
# smallest a real diversion round a block can be; two directions agreeing more closely than that
# mean direction is not in the graph at all.
ONEWAY_DETOUR_RATIO = 1.4

# Driving round a bridge must cost at least this many times the straight-line gap. A tile build
# that wrongly joined the two would answer roughly the gap itself and nothing else would.
GRADE_SEPARATION_RATIO = 5

# Any single road route longer than this is not a detour, it is a routing failure wearing one.
SANE_LOCAL_DETOUR_M = 8_000


def route(config: str, frm: list, to: list, extra: dict | None = None) -> dict | None:
    """One route, or None when the router refused. None is a RESULT, not an error: a package
    cut at the frontier answers exactly this, and that is what the border probe is asking."""
    request = {"locations": [{"lat": frm[0], "lon": frm[1]}, {"lat": to[0], "lon": to[1]}],
               "costing": "auto"}
    request.update(extra or {})
    try:
        out = subprocess.run(
            ["valhalla_service", config, "route", json.dumps(request)],
            capture_output=True, text=True, timeout=180).stdout
        return json.loads(out)
    except Exception:                              # noqa: BLE001 — any failure means "no route"
        return None


def metres(response: dict | None) -> int | None:
    try:
        return int(response["trip"]["summary"]["length"] * 1000)
    except Exception:                              # noqa: BLE001
        return None


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def ok(self, name: str, detail: str) -> None:
        print(f"ok   {name}: {detail}")

    def fail(self, name: str, detail: str) -> None:
        print(f"FAIL {name}: {detail}")
        self.failed += 1


def probe_oneway(config: str, probe: dict, report: Report) -> None:
    with_way = metres(route(config, probe["from"], probe["to"]))
    against = metres(route(config, probe["to"], probe["from"]))
    name = probe["name"]
    if with_way is None or against is None:
        report.fail(name, "no route in one or both directions")
        return
    if against < with_way * ONEWAY_DETOUR_RATIO:
        report.fail(name, f"{against} m against vs {with_way} m with — the directions agree, "
                          f"so direction is not in the graph")
        return
    report.ok(name, f"{with_way} m with, {against} m against")


def probe_grade_separation(config: str, probe: dict, report: Report) -> None:
    gap = probe["separation_m"]
    driven = metres(route(config, probe["from"], probe["to"]))
    name = probe["name"]
    if driven is None:
        report.fail(name, "no route at all")
    elif driven < gap * GRADE_SEPARATION_RATIO:
        report.fail(name, f"{driven} m for a {gap} m gap — the bridge has become a junction")
    elif driven > SANE_LOCAL_DETOUR_M:
        report.fail(name, f"{driven} m — far beyond any detour round one bridge")
    else:
        report.ok(name, f"{driven} m round a {gap} m gap")


def probe_timezone(config: str, probe: dict, report: Report) -> None:
    """Valhalla writes a timezone id into every node at BUILD time, from a database it fetches
    separately; without it the build warns once and carries on. CI goes green, the archive
    exists, routes work — and every time-conditional restriction and cross-border arrival time
    is quietly wrong. Only the artifact can be asked, and only through a route carrying
    `date_time`: `locate` does not report it (measured 2026-09-17).

    Two distinct points, because a route needs somewhere to go; the zone reported for the first
    is the answer, and the second is a few hundred metres away in the same zone."""
    lat, lon = probe["at"]
    response = route(config, [lat, lon], [lat + 0.004, lon + 0.004],
                     {"date_time": {"type": 1, "value": "2026-09-17T08:00"}})
    name = probe["name"]
    try:
        got = response["trip"]["locations"][0].get("time_zone_name") or "none"
    except Exception:                              # noqa: BLE001
        got = "none"
    if got == probe["expect"]:
        report.ok(name, got)
    elif got == "none":
        report.fail(name, f"no timezone in the graph, expected {probe['expect']} — this package "
                          f"was built without the timezone database")
    else:
        report.fail(name, f"{got}, expected {probe['expect']}")


def probe_border(config: str, probe: dict, report: Report) -> None:
    """Two countries built separately are each cut at the frontier, so this is the probe the
    whole single-pass multi-PBF build exists for. A cut graph answers 'no route'."""
    driven = metres(route(config, probe["from"], probe["to"]))
    name = probe["name"]
    if driven is None:
        report.fail(name, "NO ROUTE — the frontier has no edges across it, so this package was "
                          "not built in one pass over both extracts")
        return
    km = driven / 1000
    if not probe["min_km"] <= km <= probe["max_km"]:
        report.fail(name, f"{km:.1f} km, expected {probe['min_km']}..{probe['max_km']} km")
        return
    report.ok(name, f"{km:.1f} km")


PROBES = {
    "oneway": probe_oneway,
    "grade_separation": probe_grade_separation,
    "timezone": probe_timezone,
    "border": probe_border,
}


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    config, plan_path = sys.argv[1], sys.argv[2]
    plan = json.load(open(plan_path, encoding="utf-8"))

    package = plan["package"]
    print(f"== probes for {package} "
          f"({', '.join(plan['countries'])}, {len(plan['probes'])} checks) ==")

    report = Report()
    for probe in plan["probes"]:
        runner = PROBES.get(probe["kind"])
        if runner is None:
            report.fail(probe.get("name", "?"), f"unknown probe kind '{probe['kind']}'")
            continue
        runner(config, probe, report)

    verdict = "PROMOTABLE" if report.failed == 0 else f"BLOCKED ({report.failed} failed)"
    print(f"-- {package}: {verdict}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
