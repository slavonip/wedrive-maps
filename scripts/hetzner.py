"""The only file in this repository that can create or destroy a machine.

    hetzner.py create   --name <n> --ttl-hours 6 --user-data <file> [--dry-run]
    hetzner.py delete   --name <n>
    hetzner.py sweep    [--dry-run]
    hetzner.py list

TWO DANGERS, AND THEY PULL IN OPPOSITE DIRECTIONS.

A server that is never deleted bills by the hour until somebody notices, which for a monthly job
means an invoice. And a delete that is too eager destroys something it was never meant to touch.
Both are guarded structurally rather than by care:

  * **Nothing is deleted unless it carries our labels.** `delete` and `sweep` filter on
    `project=wedrive-maps` server-side AND check the label again on the object before acting. A
    machine created by hand, or by any other project sharing this token, is invisible to them.
  * **Every server is born with an expiry.** `create` writes `expires` into the labels, and
    `sweep` deletes only what is past it. So a lost workflow costs hours, not weeks — and the
    guarantee does not depend on the workflow that created the machine still being alive to
    clean up, which is exactly the case that fails.

THE FALLBACK LADDER exists because capacity is real. The probe found `cx53` — the cheapest type
that meets our needs — unavailable in every location at that moment, which is precisely the
surprise a price list cannot show. So `create` walks a list of (type, location) pairs and takes
the first that works, rather than failing because one datacentre is full.

    cax41/nbg1 -> cax41/hel1 -> cpx62/nbg1 -> cpx62/hel1

`cax41` is ARM, and it leads because it is a SEVENTH the price of the dedicated alternative:
EUR 0.0657/h against 0.4423. That is only usable because `ghcr.io/valhalla/valhalla:3.6.3`
publishes linux/arm64 and every binary the factory calls was found inside it — checked by opening
the image under emulation, not by reading its manifest.
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request

API = "https://api.hetzner.cloud/v1"

# THE ONLY THING THIS FILE WILL EVER TOUCH. A server without both labels is not ours, whatever
# its name looks like.
OURS = {"project": "wedrive-maps", "purpose": "europe-build"}

# (server type, location), cheapest first. Measured need: ~172 GB of scratch at peak, so every
# entry clears it on LOCAL NVMe and no network Volume is involved.
LADDER = [
    ("cax41", "nbg1"),   # 16 ARM vCPU · 32 GB · 320 GB · EUR 0.0657/h
    ("cax41", "hel1"),
    ("cpx62", "nbg1"),   # 16 x86 vCPU · 32 GB · 640 GB · EUR 0.2083/h
    ("cpx62", "hel1"),
]

IMAGE = "ubuntu-24.04"

# Which runner tarball a machine must fetch. It follows the rung that WINS, which is why the
# substitution happens here and not in the workflow: only this code knows which type was created,
# and a machine that downloads a runner for the wrong architecture boots, fails silently and
# bills until the watchdog notices.
ARCH_OF = {"cax": "arm64", "cpx": "x64", "ccx": "x64", "cx": "x64"}


def call(method: str, path: str, body=None):
    token = os.environ.get("HETZNER_API_TOKEN", "").strip()
    if not token:
        raise SystemExit("HETZNER_API_TOKEN is not set")
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:400]
        # The token is never echoed, on any path.
        raise RuntimeError(f"HTTP {error.code} on {method} {path}: {detail}") from None


def ours(server: dict) -> bool:
    """Belongs to this factory. Checked on the object itself, not inferred from the query that
    returned it — a server-side filter is a convenience, not a safety property."""
    labels = server.get("labels") or {}
    return all(labels.get(key) == value for key, value in OURS.items())


def find(name: str = None):
    query = "&".join(f"label_selector={k}%3D{v}" for k, v in OURS.items())
    servers = [s for s in call("GET", f"/servers?{query}")["servers"] if ours(s)]
    return [s for s in servers if s["name"] == name] if name else servers


def create(args) -> int:
    if find(args.name):
        print(f"{args.name} already exists — refusing to create a second one", file=sys.stderr)
        return 1

    expires = (datetime.datetime.now(datetime.timezone.utc)
               + datetime.timedelta(hours=args.ttl_hours)).strftime("%Y-%m-%dT%H-%M-%SZ")
    template = open(args.user_data, encoding="utf-8").read() if args.user_data else None
    given = dict(pair.split("=", 1) for pair in (args.sub or []))

    last = None
    for server_type, location in LADDER:
        body = {
            "name": args.name,
            "server_type": server_type,
            "location": location,
            "image": IMAGE,
            # The expiry is a LABEL rather than a note, so the sweeper can act on it without
            # knowing anything about the job that created the machine.
            "labels": {**OURS, "expires": expires},
            "start_after_create": True,
        }
        if template:
            filled = template.replace("__ARCH__", ARCH_OF.get(server_type[:3], "x64"))
            for key, value in given.items():
                filled = filled.replace(f"__{key}__", value)
            left = re.findall(r"__[A-Z_]+__", filled)
            if left:
                # Refuse rather than boot a machine whose cloud-init has holes in it. That
                # machine would come up, do nothing, and bill until the watchdog swept it.
                print(f"cloud-init still has {sorted(set(left))} — refusing to create",
                      file=sys.stderr)
                return 1
            body["user_data"] = filled
        if args.dry_run:
            print(f"   would create {server_type} in {location}, expiring {expires}")
            return 0
        try:
            answer = call("POST", "/servers", body)
        except RuntimeError as error:
            print(f"   {server_type}/{location}: {error}".replace("\n", " ")[:160])
            last = error
            continue
        server = answer["server"]
        print(f"created {server['name']}  {server_type}  {location}  "
              f"ip {server['public_net']['ipv4']['ip']}  expires {expires}")
        for key, value in (("HETZNER_SERVER_ID", server["id"]),
                           ("HETZNER_SERVER_IP", server["public_net"]["ipv4"]["ip"]),
                           ("HETZNER_SERVER_TYPE", server_type),
                           ("HETZNER_LOCATION", location)):
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
                    handle.write(f"{key.lower()}={value}\n")
        return 0

    print(f"every option in the ladder failed; last error: {last}", file=sys.stderr)
    return 1


def delete(args) -> int:
    targets = find(args.name)
    if not targets:
        # NOT an error. The cleanup job runs `if: always()`, including when creation never
        # happened, and a cleanup that fails because there was nothing to clean would cry wolf
        # on every failed build.
        print(f"no server named {args.name} carries our labels — nothing to delete")
        return 0
    for server in targets:
        if args.dry_run:
            print(f"   would delete {server['name']} ({server['id']})")
            continue
        call("DELETE", f"/servers/{server['id']}")
        print(f"deleted {server['name']} ({server['id']}, {server['server_type']['name']})")
    return 0


def sweep(args) -> int:
    """Delete OUR servers whose expiry has passed. The safety net for a workflow that died."""
    now = datetime.datetime.now(datetime.timezone.utc)
    servers = find()
    if not servers:
        print("nothing of ours is running")
        return 0
    for server in servers:
        stamp = (server.get("labels") or {}).get("expires")
        if not stamp:
            # Ours, but unlabelled: something created it outside `create`. Reported, never
            # deleted — guessing at intent is how a sweeper destroys a running build.
            print(f"   {server['name']}: no expiry label, LEFT ALONE — investigate")
            continue
        try:
            expiry = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H-%M-%SZ").replace(
                tzinfo=datetime.timezone.utc)
        except ValueError:
            print(f"   {server['name']}: unreadable expiry {stamp!r}, LEFT ALONE")
            continue
        age = (now - expiry).total_seconds() / 3600
        if age <= 0:
            print(f"   {server['name']}: alive, {-age:.1f} h left")
            continue
        if args.dry_run:
            print(f"   {server['name']}: EXPIRED {age:.1f} h ago — would delete")
            continue
        call("DELETE", f"/servers/{server['id']}")
        print(f"   {server['name']}: EXPIRED {age:.1f} h ago — DELETED")
    return 0


def show(args) -> int:
    servers = find()
    print(f"{len(servers)} server(s) of ours")
    for server in servers:
        labels = server.get("labels") or {}
        print(f"   {server['name']}  {server['server_type']['name']}  "
              f"{server['datacenter']['location']['name']}  {server['status']}  "
              f"created {server['created'][:19]}  expires {labels.get('expires', '—')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    make = sub.add_parser("create")
    make.add_argument("--name", required=True)
    make.add_argument("--ttl-hours", type=float, default=6)
    make.add_argument("--user-data", default=None)
    make.add_argument("--sub", action="append", metavar="KEY=VALUE",
                      help="fill __KEY__ in the cloud-init template")
    make.add_argument("--dry-run", action="store_true")
    make.set_defaults(fn=create)

    drop = sub.add_parser("delete")
    drop.add_argument("--name", required=True)
    drop.add_argument("--dry-run", action="store_true")
    drop.set_defaults(fn=delete)

    clean = sub.add_parser("sweep")
    clean.add_argument("--dry-run", action="store_true")
    clean.set_defaults(fn=sweep)

    ls = sub.add_parser("list")
    ls.set_defaults(fn=show)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
