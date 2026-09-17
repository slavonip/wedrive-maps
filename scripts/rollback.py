"""Point a package back at an older build. One commit, no rebuild.

    usage: python3 rollback.py <package> <YYYY-MM-DD>   # point index.json at that build
           python3 rollback.py <package> --list         # what is still on the release

THIS IS §17's ROLLBACK, and it is the reason assets carry their data date. `git revert` restores
code and cannot restore an artifact; the archived known-good build is what gets a car driving
again while the real fault is debugged at leisure. For three weeks the factory uploaded
`md-ro.tar` with `--clobber`, so every build ate the previous one and there was nothing to roll
back TO — the plan said rollback was free and the storage made it impossible.

What this does NOT do, deliberately: rebuild, re-upload, or touch the release. It rewrites the
URLs, sizes and hashes in `index.json` to name an older asset that is still there. The cars read
`index.json`, see a package whose sha256 no longer matches what they hold, and fetch the older
one exactly as they would fetch a newer one — a rollback is an update that happens to point
backwards, which is why nothing on the car has to understand the idea at all.
"""
import json
import subprocess
import sys
import urllib.request

REPO = "slavonip/wedrive-maps"
RELEASE = f"https://github.com/{REPO}/releases/download/maps/"
API = f"https://api.github.com/repos/{REPO}/releases/tags/maps"

HALVES = {"graph": "tar", "basemap": "pmtiles"}


def release_assets() -> dict:
    """Every asset on the rolling release, by name. Public repo, so no token is needed."""
    request = urllib.request.Request(API, headers={"User-Agent": "wedrive-maps/1.0",
                                                   "Accept": "application/vnd.github+json"})
    data = json.load(urllib.request.urlopen(request, timeout=60))
    return {asset["name"]: asset for asset in data.get("assets", [])}


def builds_of(assets: dict, package: str) -> dict:
    """Which dates this package still has on the release, and which halves each has."""
    found: dict = {}
    for name in assets:
        for half, extension in HALVES.items():
            prefix, suffix = f"{package}-", f".{extension}"
            if name.startswith(prefix) and name.endswith(suffix):
                date = name[len(prefix):-len(suffix)]
                if len(date) == 10 and date[4] == "-":
                    found.setdefault(date, {})[half] = name
    return dict(sorted(found.items(), reverse=True))


def sha256_of(asset: dict) -> str | None:
    """GitHub publishes a digest per asset on newer API versions; fall back to downloading.

    Downloading half a gigabyte to roll back is acceptable — it happens once, by hand, when
    something is already wrong — but it is not done unless the API withholds the digest.
    """
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:"):
        return digest.split(":", 1)[1]
    return None


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    package, when = sys.argv[1], sys.argv[2]

    assets = release_assets()
    builds = builds_of(assets, package)

    if when == "--list":
        if not builds:
            print(f"{package}: nothing dated on the release")
            return 1
        for date, halves in builds.items():
            complete = "complete" if len(halves) == len(HALVES) else f"only {', '.join(halves)}"
            size = sum(assets[n]["size"] for n in halves.values()) // 1_048_576
            print(f"  {date}  {complete}, {size} MB")
        return 0

    if when not in builds:
        print(f"{package} has no build dated {when} on the release. Available:", file=sys.stderr)
        for date in builds:
            print(f"  {date}", file=sys.stderr)
        return 2

    index = json.load(open("index.json", encoding="utf-8"))
    entry = index.get("packages", {}).get(package)
    if entry is None:
        print(f"index.json does not carry {package}", file=sys.stderr)
        return 2

    halves = builds[when]
    if len(halves) != len(HALVES):
        # Half a package is worse than an old one: the car would route on one vintage and draw
        # another, which is the vintage-skew failure the manifest exists to surface.
        print(f"{when} has only {', '.join(halves)} — refusing to point at half a package",
              file=sys.stderr)
        return 2

    for half, name in halves.items():
        asset = assets[name]
        current = entry.get(half, {})
        digest = sha256_of(asset)
        if digest is None:
            print(f"::warning::the API did not publish a sha256 for {name}; keeping the one "
                  f"already in index.json, which is WRONG unless it came from this same build")
        entry[half] = {
            **current,
            "url": RELEASE + name,
            "bytes": asset["size"],
            "sha256": digest or current.get("sha256"),
            "dataDate": when,
        }
        print(f"  {half}: {name} ({asset['size'] // 1_048_576} MB)")

    entry["rolledBackAt"] = subprocess.run(
        ["git", "log", "-1", "--format=%cs"], capture_output=True, text=True).stdout.strip()
    json.dump(index, open("index.json", "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    open("index.json", "a", encoding="utf-8").write("\n")

    print(f"{package} now points at {when}. Commit index.json and every car will fetch it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
