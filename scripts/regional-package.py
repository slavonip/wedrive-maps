#!/usr/bin/env python3
"""Package the countries rebuilt in this run: deterministic gzip, then parts if over the limit.

    regional-package.py <work-dir> [part-bytes]

For every <code>.package.json that is not "carried":
    <cc>-<date>.tar  ->  gzip -n -6  ->  <cc>-<date>.tar.gz   (no name, no timestamp in the header:
                                                              the same tar gives the same bytes)
then, when the .tar.gz is larger than part-bytes (default split-assets.PART_BYTES, just under
GitHub's 2 GiB asset limit), it is cut into <name>.partNNN with their own sha256 plus the whole
file's sha256. The package description is rewritten: package, bytes, sha256 and parts describe the
.tar.gz; tar_bytes and tar_sha256 keep the archive the tiles were checked in.

Runs AFTER the regression: the plain .tar is what the set was unpacked from, and it is removed
here. The app decides gzip by the .gz file name (RegionalInstaller), so the name is the contract.
"""
import importlib.util, json, os, pathlib, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("split_assets", HERE / "split-assets.py")
split_assets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(split_assets)


def package(work, part_bytes=None):
    work = pathlib.Path(work)
    if part_bytes:
        split_assets.PART_BYTES = int(part_bytes)
    done = []
    for desc_path in sorted(work.glob("*.package.json")):
        d = json.loads(desc_path.read_text(encoding="utf-8"))
        if d.get("carried"):
            continue
        tar = work / d["package"]
        if not d["package"].endswith(".tar") or not tar.is_file():
            raise SystemExit("%s: expected the plain archive %s" % (d["code"], tar))
        gz = tar.with_name(tar.name + ".gz")
        with gz.with_name(gz.name + ".tmp").open("wb") as out:
            subprocess.run(["gzip", "-n", "-6", "-c", str(tar)], stdout=out, check=True)
        gz.with_name(gz.name + ".tmp").replace(gz)
        d["tar_bytes"], d["tar_sha256"] = tar.stat().st_size, split_assets.sha256(tar)
        tar.unlink()
        info = split_assets.split(gz)
        d.update(package=gz.name, bytes=info["bytes"], sha256=info["sha256"], parts=info["parts"])
        desc_path.write_text(json.dumps(d, indent=2), encoding="utf-8")
        print("   %-3s %s: tar %d -> gz %d bytes (%.1f %%)%s"
              % (d["code"], gz.name, d["tar_bytes"], d["bytes"], 100.0 * d["bytes"] / d["tar_bytes"],
                 ", %d parts" % len(info["parts"]) if info["parts"] else ", one asset"))
        done.append(d)
    return done


def main():
    if len(sys.argv) not in (2, 3):
        print(__doc__); return 2
    package(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 and sys.argv[2] else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
