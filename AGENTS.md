# AGENTS.md — lb-bot

Context for working on this repo with Codex. Read this before touching the
gap-fill / repair pipeline.

## What this is

A single-file Python bot (`listenbrainz_bot.py`, ~10.3k lines) that fills gaps in a
self-hosted music library. It exposes a Telegram bot **and** a Flask web UI
(port 8899). It talks to:

- **Navidrome** — music server, Subsonic API. `http://navidrome:4533`.
  This is the source of truth for what's in the library and the success oracle
  for placement (see below).
- **slskd** — Soulseek client, REST API. `http://slskd:5030`.
  Acquisition. API key via `X-API-Key`.
- **MusicBrainz** — canonical release/track metadata, rate-limited.
- **beets** — currently used for placement. **We are removing it from the
  placement path** (see Decision).

`FORMAT_PRIORITY` = flac, opus. Everything else is rejected at search time.

### Runtime / Docker

`docker-compose.yml` mounts (host → container):
- `/mnt/user/appdata/beets/` → `/beets`
- `/mnt/user/appdata/lb-bot` → `/config`
- `/mnt/user/appdata/slskd/downloads` → `/downloads`  (`SLSKD_DOWNLOAD_DIR`)
- `/mnt/user/Music` → `/music`  (`LB_BOT_MUSIC_DIR`, the library root)

Network: `media` (external). mutagen ships with beets, so it's already in the
image even though it's not in `requirements.txt`.

## The goal (confirmed spec)

1. Scan the library for missing tracks.
2. Produce a JSON of the missing tracks. *(works today)*
3. User chooses what to download and from which source. **Source selection needs
   to be robust — peers frequently reject/stall.** *(work item B)*
4. Downloaded tracks are pulled into the library **automatically, with no
   retagging of the existing library.** *(work item A)*

Steps 1–2 work. The two pieces that break under real use are 3 and 4.

## Decision: deterministic placement, no beets in the hot path

Placement currently runs through `_trusted_pinned_merge` →
`beet import --search-id <release> ` with `duplicate_action: merge`. That merge
**only works if the existing album is already a row in the beets DB**
(`_trusted_profile_preflight` requires the beets library to exist and resolve;
`_cleanup_stale_trusted_recordings` and `_verify_trusted_beets_import` query it).
For 4k+ albums that means importing the whole library into beets first — the exact
cost we're trying to avoid.

We do **not** need beets here. For every missing track the gap-detection step
already resolves the `recording_mbid`, `position`, canonical `mb_albumid`, and the
full `canonical_tracklist`. So placement is deterministic: tag the one downloaded
file from data we already have, and drop it into the existing album folder. The
library never enters beets, so nothing gets retagged.

## Work item A — deterministic placement (step 4)

Replace the beets placement chain with: **find folder → tag → move → Navidrome
verify.**

Touch points:
- `repair_import_matched_tracks` (~line 1030) — currently calls
  `_trusted_pinned_merge`, else falls back to a beets register/modify/write/move
  chain. Replace the placement body.
- `_trusted_pinned_merge` (~line 3438) — remove / supersede.
- `_repair_track_metadata` (~line 1002) — **reuse as-is.** It already builds the
  exact canonical tag dict: `title, track, tracktotal, disc, disctotal, album,
  albumartist, artist, mb_albumid, mb_releasegroupid, mb_trackid, year`.

New placement flow:
1. **Find the target album folder without beets.**
   - Primary: pull the on-disk `path` of any existing track in that album from
     Navidrome (Subsonic `getAlbum` → child `path`); take its parent dir.
   - Fallback: a cached filesystem index `mb_albumid → folder` (scan tags once).
   - Split/duplicate albums: reuse `_bucket_duplicate_albums` (~line 2116); pick
     the folder already holding the most tracks of that release.
2. **Tag the downloaded file** with mutagen using the `_repair_track_metadata`
   dict. FLAC/Opus are Vorbis comments — mutagen writes them natively. Tag-read
   reference: `_audio_file_tags` (~line 8430).
3. **Move** with `shutil.move` into the target folder. Filename is cosmetic
   (`{track:02d} - {title}.{ext}`) — Navidrome organizes by tags, not paths.
4. **Trigger Navidrome rescan** (existing mechanism ~line 1992), then confirm via
   the existing `navidrome_pending → navidrome_verified` loop
   (`nd_find_duplicate`, `_navidrome_song_matches_track` ~line 1182). Navidrome is
   already the success oracle — keep it.

Dead code to remove once the above lands: `_trusted_profile_preflight`,
`_cleanup_stale_trusted_recordings`, `_verify_trusted_beets_import`, the
`trusted`/`merge`/`register` beets profile config generation
(`_beets_profile_config`, ~line 3642), and the register/modify/write/move chain in
`repair_import_matched_tracks`.

**Separate branch — fully missing album** (no existing folder at all): create
`Artist/Album/` and write tagged files. This is the only case that needs canonical
foldering; keep it out of the gap-fill path.

## Work item B — robust source selection (step 3)

The album download path already does automatic source failover. The per-track
repair path — the one this whole feature runs on — does not. Port the pattern.

Touch points:
- `_switch_album_source` (~line 3968) — the **reference implementation**: walks
  `ag["alt_sources"]`, abandons the failing peer (`_abandon_group_downloads` →
  `_slskd_cancel`), re-enqueues from the next candidate. Per-track needs the
  equivalent.
- `slskd_enqueue` (~line 2892) with `repair_job_id` — on rejection it only sets
  `retry_available=True` and stops (`_repair_update_download`, ~line 760). No
  auto-advance to the next source. This is the main "works on paper, stalls in
  practice" failure.
- `slskd_run_search` / `_score_folder` / `slskd_pick` (~lines 2746 / 2684 / 2853)
  — ranking is fine (Tubifarry-style: track-count match, availability ratio,
  upload speed, queue length, free slot, collection size).

Three fixes:
1. **Per-track candidate failover.** Carry the ranked folder list into the repair
   download; on reject/stall, advance to the next source automatically instead of
   bouncing to the UI. Mirror `_switch_album_source`.
2. **Enqueue-time staleness.** Scores come from the search response; peer
   `hasFreeUploadSlot` / `queueLength` / online state is often stale 30s later at
   enqueue — the most common rejection cause. Try-next-on-immediate-reject rather
   than trusting the search-time pick.
3. **Stall watchdog.** A peer can accept the enqueue (HTTP 2xx) then park you at
   "Queued, Remotely" forever. That never hits `SLSKD_FAIL_SUBSTATES` (~line 3153),
   so failover never fires. Add a no-progress timeout (no bytes in N seconds →
   `_slskd_cancel`, advance). Detect progress via `_slskd_transfer_percent` /
   `bytesDownloaded` (~line 3180).

Failure detection itself is already solid: `_slskd_failed` / `_slskd_succeeded`
(~3155), `slskd_get_all_downloads` (~3161).

## Suggested order

Do **A first** — it's the cleaner change and it's what makes "downloaded → in
library, no retag" actually true. Then **B**.

## Notes for testing

These can't be unit-tested without the live stack (slskd + Navidrome + the real
library on `/music`). Validate against the running containers. Watch for: target
folder resolution on split/duplicate albums, Opus vs FLAC tag keys, and Navidrome
path format vs container `/music` path (they must reconcile).

Line numbers above are hints from a 2026-06 snapshot and will drift — navigate by
function name.
