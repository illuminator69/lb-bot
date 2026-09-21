# lb-bot — status

What has landed and what has not yet been confirmed against the live stack (slskd + Navidrome +
the real library). This is a rolling note; treat an item here as unverified until it is removed.

---

## Active work / status

Two work items are in progress (full detail in `CLAUDE.md`):

- **A — deterministic placement:** replace the beets placement chain with
  find-folder → tag → move → Navidrome-verify. `beets` is being removed from the
  hot path; several `_trusted_*`/beets-profile helpers are slated for deletion.
  A separate branch handles the fully-missing-album case (create `Artist/Album/`).
- **B — robust source selection:** port the album path's automatic source
  failover to the per-track repair path (candidate failover, enqueue-time
  staleness handling, stall watchdog).

These can't be unit-tested without the live stack (slskd + Navidrome + the real
library). Validate against running containers; watch target-folder resolution on
split/duplicate albums, Opus vs FLAC tag keys, and Navidrome path vs container
`/music` path reconciliation.

From the 2026-08-08 download-reliability round, landed with 21 new unit tests
but **not yet run against the live stack**: the multi-pass query builder,
album-aware folder ranking, the quality preference, the extended matcher tiers
(fuzzy / duration / position) with match-basis reporting, and the album-fetch
leftover rescue. Specifically worth checking on the real stack — **Led Zeppelin
– Led Zeppelin**, whose source list must now lead with the debut rather than
the discography sorted by upload speed; an album that previously needed manual
picks despite all files being present; a known localized-filename album; and
that a deliberately wrong folder is still refused by the placement guard rather
than mis-filed.

The Fill-gaps cursor **walks the queue**: when the focused album leaves the
filtered list — you picked a source and it went `downloading`, it completed, or
you skipped it — the cursor lands on whatever took its slot, which is the next
album in the rail. It used to snap back to `items[0]`, which sent you to the top
of a 46-album list after every pick and re-offered albums you had just dealt
with. This is positional rather than a server-supplied `nextId`: `_gaps_view`
already returns a stable, ordered list across polls, so the index is enough.

From the 2026-08-08 design-handoff round, all landed and building but **not yet run
against the live stack**: the scoped `POST /api/gaps/<id>/rescan`; per-track
`present` on `/api/album/tracklist`; the variant × edition shape of
`/api/album/releases` and per-release cover art; `/api/album/similar`; and
compilations appearing in a discography at all.

That last one bumps **`INDEX_SCAN_VERSION` to 2**, because compilations were
previously excluded from the scan outright — every existing index is missing
rows, not merely stale. Indexed artists will read "may be out of date" until
rescanned, and `release_groups` gains a `secondary_types` column via an
idempotent `ALTER` on first open.

From the 2026-08-08 navi-connect integration round, landed but **not yet run
against the live stack**: idempotent `POST /api/album/download`; the
`_album_fill_status` ledger and `GET /api/album/status`; the post-placement
verifier for fills with no review group; and `no_source_reason` /
`mp3_would_help` on the album-download path. Worth checking specifically that a
fill triggered from a client walks searching → downloading → placed → verified
with `verified` arriving only after Navidrome has actually indexed the tracks, and
that a second download POST for the same release returns `existing: true` instead
of starting a second fetch.

Same round, second pass (also unrun): `_index_mark_release_present` on placement,
and the optional `LB_BOT_HUB_URL` ping. Check that a filled album disappears from
the clients' "not in your library" the moment it appears in the library rather
than showing in both, and that a placement still succeeds with the hub down.

**Needs live-stack confirmation** (landed, unit-tested, not yet run against the real
library): the placement guard and its refusal reasons; audio-signature duplicate
detection on an album that already holds a bad pair; the count refresh after a fill;
merge on an mp3 set and on one with an unresolvable path; the cross-language pass on
a known Japanese/English pair; the expandable source file list in both pickers.

Also awaiting the live stack, from the duplicate-aliasing fix: a full duplicate scan
must go back to reporting **few or no** sets; a confirmed delete must land under
`/music/.lb-bot-trash/<date>/` and Restore must put it back; deleting the last copy
in a set must be refused with a visible reason; Navidrome must not index
`.lb-bot-trash` (check its ignored-patterns setting, else move `LB_BOT_TRASH_DIR`
outside the library root); and on an album still missing a track, expanding the source
should list the file you know is there, pick it for that track, and land it in that
slot. The albums whose files were destroyed by the old behaviour need re-scanning and
re-filling — `_inherited_decision` already resets a stale `placed` when a scan finds
the track gone.
