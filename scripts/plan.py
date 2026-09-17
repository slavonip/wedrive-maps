"""Which packages this run should build.

Reads regions.yml and prints a GITHUB_OUTPUT line the matrix consumes. Split out of the workflow
because a matrix that is computed inline can only be debugged by pushing a commit, while this can
be run locally with the same file it will read in CI.
"""
import json
import sys

import packages as packages_module


def main() -> int:
    wanted = (sys.argv[1] if len(sys.argv) > 1 else "").split()
    config = packages_module.load()

    if wanted:
        # An id need not be named in regions.yml: `md-ro-ua` is derived from its country codes,
        # which is what makes "plan a trip through a new country" a dispatch rather than a
        # commit. It still has to RESOLVE, and the error says what is missing.
        for package_id in wanted:
            try:
                packages_module.countries_of(config, package_id)
            except ValueError as error:
                print(error, file=sys.stderr)
                return 2
        chosen = wanted
    else:
        # The scheduled run builds exactly the named list: an unattended monthly job must not
        # invent work for itself.
        chosen = list(config.get("packages", {}))

    if not chosen:
        print("regions.yml defines no packages", file=sys.stderr)
        return 2

    print("packages=" + json.dumps(chosen))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
