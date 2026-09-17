"""Promote per-country tile sets into index-tiles.json — the manifest for the cut pipeline.

    usage: manifest-tiles.py <incoming-dir> <index-tiles.json>

Deliberately a SEPARATE file from index.json (owner, 2026-09-17: do the rework in a copy). The
package pipeline keeps publishing whole packages that the car uses today; this one publishes
countries, and nothing switches over until the cut path has proved itself in the vehicle.

**THE BUILD ID IS THE WHOLE CONTRACT.** A `GraphId` carries an index assigned during the build,
so an edge in Moldova's tile refers to a node in Romania's by a number that means something only
within ONE build. Two countries from different builds give edges pointing at the wrong nodes,
with no error and no crash — just wrong routes. So every country carries its build id, the
manifest states which id is current, and the car refuses to hold a mixture.

That is also why this manifest replaces its country list wholesale instead of merging: a run
that rebuilt only some countries would leave the rest advertising an id that no longer matches,
which is precisely the state that must never exist.
"""
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verdicts  # noqa: E402 — one vocabulary, one predicate, shared by every gate

RELEASE = "https://github.com/slavonip/wedrive-maps/releases/download/tiles/"


def main(incoming: str, manifest: str) -> int:
    root = pathlib.Path(incoming)
    out = pathlib.Path(manifest)

    metas = []
    for path in sorted(root.rglob("*-tiles.json")):
        metas.append(json.loads(path.read_text(encoding="utf-8")))
    if not metas:
        print("no country tile manifests found; nothing to promote")
        return 0

    build_ids = {meta["buildId"] for meta in metas}
    if len(build_ids) != 1:
        # Refusing here is the point: publishing a mixture would hand cars a set of countries
        # that cannot legally be held together, and nothing downstream could tell.
        print(f"refusing to promote a mixture of builds: {', '.join(sorted(build_ids))}",
              file=sys.stderr)
        return 2
    build_id = build_ids.pop()

    # ── WHAT EACH GATE'S VERDICT MEANS FOR PROMOTION ────────────────────────────────────────
    # `verdicts.is_pass` refuses to guess: only the literal string PASS is a pass, so ABSENT,
    # UNCHECKED, a legacy boolean and a truthy dict are all "not PASS". What to DO about a
    # not-PASS is a separate decision, and it is made here, per gate, with its reason — because
    # the gates are not equally consequential and treating them alike would be wrong in both
    # directions.
    #
    #   gate       field                          blocks?  why
    #   timezone   timezones.compatibilityGate    YES      the zone is written into every node at
    #                                                      build time and is uncorrectable later;
    #                                                      a graph without it gives silently wrong
    #                                                      arrival times across a border
    #   admins     admins.adminsGate              YES      driving side, access defaults and
    #                                                      country-crossing costs. Routes still
    #                                                      come back, so nothing else notices
    #   freshness  osm.freshnessGate              YES      see below — the build is kept, the
    #                                                      promotion is refused
    #
    # FRESHNESS BLOCKS PROMOTION BUT NOT THE BUILD, and the distinction is the whole point.
    # An unreadable replication timestamp does not prove the graph is bad, so throwing away an
    # hour of runner time would be wrong — the artifacts stay as a candidate. But making it
    # CURRENT without evidence that it is at least as new as what it replaces reopens exactly the
    # silent-failure path the gate exists to close: osmium breaks one month, every extract comes
    # back `UNCHECKED: cannot read replication timestamp`, the job goes green, and the car is
    # handed data of unknown age under a fresh-looking release date.
    #
    # An earlier version of this table had freshness merely ANNOTATING, on the reasoning that
    # "unverified is not wrong". That reasoning is sound and argues for keeping the build; it
    # says nothing about promoting it.
    BLOCKING = {
        "timezone": ("timezones", "compatibilityGate"),
        "admins": ("admins", "adminsGate"),
        "freshness": ("osm", "freshnessGate"),
    }
    ANNOTATING = {}

    def verdict_of(meta, field, key):
        found = meta.get(field)
        if isinstance(found, dict):
            return found.get(key)
        # Manifests published before 2026-09-17 carry `"timezones": true`. A stale manifest must
        # not become unreadable because a field grew, so the old shape is translated ONCE, here,
        # where it is visible — rather than by making the predicate lenient, which is how the
        # bug happened the first time.
        if isinstance(found, bool):
            return verdicts.PASS if found else verdicts.FAIL
        return found

    refused = []
    for label, (field, key) in BLOCKING.items():
        bad = {verdicts.describe(verdict_of(m, field, key)) for m in metas
               if not verdicts.is_pass(verdict_of(m, field, key))}
        if bad:
            refused.append(f"{label} gate says {', '.join(sorted(bad))}")

    for label, (field, key) in ANNOTATING.items():
        seen = {verdicts.describe(verdict_of(m, field, key)) for m in metas
                if not verdicts.is_pass(verdict_of(m, field, key))}
        if seen:
            print(f"{build_id}: {label} gate says {', '.join(sorted(seen))} — recorded, "
                  f"not blocking (the graph is unverified, not wrong)")

    if refused:
        for line in refused:
            print(f"{build_id}: {line}, NOT promoted")
        # Said explicitly, because "not promoted" and "lost" are different outcomes and the
        # difference is worth an hour of runner time: the archives and their manifests are
        # already uploaded and can be promoted by hand once the verdict is understood.
        print(f"{build_id}: the artifacts are kept as a candidate — nothing was discarded")
        return 0

    index = {
        "schema": 1,
        "buildId": build_id,
        "region": metas[0].get("region"),
        "engineVersion": metas[0].get("engineVersion"),
        "dataDate": metas[0].get("dataDate"),
        # Every country in the build, whether or not a given car wants it. The list is what the
        # Maps screen ticks; the ids are what stop it mixing vintages.
        "countries": {},
        "release": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }

    for meta in sorted(metas, key=lambda m: m["country"]):
        code = meta["country"]
        index["countries"][code] = {
            "url": RELEASE + f"{code}-tiles.tar",
            "bytes": meta["bytes"],
            "sha256": meta["sha256"],
            "tiles": meta.get("tiles"),
            "dataDate": meta.get("dataDate"),
            "buildId": meta["buildId"],
            # ── WHAT THIS GRAPH WAS BUILT FROM, CARRIED ALL THE WAY OUT ──────────────────────
            # The gates' verdicts were computed, used to decide promotion, and then dropped: the
            # per-country `*-tiles.json` that holds them is a workflow artifact with two days'
            # retention and is not published at all — only the `.tar` reaches the release. So
            # every question this session went to trouble to make answerable — which timezone
            # dataset, which tzdata accepted it, which OSM snapshot, how many admin records were
            # dropped — became unanswerable the moment the run expired.
            #
            # A graph outlives its build log by design; that is the whole point of a monthly
            # factory. Its provenance has to travel with it.
            "timezones": meta.get("timezones"),
            "osm": meta.get("osm"),
            "admins": meta.get("admins"),
        }

    out.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    total = sum(c["bytes"] for c in index["countries"].values())
    print(f"promoted {build_id}: {len(index['countries'])} countries, "
          f"{total / 1_048_576:.0f} MB in total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
