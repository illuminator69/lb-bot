# lb-bot

A self-hosted **music-library gap-filler**. It scans a Navidrome library for
missing tracks and incomplete albums, acquires the missing audio from Soulseek
(via slskd), and places it back into the library — correctly tagged, no retagging
of what's already there. It runs as a single long-lived Python process exposing
**both a Telegram bot and a React web UI**.

> **For agents / new contributors:** this README is the fast orientation.
> `CLAUDE.md` holds the deep, opinionated design notes (placement internals,
> beets removal, runtime chown quirks). Read this first, then `CLAUDE.md` before
> touching the gap-fill / repair pipeline.

---

## What it does (scope)

The core loop, in the confirmed spec order:

1. **Scan** the library for missing tracks (from ListenBrainz playlists, Spotify
   playlists, or MusicBrainz discography checks).
2. **Produce a review** — a JSON of missing tracks/albums the user can inspect.
3. **Acquire** — the user (or an auto mode) chooses what to download and from
   which Soulseek source. Source selection is failover-aware because peers
   frequently reject or stall.
4. **Place** — downloaded tracks are moved into the existing album folder,
   tagged from canonical MusicBrainz metadata, and verified present in Navidrome.
   No existing library files are retagged.

Steps 1–2 are stable. Steps 3–4 (robust source selection and deterministic
placement) are the active work area — see **Active work** below and `CLAUDE.md`.

**Format policy:** only `flac` and `opus` are accepted (`FORMAT_PRIORITY`).
Everything else is rejected at search time. Placement and retagging also handle
`mp3` (ID3), since a group can opt into mp3 as a last resort.

---

## Architecture at a glance

Single file, `listenbrainz_bot.py` (~11k lines). One process runs three things
concurrently:

- **Telegram bot** — `python-telegram-bot`, command + inline-button driven.
- **Flask web UI + JSON API** — port `8899`, serving a prebuilt React SPA plus a
  large `/api/*` surface.
- **Background workers** — startup scan, periodic housekeeping sweeps, download
  polling, and durable "repair jobs" that survive restarts.

State is held in module-level dicts and periodically flushed to JSON on disk
(`lb_bot_state.json`), plus a review file (`missing_album_review.json`) and a SQLite
index (`library_index.db`: indexed `artists` and `release_groups`, plus
`file_signatures` — cached audio identity per file, see "Duplicates").
`pending_album_groups` is persisted as JSON, so anything stashed on a group must be
JSON-serializable (lists, not sets).

**Changing the index schema needs two things.** `CREATE TABLE IF NOT EXISTS`
never adds a column to a database that already exists, so a new column needs an
explicit `ALTER` in `_index_db()`, guarded on `PRAGMA table_info` rather than by
swallowing the duplicate-column error — a real failure should still surface.
And if the change means existing rows are *wrong* or *missing* rather than
merely old, bump **`INDEX_SCAN_VERSION`**: `_index_get_artist` compares it and
marks mismatched artists stale, which is what puts "may be out of date" and a
Rescan button in front of the user.

### The React web frontend

Lives in `web/` (Vite + React 19 + Tailwind 4). `npm run build` emits `web/dist`,
which the Docker build copies into the image; Flask serves it from `/`. Without a
build, `/` serves a short "run `npm run build`" page — the JSON API works either
way.

The hash is the single source of truth for navigation: every tab and drill-down
has a route (`#/gaps/<id>`, `#/artist/<id>/<rgid>`, `#/system/<subtab>`,
`#/library/duplicates`, `#/import/<path>[/<groupId>[/<mode>]]`), parsed once in
`App.jsx` (`parseHash` → `SET_ROUTE`). Navigate with `navigate()` for a history
entry, `replaceRoute()` for transient corrections. Panels never parse the hash
themselves.

Shared primitives live in `components/ui.jsx` and are **built once, used
everywhere** — `SourceRow` appears in the Fill-gaps picker, the hero's chosen
source and the Library search results; `TrackList`, `ReleaseTile`, `ArtistTile`,
`Badge`, `Chip`, `StatusChip`, `Pager`, `Toggle`, `EmptyState`, `Skeleton` and
`SortToggle` back the rest. Two of them carry behavior worth knowing about:

- **`useSourceFiles`** is the fetch-once-and-keep "show files" expansion, shared
  by `SourceRow` and the hero card so a peer's folder is never read twice or
  read differently in two places.
- **`Cover`** takes a `fallbackUrl` and walks a chain before giving up: the
  requested art, then the fallback, then a deterministic gradient from the
  album name. That chain is what lets a per-release cover degrade to its
  release-group's sleeve rather than to a coloured rectangle.

All colors come from the generated CSS custom properties in `lib/tokens.js`, so
the appearance menu re-themes every screen live. Don't hardcode hex outside
cover-gradient fallbacks.

---

## External systems it talks to

| System | Role | API | Default URL |
|---|---|---|---|
| **Navidrome** | Music server; source of truth for what's in the library and the **success oracle** for placement | Subsonic API | `http://navidrome:4533` |
| **slskd** | Soulseek client; the acquisition backend | REST, `X-API-Key` auth | `http://slskd:5030` |
| **MusicBrainz** | Canonical release/track/recording metadata (rate-limited; contact email required) | `ws/2` | `musicbrainz.org` |
| **ListenBrainz** | Weekly playlist sources (Weekly Jams, Weekly Exploration); fresh releases; **similar artists** (the `labs.api` dataset, a separate host from the main API) | `api.listenbrainz.org/1`, `labs.api.listenbrainz.org` | — |
| **Last.fm** | Optional cross-check for similar artists (`artist.getsimilar`). Absent `LASTFM_API_KEY`, the ListenBrainz half runs alone | REST | — |
| **Cover Art Archive** | Album art for releases the library doesn't hold, keyed by **release** or release-group | REST | `coverartarchive.org` |
| **Spotify** | Optional public-playlist import (Client Credentials flow) | Web API | — |
| **beets** | Legacy placement path — **being removed** from the hot path (see below) | CLI subprocess | — |

