#!/bin/bash
# Bring the runner up, and RECORD WHICH STAGE FAILED.
#
# Written as one script rather than a dozen `runcmd` lines because the previous version could not
# answer the only question that mattered after a failure: how far did it get? Two machines have
# now been created, billed, and destroyed without anyone learning why their runner never
# connected — the second one even had systemd and a journal, on a host that was deleted ten
# seconds later.
#
# So every stage writes PASS or FAIL to /var/log/wedrive-stages before moving on, and the file is
# the first thing the diagnostic dump reads. A stage that fails does NOT abort the script: the
# machine must stay up long enough to be asked what happened, and a half-built machine that can
# be interrogated is worth far more than a clean exit that explains nothing.
#
# NOTHING IS MASKED WITH `|| true`. The previous version wrapped `installdependencies.sh` that
# way, so a failure there would have been invisible and the next stage would have started anyway.
# Here a failure is recorded as a failure and the script keeps going deliberately, which is a
# different thing from pretending it succeeded.
set -uo pipefail

STAGES=/var/log/wedrive-stages
LOG=/var/log/wedrive-bootstrap.log
exec > >(tee -a "$LOG") 2>&1
: > "$STAGES"

RUNNER_VERSION="__RUNNER_VERSION__"
ARCH="__ARCH__"

stage() {
  local name="$1"; shift
  echo "=== $name ==="
  if "$@"; then
    echo "$name=PASS" >> "$STAGES"
    return 0
  fi
  local code=$?
  echo "$name=FAIL(exit $code)" >> "$STAGES"
  return "$code"
}

# apt at boot fights unattended-upgrades for the dpkg lock, and losing that race looks exactly
# like a broken package list. Wait for it rather than guess.
wait_for_apt() {
  for _ in $(seq 1 60); do
    fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || return 0
    sleep 5
  done
  echo "the dpkg lock was still held after five minutes"
  return 1
}

stage apt_lock wait_for_apt
stage docker systemctl enable --now docker
stage user_create bash -c 'id runner >/dev/null 2>&1 || useradd -m -s /bin/bash runner'
stage user_docker usermod -aG docker runner
stage workdir bash -c 'mkdir -p /opt/runner && chown runner:runner /opt/runner'

URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-${ARCH}-${RUNNER_VERSION}.tar.gz"
echo "runner tarball: $URL"
# DELIBERATELY STILL `su - runner`, and this is a reversal.
#
# When the diagnostic SSH failed on an expired root password I also swapped this for `runuser`,
# reasoning that PAM's account phase would refuse `su` the same way. That may well be true — and
# there is NO evidence for it. Changing the mechanism under investigation at the same moment as
# fixing the instrument would have meant that a fourth green run proved nothing about which of
# the two changes mattered, and a fourth red one would have lost the original symptom.
#
# The next run is an OBSERVATION, not a repair. If `runner_download` fails with a password
# message, the swap is justified by evidence and takes one line. If it passes, the whole theory
# was wrong and the cause is somewhere else entirely.
stage runner_download su - runner -c "curl -fsSL -o /opt/runner/runner.tar.gz '$URL'"
stage runner_extract su - runner -c "cd /opt/runner && tar xzf runner.tar.gz && rm runner.tar.gz"

# The check the previous version never made: the unit is about to point at this file, and with a
# restart policy a missing binary would either loop or sit dead. Either way the cause would be
# two stages upstream and invisible.
stage runner_present test -x /opt/runner/run.sh

# The runner is .NET and will not start without ICU, complaining about globalization in terms
# that read like nothing to do with CI. Its own installer knows what this release needs on this
# distribution — and its exit code is now believed rather than discarded.
stage dependencies /opt/runner/bin/installdependencies.sh

stage service_install systemctl daemon-reload
stage service_start systemctl enable --now github-runner.service
stage timer_start systemctl enable --now self-destruct.timer

# Recorded whether or not anything failed: if a password policy is in play, this is the line
# that shows it, and it costs nothing to have it already in the log.
echo "=== account state ==="
chage -l runner 2>&1 | head -5
passwd -S root 2>&1
passwd -S runner 2>&1

echo "=== stages ==="
cat "$STAGES"
