# lb-bot — status

What has landed and what has not yet been confirmed against the live stack (slskd + Navidrome +
the real library). This is a rolling note; treat an item here as unverified until it is removed.

---

## Since the 2026-10-02 release

Landed since, with the Python suite at 0 errors and 0 failures and the web UI's
wording matrices green (`node --test web/src/lib/*.test.mjs`):

- **Artist MusicBrainz overrides** — say which artist a library artist is; the
  ones worth a look are listed.
- **The listens mirror** — a copy of your ListenBrainz listens and the aggregates
  over it, including what you play most that the library lacks.
- **AcoustID through `fpcalc`** — until this the fingerprint check answered "no
  opinion" for every file in the image; it now gives a verdict, and a refusal
  says what the audio was identified as.
- **Album overrides** — retag an album's files to the release you pick, with or
  without a cover; **artist name merge**; both undoable.
- **One library bucket per MusicBrainz artist id** — `INDEX_SCAN_VERSION` is 3,
  so every indexed artist reads "may be out of date" until it is scanned again,
  which the background indexer does by itself over some hours.
- **Acquisition and placement** — a click downloads the source that was clicked;
  a folder must show the album before anything is taken unattended; the import
  files nothing that has not shown the album; multi-disc releases; one spelling
  per tag. ARCHITECTURE, "What changed since the 2026-10-02 release".
- **The web UI on a new frame** — five sections, artist and album pages on top,
  the override tools in lb-bot's own page, and a page that comes back as it was
  left.

This line ran on a live stack as development builds before it was released, and
the web UI was walked there. What that did **not** settle:

- **Whether a favourite, a rating or a play count survives an album override.**
  A retag is expected to change the album's Navidrome id; what Navidrome carries
  across that was not measured, and the UI says nothing about it.
- **An artist override stored for a name that a merge then removed** stays in the
  list applying to nobody; whether undoing the merge brings it back into force
  depends on Navidrome giving the re-created artist the id it had, which was not
  checked.
- **An ID3v2.3 file does not come back byte for byte** from an override and its
  undo: the values are the same, some frames are written in their v2.4 form.
- **The web frame's "comes back as it was left"** was exercised in Chromium, at a
  desktop and a phone width. Other engines are unconfirmed, and so is how far
  apart a real touchpad's last inertia events come (the rule that keeps a swipe
  back from losing the place assumes under a quarter of a second).

The sections below predate that release and are kept as they were written.

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