Navidrome derives `album.created_at` from the *oldest* file, so gap-filled albums
never move in the default `newest` sort. Placement stamps placed files with a
current mtime; set **`ND_RECENTLYADDEDBYMODTIME=true` on the Navidrome container**
so AudioMuse's newest-albums scan surfaces filled albums. The bot does **not**
call AudioMuse directly — it only makes filled tracks visible to it.

---

## Runtime & deployment

Docker, defined by `Dockerfile` (two-stage: Node builds the SPA, then a
`python:3.11-slim` runtime) and `docker-compose.yml`.

A prebuilt image is published to **GHCR** on every push to `main`
(`.github/workflows/docker.yml`), and `docker-compose.yml` pulls from there:

```bash
docker compose pull && docker compose up -d    # update to the latest build
```

To build locally instead, comment the `image:` line in the compose file and
uncomment `build: .`.

**Mounts (host → container):**

- `/mnt/user/appdata/lb-bot` → `/config` — state, index DB, review file
- `/mnt/user/appdata/slskd/downloads` → `/downloads` (`SLSKD_DOWNLOAD_DIR`)
- `/mnt/user/Music` → `/music` (`LB_BOT_MUSIC_DIR`, the library root)

The container runs as `user: "99:100"` (Unraid `nobody:users`) so placed tracks
aren't root-owned. **All three mounts must be writable by that uid** — including
`/downloads`, since placement unlinks the source file after copying. `LB_BOT_UMASK`
(default `002`) is applied by the bot itself via `os.umask()` at import.

> **Gotcha:** files left behind by an earlier root run (the SQLite index,
> `lb_bot_state.json`, album folders) stay root-owned and cause "attempt to write
> a readonly database" errors. One-time host fix:
> `chown -R 99:100 /mnt/user/appdata/lb-bot` (and `/mnt/user/Music` if placement
> into pre-existing folders fails). See `CLAUDE.md`.

Network: `media` (external Docker network).

### Local development

```bash
pip install -r requirements.txt          # python-telegram-bot, httpx, requests, Flask, mutagen, rapidfuzz
cd web && npm ci && npm run build          # build the SPA (or `npm run dev` for HMR)
python listenbrainz_bot.py                 # starts bot + web UI on :8899
```

`web/stub_server.py` exists for frontend work without the full backend.

---

## Configuration (environment variables)

Credentials must be supplied via env (compose reads them from the host env). Do
not hardcode.

**Credentials / connections**

- `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`
- `NAVIDROME_URL`, `NAVIDROME_USER`, `NAVIDROME_PASSWORD`
- `SLSKD_URL`, `SLSKD_API_KEY` (from slskd UI → Options → API Keys)
- `LISTENBRAINZ_USER`
- `MBZ_CONTACT` — contact email required by the MusicBrainz API ToS
- `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` — optional, for Spotify playlists
- `LASTFM_API_KEY` — optional. Cross-checks the ListenBrainz similar-artists
  lookup that backs the album page's "Similar albums" shelf. Without it the
  ListenBrainz half runs alone; the shelf still works

**Paths & files**

- `LB_BOT_MUSIC_DIR` (`/music`), `SLSKD_DOWNLOAD_DIR` (`/downloads`)
- `LB_BOT_STATE` (`lb_bot_state.json`), `LB_BOT_REVIEW_FILE`
  (`missing_album_review.json`), `LB_BOT_LIBRARY_INDEX` (`library_index.db`)
- `LB_BOT_INDEX_TTL_DAYS` (default `30`), `LB_BOT_UMASK` (`002`)

**Performance tuning** (defaults are fine; see "Keeping the polled path cheap")

- `SLSKD_SEARCH_TIMEOUT` (`75`), `SLSKD_SEARCH_POLL_INT` (`1`) — safety cap and
  poll interval for an slskd search. The cap must outlast slskd's own straggler
  timeout; results are unreadable until the search completes
- `SLSKD_SEARCH_MIN_RESPONSES` (`5`), `SLSKD_SEARCH_MIN_WAIT` (`5`) — when to ask
  slskd to wrap the search up. Raise for better recall, lower for speed
- `SLSKD_SEARCH_SETTLE` (`25`) — how long to wait for slskd to publish responses
  it has already counted, when the early exit outran it
- `SOURCE_RESULTS_TTL` (`180`) — how long a group's source list is reused before
  a re-search
- `LB_BOT_TRASH_DIR` (`/music/.lb-bot-trash`) — where deleted duplicates go
  instead of being unlinked; must be on the library's filesystem for the move to
  stay a rename, and hidden from Navidrome

**Web UI**

- `LB_BOT_WEB` (enable, default on), `LB_BOT_WEB_HOST` (`0.0.0.0`),
  `LB_BOT_WEB_PORT` (`8899`)

**Behavior toggles**

- `LB_BOT_REPAIR_JOBS` (durable per-track repair jobs, default on)
- `FUZZY_DUPLICATES_DEFAULT`

**Multi-user:** the `USERS` list (near the top of the file) holds one entry per
user — Telegram token/chat, Navidrome creds, ListenBrainz user, and which
playlists to pull. The first entry is env-driven; add more inline.

---

## Telegram commands

| Command | Action |
|---|---|
| `/start`, `/help` | Help text |
| `/scan` | Scan configured playlists for missing tracks |
| `/status` | Current run / pending status |
| `/pending` | Show pending approvals & retries |
| `/diag` | Diagnostics |
| `/album <query>` | Look up and download a specific album |
| `/search <query>` | Manual slskd search with per-file / per-album download buttons |
| `/checkalbums` | Find incomplete albums in the library via MusicBrainz |
| `/spplaylist <url>` | Import a Spotify playlist |
| `/beets` | (legacy) beets folder picker |
| `/rescan` | Trigger a library rescan |

