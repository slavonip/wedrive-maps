# -*- coding: utf-8 -*-
"""Calling valhalla_service in Docker for the gates.

A GRAPH is given as "HOSTDIR" or "HOSTDIR:CONFIG": HOSTDIR is mounted at /w and /w/CONFIG
(default config.json) is the Valhalla config, whose tile_dir must point inside /w — which is what
build.sh writes (tile_dir /w/t). A graph whose config is not config.json: "/data/prod:c_europe2.json".

The request goes through a file, one per process: two gates once shared a single request file,
overwrote each other's requests and produced a report that looked like a routing disaster.

A BASELINE can instead be a recorded manifest: the answers a baseline graph gave to exactly the
questions a gate asks, written with --record-baseline and read with --baseline-manifest. A gate
that meets a question the manifest does not hold stops with exit code 2 rather than guess.

Environment:
  LITE_VALHALLA_IMAGE  image with valhalla_service (default wedrive-valhalla:lite)
"""
import json, os, subprocess

IMAGE = os.environ.get("LITE_VALHALLA_IMAGE", "wedrive-valhalla:lite")


def split_graph(graph):
    host, _, cfg = graph.partition(":")
    return os.path.abspath(os.path.expanduser(host)), (cfg or "config.json")


def svc(graph, action, req, image=None, env=None, service="valhalla_service"):
    """Run one valhalla_service action on GRAPH. Returns (parsed JSON or {"err": ...}, stderr)."""
    host, cfg = split_graph(graph)
    qdir = os.path.join(host, ".gate_requests")
    os.makedirs(qdir, exist_ok=True)
    rq = "req_%d.json" % os.getpid()
    with open(os.path.join(qdir, rq), "w") as f:
        f.write(json.dumps(req))
    e = " ".join("-e %s='%s'" % kv for kv in (env or {}).items())
    cmd = ("docker run --rm -v '%s':/w %s --entrypoint bash %s -c "
           "'%s /w/%s %s \"$(cat /w/.gate_requests/%s)\"'"
           % (host, e, image or IMAGE, service, cfg, action, rq))
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    out = p.stdout.strip().split("\n")[-1] if p.stdout.strip() else ""
    try:
        r = json.loads(out)
    except Exception:
        r = {"err": (p.stderr or out)[-300:]}
    return r, p.stderr


MANIFEST_SCHEMA = 1


def load_manifest(path, section):
    """One section of a recorded baseline (see README, "Baseline for the gates"). Exits 2 if the
    file or the section is missing: a gate never falls back to anything weaker."""
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError) as e:
        print("baseline manifest %s unreadable: %s" % (path, e)); raise SystemExit(2)
    if m.get("schema") != MANIFEST_SCHEMA or section not in m:
        print("baseline manifest %s: schema %r, no section %r" % (path, m.get("schema"), section))
        raise SystemExit(2)
    return m[section]


def record_manifest(path, section, source, data):
    """Write one section of a baseline manifest, keeping the other sections; atomic rename."""
    m = {"schema": MANIFEST_SCHEMA}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    m.setdefault("sources", {})[section] = source
    m[section] = data
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, sort_keys=True, separators=(",", ":"))
    os.replace(tmp, path)


def not_covered(what, missing):
    """The manifest does not hold an answer this run needs: stop, never guess."""
    print("BASELINE MANIFEST DOES NOT COVER %d %s (first: %s)." % (len(missing), what, missing[:5]))
    print("It was recorded for a different input. Run this gate against a baseline GRAPH instead.")
    raise SystemExit(2)


def gid(v):
    return "%d/%d/%d" % (v & 7, (v >> 3) & 0x3fffff, (v >> 25) & 0x1fffff)


def ferries_of(trip):
    """Ferry names from the route's MANOEUVRES. trace_attributes refuses shapes over 16 000 points
    (London -> Rome has 25 968) and silently reads as 'no ferry', so it is never used for this."""
    fer = []
    for leg in trip["legs"]:
        for m in leg["maneuvers"]:
            if m.get("travel_type") == "ferry" or m.get("type") in (28, 29):
                n = ",".join(m.get("street_names") or [])
                if n and n not in fer:
                    fer.append(n)
    return fer
