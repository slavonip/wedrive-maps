"""Keep the last few dated builds per package; delete what is older.

    usage: python3 prune-release.py [keep]        # default 3

A release has no total size limit, so keeping history is free in the sense that matters. It is
not free in the sense that matters SECOND: a release page with forty assets is one nobody can
read, and an accidental download of the wrong vintage is a real way to put a stale graph in a
car. Three months is enough for §17's rollback and short enough to stay legible.

WHAT IS NEVER DELETED: whatever `index.json` currently points at, whatever a package's newest
build is, and any build that is missing a half. The first because deleting the live asset breaks
every car at once; the second because a package must always have something to fetch; the third
because half-published builds are evidence of a problem and should be looked at, not tidied away.
"""
import json
import subprocess
import sys

HALVES = ("tar", "pmtiles")


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def main() -> int:
    keep = int(sys.argv[1]) if len(sys.argv) > 1 else 3

    listing = json.loads(gh("release", "view", "maps", "--json", "assets"))
    names = [asset["name"] for asset in listing.get("assets", [])]

    try:
        index = json.load(open("index.json", encoding="utf-8"))
    except FileNotFoundError:
        index = {"packages": {}}
    live = {half.get("url", "").rsplit("/", 1)[-1]
            for entry in index.get("packages", {}).values()
            for half in (entry.get("graph"), entry.get("basemap")) if half}

    # name -> (package, date, extension)
    parsed = {}
    for name in names:
        for extension in HALVES:
            if not name.endswith(f".{extension}"):
                continue
            stem = name[: -len(extension) - 1]
            if len(stem) > 11 and stem[-11] == "-" and stem[-6] == "-":
                parsed[name] = (stem[:-11], stem[-10:], extension)

    packages = sorted({package for package, _, _ in parsed.values()})
    removed = 0
    for package in packages:
        dates = sorted({date for p, date, _ in parsed.values() if p == package}, reverse=True)
        doomed = dates[keep:]
        for name, (p, date, _) in sorted(parsed.items()):
            if p != package or date not in doomed:
                continue
            if name in live:
                print(f"keeping {name}: index.json still points at it")
                continue
            halves = [n for n in parsed if parsed[n][:2] == (package, date)]
            if len(halves) < len(HALVES):
                print(f"keeping {name}: {date} is missing a half, which is worth looking at")
                continue
            gh("release", "delete-asset", "maps", name, "--yes")
            print(f"deleted {name}")
            removed += 1
        print(f"{package}: kept {dates[:keep]}")

    print(f"pruned {removed} asset(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
