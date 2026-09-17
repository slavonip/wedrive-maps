"""What a package id means. The one place that resolves one, so three scripts cannot disagree.

A package is a list of countries. `regions.yml` NAMES the few built every month unattended, and
any other combination is derived from its id: `md-ro-ua` is MD + RO + UA, and it needs no commit
because every fact it depends on — the Geofabrik path, the probes, the borders — already lives
under `countries:` and `borders:`.

WHY DERIVED RATHER THAN ENUMERATED. Thirty countries have a billion subsets and the car drives a
handful. Enumerating them is busywork; enumerating them badly is worse, because a package listed
without probes ships a country tested by nothing (which is exactly what the first md-ro build
did to Romania). Deriving means the expensive half — a country's probe coordinates, found in OSM
and confirmed by a router — is paid ONCE per country, and every combination that country appears
in is covered for free.

GitHub cannot work the combinations out by itself, and must not try: §1 forbids the car
reporting anything back, so nothing upstream knows where it has driven. A person planning a trip
is the input, and `workflow_dispatch` with an id is the whole interface.
"""
import re

import yaml

CONFIG = "regions.yml"

# Country codes as they appear in a package id: two letters, lowercased, joined by dashes.
ID_PATTERN = re.compile(r"^[a-z]{2}(-[a-z]{2})*$")


def load(path: str = CONFIG) -> dict:
    return yaml.safe_load(open(path, encoding="utf-8"))


def countries_of(config: dict, package_id: str) -> list:
    """The country codes this package contains, named or derived.

    Raises ValueError with a sentence a person can act on — these messages are what a failed
    workflow_dispatch shows, and "KeyError: 'md-ro-ua'" is not an instruction.
    """
    named = config.get("packages", {}).get(package_id)
    if named is not None:
        return list(named.get("countries", []))

    if not ID_PATTERN.match(package_id):
        raise ValueError(
            f"'{package_id}' is not a package in regions.yml, and does not read as country "
            f"codes joined by dashes (like md-ro-ua)")

    known = config.get("countries", {})
    codes = [part.upper() for part in package_id.split("-")]
    unknown = [c for c in codes if c not in known]
    if unknown:
        raise ValueError(
            f"'{package_id}' would need {', '.join(unknown)}, which {'has' if len(unknown) == 1 else 'have'} "
            f"no entry under `countries:` in regions.yml. Adding a country is a one-time job: run "
            f"scripts/find-probes.py over one of its cities, confirm the candidates with "
            f"scripts/verify-candidates.py, and add the block.")
    if len(set(codes)) != len(codes):
        raise ValueError(f"'{package_id}' repeats a country")
    return codes


def regions_of(config: dict, package_id: str) -> list:
    """The Geofabrik paths to build from, in the package id's own order."""
    countries = config.get("countries", {})
    paths = []
    for code in countries_of(config, package_id):
        region = countries.get(code, {}).get("region")
        if not region:
            raise ValueError(f"country {code} has no `region:` (its Geofabrik path) in regions.yml")
        paths.append(region)
    return paths


def title_of(config: dict, package_id: str) -> str:
    named = config.get("packages", {}).get(package_id)
    if named and named.get("title"):
        return named["title"]
    countries = config.get("countries", {})
    return " + ".join(countries.get(c, {}).get("name", c)
                      for c in countries_of(config, package_id))


def bbox_of(config: dict, package_id: str) -> list:
    """The basemap's cut, as [W, S, E, N]: the union of the package's countries' boxes.

    A union rather than a per-country cut because `.pmtiles` is ONE file per package, the same
    way the graph is one archive — and because the interesting ground is precisely the strip
    where two countries meet, which a per-country cut would leave to whichever file happened to
    be consulted.
    """
    countries = config.get("countries", {})
    boxes = []
    for code in countries_of(config, package_id):
        box = countries.get(code, {}).get("bbox")
        if not box or len(box) != 4:
            raise ValueError(f"country {code} has no 4-number `bbox:` in regions.yml")
        boxes.append([float(v) for v in box])
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]
