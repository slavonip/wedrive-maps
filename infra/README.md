# The builder's control plane — what a human sets up once

Everything here exists so that the monthly Europe build needs no person in the loop. Two things
cannot be automated, because they are the act of granting authority, and they are both done once.

## 1. A GitHub App, not a personal token

A PAT would work and is the wrong answer: it belongs to a person, it outlives their involvement,
and its blast radius is everything that person can reach. The App belongs to the project.

Create it at **Settings → Developer settings → GitHub Apps → New GitHub App**:

| field | value |
|---|---|
| name | anything, e.g. `wedrive map factory` |
| homepage | the repository URL |
| webhook | **uncheck Active** — nothing here listens for events |
| repository permissions | **Administration: Read and write** — and nothing else |
| where can it be installed | Only on this account |

`Administration` is the permission that allows minting a runner configuration. It is the only one
needed; granting more would widen what a leaked key reaches, and the workflow narrows the token
further anyway — `permission-administration: write` on `create-github-app-token` means the
*installation token* carries that one permission even if the App is later granted others.

Then **Install App** on `slavonip/wedrive-maps` only. Not "All repositories".

Generate a private key and put both values in **repository secrets**:

```
RUNNER_APP_ID            the numeric App ID from its settings page
RUNNER_APP_PRIVATE_KEY   the whole .pem, BEGIN and END lines included
```

> **The key is the control plane's only long-lived secret, and it is deliberately a key rather
> than a token.** If it leaks it is rotated in one place, and nobody's personal account is
> involved in the incident. Everything downstream is short-lived: the installation token lives an
> hour, and what actually reaches the machine is a JIT config valid for one job.

### The fallback nobody should use

`RUNNER_ADMIN_TOKEN` is read if — and only if — `RUNNER_APP_ID` is unset: a fine-grained PAT with
`Administration: Read and write` on this repository. It exists so the control plane can be
debugged without an App, and the preflight prints which credential answered precisely so that
using it is a visible choice rather than a quiet default.

Leave it unset. A PAT is a person's credential: it outlives their involvement with the project,
and rotating it touches their account rather than this repository.

## 2. A Hetzner project and its API token

Create a project — `WeDrive Map Factory` — containing nothing else, and an API token **scoped to
that project** with read and write. Put it in repository secrets as `HETZNER_API_TOKEN`.

A token scoped to a project of its own means a leak cannot reach anything else, and it means the
watchdog's "delete everything of ours that has expired" is bounded by a project that contains
only ours. Both of those stop being true the moment other servers share the project.

## What runs afterwards, without anyone

```
europe-build.yml     manual for now, monthly once one green run exists
   bootstrap         App -> installation token -> JIT config -> create the machine
   build             the machine takes exactly one job
   cleanup           if: always() — deletes the machine whatever happened

hetzner-watchdog.yml hourly, deletes anything of ours past its `expires` label
hetzner-probe.yml    weekly, FAILS if any server or volume exists at all
```

Three independent guarantees that a machine cannot be left running: the cleanup job, the
watchdog, and the machine's own uptime shutdown. They are independent on purpose — cleanup fails
exactly when the build does, and the watchdog needs GitHub to be working.

## The first paid run, deliberately tiny

`europe-build` defaults to `dry_run: true` and creates nothing. Run it that way first and read
the table.

When it is green, run it once with `dry_run` **unchecked and nothing else changed**. The build
job currently prints `uname`, `nproc`, memory and disk, and stops. That costs a few minutes of
CAX41 — pennies — and proves the whole chain end to end: create → JIT → a runner that connects →
one job → guaranteed delete → `servers=0`.

Only then does the real Europe build go in. Debugging a runner and a two-hundred-gigabyte Valhalla
build at the same time is how a week disappears.
