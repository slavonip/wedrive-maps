"""Name every artifact by its data date, then cut anything over GitHub's per-asset limit.

THE DATE IN THE NAME IS WHAT MAKES ROLLBACK POSSIBLE, and it was the first thing this factory
got wrong. Assets were uploaded as `md-ro.tar` with `--clobber`, so a new build ATE the previous
one: last month's package simply ceased to exist, and §17's rule — keep the last known-good
artifact so a bad update costs a reinstall rather than a car — could not be honoured at all. A
release has no total size limit, so keeping several months is free; only the per-asset cap below
is real.

With `md-ro-2026-09-17.tar` on the release and `index.json` naming which date is current, a
rollback is one commit to `index.json` and no rebuild whatsoever (scripts/rollback.py).


A release asset is capped at just under 2 GiB, while a release itself has no size limit and no
bandwidth limit — so the cap is the only thing standing between us and shipping Germany. Parts
are plain byte ranges: `<name>.part001`, `.part002`, … concatenated back in order.

Each part carries its own sha256 so a resumed download can be checked piece by piece, and the
WHOLE file's sha256 is written beside them. The whole is what gates activation; a set of valid
parts that assemble into the wrong file is exactly the failure a per-part check alone would miss.
"""
import hashlib
import json
import pathlib
import sys

# Just under 2 GiB, with room for the multipart overhead GitHub adds.
PART_BYTES = 1_900_000_000


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def split(path: pathlib.Path) -> dict:
    whole = {"bytes": path.stat().st_size, "sha256": sha256(path), "parts": []}
    if whole["bytes"] <= PART_BYTES:
        return whole

    with path.open("rb") as f:
        index = 1
        while True:
            chunk = f.read(PART_BYTES)
            if not chunk:
                break
            part = path.with_suffix(path.suffix + f".part{index:03d}")
            part.write_bytes(chunk)
            whole["parts"].append(
                {"name": part.name, "bytes": len(chunk), "sha256": sha256(part)}
            )
            index += 1
    # The original is removed: uploading both would double the release for no reason, and the
    # parts are the only form that fits the limit.
    path.unlink()
    return whole


def data_date_beside(path: pathlib.Path) -> str | None:
    """The dataDate from whichever manifest describes this artifact.

    Taken from the manifest rather than from the clock, because the date that matters is the
    OSM snapshot the artifact was built from — a rebuild in October from September data is
    September's package, and naming it October's would make two different files claim the same
    vintage.
    """
    suffix = "-graph.json" if path.suffix == ".tar" else "-basemap.json"
    manifest = path.with_name(path.stem + suffix)
    if not manifest.exists():
        # Artifacts and manifests are uploaded together but land in per-job directories; look
        # for it anywhere under the same root before giving up.
        candidates = list(path.parents[1].rglob(path.stem + suffix))
        if not candidates:
            return None
        manifest = candidates[0]
    try:
        return json.loads(manifest.read_text(encoding="utf-8")).get("dataDate")
    except Exception:                              # noqa: BLE001
        return None


def main(root: str) -> int:
    base = pathlib.Path(root)
    report = {}
    for path in sorted(base.rglob("*")):
        if path.suffix not in {".tar", ".pmtiles"} or not path.is_file():
            continue

        date = data_date_beside(path)
        if date:
            dated = path.with_name(f"{path.stem}-{date}{path.suffix}")
            path.rename(dated)
            path = dated
        else:
            # Undated means unrollbackable, so say so rather than quietly publishing a name that
            # the next build will overwrite.
            print(f"::warning::{path.name} has no dataDate; it will be overwritten by the next "
                  f"build and cannot be rolled back to")

        report[path.name] = split(path)
        parts = len(report[path.name]["parts"])
        print(f"{path.name}: {report[path.name]['bytes']} bytes"
              + (f", {parts} parts" if parts else ""))
    (base / "assets.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "incoming"))
