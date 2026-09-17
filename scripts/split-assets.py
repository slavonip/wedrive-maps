"""Cut anything over GitHub's per-asset limit into parts the car can reassemble.

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


def main(root: str) -> int:
    base = pathlib.Path(root)
    report = {}
    for path in sorted(base.rglob("*")):
        if path.suffix in {".tar", ".pmtiles"} and path.is_file():
            report[path.name] = split(path)
            parts = len(report[path.name]["parts"])
            print(f"{path.name}: {report[path.name]['bytes']} bytes"
                  + (f", {parts} parts" if parts else ""))
    (base / "assets.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "incoming"))
