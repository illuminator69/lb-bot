# CLAUDE.md — lb-bot

Context for working on this repo with Claude Code. Read this before touching the
gap-fill / repair pipeline.

## Local dev environment (this workstation, since 2026-09-20)

The repo moved off the Windows box; it now lives at
`/home/ilya/Documents/navi-connect+lb-bot/Lb-bot-missing`, a sibling of `navi-connect/` and
`navi-connect-publish/lb-bot/` (the publish clone). Node and pip are dnf-installed system-wide;
**this project's Python deps are not** — they live in `.venv`, because Fedora's
`python3-telegram-bot` is 22.8 against our pinned 21.10:

```bash
.venv/bin/python listenbrainz_bot.py            # deps from requirements.txt, Python 3.14
.venv/bin/python -m unittest test_album_review  # suite; see the baseline below
cd web && npm ci && npm run build               # SPA → web/dist (system Node 24, npm not pnpm)

./deploy.sh status                              # what is actually running on the NAS
./deploy.sh dev                                 # this tree onto the NAS in ~1 min (see Deployment)
```

Deploying no longer means SSHing into the NAS by hand — see **Deployment** below.

**Test baseline:** 230 tests, **32 errors, 0 failures**. The errors are all stale beets tests kept
from before the beets removal (see Decision below). Anything *else* failing is yours.

**Placement goes through `_place_file`.** Move, `chmod 0o664`, `_touch`, in that order, and each
step is load-bearing — read its docstring before changing the placement path. It was extracted
from `_deterministic_album_import` on 2026-09-20 because
`test_move_does_not_inherit_source_mtime_or_mode` was re-implementing the move inline and so never
exercised the chmod: on Windows `/downloads` and `/music` were different volumes, the copy reset
the mode by accident, and the test passed. On Linux, with both paths on one filesystem,
`shutil.move` is an `os.rename` that preserves the source's `0o600` — which is exactly the
production bug the chmod exists to prevent (slskd writes 0644/0444; without it the placed track is
read-only to the users group).

Line endings: the tree is LF and the repo is `core.autocrlf=input`. Don't reintroduce CRLF.

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

`docker-compose.yml` in this repo is **reference only** — nothing runs it (see
Deployment below). It documents the mounts the live container gets
(host → container):
- `/mnt/user/appdata/beets/` → `/beets`
- `/mnt/user/appdata/lb-bot` → `/config`
- `/mnt/user/appdata/slskd/downloads` → `/downloads`  (`SLSKD_DOWNLOAD_DIR`)
- `/mnt/user/Music` → `/music`  (`LB_BOT_MUSIC_DIR`, the library root)

Network: `media` (external). mutagen ships with beets, so it's already in the
image even though it's not in `requirements.txt`.

