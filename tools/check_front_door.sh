#!/usr/bin/env bash
# The front door (README.md) names no download network and no acquisition verb.
# Worbus P7 design §9 #5: a stranger reads discographies and gaps first; the
# optional downloading setup lives in docs/DOWNLOADING.md, off the launch path.
# The whole file is scanned, image paths and code blocks included.
# Exit 0 clean, 1 on a hit (each printed as file:line:text).
set -u
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
re='soulseek|slskd|torrent|acquir|\bfills?\b|download(s|ed|ing)? (the |an? )?(album|music|song|track)'
hits="$(grep -n -i -E "$re" "$root/README.md")"
if [ -n "$hits" ]; then
  printf '%s\n' "$hits" | sed 's/^/README.md:/' >&2
  echo "check_front_door: README.md says a banned word (see above)" >&2
  exit 1
fi
echo "check_front_door: README.md is clean"
