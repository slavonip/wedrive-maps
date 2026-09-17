"""Turning a list of countries into a list of probes. Shared by both pipelines.

A country's one-way street, its grade separation and its timezone are the same facts whichever
way its tiles are shipped — whole in a package, or cut out of a super-region. Having each
pipeline build its own probe list would be the surest way to let the two drift, and a drifted
gate is worse than no gate, because it still looks like one.

A dash is not legal in a Python module name, which is why this is a module and `probe-plan.py`
and `region-plan.py` are the scripts.
"""
import itertools

REQUIRED_COUNTRY_PROBES = ("region", "bbox", "timezone", "timezone_at", "oneway",
                           "grade_separation")


def border_key(a: str, b: str) -> str:
    """Borders are undirected, so the key is the pair sorted — MD-RO, never RO-MD."""
    return "-".join(sorted((a, b)))


def plan_for_countries(config: dict, plan_id: str, members: list) -> dict:
    """Every probe that applies to this set of countries, plus what they are made of.

    The border probes appear only for pairs BOTH of which are in the set, which is what makes
    the same function serve a single country's cut (no borders, and rightly so — a cut on its
    own must NOT reach the next country) and the assembled whole (every border, because that is
    the only configuration where a frontier can be crossed).
    """
    countries = config.get("countries", {})
    borders = config.get("borders", {})
    members = sorted(members)

    probes = []
    for code in members:
        c = countries[code]
        label = c.get("name", code)

        lat, lon = c["timezone_at"]
        probes.append({"kind": "timezone", "name": f"{label}: timezone",
                       "at": [lat, lon], "expect": c["timezone"]})

        # Same coordinate, different question, and no extra configuration: the country code IS
        # the key in regions.yml. Admin records carry driving side, access defaults and
        # country-crossing costs, none of which a route coming back would reveal as missing.
        probes.append({"kind": "admin", "name": f"{label}: country",
                       "at": [lat, lon], "expect": code})

        f_lat, f_lon, t_lat, t_lon = c["oneway"]
        probes.append({"kind": "oneway", "name": f"{label}: one-way",
                       "from": [f_lat, f_lon], "to": [t_lat, t_lon]})

        b_lat, b_lon, u_lat, u_lon, separation = c["grade_separation"]
        probes.append({"kind": "grade_separation", "name": f"{label}: grade separation",
                       "from": [b_lat, b_lon], "to": [u_lat, u_lon],
                       "separation_m": separation})

    for a, b in itertools.combinations(members, 2):
        entry = borders.get(border_key(a, b), {})
        if entry.get("adjacent") is False:
            continue
        probes.append({
            "kind": "border", "name": f"{a} → {b}: across the frontier",
            "from": entry["from"], "to": entry["to"],
            "min_km": entry.get("min_km", 1), "max_km": entry.get("max_km", 2000),
        })

    return {
        "package": plan_id,
        "title": " + ".join(countries.get(c, {}).get("name", c) for c in members),
        "countries": members,
        "countryNames": {c: countries.get(c, {}).get("name", c) for c in members},
        "regions": [countries[c]["region"] for c in members],
        "bbox": [
            min(countries[c]["bbox"][0] for c in members),
            min(countries[c]["bbox"][1] for c in members),
            max(countries[c]["bbox"][2] for c in members),
            max(countries[c]["bbox"][3] for c in members),
        ],
        "probes": probes,
    }
