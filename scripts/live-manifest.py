#!/usr/bin/env python3
"""Replace the checked-out manifest.json with the one on main NOW.

A dispatched run checks out the commit of the moment it was DISPATCHED. When it waits in the
concurrency queue behind a run that publishes, its manifest.json is one release old — and the
carry then copies from that. Found 2026-09-27: run 36279166763 carried RO/HU/AT/DE from
regional-2026-09-26-14 (no map, no search) although 26-19 had just published both, and dropped them.

Read through the API (raw, uncached), not raw.githubusercontent.com (cached for minutes).
    usage: live-manifest.py <repo> <out>     (GITHUB_TOKEN in the environment)
"""
import json, os, sys, urllib.request

repo, out = sys.argv[1], sys.argv[2]
req = urllib.request.Request("https://api.github.com/repos/%s/contents/manifest.json?ref=main" % repo,
                             headers={"Accept": "application/vnd.github.raw",
                                      "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"]})
data = urllib.request.urlopen(req, timeout=60).read()
m = json.loads(data)
old = json.load(open(out, encoding="utf-8")).get("tag") if os.path.exists(out) else None
open(out, "wb").write(data)
print("manifest.json: %s (checked out) -> %s (main now)" % (old, m.get("tag")))
