#!/usr/bin/env python3
"""TEMPORARY, READ-ONLY: everything the Hetzner Cloud project holds that can bill or linger.

    usage: hetzner-inventory.py        (reads HETZNER_API_TOKEN from the environment)

Written for the decommissioning of Hetzner (MASTER TASK block 3, 2026-09-26) and deleted together
with the other Hetzner files once the owner has revoked the token. It has ONE request function and
it sends GET only; the token is never printed. Resources outside the Cloud API (Robot dedicated
servers, Storage Boxes, DNS zones) are not reachable with a Cloud token and go to the owner's
manual checklist.
"""
import json, os, sys, urllib.error, urllib.request

API = "https://api.hetzner.cloud/v1"

COLLECTIONS = [
    ("servers", "/servers"),
    ("volumes", "/volumes"),
    ("snapshots", "/images?type=snapshot"),
    ("backups", "/images?type=backup"),
    ("primary_ips", "/primary_ips"),
    ("floating_ips", "/floating_ips"),
    ("networks", "/networks"),
    ("firewalls", "/firewalls"),
    ("load_balancers", "/load_balancers"),
    ("ssh_keys", "/ssh_keys"),
    ("certificates", "/certificates"),
    ("placement_groups", "/placement_groups"),
]


def get(path, token):
    request = urllib.request.Request(API + path, method="GET",
                                     headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def all_pages(key, path, token):
    items, page = [], 1
    while True:
        sep = "&" if "?" in path else "?"
        body = get("%s%spage=%d&per_page=50" % (path, sep, page), token)
        items += body.get(key if key not in ("snapshots", "backups") else "images", [])
        nxt = (body.get("meta") or {}).get("pagination", {}).get("next_page")
        if not nxt:
            return items
        page = nxt


def describe(item):
    bits = [str(item.get("id")), str(item.get("name") or item.get("description") or "-")]
    for k in ("server_type", "location", "datacenter", "home_location"):
        v = item.get(k)
        if isinstance(v, dict):
            bits.append(v.get("name", ""))
    for k in ("size", "image_size", "disk_size", "ip", "ip_range", "type", "status"):
        if item.get(k) not in (None, ""):
            bits.append("%s=%s" % (k, item[k]))
    if item.get("labels"):
        bits.append("labels=" + ",".join("%s:%s" % kv for kv in sorted(item["labels"].items())))
    bits.append("created=" + str(item.get("created", "?"))[:19])
    return "  ".join(bits)


def main():
    token = os.environ.get("HETZNER_API_TOKEN", "").strip()
    if not token:
        print("HETZNER_API_TOKEN is not set", file=sys.stderr)
        return 2
    total = 0
    for key, path in COLLECTIONS:
        try:
            items = all_pages(key, path, token)
        except urllib.error.HTTPError as e:
            print("%-17s NOT READABLE (HTTP %d) -> owner checks it in the console" % (key, e.code))
            continue
        total += len(items)
        print("%-17s %d" % (key, len(items)))
        for it in items:
            print("   " + describe(it))
    print("TOTAL resources: %d" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
