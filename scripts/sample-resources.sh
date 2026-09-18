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

# ── THE NUMBER THAT PREDICTS AN OOM IS NOT `memory.current` ─────────────────────────────────
# In cgroup v2 `memory.current` is anon + page cache + kernel, and Valhalla's build writes
# GIGABYTES of temporary sequence files. Every one of those bytes lands in page cache and counts
# toward `memory.current` — and every one is RECLAIMABLE, so it is released under pressure
# instead of killing the process.
#
# Measured on the four-country rung: 12.83 GB of a 15.6 GB runner, which read alone says a master
# twice that size cannot exist. If most of it is cache, that conclusion is wrong by a factor of
# three, and the split-master decision would have been made on it.
#
# `anon` from memory.stat is the part that cannot be reclaimed. Both are sampled, because the
# question "does this fit" is about anon and the question "what is this machine doing" is about
# current.
anon_now() {
  if [ -r /sys/fs/cgroup/memory.stat ]; then
    awk '/^anon /{print $2; found=1} END{if(!found) print 0}' /sys/fs/cgroup/memory.stat
  elif [ -r /sys/fs/cgroup/memory/memory.stat ]; then
    awk '/^rss /{print $2; found=1} END{if(!found) print 0}' /sys/fs/cgroup/memory/memory.stat
  else
    awk '/MemTotal/{t=$2} /MemFree/{f=$2} /Cached/{c=$2} END{print (t-f-c)*1024}' /proc/meminfo
  fi
}

# WHICH SOURCE the memory came from, recorded once, because the number means different things.
# A cgroup reading is THIS CONTAINER. The /proc/meminfo fallback is the WHOLE MACHINE, and on a
# shared runner that includes whatever else is on it. A peak of 4.5 GB is a fact about our build
# only if we know which one answered.
if [ -r /sys/fs/cgroup/memory.current ]; then MEM_SOURCE=cgroup2
elif [ -r /sys/fs/cgroup/memory/memory.usage_in_bytes ]; then MEM_SOURCE=cgroup1
else MEM_SOURCE=meminfo-hostwide
fi
# ── THREE MORE SIGNALS, ADDED 2026-09-18 FOR THE FIRST EUROPE BUILD ────────────────────────
# `anon` against a cgroup limit is the right question on a SHARED runner, where the limit is what
# we are fighting. On a DEDICATED machine the edge belongs to the whole host, and the number that
# names it directly is MemAvailable: what a new allocation can still have. Extrapolation puts
# Europe near that edge, so it is the number this build exists to read.
#
# `disk_free_kb` sits beside `disk_kb` because FREE is what decides whether a build survives, and
# used cannot yield it without knowing the volume size, which differs on every rung.
#
# `load1` is the cheapest evidence of what a stalled build is doing. A hierarchy stage that has
# stopped making progress looks identical to a working one from memory alone; load separates
# single-threaded-and-grinding from waiting-on-nothing.
#
# APPENDED AT THE END of the row. Both readers parse by HEADER NAME (`csv.DictReader`), so older
# four-column samples still load and this cannot shift a column out from under them.
avail_now() {
  awk '/MemAvailable/{print $2 * 1024; found=1} END{if(!found) print 0}' /proc/meminfo
}

echo "# memory source: $MEM_SOURCE" > "$CSV"
echo "epoch,mem_bytes,anon_bytes,disk_kb,mem_available_bytes,disk_free_kb,load1" >> "$CSV"
INTERVAL="${SAMPLE_INTERVAL:-5}"
while :; do
  # ONE `df` call for both numbers. Two calls seconds apart can straddle a large delete and
  # report a used and a free that never coexisted.
  set -- $(df -k --output=used,avail "$WATCH" 2>/dev/null | tail -1)
  printf '%s,%s,%s,%s,%s,%s,%s\n' "$(date +%s)" "$(mem_now)" "$(anon_now)" "${1:-0}" "$(avail_now)" "${2:-0}" "$(cut -d' ' -f1 /proc/loadavg 2>/dev/null || echo 0)" >> "$CSV"
  sleep "$INTERVAL"
done
