#!/usr/bin/env python3
"""Network preflight of the charging factory: can this runner reach the hosts the OSM half needs?

    charging-netcheck.py

Tries https://overpass-api.de, https://overpass.private.coffee and https://download.geofabrik.de
(one cheap HEAD each, TIMEOUT seconds, TRIES tries). Any HTTP answer — 200, 403, 504 alike — counts
as REACHABLE: the host answered, so the network path works and the fetcher's own retries and
fallbacks apply. Only a network-level failure (URLError / OSError: `[Errno 101] Network is
unreachable`, a timeout, a refused connection) counts as unreachable.

overpass-api.de and download.geofabrik.de both sit on Hetzner. When BOTH are unreachable the runner
has no route there (run 45024134477, 2026-11-22), and the only remaining source is
overpass.private.coffee alone, with no Geofabrik fallback behind it: one overloaded endpoint would
then fail a country and the whole run. That is a bad runner, not bad data — exit 1 with an
::error::, and the workflow re-dispatches itself on a fresh runner (input `attempt`, at most 3).

Prints the reachability of all three either way. Standard library only.
"""
import sys, urllib.error, urllib.request

HOSTS = ("https://overpass-api.de", "https://overpass.private.coffee", "https://download.geofabrik.de")
# the two on Hetzner: both down at once = this runner has no route there
REQUIRED_ONE_OF = ("https://overpass-api.de", "https://download.geofabrik.de")
TIMEOUT = 20
TRIES = 2
USER_AGENT = "wedrive-charging-factory/1 (network preflight)"


def http_probe(url, timeout=TIMEOUT):
    """-> a short description of the answer; raises OSError/URLError when the host is not reached."""
    req = urllib.request.Request(url + "/", method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return "HTTP %s" % r.status
    except urllib.error.HTTPError as e:          # the host answered: reachable
        return "HTTP %s" % e.code


def reachable(url, probe=http_probe, tries=TRIES):
    """-> (True, what it answered) or (False, why the network failed)."""
    why = ""
    for _ in range(tries):
        try:
            return True, probe(url)
        except urllib.error.HTTPError as e:      # a probe that raises it: still an answer
            return True, "HTTP %s" % e.code
        except (urllib.error.URLError, OSError, ValueError) as e:
            why = "%s: %s" % (type(e).__name__, getattr(e, "reason", e))
    return False, why


def check(probe=http_probe, tries=TRIES, log=print):
    """-> 0 when the runner can work, 1 when it is a bad runner."""
    state = {}
    for url in HOSTS:
        ok, what = reachable(url, probe, tries)
        state[url] = ok
        log("%-36s %s  (%s)" % (url, "reachable" if ok else "UNREACHABLE", what))
    if not any(state[u] for u in REQUIRED_ONE_OF):
        log("::error::bad runner: neither %s is reachable at the network level — "
            "no Overpass main endpoint and no Geofabrik fallback from here" % " nor ".join(REQUIRED_ONE_OF))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(check())