The container runs as `user: "99:100"` (Unraid's nobody:users) so placed tracks
aren't root-owned. All three mounts must be writable by that uid — `/downloads`
included, since placement unlinks the source file after copying it.

**One-time host chown when switching to `99:100`:** files an earlier root run
left behind (the SQLite index `/config/library_index.db`, `lb_bot_state.json`,
`missing_album_review.json`, and any album folders under `/music`) stay
root-owned — the umask only governs *new* files, and the container can't chown
what root owns. Until they're fixed, writes fail: the index DB surfaces this as
"Discography scan failed / attempt to write a readonly database" — and since
2026-09-22 the **album review lives in that DB too**, so the same permission
problem also costs every group mutation on restart, logged as `review group save
failed: ...` once per flush. It degrades safely (ids stay marked for retry, the
in-memory review is intact, the JSON half still writes), but nothing about the
review survives a restart until this is fixed. Fix on the
host once: `chown -R 99:100 /mnt/user/appdata/lb-bot` (and `/mnt/user/Music` if
placement into pre-existing album folders fails).

`LB_BOT_UMASK` (default `002` in compose) is applied by the bot itself via
`os.umask()` at import. The image is `python:3.11-slim` with a bare `python`
CMD — there is no s6/linuxserver init, so a bare `UMASK` var would do nothing.

### Deployment

The live container is **`lb-bot-lb-bot-1`**, started by Unraid's **Compose Manager**
plugin from its own project dir on the NAS:

```
/boot/config/plugins/compose.manager/projects/lb-bot/
    compose.yaml            image: ghcr.io/illuminator69/lb-bot:latest
    compose.override.yaml   plugin-managed UI labels
    .env                    the credentials — the only place they live
```

That `compose.yaml` is near-identical to the repo's, except it pulls the published
image instead of building. **The repo's `docker-compose.yml` is not what runs**, so
don't "fix" it to match the NAS, and don't add a local `.env` — compose reads the
NAS-side one.

The path from an edit here to a running container:

```
Lb-bot-missing  --release-->  navi-connect-publish/lb-bot  --push main-->  GH Actions
     (this repo)                 (curated subset, public)                      |
Unraid Compose Manager  <--pull--  ghcr.io/illuminator69/lb-bot:latest  <-------+
```

`.github/workflows/docker.yml` lives **only in the publish clone**, not here. It fires
on push to `main` and tags `:latest` plus `:sha-<short>`, and takes 35–60 s.

That build is where a release stalls, so check it with `./deploy.sh ci` before pulling. Note
that "the newest run is green" does **not** mean there is anything to pull — the newest run
is usually the *last* release, and pulling on it silently re-pulls the running image. `ci`
therefore compares the run's commit against the deployed image's
`org.opencontainers.image.revision` and says which of the two you are looking at.

`./deploy.sh` drives all of it from the workstation over SSH (host alias `unraid`,
configured in `~/.ssh/config`; a `unraid` docker context points at its daemon):

| command | does |
|---|---|
| `check` / `status` | SSH + remote daemon reachable; running image, its revision and build date |
| `dev` | builds **this tree** straight onto the NAS daemon under the GHCR tag and recreates the container — ~1 min, skips git/CI entirely |
| `release` | copies the published file set into the publish clone, then stops — you review, commit and push |
| `ci` | GHCR build status, compared against what is actually deployed (needs `gh`, logged in) |
| `pull` | NAS pulls the released image and recreates (also the way to undo a `dev` build) |
| `logs` / `restart` / `shell` | against the live container |

`dev` is the fast iteration loop; it leaves an **unreleased** image running, so finish with
`release` + `pull` for anything that should outlive the session.

Two things that make this work, both easy to break:

- **`.dockerignore` must stay tight.** The build context is shipped to the NAS on every
  `dev` build — and to GitHub on every CI build. Without it, it is ~152 MB (`.venv`,
  `web/node_modules`, `.git`, design bundles); with it, ~375 kB.
- **Compose on the NAS is driven over plain `ssh`, not the docker context.** `-f` resolves
  the compose file *and its `.env`* client-side, and both live on the NAS.

`release` only overwrites files the publish clone already tracks, so the `SESSION-*` /
`PLAN-*` notes and `design_handoff_lb_bot_frontend/` never leak into the public repo; a new
source file is reported rather than published silently, so adding one stays deliberate.

`release` also skips **`.gitignore`** and **`docker-compose.yml`** (added 2026-09-22).
Before that it copied both over the clone's, which un-ignored `.claude/` and `covers/`
in the public repo and reverted the compose file from the GHCR image to
`image: lb-bot:local` — twice in one day, each time undone by hand after the fact.
The two `.gitignore`s are now kept byte-identical as well, so even an unskipped copy
would be a no-op. `deploy.sh` and `.dockerignore` are tracked here now too; they were
untracked, which meant the deployment path had no version control at all.

**`README.md` and `docs/` in the clone are the clone's own, and `release` skips them.** The
public README is a curated front page — description, one screenshot, a two-command quick start
and links — with the architecture, deployment and status notes split into `docs/` there on
2026-09-22. This tree's `README.md` is the long internal version and always has been: the two
diverged by ~100 lines and a straight copy would have quietly reverted the public one. Edit the
public docs in the clone, and don't "fix" the divergence by syncing them.

### AudioMuse-AI

The bot does **not** call AudioMuse. AudioMuse is run on its own schedule; the
bot's only job is to make the filled tracks visible to it. Placement stamps the
placed file — and its album and artist folders — with a current mtime (`_touch`,
called from `_deterministic_album_import`).

**Required Navidrome setting:** set `ND_RECENTLYADDEDBYMODTIME=true` on the
*Navidrome* container (not lb-bot — it's not in this repo's compose). Navidrome
derives `album.created_at` from the *oldest* file ctime in the album, so a
gap-filled album never moves in the default `newest` sort, and AudioMuse (which
asks Navidrome for `getAlbumList2?type=newest`) never sees it. With the flag,
`newest` sorts by `album.updated_at` = *newest* file mtime, which placement now
stamps to now — so a filled album ranks as recently-added and AudioMuse's
recent-albums analysis picks it up on its next run.

### Editorial metadata — `GET /api/meta/artist`, `GET /api/meta/album`

The "About" the clients show for an artist or an album: real, attributed,
full-length text plus credits, relations and external links. Both are
whitelisted on the hub as `/lb/meta/*`.

**Resolution chain, per entity.** MusicBrainz `?inc=url-rels` → the `wikidata`
relation → Wikidata `wbgetentities` (`props=sitelinks|descriptions`) → the enwiki
title and the one-line description → Wikipedia `action=query&prop=extracts`.
A bare `wikipedia` url-rel is the fallback for entities that predate the Wikidata
migration. On top of that: `artist?inc=artist-rels` for band members and side
projects, and `release?inc=artist-rels work-rels` for producer/engineer/writer
credits. The external-links row falls out of the url-rels already fetched.

It lives here rather than in a Navidrome metadata-agent plugin **because it also
has to serve the virtual `mb:<mbid>` pages** — an agent by definition only knows
about releases in the library.

Four constraints that will bite if ignored:

1. **Wikimedia is never routed through `mbz_get`.** That function holds
   `_mbz_lock` across a hard global 1 req/sec sleep, which is the discography
   scanner's entire budget; putting two Wikimedia calls behind it would make
   every artist page cost two scan-seconds. `_wiki_get` has its own cache, its
   own Session (`_http` pools per host), and the descriptive `User-Agent`
   Wikimedia's ToS requires.
2. **The MusicBrainz leg does spend that budget** — one `url-rels` request per
   entity. Durably cached in `mbz_cache.json`, so it is one-time per entity, but
   a cold artist page can queue behind a running scan. Keep it to one request.
3. **`strict=False`, and mind the per-key failure memory.** "No Wikipedia
   article" is a legitimate answer and a blank result must not raise. But
   `mbz_get` caches failures against the exact `path?params` key, so
   `inc=url-rels` carries a *separate* five-minute `MBZ_FAIL_COOLDOWN` memory
   from the discography path's `inc=releases artist-credits media` — the same
   trap `SESSION-2026-09-06-fresh-tab-followups` records.
4. **Cached in SQLite, not another JSON.** An additive `meta(kind, mbid, payload,
   ok, fetched_at)` table in `library_index.db`, created alongside the existing
   schema. Positive TTL 30 days, negative 7 so a newly-written article is picked
   up without a manual purge; `ok` is what distinguishes "we looked and there is
   nothing" from a hit. **`INDEX_SCAN_VERSION` is deliberately not bumped** — it
   gates the discography matcher, and bumping it would force a full library
   rescan for a cache that can simply be cold. `?refresh=1` bypasses the cache
   for one entity.

Everything is capped server-side (`META_MAX_PARAGRAPHS`, `META_MAX_CHARS`,
`META_MAX_CREDITS`, `META_MAX_RELATIONS`, `META_MAX_LINKS`): a long Wikipedia
article otherwise runs past the hub's 4 MB `PROXY_MAX_RESPONSE`, which is
answered 502 `tooLarge` — correct, but user-visible. Two further caps came out of
a live run rather than theory: links are capped **per label**
(`META_MAX_LINKS_PER_LABEL` — two relation types can share one, so a per-type
cap still let `Stream` through four times), because a well-tagged artist carries
a purchase relation per storefront and rendered five identical `Buy` chips; and band members
are collapsed to one row per person, because MusicBrainz states one relation per
instrument and per stint, so a four-piece came back with the bassist four times.
Recording-level relations are deliberately **not** requested for credits: they
multiply the response by the tracklist for a section nobody reads per-track.

### The album review — origins, and why it lives in SQLite

The review is the working set behind Fill Gaps: one *group* per album that needs
something, carrying the user's decisions (hidden/skip, approved tracks, chosen
source) and the download state. It is **not** the same thing as the library index
— `library_index.db`'s `release_groups` is the durable catalogue of what exists
and what is missing (68k rows); the review is the much smaller list of work items
layered on top.

