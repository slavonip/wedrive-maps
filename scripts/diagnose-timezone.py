"""Ask the GRAPH what timezone it holds, rather than inferring one from a route.

    usage: diagnose-timezone.py <tile-dir> --label "master" [--points diagnostics/timezone-points.json]

WHY THIS EXISTS. Austria failed its timezone probe on the four-country build — "no timezone" —
while Hungary, Moldova and Romania reported theirs correctly from the same build, against a
dataset that had passed an exhaustive compatibility gate and that contains `Europe/Vienna`, on a
runtime whose own tzdata also contains it. Every explanation that fits three countries fails to
fit the fourth.

The route-level probe cannot take this further, because it observes one derived string. This
observes the graph:

    a point
      → trace_attributes with node.time_zone       what the NODE actually holds
      → route with date_time                       what a caller would be told
      → compared, at several points per country

and it is run against BOTH the master tile directory and the country cut, which is the shortest
discriminator available:

    master PASS, cut FAIL   →  the cut drops something the timezone depends on
    master FAIL, cut FAIL   →  it was never written, so the fault is in the build
    both PASS               →  the route-level probe is what is wrong

Several points per country separate "the whole country" from "this one coordinate", which is the
difference between a build-time assignment fault and a bad probe coordinate. A country known to
pass is included as a control: a diagnostic that reports a problem everywhere is measuring itself.

This DIAGNOSES; it never gates. It prints what it saw and exits 0 whatever it finds, because a
diagnostic that can fail a build invites being made to pass.
"""
import argparse
import json
import pathlib
import subprocess
import sys

TRACE_ATTRIBUTES = ["node.time_zone", "edge.id", "edge.begin_shape_index"]


def config_for(tile_dir: str, path: str) -> str:
    subprocess.run(["valhalla_build_config", "--mjolnir-tile-dir", tile_dir],
                   stdout=open(path, "w"), check=True)
    config = json.load(open(path))
    # Popped, never blanked: an empty string sends Valhalla looking for a file called "" and
    # yields a graph with no tiles and no error at all.
    config["mjolnir"].pop("tile_extract", None)
    json.dump(config, open(path, "w"), indent=2)
    return path


def call(config: str, action: str, request: dict):
    try:
        done = subprocess.run(["valhalla_service", config, action, json.dumps(request)],
                              capture_output=True, text=True, timeout=120)
        return json.loads(done.stdout)
    except Exception:                               # noqa: BLE001 — any failure is "no answer"
        return None


def zone_from_route(config: str, lat: float, lon: float):
    """What a CALLER is told — the same thing the gate's probe reads."""
    answer = call(config, "route", {
        "locations": [{"lat": lat, "lon": lon}, {"lat": lat + 0.004, "lon": lon + 0.004}],
        "costing": "auto",
        "date_time": {"type": 1, "value": "2026-09-17T08:00"},
    })
    if not answer or "trip" not in answer:
        return None, "no route"
    locations = answer["trip"].get("locations") or []
    if not locations:
        return None, "no locations"
    return locations[0].get("time_zone_name"), f"{answer['trip']['summary']['length']:.2f} km"


def zone_from_graph(config: str, lat: float, lon: float):
    """What the NODES hold. `trace_attributes` is the only public way in: `locate` does not
    report a timezone (measured 2026-09-17), and no API enumerates node adjacency.

    Requested through a FILTER list so `matched_points` is never serialised — Valhalla writes
    UINT64_MAX in `matched_points[].edge_index` for an unmatched point and the typed wrapper
    cannot decode it, which loses the whole response.
    """
    answer = call(config, "trace_attributes", {
        "shape": [{"lat": lat, "lon": lon},
                  {"lat": lat + 0.002, "lon": lon + 0.002},
                  {"lat": lat + 0.004, "lon": lon + 0.004}],
        "costing": "auto",
        "shape_match": "map_snap",
        "filters": {"action": "include", "attributes": TRACE_ATTRIBUTES},
    })
    if not answer:
        return None, "unparseable"
    edges = answer.get("edges") or []
    if not edges:
        return None, "no edges matched"
    zones = []
    for edge in edges:
        for key in ("end_node", "begin_node"):
            node = edge.get(key) or {}
            if "time_zone" in node:
                zones.append(node["time_zone"])
    if not zones:
        # The attribute was requested and did not come back. That is itself the finding: either
        # this Valhalla does not expose it, or the nodes carry nothing to expose.
        return None, f"{len(edges)} edges, none carrying node.time_zone"
    unique = sorted(set(zones))
    return unique[0] if len(unique) == 1 else "/".join(unique), f"{len(edges)} edges"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tile_dir")
    parser.add_argument("--label", default="")
    parser.add_argument("--points", default=None)
    parser.add_argument("--only", default=None, help="restrict to one country code")
    args = parser.parse_args()

    here = pathlib.Path(__file__).resolve().parent.parent
    points_file = pathlib.Path(args.points) if args.points \
        else here / "diagnostics" / "timezone-points.json"
    points = json.loads(points_file.read_text(encoding="utf-8"))

    if not pathlib.Path(args.tile_dir).is_dir():
        print(f"   {args.label}: {args.tile_dir} is not there — nothing to diagnose")
        return 0

    config = config_for(args.tile_dir, f"/tmp/diag_{args.label or 'x'}.json")
    print(f"\n=== {args.label or args.tile_dir} ===")
    print(f"   {'point':26s} {'graph node':22s} {'route reports':22s} detail")

    for point in points:
        if args.only and point["country"] != args.only:
            continue
        lat, lon = point["at"]
        graph_zone, graph_detail = zone_from_graph(config, lat, lon)
        route_zone, route_detail = zone_from_route(config, lat, lon)
        agree = "" if graph_zone == route_zone else "   <- DISAGREE"
        expected = point["expect"]
        mark = "ok " if route_zone == expected else "   "
        print(f"{mark}{point['name']:26s} {str(graph_zone):22s} {str(route_zone):22s} "
              f"{graph_detail}; {route_detail}{agree}")

    print("   (expected per point is in diagnostics/timezone-points.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
