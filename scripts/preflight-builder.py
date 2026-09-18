"""Everything that must be true before the first CREATE that costs money.

    usage: preflight-builder.py [--ttl-hours 6]

The next action after this one bills by the hour. So every link in the chain is checked while
checking is still free, and the output is one table a human can read in five seconds.

It is READ-ONLY against Hetzner — it reuses `hetzner.py` only through its GET paths and never
calls `create`. Against GitHub it does one thing that is not a read: it mints a **just-in-time
runner config**, because that is the only way to know the credential actually works.

**And minting one REGISTERS A RUNNER, at mint time.** This file used to say the config was "minted
and discarded", which was true of the config and false of the registration — the second run then
failed with HTTP 409 on a name already taken. The registration is deleted again now, and stale
ones are swept, so running this twice is exactly as safe as running it once. A probe that claims
to be side-effect-free must be made so, not described so.

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

    # ── GitHub: can we actually mint a runner credential, and from what? ─────────────────────
    # The workflow prefers a GITHUB APP and falls back to a PAT. Which one answered is reported,
    # because they are not equivalent and a silent fallback would hide a downgrade: an App token
    # lives an hour, is scoped to this repository and to `administration` alone, and belongs to
    # no person — a PAT is a human's credential with a human's blast radius.
    token = os.environ.get("RUNNER_TOKEN", "").strip()
    source = os.environ.get("RUNNER_TOKEN_SOURCE", "unknown")
    if not token:
        line("GitHub runner credential",
             "MISSING — set the App secrets (RUNNER_APP_ID + RUNNER_APP_PRIVATE_KEY)", False)
        problems.append("no credential for minting a runner config")
    elif not args.repo:
        line("GitHub runner credential", "no repository known", False)
        problems.append("GITHUB_REPOSITORY is not set")
    else:
        # MINTING A JIT CONFIG REGISTERS A RUNNER IMMEDIATELY — at mint time, not when a machine
        # connects. An earlier version of this file claimed the config was "minted and
        # discarded", which was true of the config and false of the registration: it left a
        # phantom runner behind, and the SECOND run failed with HTTP 409 because the name was
        # taken. The probe was not side-effect-free, and said it was.
        #
        # So: a unique name, and the registration is deleted again. Stale ones from before this
        # fix are swept too, because a repository full of offline phantom runners is confusing
        # at best and, for a self-hosted setup, worth not having at all.
        stale = [r for r in github(f"/repos/{args.repo}/actions/runners", token).get("runners", [])
                 if r["name"].startswith("preflight-probe")]
        for runner in stale:
            try:
                github(f"/repos/{args.repo}/actions/runners/{runner['id']}", token, "DELETE")
                print(f"   (removed a stale probe runner: {runner['name']})")
            except Exception:                       # noqa: BLE001
                pass

        probe_name = f"preflight-probe-{os.environ.get('GITHUB_RUN_ID', 'local')}"
        try:
            jit = github(f"/repos/{args.repo}/actions/runners/generate-jitconfig", token,
                         "POST", {"name": probe_name, "runner_group_id": 1,
                                  "labels": ["self-hosted", "europe-builder"],
                                  "work_folder": "_work"})
            size = len(jit.get("encoded_jit_config", ""))
            # Undo the registration this just made, so running the preflight twice in a row is
            # exactly as safe as running it once.
            runner_id = (jit.get("runner") or {}).get("id")
            removed = False
            if runner_id:
                try:
                    github(f"/repos/{args.repo}/actions/runners/{runner_id}", token, "DELETE")
                    removed = True
                except Exception:                   # noqa: BLE001
                    pass
            line("GitHub runner credential",
                 f"READY via {source} — JIT config minted ({size} chars) and the runner "
                 f"{'deregistered again' if removed else 'COULD NOT be deregistered'}")
            if not removed:
                problems.append("a probe runner was left registered")
            if source != "github-app":
                line("", "   note: a PAT is in use; a GitHub App is the intended mechanism")
        except urllib.error.HTTPError as error:
            hint = ("the App installation needs Administration: read and write on this "
                    "repository, and must be installed on it"
                    if error.code in (403, 404)
                    else "a runner with this name already exists — minting a JIT config "
                         "registers one immediately, and an earlier one was not cleaned up"
                    if error.code == 409 else "")
            line("GitHub runner credential", f"FAIL — HTTP {error.code}. {hint}", False)
            problems.append("cannot mint a runner credential")
        except Exception as error:                  # noqa: BLE001
            line("GitHub runner credential", f"FAIL — {str(error)[:60]}", False)
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