**Every group has an `origin`, and a scan replaces only its own.** Values are in
`REVIEW_ORIGINS`: `library` (the full scan-all, the single-artist scan, a release
refresh), `playlist`, `spotify`, `duplicate`, `repair`. `_replace_review_groups`
is the entry point; `_union_review_groups` is for a scan that covers one artist or
one release and so is authoritative for nothing else.

This is the fix for a real data loss. Until 2026-09-22 the entry point was
`_store_review_groups`, which replaced the **whole** list, with a comment stating
that was fine because "callers are full-library rebuilds where `groups` is the
complete truth". That was true of scan-all and of neither other caller. On
2026-09-20 a ListenBrainz playlist scan therefore replaced a ~3000-group library
review with its own 97, silently and with no way back — `gaps.needs` went from
~3000 to 78 and stayed there. Nothing in the UI or the logs said a thing. If you
add a scan, give it an origin and use `_replace_review_groups`; if it only covers
part of a set, union instead.

Group **ids are per-origin** (`sha1("playlist|<release>|<artist>|<album>")` vs
`sha1("<group_type>|<artist_key>|<album_key>|<ids>")`), so the same
owned-but-incomplete album reached two ways has two ids. `_review_group_identity`
— release MBID, else `artist_key|album_key` — is what collapses them; an incoming
group whose identity another origin already holds folds its missing tracks into
that row rather than becoming a second tile. The richer row wins, which is always
the library one (it alone carries `albums`, `canonical_album_id` and `extra`).

