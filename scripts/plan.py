"""Which packages this run should build.

Reads regions.yml and prints a GITHUB_OUTPUT line the matrix consumes. Split out of the workflow
because a matrix that is computed inline can only be debugged by pushing a commit, while this can
be run locally with the same file it will read in CI.
"""
import json
import sys

import yaml


def main() -> int:
    wanted = (sys.argv[1] if len(sys.argv) > 1 else "").split()
    config = yaml.safe_load(open("regions.yml", encoding="utf-8"))
    packages = list(config.get("packages", {}))

    if wanted:
        unknown = [p for p in wanted if p not in packages]
        if unknown:
            print(f"unknown package(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
        packages = wanted

    if not packages:
        print("regions.yml defines no packages", file=sys.stderr)
        return 2

    print("packages=" + json.dumps(packages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
