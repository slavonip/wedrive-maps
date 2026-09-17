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

    promoted = []
    for meta_path in sorted(root.rglob("*-graph.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        package = meta["package"]
        asset = assets.get(f"{package}.tar")
        if asset is None:
            # Built but not uploaded: nothing to point a car at, so nothing to promote.
            print(f"{package}: no asset, skipped")
            continue

        entry = index["packages"].setdefault(package, {})
        entry["countries"] = entry.get("countries") or meta.get("countries", [])
        entry["regions"] = meta["regions"]
        entry["graph"] = {
            "url": RELEASE + f"{package}.tar",
            "bytes": asset["bytes"],
            "sha256": asset["sha256"],
            "parts": asset["parts"] or None,
            "dataDate": meta["dataDate"],
            # The BUILDER's version. A tile's own version string is whatever built it, and the
            # app has no version gate — so the factory states this and the app enforces it.
            "engineVersion": meta["engineVersion"],
        }
        promoted.append(package)

    index["release"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("promoted:", ", ".join(promoted) if promoted else "nothing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
