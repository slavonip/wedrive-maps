#!/usr/bin/env bash
# Sample what the build is actually costing, every few seconds, into a CSV.
#
#   usage: sample-resources.sh <csv-path> <disk-path> &
#
# WHY SAMPLING AND NOT `/usr/bin/time -v`. GNU time reports the peak RSS of ONE process, and the
# thing we need to size a runner against is the peak of the whole container — Valhalla's build
# forks, and much of its working set is page cache over disk-backed sequence files that `time`
# never attributes to it. The cgroup knows the true number; nothing inside the process does.
#
# Peaks are what matter and averages are noise here: a build that fits in 16 GB for 59 minutes and
# wants 17 GB for one is a build that does not fit.
CSV="${1:?csv path}"
WATCH="${2:-/data}"

# cgroup v2 first (what a modern runner gives us), then v1, then nothing.
mem_now() {
  if [ -r /sys/fs/cgroup/memory.current ]; then
    cat /sys/fs/cgroup/memory.current
  elif [ -r /sys/fs/cgroup/memory/memory.usage_in_bytes ]; then
    cat /sys/fs/cgroup/memory/memory.usage_in_bytes
  else
    # MemAvailable is what is left for a new allocation, so used is total minus that — closer to
    # the truth than MemFree, which counts cache as gone.
    awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{print (t-a)*1024}' /proc/meminfo
  fi
}

echo "epoch,mem_bytes,disk_kb" > "$CSV"
while :; do
  printf '%s,%s,%s\n' "$(date +%s)" "$(mem_now)" \
    "$(df -k --output=used "$WATCH" 2>/dev/null | tail -1 | tr -d ' ')" >> "$CSV"
  sleep 5
done
