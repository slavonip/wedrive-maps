#!/bin/bash
# What the machine has to say for itself, run over SSH before it is destroyed.
#
# TWO BUILDERS HAVE NOW BEEN CREATED, BILLED AND DELETED WITHOUT ANYONE LEARNING WHY their runner
# never connected. The second one had systemd and a journal — on a host that cleanup removed ten
# seconds after the failure. The logs existed and were unreachable, which is the same blindness
# as the first failure wearing better clothes.
#
# So this runs FIRST and deletion waits for it. It is deliberately verbose and deliberately
# unconditional: every command runs even if an earlier one failed, because the interesting
# machine is the broken one and a broken machine answers some questions and not others.
#
# It reads only. Nothing here changes the host.
set +e

section() { printf '\n========== %s ==========\n' "$1"; }

section "stages — the last PASS is where it got to"
cat /var/log/wedrive-stages 2>/dev/null || echo "no stage file: the bootstrap script never ran"

section "bootstrap script output"
tail -100 /var/log/wedrive-bootstrap.log 2>/dev/null || echo "no bootstrap log"

section "cloud-init status"
cloud-init status --long 2>&1

section "cloud-init units"
systemctl status cloud-init cloud-init-local cloud-config cloud-final --no-pager 2>&1 | head -60

section "cloud-init output, last 80 lines"
tail -80 /var/log/cloud-init-output.log 2>&1

section "cloud-init log, errors only"
grep -iE "error|fail|traceback|warn" /var/log/cloud-init.log 2>/dev/null | tail -40

section "github-runner unit"
systemctl status github-runner --no-pager 2>&1 | head -40

section "github-runner journal"
journalctl -u github-runner --no-pager 2>&1 | tail -80

section "the unit as systemd sees it"
systemctl cat github-runner --no-pager 2>&1 | sed 's/\(--jitconfig \)[A-Za-z0-9+/=]\{16\}[A-Za-z0-9+/=]*/\1<REDACTED>/'

section "/opt/runner"
ls -la /opt/runner 2>&1 | head -30
echo "run.sh present: $([ -x /opt/runner/run.sh ] && echo yes || echo NO)"

section "machine"
uname -a
dpkg --print-architecture
docker --version 2>&1
df -h / 2>&1 | tail -2
free -h 2>&1 | head -2

section "processes that should exist if the runner is alive"
ps aux | grep -E "Runner|run\.sh" | grep -v grep

section "apt and unattended-upgrades — the lock that breaks boot-time installs"
systemctl is-active unattended-upgrades 2>&1
fuser -v /var/lib/dpkg/lock-frontend 2>&1 | head -5
tail -20 /var/log/unattended-upgrades/unattended-upgrades.log 2>/dev/null

section "network to GitHub, which the runner needs and nothing else here proves"
curl -sS -o /dev/null -w "api.github.com: %{http_code} in %{time_total}s\n" \
  https://api.github.com 2>&1
