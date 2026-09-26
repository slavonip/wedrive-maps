#!/usr/bin/env python3
"""Carry an unchanged country into this run: fetch its PUBLISHED package and verify it.

    regional-carry.py <manifest.json> <CODE> <work-dir>

The package is needed locally only to build the portal tables and to run the regression on the
whole set; it is NOT uploaded again. The manifest keeps pointing at the immutable asset of the
release it was published in, which is why the original URL, parts and provenance are written into
<work>/<code>.package.json with "carried": true.

Every byte is checked: each part against its own sha256, the joined file against the package
sha256 and size. The previous version of this step trusted the download.
"""
import hashlib, json, os, subprocess, sys

KEEP = ("graph_version", "tiles", "package", "bytes", "sha256", "engine", "engine_sha",
        "source", "timezones", "tar_bytes", "tar_sha256", "map", "search")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url, dst):
    tmp = dst + ".part"
    subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", tmp, url], check=True)
    os.replace(tmp, dst)


def carry(manifest, code, work, fetch=fetch):
    r = manifest.get("regions", {}).get(code)
    if not r:
        raise SystemExit("%s is not in the current manifest: it cannot be carried" % code)
    dst = os.path.join(work, r["package"])
    base = r["url"].rsplit("/", 1)[0]
    if not (os.path.isfile(dst) and os.path.getsize(dst) == r["bytes"] and sha256(dst) == r["sha256"]):
        parts = r.get("parts") or []
        if parts:
            print("   %s: %s in %d parts" % (code, r["package"], len(parts)))
            with open(dst + ".join", "wb") as out:
                for p in parts:
                    tmp = os.path.join(work, p["name"])
                    fetch("%s/%s" % (base, p["name"]), tmp)
                    if os.path.getsize(tmp) != p["bytes"] or sha256(tmp) != p["sha256"]:
                        raise SystemExit("%s: part %s does not match the manifest" % (code, p["name"]))
                    with open(tmp, "rb") as f:
                        for chunk in iter(lambda: f.read(1 << 20), b""):
                            out.write(chunk)
                    os.remove(tmp)
            os.replace(dst + ".join", dst)
        else:
            print("   %s: %s" % (code, r["url"]))
            fetch(r["url"], dst)
        if os.path.getsize(dst) != r["bytes"] or sha256(dst) != r["sha256"]:
            os.remove(dst)
            raise SystemExit("%s: %s does not match the manifest (bytes/sha256)" % (code, r["package"]))
    desc = {k: r[k] for k in KEEP if k in r}
    desc.update({"code": code, "carried": True, "url": r["url"], "parts": r.get("parts") or []})
    # The frontier travels with its country and is checked like the package. A country published
    # before frontiers existed has none; regional-frontier.py backfill makes it after unpacking.
    fr = r.get("frontier")
    if fr:
        fdst = os.path.join(work, fr["file"])
        if not (os.path.isfile(fdst) and sha256(fdst) == fr["sha256"]):
            fetch(fr["url"], fdst)
        if os.path.getsize(fdst) != fr["bytes"] or sha256(fdst) != fr["sha256"]:
            os.remove(fdst)
            raise SystemExit("%s: frontier %s does not match the manifest" % (code, fr["file"]))
        desc["frontier"] = dict(fr)
    # Navigation features travel the same way: the carried country keeps the very file of its own
    # graph version. One published before features existed simply has none until it is rebuilt.
    fe = r.get("features")
    if fe:
        edst = os.path.join(work, fe["file"])
        if not (os.path.isfile(edst) and sha256(edst) == fe["sha256"]):
            fetch(fe["url"], edst)
        if os.path.getsize(edst) != fe["bytes"] or sha256(edst) != fe["sha256"]:
            os.remove(edst)
            raise SystemExit("%s: features %s does not match the manifest" % (code, fe["file"]))
        desc["features"] = dict(fe)
    with open(os.path.join(work, "%s.package.json" % code.lower()), "w", encoding="utf-8") as f:
        json.dump(desc, f, indent=2)
    print("   %s carried: %s (%d bytes, sha256 ok)" % (code, r["package"], r["bytes"]))
    return desc


def main():
    if len(sys.argv) != 4:
        print(__doc__); return 2
    manifest = json.load(open(sys.argv[1], encoding="utf-8"))
    os.makedirs(sys.argv[3], exist_ok=True)
    carry(manifest, sys.argv[2].upper(), sys.argv[3])
    return 0


if __name__ == "__main__":
    sys.exit(main())
