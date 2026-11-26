#!/usr/bin/env bash
# Is this runner able to do the job at all? Asked FIRST, answered in under a minute.
#
#   usage: runner-preflight.sh <need-GB> [url ...]
#
#   need-GB   free disk the job needs on PREFLIGHT_PATH (default /), whole or fractional GB
#   url       every one must answer over HTTPS from this runner (any HTTP status counts: the
#             question is whether the host is reachable, not whether the file exists)
#
# WHY. GitHub guarantees a standard Linux runner 14 GB of disk and nothing about its network. The
# factory was sized on what Azure-hosted runners happened to give (145 GB disk, ~85 GB free).
# Measured 2026-11-04 (run 41818389322): 5 of 103 jobs landed on AWS-hosted runners with a 72 GB
# disk and ~13 GB free — Germany's graph died there with `Bus error` five minutes into the tiles —
# and those runners cannot reach download.geofabrik.de:443 either (charging factory, 22–24 Nov).
# A job that cannot succeed must say so in a minute, by name, not half an hour later as a crash.
#
# Exit 0 = good runner. Exit 1 = BAD RUNNER (the line starts with those words; factory-rerun.yml
# reruns a job that failed in its `preflight` step, and a rerun is usually a different machine).
#
#   PREFLIGHT_PATH        where the free space is measured (default /)
#   PREFLIGHT_URL_SECS    budget per url, seconds (default 20; the whole script stays under 60)
#   PREFLIGHT_TEST_FAIL   =1: fail on purpose (control runs of the rerun mechanics only)
set -u
NEED_GB="${1:?usage: runner-preflight.sh <need-GB> [url ...]}"
shift
WHERE="${PREFLIGHT_PATH:-/}"
PER_URL="${PREFLIGHT_URL_SECS:-20}"
STARTED=$(date +%s)
BUDGET=55
bad=0

# Which cloud this is. Informational only — the verdict comes from disk and network, never from
# the name of the host — but it is the first thing wanted when reading a failed run.
cloud=unknown
if [ -r /sys/class/dmi/id/sys_vendor ]; then
  case "$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null)" in
    *Amazon*) cloud=AWS ;;
    *Microsoft*) cloud=Azure ;;
    *Google*) cloud=GCP ;;
  esac
fi
echo "runner: ${RUNNER_NAME:-?}  cloud: $cloud  arch: ${RUNNER_ARCH:-$(uname -m)}"

# --- disk ------------------------------------------------------------------------------------------
df -h "$WHERE" 2>/dev/null | tail -1
free_kb=$(df -Pk "$WHERE" 2>/dev/null | awk 'NR==2{print $4}')
case "$free_kb" in
  ''|*[!0-9]*) echo "BAD RUNNER: cannot read the free space of $WHERE"; bad=1 ;;
  *)
    verdict=$(awk -v f="$free_kb" -v n="$NEED_GB" 'BEGIN{
      if (n !~ /^[0-9]+(\.[0-9]+)?$/) { print "usage"; exit }
      printf "%s %.1f\n", (f / 1048576 >= n) ? "ok" : "short", f / 1048576 }')
    case "$verdict" in
      usage) echo "runner-preflight.sh: need-GB '$NEED_GB' is not a number"; exit 2 ;;
      ok\ *) echo "disk: ${verdict#ok } GB free on $WHERE, need $NEED_GB GB — ok" ;;
      *) echo "BAD RUNNER: ${verdict#short } GB free on $WHERE, this job needs $NEED_GB GB"; bad=1 ;;
    esac ;;
esac

# --- network ---------------------------------------------------------------------------------------
for url in "$@"; do
  left=$(( BUDGET - ( $(date +%s) - STARTED ) ))
  if [ "$left" -lt 3 ]; then
    echo "BAD RUNNER: no time left to check $url (the checks before it used the whole budget)"; bad=1
    continue
  fi
  t=$PER_URL; [ "$t" -gt "$left" ] && t=$left
  c=$(( t / 2 )); [ "$c" -lt 2 ] && c=2
  code=$(curl -sS -o /dev/null -I -L --connect-timeout "$c" --max-time "$t" -w '%{http_code}' "$url" 2>/tmp/preflight-curl.$$)
  rc=$?
  if [ "$rc" = 0 ] && [ "${code:-000}" != 000 ]; then
    echo "network: $url answers ($code) — ok"
  else
    echo "BAD RUNNER: $url UNREACHABLE (curl exit $rc: $(tr '\n' ' ' < /tmp/preflight-curl.$$ | cut -c1-200))"; bad=1
  fi
  rm -f /tmp/preflight-curl.$$
done

if [ "${PREFLIGHT_TEST_FAIL:-0}" = 1 ]; then
  echo "BAD RUNNER: PREFLIGHT_TEST_FAIL=1 — failing on purpose (control run of the rerun mechanics)"; bad=1
fi

echo "preflight: $(( $(date +%s) - STARTED )) s"
if [ "$bad" != 0 ]; then
  echo "::error::BAD RUNNER (${RUNNER_NAME:-?}, $cloud): this machine cannot do the job; a rerun usually lands on another one"
  exit 1
fi
echo "preflight: good runner"
