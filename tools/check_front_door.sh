#!/usr/bin/env bash
# The front door (README.md) names no download network and no acquisition verb.
# Worbus P7 design §9 #5: a stranger reads discographies and gaps first; the
# optional downloading setup lives in docs/DOWNLOADING.md, off the launch path.
# The whole file is scanned, image paths and code blocks included.
# Exit 0 clean, 1 on a hit (each printed as file:line:text), 2 when the check
# could not run (README.md missing or unreadable, or grep failed): it fails closed.
set -u
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
re='soulseek|slskd|torrent|acquir|\bfills?\b|download(s|ed|ing)? (the |an? )?(album|music|song|track)'
# wide: what the fixed regex misses, "acquisition" and the fill family (Worbus P7 C1). "fetch" stays allowed in the
# "Downloading (optional)" section (ruling R17).
wide='acqui|\bfill(s|ed|ing|er)?\b'
readme="$root/README.md"
if [ ! -r "$readme" ]; then
  echo "check_front_door: cannot read $readme; nothing was checked" >&2
  exit 2
fi
hits="$(grep -n -i -E -e "$re" -e "$wide" "$readme")"
rc=$?
if [ "$rc" -gt 1 ]; then
  echo "check_front_door: grep failed (status $rc); nothing was checked" >&2
  exit 2
fi
if [ -n "$hits" ]; then
  printf '%s\n' "$hits" | sed 's/^/README.md:/' >&2
  echo "check_front_door: README.md says a banned word (see above)" >&2
  exit 1
fi
echo "check_front_door: README.md is clean"
