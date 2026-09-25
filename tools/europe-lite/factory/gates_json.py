# -*- coding: utf-8 -*-
"""Turn the three gate reports into gates.json, the machine-readable record published with a release.

    gates_json.py --struct struct.txt --struct-exit N --transit transit.txt --transit-exit N \
                  --routes routes.txt --routes-exit N --baseline "B1': ..." [--baseline-image REF] \
                  --out gates.json

The verdict is PASS only when every gate exited 0 AND printed its own PASS line AND the summary
line this script reads is present AND the transit gate examined every way the structural gate
found with destination_only lifted (12 pairs each; see transit_coverage). A transit PASS over
nothing, while there was something to examine, is a FAIL. A report it cannot parse is a FAIL, never a guess: a gate that
crashed half-way prints no summary, and a missing number must not read as zero. The full text of
each report travels inside the JSON, so the release carries exactly what was checked.

Exit code 0 when the verdict is PASS, 1 otherwise (gates.json is written either way).
"""
import argparse, json, re, sys

PATTERNS = {
    "struct": [
        (r"^connector ways (\d+), found in baseline (\d+), in new (\d+)$",
         ("connector_ways", "found_in_baseline", "found_in_new")),
        (r"^destonly True->False: inferred service (\d+); explicit access in ferry reclassification (\d+); "
         r"explicit access OUTSIDE it (\d+); other (\d+); False->True (\d+)$",
         ("lost_inferred_service", "lost_in_ferry_reclassification", "lost_outside_reclassification",
          "lost_other", "gained")),
        (r"^explicit access outside ferry reclassification kept destonly: (\d+) of (\d+)$",
         ("explicit_access_kept", "explicit_access_total")),
    ],
    "transit": [
        (r"^pairs (\d+), land-only (\d+), new transits through lifted destonly: (\d+)$",
         ("pairs", "land_only", "new_transits")),
    ],
    "routes": [
        (r"^TOTAL PASS (\d+), KNOWN (\d+), ACCEPTED (\d+), FAIL (\d+)$",
         ("pass", "known", "accepted", "fail")),
    ],
}
VERDICT_LINE = {"struct": "STRUCT GATE:", "transit": "TRANSIT GATE:", "routes": "ROUTES GATE:"}


def parse(name, text, exit_code):
    lines = [l.rstrip() for l in text.splitlines()]
    out = {"exit_code": exit_code, "counts": {}, "problems": []}
    for pattern, keys in PATTERNS[name]:
        m = next((re.match(pattern, l) for l in lines if re.match(pattern, l)), None)
        if m is None:
            out["problems"].append("summary line missing: " + pattern)
            continue
        out["counts"].update({k: int(v) for k, v in zip(keys, m.groups())})
    said = [l.split(":", 1)[1].strip() for l in lines if l.startswith(VERDICT_LINE[name])]
    if said != ["PASS"]:
        out["problems"].append("verdict line is %r, expected exactly one PASS" % said)
    if exit_code != 0:
        out["problems"].append("exit code %d" % exit_code)
    out["verdict"] = "PASS" if not out["problems"] else "FAIL"
    out["report"] = text
    return out


# transit_gate.py routes exactly this many pairs around every lifted way (pairs[:12]); a lifted way
# it cannot place fails the gate by itself.
PAIRS_PER_LIFTED_WAY = 12


def transit_coverage(res, lifted_path=None):
    """The transit gate examines the ways the structural gate found with destination_only lifted
    (lifted.json). If there are such ways and the transit gate examined fewer pairs than they
    need — zero in particular — its PASS proves nothing, and it is a FAIL. No lifted ways and no
    pairs is the legitimate empty case and stays PASS."""
    s, t = res["struct"], res["transit"]
    if lifted_path:
        try:
            with open(lifted_path, encoding="utf-8") as f:
                lifted = len(json.load(f))
        except (OSError, ValueError) as e:
            t["problems"].append("lifted.json unreadable (%s): transit coverage cannot be shown" % e)
            t["verdict"] = "FAIL"
            return
        from_counts = s["counts"].get("lost_in_ferry_reclassification", 0) + s["counts"].get("lost_outside_reclassification", 0)
        if s["verdict"] == "PASS" and from_counts != lifted:
            t["problems"].append("lifted.json has %d ways, the structural report counts %d" % (lifted, from_counts))
    elif "lost_in_ferry_reclassification" in s["counts"]:
        lifted = s["counts"]["lost_in_ferry_reclassification"] + s["counts"].get("lost_outside_reclassification", 0)
    else:
        t["problems"].append("the structural report gives no lifted count: transit coverage cannot be shown")
        t["verdict"] = "FAIL"
        return
    t["counts"]["lifted_ways"] = lifted
    pairs = t["counts"].get("pairs")
    if pairs is not None and pairs != PAIRS_PER_LIFTED_WAY * lifted:
        t["problems"].append("transit examined %d pairs; %d lifted ways need %d" % (pairs, lifted, PAIRS_PER_LIFTED_WAY * lifted))
    if t["problems"]:
        t["verdict"] = "FAIL"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for g in ("struct", "transit", "routes"):
        ap.add_argument("--" + g, required=True, help="%s gate report (text)" % g)
        ap.add_argument("--%s-exit" % g, type=int, required=True)
    ap.add_argument("--baseline", required=True, help="what the gates compared against")
    ap.add_argument("--baseline-image", default="")
    ap.add_argument("--lifted", help="struct_gate's lifted.json: the ways the transit gate must examine")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    res = {"schema": 1, "baseline": {"description": a.baseline, "image": a.baseline_image}}
    for g in ("struct", "transit", "routes"):
        try:
            text = open(getattr(a, g), encoding="utf-8").read()
        except OSError as e:
            text = ""
            res[g] = {"exit_code": getattr(a, g + "_exit"), "counts": {}, "verdict": "FAIL",
                      "problems": ["report unreadable: %s" % e], "report": ""}
            continue
        res[g] = parse(g, text, getattr(a, g + "_exit"))
    transit_coverage(res, a.lifted)
    res["verdict"] = "PASS" if all(res[g]["verdict"] == "PASS" for g in ("struct", "transit", "routes")) else "FAIL"
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    for g in ("struct", "transit", "routes"):
        print("%-8s %s %s %s" % (g, res[g]["verdict"], res[g]["counts"], "; ".join(res[g]["problems"])))
    print("GATES:", res["verdict"])
    return 0 if res["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
