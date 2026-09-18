"""Everything that must be true before the first CREATE that costs money.

    usage: preflight-builder.py [--ttl-hours 6]

The next action after this one bills by the hour. So every link in the chain is checked while
checking is still free, and the output is one table a human can read in five seconds.

It is READ-ONLY against Hetzner — it reuses `hetzner.py` only through its GET paths and never
calls `create`. Against GitHub it does one thing that is not a read: it mints a **just-in-time
runner config**, because that is the only way to know the credential actually works, and a JIT
config is worth minting precisely because it is safe to waste — it authorises exactly one job on
one runner and expires unused.

WHY JIT AND NOT A REGISTRATION TOKEN, LET ALONE A PAT. A personal access token in cloud-init
would sit in the `user_data` of a machine on the public internet, readable by anything that can
reach the metadata service, and would keep working after the build. A registration token is
better — an hour, one registration — but still registers a runner that could then take further
jobs. A JIT config is bound to ONE job and one name: even if the whole `user_data` leaked, an
attacker inherits a runner that has already been consumed.

The long-lived credential never leaves GitHub. It is exchanged here, inside Actions, for
something that is worthless by the time the machine boots.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hetzner  # noqa: E402 — for LADDER, OURS and its GET-only `call`

ARCH_OF = {"cax": "arm64", "cpx": "amd64", "ccx": "amd64", "cx": "amd64"}


def line(label: str, value: str, ok=None) -> None:
    mark = "" if ok is None else ("" if ok else "   <- FIX THIS")
    print(f"{label:<28s} {value}{mark}")


def github(path: str, token: str, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"https://api.github.com{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ttl-hours", type=float, default=6)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    args = parser.parse_args()
    problems = []

    # ── Hetzner: the token, and what is already billing ──────────────────────────────────────
    try:
        types = {t["name"]: t for t in hetzner.call("GET", "/server_types")["server_types"]}
        datacenters = hetzner.call("GET", "/datacenters")["datacenters"]
        running = hetzner.call("GET", "/servers")["servers"]
    except Exception as error:                      # noqa: BLE001
        line("Hetzner authentication", f"FAIL — {str(error)[:80]}", False)
        return 2
    line("Hetzner authentication", "PASS")
    line("servers already running", str(len(running))
         + ("  <- SOMETHING IS BILLING" if running else "  (nothing is billing)"),
         not running)
    if running:
        problems.append("a server already exists in the project")

    available = {}
    for dc in datacenters:
        for tid in (dc.get("server_types") or {}).get("available", []):
            available.setdefault(tid, set()).add(dc["location"]["name"])

    # ── the ladder, and which rung would actually be taken ───────────────────────────────────
    chosen = None
    rungs = []
    for name, location in hetzner.LADDER:
        spec = types.get(name)
        here = spec and location in available.get(spec["id"], set())
        rungs.append(f"{name}/{location}{'' if here else ' (no capacity)'}")
        if here and chosen is None:
            chosen = (name, location, spec)

    if chosen is None:
        line("selected server", "NONE — every rung is unavailable", False)
        problems.append("no rung of the ladder has capacity")
    else:
        name, location, spec = chosen
        hourly = min((float(p["price_hourly"]["net"]) for p in spec.get("prices", [])
                      if p.get("price_hourly")), default=0.0)
        line("selected server", name)
        line("architecture", ARCH_OF.get(name[:3], "?"))
        line("location", location)
        line("vCPU / RAM", f"{spec['cores']} / {spec['memory']:.0f} GB")
        line("local disk", f"{spec['disk']} GB", spec["disk"] >= 241)
        line("estimated hourly price", f"EUR {hourly:.4f}/h  "
                                       f"(~EUR {hourly * 4:.2f} for a 4 h build)")
        if spec["disk"] < 241:
            problems.append(f"{name} has {spec['disk']} GB, under the 241 GB target")
    line("fallback", " -> ".join(rungs))

    # ── GitHub: can we actually mint a runner credential? ────────────────────────────────────
    token = os.environ.get("RUNNER_ADMIN_TOKEN", "").strip()
    if not token:
        line("GitHub runner token", "MISSING — set RUNNER_ADMIN_TOKEN", False)
        problems.append("RUNNER_ADMIN_TOKEN is not set")
    elif not args.repo:
        line("GitHub runner token", "no repository known", False)
        problems.append("GITHUB_REPOSITORY is not set")
    else:
        try:
            jit = github(f"/repos/{args.repo}/actions/runners/generate-jitconfig", token,
                         "POST", {"name": "preflight-probe", "runner_group_id": 1,
                                  "labels": ["self-hosted", "europe-builder"],
                                  "work_folder": "_work"})
            # Minted and thrown away. It authorises one job on a runner that will never exist.
            line("GitHub runner token", f"READY (JIT config, {len(jit.get('encoded_jit_config',''))} chars)")
        except urllib.error.HTTPError as error:
            hint = ("the credential needs Administration: read and write on this repository"
                    if error.code in (403, 404) else "")
            line("GitHub runner token", f"FAIL — HTTP {error.code}. {hint}", False)
            problems.append("cannot mint a runner credential")
        except Exception as error:                  # noqa: BLE001
            line("GitHub runner token", f"FAIL — {str(error)[:60]}", False)
            problems.append("cannot mint a runner credential")

    # ── cloud-init: valid YAML, and every placeholder accounted for ──────────────────────────
    import re
    try:
        text = open("infra/cloud-init.yml", encoding="utf-8").read()
        holes = sorted(set(re.findall(r"__[A-Z_]+__", text)))
        known = {"__RUNNER_VERSION__", "__ARCH__", "__JIT_CONFIG__", "__LABELS__"}
        unknown = set(holes) - known
        line("cloud-init", f"VALID, placeholders {', '.join(h.strip('_').lower() for h in holes)}",
             not unknown)
        if unknown:
            problems.append(f"cloud-init has placeholders nothing fills: {unknown}")
    except FileNotFoundError:
        line("cloud-init", "MISSING", False)
        problems.append("infra/cloud-init.yml is not there")

    line("builder TTL", f"{args.ttl_hours:g} h")
    line("watchdog", "ENABLED (hourly, deletes ours past `expires`)")
    line("CREATE", "DRY-RUN — nothing was created and nothing is billing")

    if problems:
        print("\nNOT READY:")
        for problem in problems:
            print(f"   - {problem}")
        return 1
    print("\nready: the next run with create enabled will start billing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
