"""Promote the packages that passed into index.json — the only file the car reads.

PUBLISHING AND PROMOTING ARE DIFFERENT ACTS, and this script is the second one. Assets can sit
in the release while no car knows about them; a package appears here only once its probes have
passed, so a failed build leaves every car on last month's data rather than on a graph nobody
drove.

Packages already in index.json are kept as they are. A month where Romania fails and Moldova
succeeds must not remove Romania — the car still has it installed and still needs to know what it
has.
"""
import json
import pathlib
import sys
from datetime import datetime, timezone

RELEASE = "https://github.com/slavonip/wedrive-maps/releases/download/maps/"


def main(incoming: str, manifest: str) -> int:
    root = pathlib.Path(incoming)
    out = pathlib.Path(manifest)

    index = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {
        "schema": 1,
        "packages": {},
    }
    assets = json.loads((root / "assets.json").read_text(encoding="utf-8"))

    # A package is TWO artifacts and the car needs both: the graph it routes on and the basemap
    # it draws. §13's rule is explicit — "a region counts as installed only when both are
    # present; a region is stale when EITHER is" — and the first version of this file promoted
    # only the graph, so index.json described a country to route across with no map under it.
    # The halves are promoted independently, because they come from different upstreams and
    # change at different times, and `complete` says whether the pair is whole.
    plans = {}
    for plan_path in sorted(root.rglob("*.json")):
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except Exception:                          # noqa: BLE001
            continue
        if "probes" in plan and "package" in plan:
            plans[plan["package"]] = plan

    promoted = []
    for meta_path in sorted(root.rglob("*-graph.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        package = meta["package"]
        asset = assets.get(f"{package}-{meta['dataDate']}.tar")
        if asset is None:
            # Built but not uploaded: nothing to point a car at, so nothing to promote.
            print(f"{package}: no asset, skipped")
            continue

        if not meta.get("timezones", True):
            # Routes would be right and arrival times wrong the moment the car left its own
            # timezone — which is a worse failure than no update, because nobody is told.
            print(f"{package}: built without timezones, NOT promoted")
            continue

        entry = index["packages"].setdefault(package, {})
        # The country list comes from the plan the run was built to, not from the graph manifest,
        # which never knew it. An empty list here was the first index.json's other defect.
        entry["countries"] = (plans.get(package, {}).get("countries")
                              or entry.get("countries") or meta.get("countries", []))
        entry["title"] = plans.get(package, {}).get("title") or entry.get("title")
        entry["regions"] = meta["regions"]
        entry["graph"] = {
            "url": RELEASE + f"{package}-{meta['dataDate']}.tar",
            "bytes": asset["bytes"],
            "sha256": asset["sha256"],
            "parts": asset["parts"] or None,
            "dataDate": meta["dataDate"],
            # The BUILDER's version. A tile's own version string is whatever built it, and the
            # app has no version gate — so the factory states this and the app enforces it.
            "engineVersion": meta["engineVersion"],
        }
        promoted.append(package)

    # ── the other half ──────────────────────────────────────────────────────────────────────
    for meta_path in sorted(root.rglob("*-basemap.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        package = meta["package"]
        asset = assets.get(f"{package}-{meta['dataDate']}.pmtiles")
        if asset is None:
            print(f"{package}: basemap built but not uploaded, skipped")
            continue

        entry = index["packages"].setdefault(package, {})
        entry["basemap"] = {
            "url": RELEASE + f"{package}-{meta['dataDate']}.pmtiles",
            "bytes": asset["bytes"],
            "sha256": asset["sha256"],
            "parts": asset["parts"] or None,
            "dataDate": meta["dataDate"],
            # SCHEMA IS LOAD-BEARING: a style pointed at the wrong one renders NOTHING and
            # reports no error (§13). The car refuses a basemap whose schema its styles cannot
            # read, rather than showing a blank screen with a working route line on it.
            "schema": meta.get("schema"),
            "maxzoom": meta.get("maxzoom"),
            "bbox": meta.get("bbox"),
            "upstreamBuild": meta.get("build"),
        }
        if package not in promoted:
            promoted.append(package)

    # ── the third artifact: names to search ─────────────────────────────────────────────────
    #
    # Small enough to be unremarkable (353 KB for Moldova) and the difference between a search
    # box that works offline and one that says "not built". Promoted independently of the other
    # two: a package whose index failed to build still routes and still draws.
    for meta_path in sorted(root.rglob("*-places-meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        package = meta["package"]
        asset = assets.get(f"{package}-places.json")
        if asset is None:
            print(f"{package}: place index built but not uploaded, skipped")
            continue
        entry = index["packages"].setdefault(package, {})
        entry["places"] = {
            "url": RELEASE + f"{package}-places.json",
            "bytes": asset["bytes"],
            "sha256": asset["sha256"],
            "count": meta.get("count"),
        }
        if package not in promoted:
            promoted.append(package)

    # VINTAGE SKEW IS A REAL FAILURE MODE (§13): the two halves come from different snapshots
    # unless deliberately paired, and the symptom — a road drawn that the router refuses, or a
    # route down a road that is not drawn — reads as "the app is broken" rather than as "these
    # files are six weeks apart". So it is stated per package rather than left to be discovered.
    for package, entry in index["packages"].items():
        halves = [half for half in ("graph", "basemap") if half in entry]
        entry["complete"] = len(halves) == 2
        if entry["complete"]:
            dates = sorted(entry[half]["dataDate"] for half in halves)
            entry["vintageSkewDays"] = (
                datetime.fromisoformat(dates[1]) - datetime.fromisoformat(dates[0])).days
        else:
            print(f"{package}: only {halves[0] if halves else 'nothing'} — "
                  f"NOT a complete package")

    # ── the names a driver reads ────────────────────────────────────────────────────────────
    #
    # The car's Maps screen is a list of COUNTRIES, not of packages: packages are how the graph
    # has to be built (one pass over several PBFs) and mean nothing to a driver. So the manifest
    # carries the country names, and the app does not hold a table of its own — adding a country
    # to regions.yml must not require an APK.
    names = {}
    for plan in plans.values():
        for code in plan.get("countries", []):
            names.setdefault(code, code)
    for package_id, entry in index["packages"].items():
        for code in entry.get("countries", []):
            names.setdefault(code, code)
    # Prefer the real names where a plan carried them.
    for plan in plans.values():
        for code, name in (plan.get("countryNames") or {}).items():
            names[code] = name
    if names:
        index["countries"] = dict(sorted(names.items()))

    index["release"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("promoted:", ", ".join(promoted) if promoted else "nothing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