**Groups are rows in `library_index.db`, not JSON.** The `review_groups` table
holds one row per group: the payload plus the scalar columns the gap list renders
(keep them in sync with `_GAP_LIST_GROUP_FIELDS`). `INDEX_SCAN_VERSION` is
deliberately not bumped for it, for the same reason the `meta` table records.

- `_find_review_group` hands out the **live** dict and marks it dirty on the way
  out. That single site is what persists all ~54 mutation callers, which get a
  group, change it in place and call `_save_review_state()`. Over-marking costs
  one small row write; under-marking is a change that silently never lands.
- The flusher serializes marked rows **under `_review_lock`** and writes them
  under `_index_lock` — never the other way round, and never serializing outside
  the lock, or a concurrent mutation can raise mid-dump.
- A group dropped from the review must be marked too; the flusher deletes any
  marked id that is no longer live.

`missing_album_review.json` keeps only the small, per-session remainder — status,
tasks, searches, operations, duplicate-file results. Migration is one-way and
self-describing: a file with a `groups` key is pre-migration, gets imported, and
is rewritten without it, so there is no marker to get out of sync and a review the
user has since emptied is not refilled on the next restart.

On the live 3075-group review this took `missing_album_review.json` from
**28.8 MB to 176 KB**, and one album's mutation from a ~490 ms whole-blob dump
plus a 28.8 MB fsync, every two seconds, to a **2.1 ms** row write.

**Don't put a big nested payload on a task row.** `_tasks_snapshot`'s docstring
has always said task rows are flat scalars; `result` broke that. One finished
artist-discography task carried a 12 MB scan payload, 109 of them came to 17.6 MB
of the review file, and `/api/tasks` served all of it — past the hub's 4 MB
`PROXY_MAX_RESPONSE`, which answers 502 `tooLarge`. `result` is now stripped from
the on-disk state and from the collection-wide snapshot; only `/api/tasks/<id>`
serves it, deep-copied, because the SPA's Artist panel polls that one task for it.

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
