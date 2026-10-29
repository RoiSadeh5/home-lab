#!/bin/bash
# drift-check.sh — is git == live? The repo carries the server's own values (no deploy-time rewrite),
# so every deployed file must be BYTE-IDENTICAL to its source in the repo.
# Read-only. Run at the start and end of every session:   bash scripts/drift-check.sh [REPO_DIR]
# Compares:  REPO/scripts/*  (+ dashboard-app/, friend-app/)      <-> /Users/Shared/HomeServer/scripts/
#            REPO/templates/*.plist                  <-> /Library/LaunchDaemons/  and  /Users/Shared/HomeServer/templates/
#            REPO/compose/<stack>/*                  <-> /Users/Shared/HomeServer/compose/<stack>/ (jellyfin-subtitle-compat at the top)
# Exit 0 = identical; 1 = drift (each difference listed); 2 = repo not found.
REPO="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
LIVE=/Users/Shared/HomeServer
# repo-only on purpose: manual tools, never installed live
REPO_ONLY="create-infuse-punctuation-test.py immich-import-from-photos.sh immich-import-watch.py"
[ -d "$REPO/scripts" ] && [ -d "$REPO/templates" ] || { echo "repo not found: $REPO"; exit 2; }
drift=0; ok=0
d() { echo "DRIFT  $1"; drift=$((drift+1)); }
for f in "$REPO"/scripts/*; do
  b=$(basename "$f"); [ -f "$f" ] || continue
  case " $REPO_ONLY " in *" $b "*) continue;; esac
  if [ ! -f "$LIVE/scripts/$b" ]; then d "scripts/$b is in git but not installed"
  elif cmp -s "$f" "$LIVE/scripts/$b"; then ok=$((ok+1)); else d "scripts/$b differs from live"; fi
done
for f in "$LIVE"/scripts/*; do
  b=$(basename "$f"); [ -f "$f" ] || continue
  case "$b" in *.bak*|*.orig|*.rej|.*) continue;; esac
  [ -e "$REPO/scripts/$b" ] || d "live scripts/$b has no source in git"
done
for sub in dashboard-app friend-app; do                 # 11 Oct 2026: friend-app/ too (its friend.js/css had gone unchecked)
  for f in "$REPO"/scripts/$sub/*; do
    [ -f "$f" ] || continue
    b=$(basename "$f"); cmp -s "$f" "$LIVE/scripts/$sub/$b" && ok=$((ok+1)) || d "scripts/$sub/$b differs from live"
  done
done
for t in "$REPO"/templates/*.plist; do
  b=$(basename "$t")
  if [ ! -f "/Library/LaunchDaemons/$b" ]; then d "templates/$b has no installed LaunchDaemon"
  elif cmp -s "$t" "/Library/LaunchDaemons/$b"; then ok=$((ok+1)); else d "templates/$b differs from /Library/LaunchDaemons/$b"; fi
  cmp -s "$t" "$LIVE/templates/$b" || d "templates/$b differs from $LIVE/templates/$b"
done
for p in /Library/LaunchDaemons/com.roisadeh.*.plist; do
  [ -e "$REPO/templates/$(basename "$p")" ] || d "installed $(basename "$p") has no template in git"
done
for t in "$LIVE"/templates/*; do [ -e "$REPO/templates/$(basename "$t")" ] || d "live templates/$(basename "$t") has no source in git"; done
for s in "$REPO"/compose/*/; do
  st=$(basename "$s"); live="$LIVE/compose/$st"; [ "$st" = jellyfin-subtitle-compat ] && live="$LIVE/jellyfin-subtitle-compat"
  for f in "$s"*; do
    [ -f "$f" ] || continue
    cmp -s "$f" "$live/$(basename "$f")" && ok=$((ok+1)) || d "compose/$st/$(basename "$f") differs from live"
  done
done
echo "identical: $ok · drift: $drift"
[ "$drift" -eq 0 ]
