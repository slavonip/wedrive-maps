#!/usr/bin/env bash
# Prove a graph BEHAVES, before anyone drives on it.
#
# The car's runtime has no version gate: a graph built by the wrong Valhalla is read, not
# refused, and a graph missing half a country routes happily up to the frontier. So "it built"
# and "it downloaded" prove nothing, and the only honest gate is a route with a known answer.
#
#   usage: probe.sh <config.json> <package-id>
#
# Exit code is the gate: non-zero means the package must NOT be promoted, whatever else passed.
set -euo pipefail

CONF="${1:?valhalla config}"
PACKAGE="${2:?package id}"

fail=0

# Ask for one route and print its distance in metres, or "none".
route_m() {
  local from_lat=$1 from_lon=$2 to_lat=$3 to_lon=$4
  local req
  req=$(printf '{"locations":[{"lat":%s,"lon":%s},{"lat":%s,"lon":%s}],"costing":"auto"}' \
    "$from_lat" "$from_lon" "$to_lat" "$to_lon")
  valhalla_service "$CONF" route "$req" 2>/dev/null |
    python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print(int(d["trip"]["summary"]["length"] * 1000))
except Exception:
    print("none")'
}

check() {
  local name=$1 got=$2 want_min=$3 want_max=$4
  if [ "$got" = "none" ]; then
    echo "FAIL $name: no route at all"
    fail=1
  elif [ "$got" -lt "$want_min" ] || [ "$got" -gt "$want_max" ]; then
    echo "FAIL $name: ${got} m, expected ${want_min}..${want_max} m"
    fail=1
  else
    echo "ok   $name: ${got} m"
  fi
}

echo "== probes for $PACKAGE =="

# ── 1. One-ways are obeyed ──────────────────────────────────────────────────────────────────
# Strada Alexandr Pușkin, Chișinău: the same two points cost 322 m with the one-way and 681 m
# against it (§7, measured 2026-08-29). A graph that has lost direction gives the same answer
# both ways, and that is the failure this catches.
with=$(route_m 47.0246 28.8320 47.0209 28.8331)
against=$(route_m 47.0209 28.8331 47.0246 28.8320)
check "one-way, with"    "$with"    250 450
check "one-way, against" "$against" 520 900

# ── 2. A bridge is not a junction ───────────────────────────────────────────────────────────
# Bulevardul Renașterii Naționale over Calea Moșilor: 37 m apart in a straight line, ~3 km by
# road, because in OSM two ways connect only if they share a node (§7).
bridge=$(route_m 47.0411 28.8447 47.0414 28.8443)
check "grade separation" "$bridge" 1500 6000

# ── 3. The timezone is baked into the nodes ─────────────────────────────────────────────────
# Valhalla writes a timezone id into every node at BUILD time, from a database it downloads
# separately; without it the build warns "Time zone db not found. Not saving time zone
# information" and carries on. CI goes green, the archive exists, routes work — and every
# time-conditional restriction and every cross-border arrival time is quietly wrong. Only the
# artifact can be asked, and only through a route carrying `date_time`: `locate` does not report
# it (measured 2026-09-17).
# Two DISTINCT points, because a route needs somewhere to go; the zone reported for the first is
# what is being asked about. The second is a few hundred metres away, close enough to be in the
# same zone and to keep the request trivial.
timezone_at() {
  local lat=$1 lon=$2
  local lat2 lon2
  lat2=$(awk -v v="$lat" 'BEGIN{printf "%.6f", v + 0.004}')
  lon2=$(awk -v v="$lon" 'BEGIN{printf "%.6f", v + 0.004}')
  valhalla_service "$CONF" route "$(printf '{"locations":[{"lat":%s,"lon":%s},{"lat":%s,"lon":%s}],"costing":"auto","date_time":{"type":1,"value":"2026-09-17T08:00"}}' "$lat" "$lon" "$lat2" "$lon2")" 2>/dev/null |
    python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print(d["trip"]["locations"][0].get("time_zone_name", "none"))
except Exception:
    print("none")'
}

check_zone() {
  local name=$1 got=$2 want=$3
  if [ "$got" = "$want" ]; then
    echo "ok   $name: $got"
  else
    echo "FAIL $name: $got, expected $want"
    fail=1
  fi
}

# NO SYNTHETIC OVERLAY, ever. A package spanning two countries spans two IANA zones — Moldova is
# Europe/Chisinau and Romania is Europe/Bucharest — and pinning one over both would be right about
# the offset today and wrong about the rules the first time they diverge.
check_zone "timezone, Chișinău" "$(timezone_at 47.0105 28.8638)" "Europe/Chisinau"
if [[ "$PACKAGE" == *"-ro"* || "$PACKAGE" == "ro"* ]]; then
  check_zone "timezone, Iași" "$(timezone_at 47.1585 27.6014)" "Europe/Bucharest"
fi

# ── 4. The border, for packages that claim to cross one ─────────────────────────────────────
# Chișinău → Iași. Only meaningful for a package containing both countries; a single-country
# package is expected to refuse, and that refusal is not a failure.
if [[ "$PACKAGE" == *"-ro"* || "$PACKAGE" == "ro"* ]]; then
  border=$(route_m 47.0105 28.8638 47.1585 27.6014)
  check "Chișinău → Iași" "$border" 100000 250000
fi

exit $fail
