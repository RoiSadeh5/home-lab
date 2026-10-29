#!/bin/bash
# tailscale-selfcheck.sh — is THIS server on the tailnet? Called every 5 min by the server heartbeat job.
# Everyone outside the house (friends, the phone, the dashboard PWA, the hotel filter, the MacBook watcher) reaches the
# server ONLY through Tailscale, and the Tailscale Mac app has a Disconnect switch in the menu bar. On 7 Oct 2026 it was
# switched off at the Studio (00:32–08:30 and 11:18–15:17 — thought to block a game; it does not) and the server was
# unreachable from outside for 12 h while every on-box check stayed green: the Healthchecks.io pings use the plain internet.
#   Up   = the macOS VPN service "Tailscale" is Connected (scutil, no side effects) AND the backend is Running.
#          Any `tailscale` CLI call starts the tunnel as a side effect, so the CLI is asked only once scutil says Connected.
#   Down for GRACE_S (10 min) -> `tailscale up --accept-routes` (RouteAll is the app's one non-default `up` flag; when that
#          changes, the CLI names the flags it wants and only those --flags are used). Back -> the log only (11 Oct 2026:
#          the "Tailscale בשרת חובר מחדש" reports push is gone — owner: a push only when something is wrong).
#   Still down -> one `alerts` incident with what to click; retried every run; resolved when Tailscale is back.
#   NeedsLogin / NeedsMachineAuth (key expired, logged out) cannot be fixed from here: incident only.
# Never touches the serve config, the app, or any pref but WantRunning. Log (changes and actions only): logs/tailscale-selfcheck.log
export PATH=/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
HS=/Users/Shared/HomeServer
D="$(cd "$(dirname "$0")" && pwd)"
TS=${TS_CLI:-/usr/local/bin/tailscale}
SCUTIL=${TS_SCUTIL:-scutil}
STATE=${TS_STATE:-$HS/.tailscale-selfcheck-state}
LOG=${TS_LOG:-$HS/logs/tailscale-selfcheck.log}
INCIDENT=${TS_INCIDENT:-$D/ntfy-incident.sh}
GRACE_S=${TS_GRACE_S:-600}
now=${TS_NOW:-$(date +%s)}
boot=${TS_BOOT:-$(sysctl -n kern.boottime 2>/dev/null | sed -E 's/.*sec = ([0-9]+).*/\1/')}
SUBJECT="Tailscale בשרת (Mac Studio)"

log() { mkdir -p "$(dirname "$LOG")"; echo "$(date '+%F %T') $1" >> "$LOG"; }
mins() { echo $(( (now - down_since + 59) / 60 )); }

probe() {   # sets vpn and be; true when the server is on the tailnet
  vpn=$("$SCUTIL" --nc status Tailscale 2>/dev/null | head -1); be=""
  [ "$vpn" = Connected ] || return 1
  be=$("$TS" status --json 2>/dev/null | grep -o '"BackendState": *"[A-Za-z]*"' | head -1 | sed -E 's/.*"([A-Za-z]+)"$/\1/')
  # a CLI that does not answer (6 Oct: the friend app saw "CLIError 3" under launchd) proves nothing: trust the VPN status
  [ -z "$be" ] && { be="no answer"; return 0; }
  [ "$be" = Running ]
}

reconnect() {   # sets out; true when `tailscale up` succeeded
  out=$("$TS" up --accept-routes --timeout=60s 2>&1); local rc=$?
  if [ $rc -ne 0 ] && grep -q 'non-default flags' <<<"$out"; then
    local line flags=() w
    line=$(grep -E '^[[:space:]]*tailscale up ' <<<"$out" | head -1)
    for w in $line; do                      # only its --flags, never its text as a command
      [[ $w == --* && $w != --reset* && $w != --force-reauth* && $w != --auth-key* && $w != --timeout* ]] && flags+=("$w")
    done
    out=$("$TS" up "${flags[@]}" --timeout=60s 2>&1); rc=$?
  fi
  return $rc
}

save() { printf 'down_since=%s\nalerted=%s\ntried=%s\n' "$down_since" "$alerted" "$tried" > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"; }

down_since=""; alerted=0; tried=0
[ -f "$STATE" ] && . "$STATE"
# an outage noted before the last boot says nothing about this boot: the grace starts again (the app connects after login)
[ -n "$down_since" ] && [ -n "$boot" ] && [ "$down_since" -lt "$boot" ] && down_since=$now

if probe; then
  [ -n "$down_since" ] && log "UP again after $(mins) min$([ "$tried" -gt 0 ] && echo " ($tried reconnect attempts before)")"
  if [ "$alerted" = 1 ]; then
    "$INCIDENT" tailscale-self resolved "$SUBJECT" "השרת מחובר שוב; מבחוץ מגיעים אליו." "" >/dev/null 2>&1 && alerted=0
  fi
  down_since=""; tried=0; save; exit 0
fi

if [ -z "$down_since" ]; then
  down_since=$now; save
  log "DOWN (vpn=${vpn:-none} backend=${be:-not asked}) — reconnect after $((GRACE_S / 60)) min"
  exit 0
fi
(( now - down_since < GRACE_S )) && { save; exit 0; }

if [ "$be" = NeedsLogin ] || [ "$be" = NeedsMachineAuth ]; then
  [ "$alerted" = 1 ] || log "DOWN $(mins) min: Tailscale wants a login ($be) — not fixable from here"
  why="Tailscale מבקש התחברות מחדש ($be)."
  action="ב-Mac Studio: תפריט Tailscale ← Log in"
else
  tried=$((tried + 1))
  if reconnect && sleep "${TS_SETTLE_S:-5}" && probe; then
    log "reconnected automatically after $(mins) min (tailscale up, attempt $tried)"
    if [ "$alerted" = 1 ]; then
      "$INCIDENT" tailscale-self resolved "$SUBJECT" "היה כבוי $(mins) דק'; השרת חיבר את עצמו מחדש." "" >/dev/null 2>&1 && alerted=0
    fi
    down_since=""; tried=0; save; exit 0
  fi
  last=$(grep -v -i -E 'https?://' <<<"${out:-}" | tail -1 | cut -c1-160)     # never a login link in a log or a push
  log "reconnect attempt $tried failed (vpn=${vpn:-none} backend=${be:-not asked}): ${last:-no output}"
  why="חיבור אוטומטי נכשל ($(printf '%s' "${last:-אין פלט}" | cut -c1-60)); מנסה שוב כל 5 דק'."
  action="ב-Mac Studio: תפריט Tailscale ← Connect (אם כתוב Log in — להתחבר)"
fi
if [ "$alerted" != 1 ]; then
  "$INCIDENT" tailscale-self active "$SUBJECT" "מנותק כבר $(mins) דק' — מבחוץ לא מגיעים לשרת. $why" "$action" >/dev/null 2>&1 && alerted=1
fi
save
exit 0
