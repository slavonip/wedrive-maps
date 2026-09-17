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

    # THREE OUTCOMES, AND THIS PROBE USED TO REPORT TWO OF THEM AS THE SAME SENTENCE.
    # `route()` returns None for any refusal, and an unroutable pair therefore produced
    # "no timezone in the graph — this package was built without the timezone database": a
    # confident diagnosis of a cause the probe had no way to observe. It cost a real
    # investigation on the four-country build, where three countries reported their zone
    # correctly — which is the one thing a missing database cannot do.
    #
    # Naming the wrong cause is worse than naming none. Each outcome now says only what was
    # actually seen, and the two-point pair is reported so a bad coordinate is visible as a bad
    # coordinate.
    if response is None or metres(response) is None:
        report.fail(name, f"the two probe points do not route "
                          f"({lat:.4f},{lon:.4f} → {lat + 0.004:.4f},{lon + 0.004:.4f}), "
                          f"so this says NOTHING about timezones — fix the coordinates")
        return

    got = response["trip"]["locations"][0].get("time_zone_name") or None
    if got == probe["expect"]:
        report.ok(name, got)
    elif got is None:
        # A route came back and carries no zone. THIS is the symptom of a graph built without
        # the timezone database — and it is only diagnostic when the other countries in the same
        # build report theirs, which is why the report prints them all before failing.
        report.fail(name, f"the route succeeded but carries no timezone, expected "
                          f"{probe['expect']} — these nodes were built without one")
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


def probe_admin(config: str, probe: dict, report: Report) -> None:
    """Which COUNTRY does the finished graph think this point is in?

    `valhalla_build_admins` drops admin records routinely — 30 of them on the four-country build —
    and upstream says to ignore that on an extract. `check-admins.py` verifies that none of the
    dropped records name a country we are building; this verifies the other half, that the records
    which were NOT dropped actually landed in the graph.

    **No routing probe can see this.** Admin records carry driving side, access defaults and
    country-crossing costs. A graph that lost them still returns routes, still returns plausible
    distances, and is quietly wrong about which side of the road to drive on.

    THE ROUTE IS THE PRIMARY SOURCE, AND THE FIRST VERSION OF THIS PROBE GOT THAT WRONG. It asked
    `trace_attributes`, which map-matches a three-point shape 400 m long — and that fails to match
    anything at some perfectly ordinary places. Measured on the four-country build: Budapest and
    Chișinău matched no edges while routing from those exact points worked, so the probe announced
    "the admin records did not land" for two countries whose records were fine. That is the same
    mistake as the timezone probe made about Vienna, shipped again within the hour, which is how
    strong the pull is to read one silent answer as one specific cause.

    So: route first, since a route is also what a driver's question is; fall back to the trace
    only if the route carries no admin block; and if neither source says anything, say THAT rather
    than inventing a reason.
    """
    lat, lon = probe["at"]
    name = probe["name"]

    response = route(config, [lat, lon], [lat + 0.004, lon + 0.004])
    if response is None or metres(response) is None:
        report.fail(name, f"the two probe points do not route "
                          f"({lat:.4f},{lon:.4f} → {lat + 0.004:.4f},{lon + 0.004:.4f}), "
                          f"so this says NOTHING about admin data — fix the coordinates")
        return

    codes = _country_codes(response.get("trip", {}).get("admins"))
    source = "route"

    if not codes:
        # SECOND: trace the ROUTE'S OWN SHAPE. A straight three-point shape 400 m long is what
        # failed at Budapest and Chișinău — it runs off the road network and map-matches nothing.
        # The route we just computed is ON the network by construction, so tracing its polyline
        # cannot fail for that reason. `trace_attributes` takes `encoded_polyline` directly, so
        # no decoding is needed.
        shape = None
        try:
            shape = response["trip"]["legs"][0]["shape"]
        except Exception:                          # noqa: BLE001
            pass
        if shape:
            traced = _trace_polyline(config, shape)
            codes = _country_codes((traced or {}).get("admins"))
            source = "the route's own shape"

    if not codes:
        # THIRD: the straight shape, in case this Valhalla answers there and not above.
        traced = _trace(config, lat, lon)
        codes = _country_codes((traced or {}).get("admins"))
        source = "a straight trace"

    if not codes:
        report.fail(name, f"no country reported here by the route, by a trace of its own shape, "
                          f"or by a straight trace — expected {probe['expect']}. Three sources "
                          f"silent is evidence the records did not land; check what "
                          f"check-admins.py counted before concluding it")
    elif probe["expect"] in codes:
        report.ok(name, codes[0] if len(codes) == 1 else "/".join(codes))
    else:
        report.fail(name, f"{'/'.join(codes)} (from the {source}), expected {probe['expect']}")


def _country_codes(admins) -> list:
    return sorted({a.get("country_code") for a in (admins or []) if a.get("country_code")})


def _trace_polyline(config: str, encoded: str):
    """Map-match an ENCODED polyline — used for a route's own shape, which is on the network by
    construction and therefore cannot fail to match for being off-road."""
    request = {"encoded_polyline": encoded, "costing": "auto", "shape_match": "map_snap"}
    try:
        out = subprocess.run(["valhalla_service", config, "trace_attributes", json.dumps(request)],
                             capture_output=True, text=True, timeout=180).stdout
        return json.loads(out)
    except Exception:                              # noqa: BLE001
        return None


def _trace(config: str, lat: float, lon: float):
    """A map-matched trace over a short shape. Returns None on any failure — and a caller must
    treat that as "no answer", never as "no data"."""
    request = {
        "shape": [{"lat": lat, "lon": lon},
                  {"lat": lat + 0.002, "lon": lon + 0.002},
                  {"lat": lat + 0.004, "lon": lon + 0.004}],
        "costing": "auto",
        "shape_match": "map_snap",
    }
    try:
        out = subprocess.run(["valhalla_service", config, "trace_attributes", json.dumps(request)],
                             capture_output=True, text=True, timeout=180).stdout
        return json.loads(out)
    except Exception:                              # noqa: BLE001
        return None


PROBES = {
    "oneway": probe_oneway,
    "grade_separation": probe_grade_separation,
    "timezone": probe_timezone,
    "admin": probe_admin,
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
