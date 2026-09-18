"""Ask Hetzner what this project actually has, and buy nothing.

    usage: hetzner-probe.py            (reads HETZNER_API_TOKEN from the environment)

WHY A READ-ONLY PROBE COMES FIRST. The factory is about to be able to create machines that cost
money by the hour, and every assumption this repo has made from documentation rather than from
the artifact has been wrong at least once — the runner's disk was an order of magnitude larger
than believed, the timezone builder targeted a different release than its docs implied, and a
"harmless" admin warning turned out to have a 21% routing consequence. A server type's specs and
a project's limits are exactly that kind of assumption.

So this asks the account, not the documentation:

    GET /locations        where can a builder run
    GET /datacenters      which server types each location actually offers RIGHT NOW
    GET /server_types     vCPU, RAM, local disk, and the price per hour per location
    GET /servers          what is running — expected: nothing
    GET /volumes          what storage exists — expected: nothing
    GET /ssh_keys         what can log in

**IT IS STRUCTURALLY INCAPABLE OF SPENDING.** There is one request function, it hardcodes GET,
and nothing else in the file performs I/O. A probe that could create a resource is a probe that
will one day create one.

The two questions it exists to answer:

  1. does the token work, and is the project empty (so nothing is already billing)?
  2. which server types clear the MEASURED requirement — 172 GB of scratch at peak for a Europe
     build — on LOCAL disk, so no network Volume is needed?

That second one matters more than it looks. A Hetzner Volume is network block storage at roughly
200 MB/s sustained, and our build is disk-bound: its peak is the hierarchy stage, not parsing.
Putting the scratch there would slow the slowest part. The requirement was measured on four rungs
rather than rounded up to "600 GB to be safe", and if a type's local NVMe clears 172 GB with
margin, the Volume disappears from the design entirely.
"""
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.hetzner.cloud/v1"

# Measured, not guessed: 32.6 GB of source plus 139 GB of build growth at 0.358 GB per million
# directed edges, with no merge stage because europe-latest.osm.pbf is a single file.
EUROPE_SCRATCH_GB = 172
SAFE_MARGIN = 1.4          # what we want on top, so a year of OSM growth does not break it


def get(path: str, token: str):
    """The ONLY request this file can make, and it is a GET. Not a parameter — a constant."""
    request = urllib.request.Request(
        f"{API}{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def main() -> int:
    token = os.environ.get("HETZNER_API_TOKEN", "").strip()
    if not token:
        print("HETZNER_API_TOKEN is not set", file=sys.stderr)
        return 2

    try:
        locations = get("/locations", token)["locations"]
    except urllib.error.HTTPError as error:
        # The token itself is never printed, here or anywhere.
        print(f"AUTH FAILED: HTTP {error.code} — the token is missing, wrong, or scoped to "
              f"another project", file=sys.stderr)
        return 2
    except Exception as error:                      # noqa: BLE001
        print(f"could not reach the API: {error}", file=sys.stderr)
        return 2

    print("authentication            PASS")
    print(f"locations                 {', '.join(sorted(l['name'] for l in locations))}")

    # ── WHAT IS ALREADY RUNNING, which is the question that costs money ──────────────────────
    servers = get("/servers", token)["servers"]
    volumes = get("/volumes", token)["volumes"]
    keys = get("/ssh_keys", token)["ssh_keys"]
    print(f"servers running           {len(servers)}"
          + ("  <- SOMETHING IS BILLING" if servers else "  (nothing is billing)"))
    for server in servers:
        print(f"   {server['name']}  {server['server_type']['name']}  "
              f"{server['status']}  created {server['created'][:19]}  "
              f"labels {server.get('labels') or '{}'}")
    print(f"volumes                   {len(volumes)}"
          + ("  <- SOMETHING IS BILLING" if volumes else ""))
    for volume in volumes:
        print(f"   {volume['name']}  {volume['size']} GB  {volume['location']['name']}")
    print(f"ssh keys                  {len(keys)}"
          + ("" if keys else "  <- none; cloud-init will need one or a password"))

    # ── WHICH TYPES CLEAR THE MEASURED REQUIREMENT ON LOCAL DISK ─────────────────────────────
    types = get("/server_types", token)["server_types"]
    datacenters = get("/datacenters", token)["datacenters"]
    available = {}
    for dc in datacenters:
        for tid in (dc.get("server_types") or {}).get("available", []):
            available.setdefault(tid, set()).add(dc["location"]["name"])

    need = EUROPE_SCRATCH_GB * SAFE_MARGIN
    print(f"\nserver types with at least {need:.0f} GB of LOCAL disk "
          f"({EUROPE_SCRATCH_GB} GB measured peak x {SAFE_MARGIN} margin):\n")
    print(f"   {'type':12s} {'vCPU':>5s} {'RAM':>7s} {'disk':>8s} {'EUR/h':>8s} "
          f"{'EUR/4h':>8s}  cpu        where")

    rows = []
    for t in types:
        if t.get("deprecated") or t["disk"] < need:
            continue
        hourly = None
        for price in t.get("prices", []):
            gross = price.get("price_hourly", {}).get("net")
            if gross and (hourly is None or float(gross) < hourly):
                hourly = float(gross)
        where = sorted(available.get(t["id"], set()))
        rows.append((hourly if hourly is not None else 9e9, t, where))

    if not rows:
        print("   NONE — every type is below the requirement, so a Volume would be unavoidable")
    for hourly, t, where in sorted(rows)[:12]:
        mark = "" if where else "   <- not available anywhere right now"
        print(f"   {t['name']:12s} {t['cores']:5d} {t['memory']:6.0f}G {t['disk']:7d}G "
              f"{hourly:8.4f} {hourly * 4:8.2f}  {t['cpu_type']:10s} "
              f"{','.join(where) or 'none'}{mark}")

    print(f"\n   (a Europe build is projected at 2-6 hours; the EUR/4h column is the middle of "
          f"that,\n    net of VAT, and the machine exists only for those hours)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
