"""How much of a package actually CHANGES between two builds. The measurement behind "no deltas".

    usage: python3 tile-delta.py <old-tile-dir> <new-tile-dir>

`docs/map-factory.md` says a month's update is a full download because there are no deltas. That
was never measured — it was an assumption wearing a finding's clothes, and this is what settles
it. Tiles are separate files inside the archive, so in principle only the changed ones need to
travel. Two forces pull against each other:

  * a month of OSM edits touches a small fraction of the territory, which argues for a tiny delta;
  * hierarchy and shortcut building is global-ish, and GraphIds are assigned during the build, so
    a rebuild may rewrite EVERY tile byte-wise even where the underlying data is identical.

Only the artifacts can say which wins. Reports the answer three ways, because they mean different
things to the download: how many tiles changed, how many BYTES those tiles are, and how much of
the change is size rather than content.
"""
import hashlib
import pathlib
import sys


def tiles(root: pathlib.Path) -> dict:
    """Every graph tile under a tile directory, keyed by its path within it."""
    found = {}
    for path in root.rglob("*.gph"):
        found[str(path.relative_to(root)).replace("\\", "/")] = path
    return found


# The first 64 bytes of a `.gph` are `GraphTileHeader`, and four of them (offsets 32..35) change
# between two builds of IDENTICAL data. Measured 2026-09-17: most tiles in a 16-day-apart rebuild
# of Moldova differ in exactly those four bytes and nowhere else.
#
# THIS IS WHY THE FIRST RUN OF THIS SCRIPT GAVE THE WRONG ANSWER. It reported "100% of tiles
# changed, a delta is not worth building" while it was measuring a build stamp. Comparing the
# BODY as well is the difference between "a delta is impossible" and "a delta needs the stamp
# made deterministic", and those are not the same sentence to put in a design note.
HEADER_BYTES = 64


def digest(path: pathlib.Path, skip_header: bool = False) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        if skip_header:
            handle.read(HEADER_BYTES)
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    old_root, new_root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    old, new = tiles(old_root), tiles(new_root)

    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    shared = sorted(set(old) & set(new))

    changed, identical, changed_bytes = [], [], 0
    stamp_only = []          # identical once the build stamp is excluded
    for name in shared:
        old_size, new_size = old[name].stat().st_size, new[name].stat().st_size
        if old_size == new_size and digest(old[name]) == digest(new[name]):
            identical.append(name)
            continue
        if old_size == new_size and (digest(old[name], skip_header=True)
                                     == digest(new[name], skip_header=True)):
            stamp_only.append(name)
            continue
        changed.append(name)
        changed_bytes += new_size

    total = len(new)
    total_bytes = sum(p.stat().st_size for p in new.values())
    moved_bytes = changed_bytes + sum(new[n].stat().st_size for n in added)

    print(f"old: {len(old)} tiles, {sum(p.stat().st_size for p in old.values()):,} bytes")
    print(f"new: {total} tiles, {total_bytes:,} bytes")
    print()
    unchanged = len(identical) + len(stamp_only)
    print(f"  byte-identical      : {len(identical):6d}  ({len(identical) / max(total, 1):6.1%})")
    print(f"  stamp differs only  : {len(stamp_only):6d}  "
          f"({len(stamp_only) / max(total, 1):6.1%})   <- same roads, different build")
    print(f"  content changed     : {len(changed):6d}  ({len(changed) / max(total, 1):6.1%})")
    print(f"  added               : {len(added):6d}")
    print(f"  removed             : {len(removed):6d}")
    print()
    print(f"as published today, a delta would move {moved_bytes:,} of {total_bytes:,} bytes "
          f"({moved_bytes / max(total_bytes, 1):6.1%})")
    print(f"with the build stamp made deterministic, {unchanged} of {total} tiles would not "
          f"move at all")
    print()

    content_bytes = changed_bytes + sum(new[n].stat().st_size for n in added)
    print("VERDICT")
    if len(changed) > total * 0.5:
        print("  the tiles really are rewritten; a delta cannot help and the full download "
              "stands")
    elif stamp_only:
        print(f"  {len(stamp_only)} tiles carry the same roads under a different build stamp. A "
              f"delta is blocked by four bytes at offset 32, not by the data — it would move "
              f"{content_bytes:,} bytes ({content_bytes / max(total_bytes, 1):.1%}) if the stamp "
              f"were deterministic.")
        print("  WORTH PURSUING, but not by us first: this is an upstream property of "
              "valhalla_build_tiles, and a delta built on a stamp we do not control would "
              "silently degrade to a full download the day it changed.")
    else:
        print(f"  a delta would move {content_bytes / max(total_bytes, 1):.1%} of the package")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