Runs a scan automatically on boot (throttled) and on a weekly schedule.

---

## Web API surface (`/api/*`)

Served by Flask (`app` built around line 9415). Highlights — this is a large,
evolving surface; navigate the source by route string:

- **Overview:** `/api/summary`, `/api/status`, `/api/system/health`,
  `/api/action-center`, `/api/diagnostics`, `/api/logs`, `/api/settings`,
  `/api/prefs` (GET/PUT)
- **Gaps & review:** `/api/gaps`, `/api/gaps/<group_id>` (+ `/fetch`, `/auto`,
  `/cancel`, `/allow-mp3`, `/duplicate-files`, `/rescan` — a *scoped* re-check of
  one album: re-reads it from Navidrome and walks its folder for files Navidrome
  hasn't indexed, returning the updated `{present, total, status, foundOnDisk}`
  so the UI reconciles without a full library scan), `/api/review`,
  `/api/duplicates`, `/api/duplicate-files`, `/api/library/delete-file`,
  `/api/library/trash` (+ `/restore`, `/empty`)
- **Scanning:** `/api/scan`, `/api/scan-all`, `/api/playlists/scan`,
  `/api/spotify/scan`, `/api/rescan`
- **Search & download:** `/api/search`, `/api/searches`,
  `/api/searches/<sid>/download`, `/api/downloads`, `/api/downloads/cancel`,
  `/api/transfers`
- **Albums & artists:** `/api/album/lookup|releases|sources|tracklist|download|status|similar`
  (`sources` pairs each peer folder against the canonical MusicBrainz tracklist —
  see "Source coverage" below),
  `/api/artists`, `/api/artist/lookup|discography`, `/api/fresh-releases`,
  `/api/cover/<album_id>`

  `releases` returns two axes, not one flat list: a **variant** (Original /
  Remaster / Deluxe) changes the tracklist, an **edition** (Digital / CD /
  Vinyl) is the same tracklist pressed differently and carries its *own* cover
  art. The release-group's Cover Art Archive image is whichever release the
  Archive picked, which is how a vinyl-tagged copy shows a photo of the disc
  where the sleeve belongs; each variant defaults to its digital edition, the
  likeliest to have a correct front. `tracklist` takes an optional `album_ids`
  or `group_id` and then marks **each track** `present` — the album page used
  to receive only `{present, total}` and had to guess which tracks were
  missing, which is wrong whenever the gaps aren't the trailing tracks.
  `similar` rolls the ListenBrainz (+ optional Last.fm) similar-artists lookup
  up to one owned album per similar artist, each attributed to the artist being
  viewed.

  **`download` is idempotent per release** and **`status` is how you watch it** —
  see "Watching a fill from outside" below.
- **Placement:** `/api/placements`, `/api/placements/identify`,
  `/api/placements/<id>/confirm|dismiss|delete`
