"""Promote per-country tile sets into index-tiles.json — the manifest for the cut pipeline.

    usage: manifest-tiles.py <incoming-dir> <index-tiles.json>

Deliberately a SEPARATE file from index.json (owner, 2026-09-17: do the rework in a copy). The
package pipeline keeps publishing whole packages that the car uses today; this one publishes
countries, and nothing switches over until the cut path has proved itself in the vehicle.

**THE BUILD ID IS THE WHOLE CONTRACT.** A `GraphId` carries an index assigned during the build,
so an edge in Moldova's tile refers to a node in Romania's by a number that means something only
within ONE build. Two countries from different builds give edges pointing at the wrong nodes,
with no error and no crash — just wrong routes. So every country carries its build id, the
manifest states which id is current, and the car refuses to hold a mixture.

That is also why this manifest replaces its country list wholesale instead of merging: a run
that rebuilt only some countries would leave the rest advertising an id that no longer matches,
which is precisely the state that must never exist.
"""
import json
import pathlib
import sys
from datetime import datetime, timezone

RELEASE = "https://github.com/slavonip/wedrive-maps/releases/download/tiles/"


def main(incoming: str, manifest: str) -> int:
    root = pathlib.Path(incoming)
    out = pathlib.Path(manifest)

    metas = []
    for path in sorted(root.rglob("*-tiles.json")):
        metas.append(json.loads(path.read_text(encoding="utf-8")))
    if not metas:
        print("no country tile manifests found; nothing to promote")
        return 0

    build_ids = {meta["buildId"] for meta in metas}
    if len(build_ids) != 1:
        # Refusing here is the point: publishing a mixture would hand cars a set of countries
        # that cannot legally be held together, and nothing downstream could tell.
        print(f"refusing to promote a mixture of builds: {', '.join(sorted(build_ids))}",
              file=sys.stderr)
        return 2
    build_id = build_ids.pop()

    if any(not meta.get("timezones", True) for meta in metas):
        print(f"{build_id}: built without timezones, NOT promoted")
        return 0

    index = {
        "schema": 1,
        "buildId": build_id,
        "region": metas[0].get("region"),
        "engineVersion": metas[0].get("engineVersion"),
        "dataDate": metas[0].get("dataDate"),
        # Every country in the build, whether or not a given car wants it. The list is what the
        # Maps screen ticks; the ids are what stop it mixing vintages.
        "countries": {},
        "release": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }

    for meta in sorted(metas, key=lambda m: m["country"]):
        code = meta["country"]
        index["countries"][code] = {
            "url": RELEASE + f"{code}-tiles.tar",
            "bytes": meta["bytes"],
            "sha256": meta["sha256"],
            "tiles": meta.get("tiles"),
            "dataDate": meta.get("dataDate"),
            "buildId": meta["buildId"],
        }

    out.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    total = sum(c["bytes"] for c in index["countries"].values())
    print(f"promoted {build_id}: {len(index['countries'])} countries, "
          f"{total / 1_048_576:.0f} MB in total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
