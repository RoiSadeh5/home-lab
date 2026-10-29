#!/bin/bash
# wait-for-drives.sh — launchd wrapper: start a service only once the external volumes it needs are mounted.
# Why: after a reboot launchd started every service ~6 s after boot, before the Thunderbolt enclosure's volumes were
# mounted. Services that found an empty mount point put their data into an error state and did not recover by themselves.
# Usage (a plist's ProgramArguments):  /bin/bash wait-for-drives.sh <Volume>[,<Volume>…] <program> [args…]
# It reads only the kernel's mount table (`mount`), never the volumes' contents, so bash needs no macOS privacy (TCC)
# grant for removable volumes; then it execs the program — same PID, the program's own TCC identity. No time limit by
# default: a server without its drives has nothing to serve; a log line every 10 min shows what it waits for.
# WAIT_FOR_DRIVES_MAX_S: a cap for a MONITOR that must run even without a drive — after the cap it starts anyway.
set -u
vols="${1:?usage: wait-for-drives.sh Volume[,Volume…] program [args…]}"; shift
[ $# -gt 0 ] || { echo "wait-for-drives: no program given" >&2; exit 64; }
say(){ echo "$(date '+%F %T') wait-for-drives [$(basename "$1")]: $2"; }
trap 'exit 143' TERM INT
waited=0; max="${WAIT_FOR_DRIVES_MAX_S:-0}"      # 0 = no limit (the media services)
while :; do
  missing=""
  for v in ${vols//,/ }; do mount | grep -q " on /Volumes/$v (" || missing="$missing $v"; done
  [ -z "$missing" ] && break
  [ "$max" -gt 0 ] && [ "$waited" -ge "$max" ] && { say "$1" "still missing$missing after ${waited}s — starting anyway (cap ${max}s)"; break; }
  [ $((waited % 600)) -eq 0 ] && say "$1" "waiting for$missing (${waited}s since start)"
  sleep "${WAIT_FOR_DRIVES_STEP:-5}"
  waited=$((waited + ${WAIT_FOR_DRIVES_STEP:-5}))
done
[ "$waited" -gt 0 ] && [ -z "$missing" ] && say "$1" "drives mounted after ${waited}s — starting"
exec "$@"