- **Groups (source & match management):** `/api/groups/<group_id>/...` —
  `sources`, `sources/<idx>/files` (expand the peer's real folder),
  `tracks/<idx>/pick-file`, `tracks/<idx>/place-anyway`, `reconcile-downloads`,
  `match`, `canonical`, `retag`, `decisions`, `download`, etc. "Merge duplicates" in the UI posts to **`retag`**: it writes the
  canonical album/albumartist/MB ids onto the other copies and lets Navidrome
  collapse them on its next scan. A failed tag write now fails the merge instead of
  reporting "tagged 0/12 file(s)" as a success, and `preview_group_retag`'s blocked
  reasons reach the UI.
- **Repair jobs:** `/api/imports/<recid>/retry|merge-retry|remove-stale-and-retry`
- **Library index:** `/api/library`, `/api/library-index/build|status`
- **Legacy beets:** `/api/beets/folders|release-candidates|import`
- **Diagnostics:** `/api/debug/slskd-search?q=<query>&wait=<s>&stopAt=<s>` — traces
  one slskd search end to end (every status poll, plus periodic probes of
  `/responses` and `?includeResponses=true` with codes and lengths) and returns
  slskd's effective search options. Read-only apart from creating and deleting
  one search, and it uses the bot's own API key. Reach for this before theorising
  about why a search returned nothing — see "Keeping the polled path cheap"

`/api/summary`, `/api/settings`, `/api/diagnostics` and `/api/action-center` are
polled every few seconds, so their expensive parts are cached: slskd transfers
(~3s), the four reachability probes (60s, and run concurrently), and the download
folder listing (30s). Pass `?fresh=1` to bypass a cache for an explicit re-check.

### Keeping the polled path cheap

The SPA polls `/api/summary` and `/api/gaps` every 2s, so anything on that path
is on a hot loop. Two rules follow, and breaking either was worth tens of
seconds of frozen UI:

- **Never hold `_review_lock` across network I/O.** It is process-wide and every
  poll wants it. Read what you need under it, do the slow part outside, then
  re-find the group and merge — `api_gap_detail` and `_run_group_source_search`
  are the reference shape. `_group_generation` gives you the token to detect a
  rescan landing mid-flight; discard rather than merge when it changes.
- **Don't deep-copy the whole review state to render a list.** `_review_snapshot`
  copies everything (including every peer's file listing); `_review_list_snapshot`
  copies only the fields `_album_view` and `_review_group_next_action` read. Add
  to `_GAP_LIST_GROUP_FIELDS` when those functions start reading something new.

The raw slskd payload (`source_results.folders[].files`, `_expanded`, `_claimed`)
is deliberately **not persisted** — `_review_state_for_disk` strips it, and
`_load_review_state` drops what's left on restart, so an album asks for a fresh
search rather than showing sources that can't be enqueued. Results younger than
`SOURCE_RESULTS_TTL` (180s) are reused instead of re-searching; "Search again"
posts `{force: true}` to bypass that.

**slskd publishes search results only on completion — there is no partial
read.** This is the single most important thing to know about this path, and
assuming otherwise cost several rounds of wrong fixes. Traced against the live
instance with `GET /api/debug/slskd-search?q=…` (`slskd_search_probe`):

| t | state | responseCount | `/responses` | `?includeResponses=true` |
|---|---|---|---|---|
| 5.1s | InProgress | 138 | 0 | 0 |
| 25.8s | InProgress | 143 | 0 | 0 |
| 35.4s | InProgress | 143 | 0 | 0 |
| 39.4s | Completed, TimedOut | 143 | **142** | **142** |

`responseCount` on the search status ticks up live — that is what the progress
line counts, and why a failing search could honestly report "143 peers" and then
surface nothing. So:

- The poll **must** run until `isComplete` / state `Completed`. `SLSKD_SEARCH_TIMEOUT`
  (75s) is a safety cap, not the intended exit, and must outlast slskd's own
  straggler timeout (~40s observed). At 30s it cut the poll off just before the
  results appeared.
- What the early exit can still buy is ending the search *sooner*: once
  `SLSKD_SEARCH_MIN_RESPONSES` peers have arrived (or the file count has
  plateaued) past `SLSKD_SEARCH_MIN_WAIT`, `PUT /searches/{id}` asks slskd to
  wrap up, and it publishes on completion as usual. Best effort — if the stop
  fails the poll just runs to the cap.
- `SLSKD_SEARCH_SETTLE` (25s) remains a backstop: if the array is still empty
  after all that, wait for completion and ask once more, including via
  `?includeResponses=true`. It fires only on an *empty* array — a short one is a
  normal ranking outcome and must not cost anyone the wait.

Use the probe endpoint before theorising about this path again.

All outbound HTTP goes through `_http`, which keeps one pooled `requests.Session`
per host. Use it rather than `requests.get`/`post` directly.

### Watching a fill from outside

The navi-connect hub proxies a whitelisted slice of this API to the Feishin and
Navic clients (`<hub>/lb/*`), which is what put two requirements on the album
download path that the bot's own SPA never needed.

**`POST /api/album/download` returns a task id that is not the answer to "is it
downloaded".** `_album_download_task` calls `_task_finish` as soon as slskd
accepts the enqueue — "Queued 9/12 file(s)" — with the transfer, the placement and
the Navidrome scan all still ahead. Polling `/api/tasks/<id>` therefore reports
success roughly a minute before anything reaches the library. That endpoint is
also expensive: like `/api/tasks`, it goes through `_review_snapshot()`, a deep
copy of the whole review state under `_review_lock`.

`GET /api/album/status?release_mbid=…` (or `?rgid=`, which costs a MusicBrainz
resolve, so prefer the release) answers properly:

```
unknown → searching → queued → downloading → placing → placed → verified
                                       ↘ needs_match   ↘ failed
```

`{state, done, total, failed, percent, reason, mp3WouldHelp, groupId, artist, album}`.
It reads the in-memory ledger `_album_fill_status` (written by the download task,
`_finalize_group` and the verifier) plus live counts off `pending_album_groups`
— **no `_review_snapshot`, no network I/O under `_review_lock`**. Progress is read
at call time rather than written by the download poller, which runs every couple
of seconds per file and must stay free of bookkeeping nobody may be watching.

`placed` means the files are in the album folder; **`verified` means Navidrome has
indexed them**. A review-group fill gets that from `_verify_placement_worker`; an
album fetched from an artist page has no review group, so
`_start_album_fill_verification` polls the placement's own `per_file` rows with one
`nd_track_present` search each (a full `_nd_album_index(force=True)` on a 30 s loop
was the alternative, and a library page-through is not one).

The ledger is in memory on purpose — an in-flight download does not survive a
restart either, and a caller that finds nothing reads `unknown`.

**`POST /api/album/download` returns the fill already in flight** rather than
starting a second, checking `pending_album_groups` and the running
`album-download` tasks for the same release first (`{ok: true, existing: true}`).
Two clients and the SPA can all be looking at one album; a second tap used to mean
a second search, a second set of transfers and a second placement pass over one
folder.

**Failure reasons reach that path too.** `no_source_reason` / `mp3_would_help` are
set on *review groups* by `_apply_group_sources`, and an artist-page download
creates no group — so `_album_download_task` fills the same `stats` dict and runs
it through `_no_source_reason`. The Allow-MP3 opt-in still needs a group id, so
clients only offer it when `status` carries one.

**A placed album leaves the index saying `missing`,** so `_finalize_group` flips
the row itself: `_index_mark_release_present(rgid=…, group_id=…)` sets
`release_groups.status = 'present'` for the release-group (matched by rgid for an
artist-page download, by review group id for a Fill-gaps completion). The rgid
rides down from `api_album_download` through `_album_download_task` into the fill
ledger purely so this call has it. Without the flip a filled album shows up twice
in every client — once in the library, once in "not in your library" — until the
artist is rescanned against MusicBrainz, which is a request per second and far too
expensive to trigger per download. `present`/`total` and the Navidrome album ids
are left alone; the next full scan fills them in.

**Per-album quality.** `POST /api/album/download` takes an optional `quality`
(one of `QUALITY_PREFERENCES`) that overrides the global Source preference for
that fill alone. The global setting is the wrong granularity in practice — a
record is worth 24/96 and a b-side single is not — and it lives three screens
away from where the decision is made. It is scoped with a ContextVar
(`_quality_preference`) for the same reason as the MP3 opt-in: the preference is
read six levels down, in `_file_score` and the folder ranking, both now going
through `_effective_quality()`. The album group also carries `quality` verbatim,
because the failover path runs on another thread long after that scope has
exited. An unknown value is a `400`: silently fetching hi-res after being asked
not to is worse than refusing.

**A group whose transfers all vanished is finalized, not left hanging.** The
download poller's sweep used to fire on `ALBUM_GROUP_TTL` alone — six hours — and
only when a Telegram app was registered for the group's token. Neither held for a
web- or client-started fill: the commoner failure is slskd forgetting every file
of a group whose counters never reached `total` (a peer that disappeared
mid-album, a row cleared by hand), which can make no further progress by
definition, and there may be no bot to narrate it to. So the sweep also finalizes
a group with nothing left in `pending_downloads` after
`ALBUM_GROUP_ORPHAN_GRACE`, skipping groups mid-failover, and `_tg_send` no-ops on
a falsy bot so placement never hinges on there being a Telegram chat. Left alone,
such a group strands its files in `/downloads`, keeps the album on the Downloads
tab and never resolves the fill a client is watching.

**Optional hub ping.** With `LB_BOT_HUB_URL` + `LB_BOT_HUB_TOKEN` set,
`_notify_hub_library_change` POSTs `<hub>/lb/notify` on placement so the
navi-connect clients refresh a page they already have open. Fire-and-forget on its
own thread: the placement path is what the user is waiting on, and the hub is
optional infrastructure that may be down. Nothing reads the response, and an unset
URL simply means no ping.

### Background work must be visible and must not strand an album

A source search is a background task: the POST returns a `task_id` immediately
and the work continues while the user looks at a different album. Two things
follow, and both were once broken in the same failure:

- **Stamp `group_id` on the task** (`_task_run(..., group_id=...)`). It is what
  lets `_gap_detail_view` attach `sourceTask` and `_gaps_view` mark the rail row
  `searching` — without it, an in-flight search and a search that failed look
  identical to a screen showing nothing. Long-running network steps report
  through a `progress(text)` callback (`slskd_run_search`) into
  `task["current"]`; there is no total to count towards, so elapsed time and the
  peer count are the only honest progress.
- **An empty result must say why.** `slskd_run_search` fills a caller-supplied
  `stats` dict (peers, files, folders before scoring, rejected formats) and
  `_no_source_reason` turns it into a sentence. "No source found on slskd" right
  after a progress line reading "103 peers, 2,047 files" reads as a bug in the
  bot; usually it means every file was in a rejected format, which is also what
  decides whether the per-album MP3 opt-in is worth offering (`mp3_would_help`).
- **Treat every slskd collection as nullable.** A response carries
  `"files": null` when the peer's only hits were locked, and `for f in
  peer.get("files", [])` iterates `None` for those — one such peer raised out of
  the loop and discarded all 139 others' results, swallowed into a bare "no
  sources". Read `peer.get("files") or []`, and the same for `lockedFiles`.
- **Never compare slskd's peer-wide `lockedFileCount` to a folder's file count.**
  A search response's `files` are the *unlocked* hits; locked ones arrive in
  `lockedFiles`. `_score_folder` uses `locked_in_folder`, counted per folder from
  that array. The peer-wide count is display-only.
- **Keep `_RETRYABLE_DECISIONS` complete.** A finished search flips every
  approved track to `source_pending`. Any decision that means "still missing,
  nobody working on it" must be in that tuple, or `_approve_pending_missing_tracks`
  won't re-approve it and `_group_source_plan` dies on "No approved tracks" —
  which strands the album on "choosing a source" with no sources to choose,
  permanently, because results are dropped on restart (above) but the decisions
  are not.

---

## How placement works (the important part)

For each missing track, the gap-detection step already resolves the
`recording_mbid`, `position`, canonical `mb_albumid`, and full
`canonical_tracklist`. Placement is therefore **deterministic** and needs no beets:

1. **Find the target album folder** — primary path pulls the on-disk `path` of an
   existing track in that album from Navidrome (`getAlbum` → child `path`) and
   takes its parent dir; fallback is the cached filesystem index
   (`mb_albumid → folder`). Split/duplicate albums pick the folder holding the
   most tracks of that release.
2. **Match each slot to a file** with the *same* ranked matcher acquisition uses
   (`_best_file_for_missing_track` + the sibling tighter-claim discard), over only
   the slots this fill is about (`group_id` → `_group_missing_slot_keys`).
3. **Guard** (`_reject_reason`) — refuse a file whose audio is already in the album,
   or whose own tags say it is a different track of this release. See below.
4. **Tag** the downloaded file with mutagen from the canonical dict built by
   `_repair_track_metadata`. FLAC/Opus are Vorbis comments; mp3 goes through ID3.
5. **Move** with `shutil.move` into the target folder (filename is cosmetic;
   Navidrome organizes by tags).
6. **Verify** — trigger a Navidrome rescan and confirm via the
   `navidrome_pending → navidrome_verified` loop. Navidrome is the success oracle.
   The same pass then re-reads the group's albums
   (`refresh_group_albums_from_navidrome`) so present/total/missing update without
   a full library scan.

A fill can span **several download directories** — a file that fails over to another
peer lands in that peer's own folder — so `_deterministic_album_import` accepts a
list of dirs and `_finalize_group` passes every directory that received a file.

### The placement guard

Placement used to run its own weaker substring matcher and then rewrite the file's
`title` and `mb_trackid` to whatever slot it had guessed. That is how a fill could
leave an 11-track album with two copies of one song and one track still missing:
slot "Sing" claimed `05 - Singularity.flac`, the tags were forged to match, the real
gap stayed open — and because both duplicate-file keys are read from tags, no
duplicate scan could see the pair it had just created.

`_place` now refuses — leaving the file in `/downloads` and marking the track
`failed` with a named reason, recoverable via the existing "Pick a source manually"
— when either holds:

- **the audio is already in the album**: `_audio_signature` compared with
  `_same_audio_exact` — FLAC's `md5_signature` (an MD5 of the *unencoded* audio,
  free from the stream header) or an identical sample count at the same rate.
  Deliberately strict, because this gates a refusal to place; the looser
  duration+size comparison belongs only in the duplicate *report*.
- **the file's own tags contradict the slot**: a disagreeing recording MBID, or an
  own-title that is exactly another track of this release.

A file with no usable tags and no md5 — the common Soulseek case — has nothing to
contradict and places exactly as before. The guard must never turn a working fill
into a no-op.

### Finding the right album in the first place

Two naive queries used to be the whole strategy — `"{artist} {album}"`, then
bare `"{album}"` — and they fail predictably. MusicBrainz canonical titles carry
edition packaging no peer folder has, and a **self-titled album collapses into
an artist-wide query**: "Led Zeppelin Led Zeppelin" asks for the discography.

`_album_search_queries` emits up to `MAX_SEARCH_PASSES` (3) ordered variants:
the cleaned title with repeated runs collapsed (`_clean_album_title`,
`_dedupe_terms`) plus the **year** when self-titled; a punctuation-free or
distinctive-words form; and a last resort that, for a self-titled album, asks
for its most distinctive *track* instead. `slskd_search_album_folders` walks
them, **merges** folders across passes keyed `(username, folder)` keeping the
better-scored copy, and stops early once two folders actually resembling the
album have turned up.

**Ranking knows which album it asked for.** `_annotate_folder_match` scores each
folder's own path against the album and artist (`_folder_name_score`, over the
last one or two components), and `_score_folder` pays up to **+3000** for it —
deliberately the largest single term, because everything above it is a property
of the *peer*. Without this, an ambiguous query returned the whole discography
and sorted it by upload speed, so "3 pages of other albums" outranked the one
being looked for.

`_folder_name_score` is not `_fuzzy_score`: `partial_ratio` scores any
superstring at 100, so "Led Zeppelin" matched the folder
`Led Zeppelin/Physical Graffiti` perfectly and every album tied. Token-sort is
the base because it pays for extra words; the partial arm returns only when the
candidate adds at most one token — room for a year, not for another album's
title. The candidate gets the same `_clean_album_title` + `_dedupe_terms`
treatment the query does, so `Artist - Album [1969 FLAC]` still matches.

**Preferred quality** (`System → Source preferences`, `_effective_prefs()["quality"]`)
picks between two copies of the same album: `flac-any` (default), `flac-16-44`,
`highest-bitrate`, `prefer-opus`. Scored up to +600 at folder level and mirrored
in `_score_file`. It never rejects a source — a gap is still filled when only
one copy exists. This replaces an implicit, unchangeable "higher bitrate always
wins" that was wrong for anyone wanting a standard CD rip.

**Source selection** ranks Soulseek folders (album-name match, track-count
match, availability ratio, upload speed, queue length, free slots, collection
size, preferred quality) with automatic
per-file failover: a file that fails while the rest of the album is progressing is
re-enqueued from the next candidate source (`_retry_file_from_alt_source`, capped
at `MAX_FILE_RETRIES`) instead of finalizing the album short. Claims on a peer's
files are tracked per (peer, folder) and persist across retries, so two tracks
failing over to the same peer can't both be paired to the same file.

**No track is left behind on an album fetch.** A track the chosen folder has no
file for used to fail on the spot, even though the search had found other
sources that might have it — the per-track path had always failed over, the
album path never did, and that is a large share of the fills that finish two or
three tracks short. `_rescue_leftover_tracks` runs the same walk over the
alternate sources (capped at 6, each expanded once and cached, one claim ledger
per source) before anything is marked failed, and the track records the peer it
was actually rescued from rather than the one originally chosen.

> **Known cost:** `_enqueue_group_source` runs under `_review_lock`, and the
> rescue pass does its `slskd_expand_directory` calls there — as the per-track
> branch already did. It is bounded (≤6 expansions per fetch, cached across
> leftovers), but it is network I/O under the process-wide lock, which the
> polled path pays for. Worth moving out with the fetch endpoint next time that
> code is opened.

### Source coverage — "is this the right album?"

`_source_coverage_summary` pairs what we want against the peer's file list with the
same ranked matcher acquisition uses. It takes either a review group (Fill gaps →
the missing tracks) or an explicit `tracks` list (the artist/album page → the
canonical MusicBrainz tracklist). Before, the album path passed no tracks at all and
coverage was `min(fileCount, total)/total`, so any folder with enough files in it
read as a complete match however unrelated its contents.

`_source_files_view` then puts the pairing on the wire: one row per file with the
slot it would fill (or none), the full peer filename, plus the tracklist slots
nothing covers. `SourceRow` renders it behind a "Show files" disclosure in every
picker, so a source whose files match no slot is visibly wrong before anything is
downloaded. The Fill-gaps hero's **chosen-source card carries the same
disclosure** — that is the source you are about to commit to, so it is the one
place the check matters most. Both go through the `useSourceFiles` hook, so
there is one fetch-once-and-keep expansion, not two that can drift.

**Opening that disclosure expands the peer's real folder.** `fd["files"]` holds only
the peer's *search hits*; the actual directory listing was fetched at download time
and thrown away, so a file that exists in the source but didn't match the search
query was invisible to the UI *and* to the matcher — which is why a source that
really does have a track reported it missing. `GET
/api/groups/<gid>/sources/<idx>/files` calls `slskd_expand_directory` once, caches it
on `fd["_expanded"]` (the field the failover path already used), and recomputes
coverage, `matchedTo` and the risk flags over the full listing. The UI labels which
of the two it is showing.

**A track can be matched by hand.** `POST /api/groups/<gid>/tracks/<idx>/pick-file
{sourceIndex, filename}` enqueues one specific peer file for one specific missing
track through the existing per-track path (`slskd_enqueue(track=,
review_group_id=, review_track_index=)`) — only the *file choice* was ever
automatic. The binding is recorded as `manual_pick`, and `_group_manual_pairs` turns
it into `manual_pairs` (slot key → local path), which `_match_slot` consults **first**
— otherwise placement would re-match and quietly undo the choice — and which also
reserves that file against any other slot claiming it. A manual pairing skips the
"the file's tags say it is a different track" refusal, since that is precisely the
judgement being overridden, but **keeps** the "this audio is already in the album"
one: that is what stops a duplicate being created. When it fires, the track reports
`canForcePlace` and the name of the file it duplicates, and `POST
/api/groups/<gid>/tracks/<idx>/place-anyway` re-runs placement for that one file with
the audio check disabled, behind a confirm that names the existing file.

**Matching a missing track to a file in a source folder** is ranked, not
substring. `_file_title_key` strips the track number and extension from the
filename and `_title_match_rank` grades the hit, tightest first:

| tier | basis | evidence |
|---|---|---|
| 0 | `exact` | the keys are identical |
| 1 | `prefix` | the filename starts with the title |
| 2 | `contained` | either contains the other — **both directions**, since a MusicBrainz title is often *longer* than the filename ("Song (feat. X)" vs `04 - Song.flac`) |
| 3 | `fuzzy` | rapidfuzz `token_sort_ratio ≥ 87`, or `partial_ratio ≥ 92` **with whole-token alignment** |
| 4 | `duration` | no title matched; the file's length fits this track and nothing else, in both directions |
| — | `position` | no title matched anywhere, but the folder *is* the whole album, so the track number is used |

`_best_file_match` returns the basis alongside the file, so the UI can chip the
weak tiers and `_match_slot` can report "medium" confidence honestly instead of
presenting a duration guess and a recording-MBID hit as the same thing.
`_best_file_for_missing_track` is the thin wrapper for callers that only want
the file.

Two rules keep the looser tiers from doing damage, and **neither is optional**:

- **A loose hit is discarded when another title on the release claims the same
  file at least as tightly** (`_sibling_claims_tighter`, fed by
  `_release_track_titles`). Without it, missing "Sing" matched
  `05 - Singularity.flac` and downloaded a song already in the library while the
  real gap stayed open. This now applies to the fuzzy tier too — it is precisely
  what lets that tier be loose without being dangerous.
- **`partial_ratio` scores any substring at 100**, so "sing" inside
  "singularity" would sail through the fuzzy tier on score alone. `_tokens_aligned`
  requires the shorter string's tokens to appear as *whole* tokens in the longer,
  which keeps "Song" ↔ "Song (feat. X)" and rejects the other.

The `position` fallback is guarded hardest, because a blind positional zip is
exactly how files get filed as the wrong song: the folder must hold as many
audio files as the release has tracks **and** either match the album name
(`album_match_ok`) or have already matched a majority of the release's titles,
and each number must resolve to exactly one unclaimed file. It exists for
localized or transliterated filenames, where the folder is plainly the album but
no filename resembles a MusicBrainz title.

When nothing matches, the **nearest rejected candidate and its score** are
recorded on the track (`closest: '05 - Singularity.flac' (100%)`). A bare "no
matching file" reads as "the source was empty" when the truth is that the one
candidate belongs to another track.

The placement guard (`_reject_reason`) is deliberately untouched by all of this:
it is the safety net *behind* the looser matcher, not part of it.

The stall watchdog distinguishes *stalled* from *waiting*. A transfer that has
actually been running gets `STALL_TIMEOUT` (120s) of no byte progress before it
is cancelled; one still sitting in the peer's remote queue ("Queued, Remotely")
gets `QUEUE_TIMEOUT` (900s), because Soulseek peers serve one or two files at a
time and the tail of an album legitimately sits at 0 bytes for minutes. Killing
those was the main reason a fill finished two or three tracks short.

**Format policy** is per album at the edges: `FORMAT_PRIORITY` stays flac/opus,
but a review group can set `allow_mp3` (Fill gaps → "Allow MP3 as a last resort",
`POST /api/gaps/<id>/allow-mp3`), which widens the accepted formats for that
album's searches and enqueues only, with mp3 ranked last. Placement always
accepts mp3 — refusing to file a track we deliberately downloaded would strand it
in `/downloads`.

**Verification is real.** After placement, a background pass polls Navidrome for
each placed track; confirmed tracks become `verified`, and anything that never
appears within `PLACEMENT_VERIFY_TIMEOUT` is reset to `pending` so the next fill
retries it. A rescan that still reports a track missing also clears any inherited
`placed`/`queued` decision — the library, not the bookkeeping, is the oracle.

### Duplicates

Library → Duplicates covers two different things.

**Duplicate albums** are bucketed by normalized artist + title, then optionally
by fuzzy title similarity ("fuzzy matching"), then structurally. The structural
pass exists because a Japanese release and its English-titled copy share no text
at all, so no amount of fuzzy title matching can ever pair them: candidates are
albums with the same track count whose total runtimes agree within a tolerance
that scales with the track count (`DUPLICATE_DURATION_TOLERANCE` floor,
`DUPLICATE_TRACK_TOLERANCE` per track — Navidrome's album duration is a sum of
whole seconds, so rounding alone drifts with length). A candidate is confirmed by,
in order: the artist tag agreeing; both albums resolving to the same MusicBrainz
release-group; or — with "match across languages" (`{"deep": true}` on
`POST /api/scan`) — their **per-track runtimes agreeing track for track**
(`_album_duration_fingerprint`). That last one needs no MBID on either side, which
is the point: the localized rip this feature exists for is usually untagged, and
requiring a release-group from *both* copies meant the deep pass could only ever
confirm pairs the title passes had already found. The whole scan therefore
considers untagged albums too, and an MBID is required only at the release-group
lookup itself.

**Duplicate files** are two files *inside one album* that are the same song —
typically the residue of a fill that matched the wrong file. Files come from
Navidrome **and** from a walk of the resolved album folder, so a file placed a
minute ago and not yet indexed still counts (`_album_tracks_with_disk`; such rows
are flagged `onlyOnDisk`). They are unioned on two kinds of evidence, reported as
`matchBasis` so a delete stays an informed choice:

| basis | evidence |
|---|---|
| `audio` | identical FLAC stream md5, or sample count at the same rate — proof |
| `tags` | normalized title or recording MBID agrees |

There used to be a third, `stream` (lossy files with no md5: same rounded duration,
rate and channels, sizes within 2%). Rate and channels are constant across a rip, so
it degenerated to *duration alone* and the size gate is a no-op for CBR — two
different songs of equal length grouped, and union-find chained them into sets of
three and more. A guess has no business feeding a delete button; it is gone.

`audio` is what catches a bad fill at all: placement rewrites the mis-slotted file's
title *and* MBID, so the two copies share neither tag key. Signatures are cached in
`library_index.db` (`file_signatures`, keyed on path + size + mtime), so a rescan
costs one `stat` per file rather than a header read.

**A file is one file.** Identity is `(st_dev, st_ino)` (`_file_identity`), never the
path string: `os.path.normpath` does not case-fold, resolve symlinks or
unicode-normalize, while `_song_abs_path` forces NFC on Navidrome's path and
`os.walk` returns the filesystem's spelling. Comparing strings therefore listed one
file as two rows carrying the same stream md5 — grouped as `audio`, the strongest
evidence there is, with the disk-side alias always ranked second and so always the
deletable row. On a real library that flagged nearly every song and deleting the
"duplicates" destroyed good files. Identity is used for the Navidrome/disk merge, the
per-set dedupe, the cross-set `claimed` ledger (one file appears in at most one set,
library-wide), the delete endpoint's prune, and the SPA's row keys. The disk walk is
also non-recursive and skipped for the library root or a directory holding
implausibly more audio than the album has tracks — counted in the scan stats.

Sets are ranked best-copy-first by format → bitrate → size and deleted one at a time
through `POST /api/library/delete-file`, which requires `{"confirm": true}` and
refuses any path that does not resolve strictly inside `LB_BOT_MUSIC_DIR`. Two
further guards: it **refuses the last copy** (`409 last_copy`) unless some other
file that still exists is genuinely the same song — checked against the stored set,
or failing that against the file's own folder with the same audio/tag rules — and it
**moves to trash rather than unlinking**. Trash is `LB_BOT_TRASH_DIR`, default
`/music/.lb-bot-trash`, laid out as `<YYYY-MM-DD>/<path relative to the library>`
with a `manifest.json` beside the files; same filesystem, so it is a rename, and
dot-prefixed so Navidrome skips it. `GET /api/library/trash`,
`POST /api/library/trash/restore {path}` (refuses an occupied original path) and
`POST /api/library/trash/empty {confirm, older_than_days}` complete it. In the UI the
row the set is keeping has **no Delete button** — other rows offer "keep this one
instead", which re-points the recommendation — so emptying a set takes a deliberate
change of mind.

The library-wide list comes from the duplicate scan's second phase; `GET
/api/gaps/<id>/duplicate-files` answers for a single album live (deduping the group's
albums by resolved folder first), which is what the Fill gaps workspace uses.

`_missing_for_album_records` also reports `extra` — files the canonical tracklist
can't account for — so an album reading 11/11 can still say something is off
instead of absorbing the duplicate into `present`.

### Navidrome paths vs `/music`

Navidrome reports paths as **its own** container sees them, and its music root need
not be lb-bot's `LB_BOT_MUSIC_DIR`. `_song_abs_path` therefore normalizes
(backslashes, percent-encoding, NFC/NFD), tries the path as given, then rebases by
walking the trailing components against `MUSIC_LIBRARY_PATH` until one resolves —
never onto a bare filename, which would pair unrelated albums sharing a track name.
The prefix that worked is remembered for the process (`_nd_path_prefix`).

Nothing needs to know Navidrome's root, and the failure is not cosmetic: it is what
emptied the duplicate-file list, blocked merges with "outside `/music`", and turned
the System → Diagnostics "Library path ↔ Navidrome" probe red while the mount was
correct. That probe now samples across several albums and reports a ratio, so one
stale Navidrome row no longer condemns the mount.

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

---

## Repo map

- `listenbrainz_bot.py` — everything (bot, web server, API, workers, placement).
- `web/` — React SPA (Vite/Tailwind); `web/dist` is the built output served by Flask.
- `Dockerfile`, `docker-compose.yml` — build & runtime.
- `requirements.txt` — Python deps (`mutagen` declared explicitly
  post-beets-removal; `rapidfuzz` for folder ranking and file↔track matching —
  wheels only, and the code falls back to `difflib` if it is missing rather than
  failing to import).
- `CLAUDE.md` — authoritative design notes; **read before editing the pipeline.**
- `AGENTS.md`, `BUILD_NOTES.md` — agent instructions and build notes.
- `test_album_review.py` — test scaffolding for the album-review flow. Its
  baseline is **32 errors**, all `No module named 'mutagen'` in this
  environment; anything beyond that is yours.


> Line numbers in `CLAUDE.md` and code comments are snapshot hints and drift —
> navigate by function name.
