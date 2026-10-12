# lb-bot — architecture

What the process is doing. The [README](../README.md) is the description and the quick
start; [`DEPLOYMENT.md`](DEPLOYMENT.md) covers running it; [`STATUS.md`](STATUS.md) says how
far each part has been confirmed on a live stack; [`CLAUDE.md`](../CLAUDE.md) holds the long
form of every rule here and is the thing to read before editing the pipeline. Code is named
by function and constant, never by line.

---

## Architecture at a glance

One module, `listenbrainz_bot.py` (about 35,000 lines), and one process running three
things concurrently:

- **Telegram bot** — `python-telegram-bot`, command and inline-button driven.
- **Flask web UI + JSON API** — port `8899`, serving a prebuilt React SPA and a large
  `/api/*` surface.
- **Background workers** — a scan at start and one a week, the download poller, the
  verifier that waits for Navidrome to list what was placed, a worker that indexes
  artists nobody has opened yet (`_auto_index_worker`; it gives way to MusicBrainz
  requests a page is waiting for), the ListenBrainz listens mirror
  (`_listens_sync_worker`), the wishlist's slow re-search, and the sender that tells a
  navi-connect hub what changed.

**What is on disk**, all under `/config`:

- `lb_bot_state.json` — module-level dicts flushed periodically: transfers in flight,
  album groups, the fill ledger. `pending_album_groups` is persisted as JSON, so anything
  stashed on a group must be JSON-serializable (lists, not sets).
- `library_index.db` (SQLite) — the catalogue: `artists` and `release_groups`; the
  **review** (`review_groups`: one row per album that needs something, with the user's
  decisions and the download state); `file_signatures` (cached audio identity per file,
  see "Duplicates"); the editorial `meta` cache; the `wishlist`; `rejected_sources`
  (peers whose file was identified as another recording); what the user has corrected
  (`artist_overrides`, `album_overrides`, `artist_merges`) and `tag_changes` — each
  file's tags as they were before lb-bot first rewrote them, which is what an undo
  restores; and `migrations`, the record of one-time data fixes.
- `library_index.db.hwm.json` — the highest change-feed sequence ever sent out (see "The
  library index and its change feed").
- `listens.db` — the listens mirror, a file of its own with its own lock; deleting it
  with lb-bot stopped is the supported way to rebuild it.
- `missing_album_review.json` — the small per-session remainder (tasks, searches,
  operations); `mbz_cache.json` — MusicBrainz answers, kept across restarts.
- `cover-backups/` — the covers a cover change moved aside.

**Every review group has an origin** (`library`, `playlist`, `spotify`, `duplicate`;
`repair` is a retired one that old rows still carry, hidden), and a scan replaces only
its own (`_replace_review_groups`; a scan of one artist or one
release unions instead, `_union_review_groups`). A scan that replaced the whole list
would wipe every other scan's work.

**Changing the index schema needs two things.** `CREATE TABLE IF NOT EXISTS` never adds a
column to a database that already exists, so a new column needs an explicit `ALTER` in
`_index_db()`, guarded on `PRAGMA table_info` rather than by swallowing the
duplicate-column error. And if the change means existing rows are *wrong* or *missing*
rather than merely old, bump **`INDEX_SCAN_VERSION`**: `_index_get_artist` compares it
and marks mismatched artists stale, which puts "may be out of date" and a Rescan button
in front of the user — and the background indexer scans them again by itself.

### The React web frontend

Lives in `web/` (Vite + React 19 + Tailwind 4). `npm run build` emits `web/dist`,
which the Docker build copies into the image; Flask serves it from `/`. Without a
build, `/` serves a short "run `npm run build`" page — the JSON API works either
way.

The hash is the single source of truth for navigation. Five sections — **Needs
you** (the queue of albums with gaps, and a "To fix" tab for what only the user
can decide), **Activity**, **Discover**, **Library**, **System** — each
`#/<section>[/<tab>[/<selection>]]`, and three pages that open on top of whatever
you were looking at: `#/artist/<id>`, `#/album/<artist>/<release-group>[/sources|/edition]`
and `#/search/<query>`. `lib/router.js` parses the hash, keeps one entry per
history step (so a page's Back button can name the page under it) and answers
older addresses through an alias table. Navigate with `navigate()` for a history
entry, `replaceRoute()` for transient corrections. Panels never parse the hash
themselves.

**A page comes back as it was left.** One page is mounted at a time, so what a
page showed is kept with its history entry: the router holds a small bag per
entry — scroll offsets and small scalars, never a list or an answer — in
`sessionStorage`. Back, Forward, a reload and returning to a section from the
bar restore it; a link, a tab or a row is a fresh page at its top. The frame
keeps its scrollers' places by itself, and a page keeps a view choice by using
`useViewState` where it would use `useState` (`lib/useView.js`; the decisions
are pure, in `lib/view.js`). Never for anything whose restoring would ask the
server for something or write something.

**Wording is code under test.** What a page may say about a job, a refusal or a
count lives in pure modules under `web/src/lib/` (`overrides.js`, `merges.js`,
`artistMatch.js`, `albumSources.js`, `indexStatus.js`, …), each with a matrix
beside it: `node --test web/src/lib/*.test.mjs`, no dependency. A change that
takes a while — an album retag, a name merge, an artist rescan — is followed in
one place outside the components (`lib/jobWatch.js`), so leaving a page stops
nothing and two surfaces never poll one job twice.

The frame's parts are in `components/Page.jsx` (`Page`, `Roll`, `Fluid`, `Grp`,
`Pick`, `Seg`, `Meter`) and the shared rows and tiles in `components/ui.jsx`
(`SourceRow`, `ReleaseTile`, `ArtistTile`, `Cover`, `Chip`, `Skeleton`,
`Notice`, `EmptyState`) — **built once, used everywhere**. Blocks fold open and
shut rather than popping, and nothing above a page's body changes height after
its first frame. Two parts carry behavior worth knowing about:

- **`useSourceFiles`** is the fetch-once-and-keep "show files" expansion, shared
  by every source row, so a peer's folder is never read twice or read
  differently in two places.
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
| **MusicBrainz** | Canonical release/track/recording metadata (rate-limited to one request a second; contact email required) | `ws/2` | `musicbrainz.org` |
| **ListenBrainz** | Weekly playlist sources (Weekly Jams, Weekly Exploration); fresh releases; the user's own listens (mirrored); **similar artists** (the `labs.api` dataset, a separate host from the main API) | `api.listenbrainz.org/1`, `labs.api.listenbrainz.org` | — |
| **Last.fm** | Optional cross-check for similar artists (`artist.getsimilar`). Absent `LASTFM_API_KEY`, the ListenBrainz half runs alone | REST | — |
| **Cover Art Archive** | Album art for releases the library doesn't hold, keyed by **release** or release-group; the covers an album override offers | REST | `coverartarchive.org` |
| **AcoustID** | Optional identity check at placement: is this audio the recording it is being filed as? Fingerprinted locally with `fpcalc` (Chromaprint) | REST | `api.acoustid.org` |
| **Wikidata / Wikipedia** | The "About" text of an artist or an album, found through MusicBrainz's own links | REST | — |
| **Deezer** | Charts and editorial picks for Discover, and related artists. Open API, no key | REST | `api.deezer.com` |
| **Spotify** | Optional public-playlist import (Client Credentials flow) | Web API | — |
| **navi-connect hub** | Optional. lb-bot tells it when a fill moves or the index changes; the hub proxies a slice of this API to the players | REST | `LB_BOT_HUB_URL` |

MusicBrainz is the scarce one: every request goes through `mbz_get`, which paces them
and lets a background caller (the indexer) wait while a page is waiting. Nothing that
renders a browse row may spend that budget — Deezer rows and similar-artist rows are
resolved against the library index, never against MusicBrainz, and Wikimedia requests
have a client of their own (`_wiki_get`).

Navidrome derives `album.created_at` from the *oldest* file, so gap-filled albums
never move in the default `newest` sort. Placement stamps placed files with a
current mtime; set **`ND_RECENTLYADDEDBYMODTIME=true` on the Navidrome container**
so a filled album counts as recently added (which is also how AudioMuse's
newest-albums scan finds it — lb-bot does not call AudioMuse itself).

## Library tagging — recommended

The discography scan resolves your Navidrome albums against MusicBrainz release-groups
in two passes (`_match_albums_to_release_groups`): an album carrying a
`musicBrainzId` claims its release-group exactly; anything untagged falls back to
fuzzy title matching at `DISCOGRAPHY_TITLE_THRESHOLD` (0.92). It therefore **works
on an untagged library** — but not equally well.

| Library | Result |
|---|---|
| **MusicBrainz-tagged** (Picard or equivalent) | Full function: missing releases **and** per-track gap filling. |
| **Untagged, consistently named** | Missing releases only. Owned albums classify as `untagged` and completeness is not computed. |
| **An artist matched to the wrong MusicBrainz artist, or to none** | Its albums line up with nothing, so the whole catalog reads as `missing`. Such artists are listed under Needs you › To fix, where you pick the right one (an *artist override*, below). |

Two deliberate design decisions produce those rows:

- In `build_artist_discography`, a release-group whose matched albums carry no
  `musicBrainzId` is emitted as `status: "untagged"` and **skipped before**
  `_make_review_group` — completeness computed from an unverified match would be
  confidently wrong, so it is flagged rather than guessed.
- An artist's library bucket is **every library entry that carries the scanned
  MusicBrainz artist id** — the one the file tags give, or the one you chose —
  plus the entry the scan was started from (`_artist_library_albums`). An album
  belongs by its artist's id; a name is consulted only for an album no id speaks
  for. Two spellings of one artist are therefore one bucket, and a namesake with
  an id of its own is somebody else. Do not go back to matching on a normalized
  name: two spellings become two buckets, each scan marks the other's albums
  missing, and every non-Latin name normalizes to nothing.

**Recommendation:** tag with Picard before the first scan. Untagged still finds whole
missing albums; it just won't fill partial ones.

---

## Web API surface (`/api/*`)

Served by Flask; every route is declared in `start_web_dashboard()`, many as a thin
wrapper round a module-level view that the unit suite calls without Flask. There is no
inbound authentication — put it behind your own network or proxy. Navigate the source by
route string. By family:

- **Overview and system:** `/api/summary`, `/api/status`, `/api/health` (a liveness
  probe that never writes and asks no other service), `/api/system/health|status`,
  `POST /api/system/recheck`, `/api/action-center`, `/api/diagnostics`, `/api/logs`,
  `/api/settings`, `/api/prefs` (GET/PUT), `/api/tasks`, `/api/tasks/<task_id>`
  (+ `/cancel`), `/api/operations`, `/api/operations/<operation_id>`,
  `POST /api/acoustid/rejected/clear`
- **Gaps and the review:** `/api/gaps`, `/api/gaps/<group_id>` (+ `/fetch`, `/auto`,
  `/cancel`, `/allow-mp3`, `/duplicate-files`, `/rescan` — a *scoped* re-check of one
  album: re-reads it from Navidrome and walks its folder for files Navidrome hasn't
  indexed), `/api/review`, `/api/library` (the review as a sortable, paged table)
- **One review group** (`/api/groups/<group_id>/…`): `sources` (GET the list, POST a
  search), `sources/<n>/files` (expand the peer's real folder), `sources/<n>/download`,
  `tracks/<n>/pick-file`, `tracks/<n>/place-anyway`, `reconcile-downloads`, `hide`,
  `download`, `decisions`, `missing`, `canonical`, `retag`, `match-mode`, `preview`,
  `release-details`, and `downloads/<id>/match`, `downloads/<id>/match-preview`,
  `downloads/<id>/match-selected` for pairing a finished download by hand. "Merge duplicates" in the UI posts to **`retag`**: it writes the canonical
  album and MusicBrainz ids onto the other copies and lets Navidrome collapse them on its
  next scan; a failed tag write fails the merge. `place-anyway` overrules the one refusal
  the row shows — "already in the album", or "another recording" — and no other; it takes
  what the page showed for the row (`recordingMbid`, `title`) and answers 409 `row_moved`
  when a rescan has since put another row at that index.
- **Scanning:** `/api/scan`, `/api/scan-all`, `/api/playlists/scan`, `/api/spotify/scan`,
  `/api/rescan` (a Navidrome scan), `/api/library-index/build|status`
- **Albums and artists:** `/api/album/lookup|releases|sources|tracklist|download|status|cancel|similar`,
  `/api/artists`, `/api/artist/lookup|discography|release|similar|related`,
  `/api/fresh-releases`, `/api/cover/<album_id>`, `POST /api/resolve-link` (one streaming
  or MusicBrainz URL in, MusicBrainz ids out, with a `confidence` that says how it was
  resolved)

  `lookup` marks each candidate with whether the library holds it (`releaseOwned`,
  `releaseAlbumId`) — it marks, it does not filter. Its order is MusicBrainz's text
  score, which a caller must not trust: pass `artist=` and `album=` for a fielded
  search when you know which half is which.

  `releases` returns two axes, not one flat list: a **variant** (Original / Remaster /
  Deluxe) changes the tracklist, an **edition** (Digital / CD / Vinyl) is the same
  tracklist pressed differently and carries its *own* cover art. Each variant defaults
  to its digital edition, the likeliest to have a correct front. It also names the
  release's first credited artist (`artistMbid`). `tracklist` takes an optional
  `album_ids` or `group_id` and then marks **each track** `present`; rows carry their
  disc. `similar` rolls the similar-artists lookup up to one owned album per similar
  artist.

  `sources` pairs each peer folder against the canonical tracklist (see "Matching a file
  to a track") and **keeps what it answered** for a quarter of an hour, so the list a
  user is looking at holds still. `download` with `sourceUsername` (and `sourceFolder`)
  takes that folder from the kept list and searches nothing: **a click downloads the
  source that was clicked**. `allFiles: true` beside it asks the folder for every audio
  file, and every answer echoes `allFiles`. A source row says how many files a plain
  download would leave out (`extraFiles`); a fill says how many it left (`leftOut`) and
  which peer was picked (`pickedSource`) beside the one it is on now. `download` is
  idempotent per release and `status` is how you watch it — see "Watching a fill from
  outside".
- **Fills and transfers:** `/api/fills` (every watched fill in one read),
  `/api/downloads` (+ `/cancel`, `/clear`), `/api/transfers` (+ `/<transfer_id>/dismiss`),
  `/api/search`, `/api/searches`, `/api/searches/<sid>/download`
- **Saying what something is** (each can be undone): `/api/overrides` — every
  correction, the artists worth a look, and the sets of one artist under two
  names; `/api/artist/override` (+ `/remove`); `/api/album/override` (+ `/preview`,
  `/remove`) — GET the album's state, POST to apply — `/api/album/covers`,
  `/api/album/cover`; `/api/artist/merge` (+ `/preview`, `/undo`) — GET to follow a
  merge, POST to start one. See "Overrides"
- **Listening:** `/api/listening/status|summary|unowned|recent`,
  `POST /api/listening/sync`. `/api/fresh-releases` rows carry `artistPlays`
- **Editorial and browse:** `/api/meta/artist|album` (the "About": attributed text,
  members, credits, links; cached, and a `refresh=1` that never replaces a good entry
  with a failed read), `/api/deezer/chart|editorial|genres`
- **Wishlist:** `/api/wishlist` (GET, POST), `/api/wishlist/remove` — albums nobody was
  sharing, searched again on a slow schedule
- **Placement by hand:** `/api/placements` (+ `/identify`,
  `/<placement_id>/confirm|dismiss|delete`), `/api/download-folders`,
  `/api/place-folder` (+ `/candidates`), and
  `/api/imports/<recid>/retry|merge-retry|remove-stale-and-retry` to place a recorded
  import again. The `/api/beets/folders|release-candidates|import|import-preview`
  routes are the older names of the same thing — nothing in them runs beets
- **Duplicates and trash:** `/api/duplicates`, `/api/duplicate-files`,
  `/api/library/delete-file`, `/api/library/trash` (+ `/restore`, `/empty`)
- **The index feed** (for clients that mirror the index): `/api/index/changes`,
  `/api/index/keys`
- **Diagnostics:** `/api/debug/slskd-search?q=…` traces one slskd search end to end
  (every status poll, the probes of its responses, slskd's effective options);
  `/api/debug/album-search?artist=&album=` runs the real album search and shows every
  folder's rank inputs and evidence. Reach for these before theorising about why a
  search returned nothing or picked what it picked

`/api/summary`, `/api/settings`, `/api/diagnostics` and `/api/action-center` are
polled, so their expensive parts are cached: slskd transfers (~3s), the reachability
probes (60s, and run concurrently), and the download folder listing (30s). Pass
`?fresh=1` to bypass a cache for an explicit re-check.

A dependency that did not answer is an error status, never an empty answer: a list
that could not be read is left out or refused (502 / 503 with `retryable`), because a
client reads an empty list as "there is none".

### Keeping the polled path cheap

The SPA polls `/api/summary` and `/api/gaps` every few seconds, so anything on that
path is on a hot loop. Two rules follow, and breaking either costs tens of seconds of
frozen UI:

- **Never hold `_review_lock` across network I/O.** It is process-wide and every
  poll wants it. Read what you need under it, do the slow part outside, then
  re-find the group and merge — `api_gap_detail` and `_run_group_source_search`
  are the reference shape. `_group_generation` gives you the token to detect a
  rescan landing mid-flight; discard rather than merge when it changes.
- **Don't deep-copy the whole review state to render a list.** `_review_snapshot`
  copies everything (including every peer's file listing); `_review_list_snapshot`
  copies only the fields the list views read. Add to `_GAP_LIST_GROUP_FIELDS` when
  those views start reading something new.

The raw slskd payload (`source_results.folders[].files`, `_expanded`, `_claimed`)
is deliberately **not persisted** — `_review_state_for_disk` strips it, and
`_load_review_state` drops what's left on restart, so an album asks for a fresh
search rather than showing sources that can't be enqueued. Results younger than
`SOURCE_RESULTS_TTL` (180s) are reused instead of re-searching; "Search again"
posts `{force: true}` to bypass that.

**slskd publishes search results only on completion — there is no partial
read.** This is the single most important thing to know about this path. Traced
against a live instance with `GET /api/debug/slskd-search?q=…`:

| t | state | responseCount | `/responses` | `?includeResponses=true` |
|---|---|---|---|---|
| 5.1s | InProgress | 138 | 0 | 0 |
| 25.8s | InProgress | 143 | 0 | 0 |
| 35.4s | InProgress | 143 | 0 | 0 |
| 39.4s | Completed, TimedOut | 143 | **142** | **142** |

`responseCount` on the search status ticks up live — that is what the progress
line counts, and why a search can honestly report "143 peers" before it has
anything to show. So:

- The poll **must** run until `isComplete` / state `Completed`. `SLSKD_SEARCH_TIMEOUT`
  (75s) is a safety cap, not the intended exit, and must outlast slskd's own
  straggler timeout (~40s observed).
- What the early exit can still buy is ending the search *sooner*: once
  `SLSKD_SEARCH_MIN_RESPONSES` peers have arrived (or the file count has
  plateaued) past `SLSKD_SEARCH_MIN_WAIT`, `PUT /searches/{id}` asks slskd to
  wrap up, and it publishes on completion as usual. Best effort — if the stop
  fails the poll just runs to the cap.
- `SLSKD_SEARCH_SETTLE` (25s) is a backstop: if the array is still empty
  after all that, wait for completion and ask once more. It fires only on an
  *empty* array — a short one is a normal ranking outcome and must not cost anyone
  the wait.
- **A search slskd cut off is a sample.** slskd ends a search at its response limit;
  for a title made of common words that happens within seconds, and each search is
  then a different random sample of the peers. `slskd_run_search` reads the pass's
  state, and a cut-off pass is followed by a second question that names a track (see
  "How a source is chosen").

All outbound HTTP goes through `_http`, which keeps one pooled `requests.Session`
per host. Use it rather than `requests.get`/`post` directly.

### Watching a fill from outside

The navi-connect hub proxies a whitelisted slice of this API to its clients
(`<hub>/lb/*`), which puts requirements on the album download path that the bot's own
SPA alone would not need.

**`POST /api/album/download` returns a task id that is not the answer to "is it
downloaded".** The task finishes as soon as slskd accepts the enqueue, with the
transfer, the placement and the Navidrome scan all still ahead.
`GET /api/album/status?release_mbid=…` (or `?rgid=`, which costs a MusicBrainz
resolve, so prefer the release) answers properly:

```
unknown → searching → queued → downloading → placing → placed → verified
                          ↘ cancelled      ↘ needs_match   ↘ failed
```

It reads the in-memory ledger `_album_fill_status` plus live counts off
`pending_album_groups` — no review snapshot, no network I/O under `_review_lock`.
What the view reports:

- `done` is files **completed**, `failed` sits beside it; `percent` is byte-based when
  the transfers report sizes, and 100 from `placing` on.
- `cancellable` is the one Cancel rule, stated by the server.
- `placed` means the files are in the album folder; **`verified` means Navidrome has
  indexed them** (`_verify_placement_worker` for a review group's fill,
  `_start_album_fill_verification` for an album fetched whole).
- `source` is the peer it is on now, `pickedSource` the one the user clicked, `leftOut`
  and `allFiles` what that source was asked for.
- `updatedAt` and `serverTime` let a client say "last checked N s ago".

`GET /api/fills?release_mbids=…&group_ids=…` answers every watched fill in one read
(`_fills_view`), and with a hub configured `_push_fill` posts each ledger transition
and throttled progress to it, so a client need not poll at all.

The download poller is the one clock: it lists slskd's transfers every
`DOWNLOAD_POLL_ACTIVE_INT` (5 s) while any group or transfer is live and once a minute
when idle — and an idle wait ends as soon as something is queued. The request path
never touches slskd.

**A cancel is terminal.** `cancelled` is a ledger state of its own, not a failure:
`_album_fill_set` refuses to write over it for the fill it stopped, and
`_cancel_album_fill` marks the group, detaches its transfers and only then cancels them
in slskd, off the request thread — so the route answers in milliseconds and nothing can
finalize the group meanwhile. Placement claims the row atomically, so a cancel that
arrives once it says `placing` is told it was too late. A *new* fill for the same album
lifts the cancelled row; nothing else does. The long form — what a cancel does to rows
whose file had already landed, and to a fetch that is still queueing — is in
`CLAUDE.md`.

**`POST /api/album/download` returns the fill already in flight** rather than
starting a second (`{ok: true, existing: true}`). Two clients and the SPA can all be
looking at one album.

**Failure reasons reach that path too.** A fill that found nothing says why
(`_no_source_reason`: every file in a rejected format, no folder that is clearly the
album, the picked source no longer listed), and whether allowing MP3 would help. A
fill that began with a click on a source gets no automatic retry — the retry would
search again and take its own choice.

**A placed album is marked in the index at once.** `_finalize_group` calls
`_index_mark_release_present`, so a filled album does not show up twice in a client —
once in the library, once in "not in your library" — until the artist is scanned
again. A later scan never forgets that mark.

**Per-album quality.** `POST /api/album/download` takes an optional `quality`
(one of `QUALITY_PREFERENCES`) that overrides the global source preference for
that fill alone. It is scoped with a ContextVar (`_quality_preference`, read through
`_effective_quality()`), and the album group carries it too, because failover runs
on another thread long after that scope has exited. An unknown value is a `400`.

**A group whose transfers all vanished is finalized, not left hanging.** The poller's
sweep finalizes a group with nothing left in `pending_downloads` after
`ALBUM_GROUP_ORPHAN_GRACE`, skipping groups mid-failover, and ledger rows that can make
no further progress are failed with a reason (`_sweep_album_fill_zombies`). Nothing in
placement hinges on there being a Telegram chat to narrate to.

### Background work must be visible and must not strand an album

A source search is a background task: the POST returns a `task_id` immediately
and the work continues while the user looks at a different album. The rules:

- **Stamp `group_id` on the task** (`_task_run(..., group_id=...)`). It is what
  lets `_gap_detail_view` attach `sourceTask` and `_gaps_view` mark the list row
  `searching` — without it, an in-flight search and a search that failed look
  identical. Long-running network steps report through a `progress(text)` callback
  into `task["current"]`; there is no total to count towards, so elapsed time and
  the peer count are the only honest progress.
- **An empty result must say why.** `slskd_run_search` fills a caller-supplied
  `stats` dict (peers, files, folders before scoring, rejected formats) and
  `_no_source_reason` turns it into a sentence.
- **Treat every slskd collection as nullable.** A response carries
  `"files": null` when the peer's only hits were locked. Read
  `peer.get("files") or []`, and the same for `lockedFiles`.
- **Never compare slskd's peer-wide `lockedFileCount` to a folder's file count.**
  A search response's `files` are the *unlocked* hits; locked ones arrive in
  `lockedFiles`. `_score_folder` uses `locked_in_folder`, counted per folder from
  that array. The peer-wide count is display-only.
- **Keep `_RETRYABLE_DECISIONS` complete.** A finished search flips every
  approved track to `source_pending`. Any decision that means "still missing,
  nobody working on it" must be in that tuple, or `_approve_pending_missing_tracks`
  won't re-approve it and `_group_source_plan` dies on "No approved tracks" —
  which strands the album on "choosing a source" with no sources to choose.
- **A group stuck on `downloaded` stops claiming to be in progress.** Files fetched
  and not placed for `DOWNLOADED_STALE_SECS` read as failed with the reason
  `stalled_placement`, so the UI offers a way out instead of "working" for ever.
- **Don't put a big nested payload on a task row.** Task rows are flat scalars;
  `/api/tasks` serves all of them and the hub caps a proxied answer. Only
  `/api/tasks/<task_id>` serves a task's `result`.

---

## Overrides — saying what an artist or an album is

Three corrections a user can make. Each is stored in the index, each can be
undone, and each is served to the navi-connect clients through the hub as well
as to lb-bot's own web UI.

- **Which MusicBrainz artist a library artist is.** One row of
  `artist_overrides`. Everything that needs a Navidrome artist's MusicBrainz id
  reads it through one function (`_nd_artist_index()`), so a set or a remove is
  seen at once; the artist's index rows are re-homed and its discography is
  scanned again. Artists whose match holds none of their albums are listed for a
  look.
- **Which release an album is.** lb-bot **retags the album's files** to the
  release you pick: release and release-group ids, disc and track numbers,
  titles, the album title, the date — never the artists, and nothing is renamed
  or moved. *The tag is the pin*: the files say it, so Navidrome and every scan
  agree without knowing an override exists; do not add a read-time lookup of the
  override table to a matcher. Each file's tags are kept, on disk, before the
  first write, so an undo goes back to before the first change however many
  followed. A Cover Art Archive image can be saved as the album's cover with it,
  or on its own; the cover that was there is moved aside, never deleted. A retag
  is expected to change the album's Navidrome id, so a job is followed by the
  override's own id, not the album's.
- **That two artist names are one artist.** Navidrome keys an artist by name, so
  "A & B" and "A and B" are two artists there whatever id both carry. A merge
  rewrites one spelling to the other wherever a file's album-artist or
  track-artist tag *is* that spelling — a substitution of values, so a co-credit
  or a featured credit beside it stays. It is offered only between entries that
  carry the same MusicBrainz id and are spellings of each other: a collaboration
  credit is not a spelling of its lead artist, and two different collaborations
  are not spellings of each other.

What they share:

- **lb-bot must reach every file Navidrome lists for the album**, or it writes
  nothing and answers 409 `files_unreachable` — it does not guess which file is
  which. That needs Navidrome's "Report Real Path" for lb-bot's player; see
  [`DEPLOYMENT.md`](DEPLOYMENT.md). An undo works from the paths it kept and does
  not ask Navidrome.
- **A running change holds the album.** Fill gaps starts nothing for it
  meanwhile, a second change answers `in_progress`, and a change asked for while
  a download for the album is in flight answers `busy`.
- **Done means Navidrome has it.** A change reads `applied` or `merged` only
  after a Navidrome scan that began after the last write, and once the album
  that holds the rewritten files is listed again. A change that wrote nothing
  reads `failed`, never done.
- **A dependency that did not answer is a 502 to retry**, never "no such album"
  or "nothing matches".
- **The cover's address is checked before it is fetched**: https on the Archive's
  own hosts only, redirects followed by hand through the same check, a size cap, JPEG
  or PNG only.

## The listens mirror

lb-bot keeps a copy of the user's ListenBrainz listens in `listens.db` — one
writer thread, the routes only read — and answers aggregates over it: the most
played artists, albums and tracks of a period, a calendar, the most recent
listens, and what is played most that the library **does not hold**. The first
import pages back through the whole history and survives a restart; after it
there is a pull every ten minutes, and once a day a wider one for listens that
were submitted late. Whether an album is owned is joined at read time and never
stored inside an aggregate, so a fill shows at once. Local days come from the
client's time zone (`tz`), since the container runs in UTC.

## The library index and its change feed

`artists` and `release_groups` are the durable catalogue of what exists and what is
missing; only lb-bot writes them. Clients keep a local copy and pull what changed:

- **A sequence number is maintained by SQLite triggers**, bumped on every row written
  and stamped on the artist the row belongs to. Every index write must therefore be
  ordinary SQL on the shared connection (`_index_db()`); a write that goes round the
  triggers is a change no client will ever see.
- **An epoch says when a client must start over**: a new database, a change in the
  shape of a wire row (`WIRE_VERSION`), or a database that went backwards — caught by
  the high-water mark kept beside it, which is written before any sequence number
  leaves the process.
- `GET /api/index/changes?since=&epoch=` pages by bytes; `GET /api/index/keys` lists
  every artist's key for a drift repair; `GET /api/health` is the hub's liveness probe
  and never writes.
- The background indexer (`_auto_index_worker`) scans one artist at a time — never
  scanned first, then stale — and backs off an artist that failed without stalling the
  rest. `/api/summary` carries how far it is (`index`), which the web UI shows.
- A scan never forgets a fill: rows marked `present` at placement are carried across a
  rescan.

## How a source is chosen

**The questions asked.** MusicBrainz titles carry edition packaging no peer folder has,
and a self-titled album collapses into an artist-wide query. `_album_search_queries`
emits up to `MAX_SEARCH_PASSES` (3) ordered variants: the cleaned title with repeated
runs collapsed, plus the year when self-titled; a punctuation-free or distinctive-words
form; and one that asks for the album's most distinctive *track*
(`_album_track_query`). `slskd_search_album_folders` walks them and merges folders
across passes by peer and folder — a folder two passes found keeps both passes' files.
It stops early once folders that are clearly the album have turned up, **except** that a
pass slskd cut off, or a title with no word of its own (a self-titled album, "EP 2", a
bare year), always gets the track question: the first answer was a sample.

**Ranking knows which album it asked for.** `_annotate_folder_match` scores each
folder's own path against the album and artist (`_folder_name_score`, over the last
one or two components), and `_score_folder` pays more for that than for anything that
is a property of the *peer* (speed, queue, free slots, collection size). Token-sort is
the base of the name score because it pays for extra words — a partial ratio scores any
superstring at 100, so the artist's name alone would match every album of theirs.
Sibling markers (a roman numeral, "part 2", "vol. 3") cap the score. The preferred
quality (`flac-any`, `flac-16-44`, `highest-bitrate`, `prefer-opus`) picks between two
copies of the same album and never rejects a source.

**Evidence is a tier, not a bonus.** `_rank_album_folders` sorts in three tiers,
whatever the score inside each:

1. *artist evidence and album evidence*;
2. artist evidence only;
3. the rest.

Artist evidence: a complete credited artist name whole in a path component or in the
artist part of a file name, or the folder's files lining up with the tracklist. Album
evidence (`_stamp_album_evidence`): the folder's name matches the album, or at least
half of the release's titles — and at least two — have a file in it. For an artist
whose name is a common word every folder with that word is "by the artist", which is
why artist evidence alone is not enough; and for a title with no word of its own the
*name* is no evidence, only the tracks count. A release known to have one track is
evidenced by that title only together with an exactly named artist and, where both
sides state one, a matching length.

**Nothing unattended guesses.** `_unattended_folder_ok` — artist evidence, and album
evidence for a folder of an album search — is the one rule behind the automatic choice
(`_auto_download_choice`), every list of alternates, the failover order
(`_source_failover_order`), the album switch and the leftover rescue. When no folder
passes, the fill says so ("none is clearly this album — pick one yourself") rather than
taking the best-scored stranger. A track search's folder is nobody's album and passes
on artist evidence.

**A click is consent for that source only.** The list a user picks from is kept
(`_album_sources_kept`); a click takes that folder from it (`_album_source_named`) and
runs no new search, so the source downloaded is the one clicked. If it is no longer
listed the fill fails and says so. What a click may fail over to is the other folders
of the same list that pass the unattended rule.

**What is asked of a folder that has the whole release** (`_whole_album_files`): when
every track of the release is paired with a file by its exact title, the files named
for a track are asked for and the rest are left out — a peer's folder often holds
another release's files too. One track paired on anything looser, or no tracklist to
be had, and the folder is taken whole, so that the import has every candidate in hand.
"Download all" (`allFiles`) asks for every audio file of the picked folder, and only
of that one.

**Failover.** A refusal is the normal answer from a peer whose free-slot flag went
stale since the search, so the ranked list is walked (`SOURCE_FAILOVER_MAX`, and in a
synchronous request `LB_BOT_SOURCE_FAILOVER_DEADLINE`), re-taking `_review_lock` per
attempt rather than holding it across slskd calls. While a download runs:

- a file that fails while the rest of the album is progressing is asked of the next
  source (`_retry_file_from_alt_source`, capped at `MAX_FILE_RETRIES`); claims on a
  peer's files are tracked per peer and folder so two tracks cannot be paired to one
  file;
- a source that has given the group **nothing** is replaced whole
  (`_switch_album_source`). "Nothing" is read from the transfer listing in hand
  (`_source_has_given`), not from a counter that lags it: a peer that refuses some
  files while serving the others is serving the album;
- a group is not judged while its files are still being asked for, one request at a
  time;
- a track the chosen folder has no file for is looked for in the alternates before
  anything is marked failed (`_rescue_leftover_tracks`);
- the stall watchdog tells *stalled* from *waiting*: a transfer that has been running
  gets `STALL_TIMEOUT` (120s) of no byte progress; one still in the peer's remote queue
  gets `QUEUE_TIMEOUT` (900s), because peers serve one or two files at a time.

**Format policy** is per album at the edges: `FORMAT_PRIORITY` stays flac/opus, but a
review group can set `allow_mp3` (`POST /api/gaps/<id>/allow-mp3`), which widens the
accepted formats for that album's searches only, with mp3 ranked last. Placement
always accepts mp3 — refusing to file a track that was deliberately downloaded would
strand it.

## Matching a file to a track

`_album_file_pairs_for_missing_tracks` is **the** folder matcher: the picker's
coverage, the enqueue, the rescue, the album switch and the per-file retry all call it,
so what a row promises and what is fetched are one pairing. Title first:
`_file_title_key` strips the track number and extension from the file name and
`_title_match_rank` grades the hit, tightest first:

| tier | basis | evidence |
|---|---|---|
| 0 | `exact` | the keys are identical |
| 1 | `prefix` | the filename starts with the title |
| 2 | `contained` | either contains the other — **both directions**, since a MusicBrainz title is often *longer* than the filename ("Song (feat. X)" vs `04 - Song.flac`) |
| 3 | `fuzzy` | rapidfuzz `token_sort_ratio ≥ 87`, or `partial_ratio ≥ 92` **with whole-token alignment** |
| 4 | `duration` | no title matched; the file's length fits this track and nothing else, in both directions |
| — | `position` | no title matched anywhere, but the folder *is* the whole album, so the track number is used |

`_best_file_match` returns the basis alongside the file, so the UI can chip the weak
tiers. Rules that keep the looser tiers from doing damage — none is optional:

- **A loose hit is discarded when another title on the release claims the same
  file at least as tightly** (`_sibling_claims_tighter`). Without it, a missing "Sing"
  matches `05 - Singularity.flac` and downloads a song already in the library while
  the real gap stays open.
- **`partial_ratio` scores any substring at 100**, so `_tokens_aligned` requires the
  shorter string's tokens to appear as *whole* tokens in the longer.
- **Track number and length count only in a trusted folder** — one whose name matches
  the album or that has already matched a majority of the release's titles — and the
  whole release is in view, so a song the library already has can veto a length guess.
  This is what pairs a library tagged as one edition with a peer sharing another in a
  different language.
- **A track number is evidence only with its disc.** A release is its media: rows carry
  their disc (`_row_slot`), a medium that only repeats another — the LP sides of a box
  that also holds the CD — is dropped (`_release_tracks_from`), and on a release of
  several media a bare "03" names no row.
- **A folder with exactly as many files as there are gaps is not zipped to them
  blindly** (`_exact_count_zip`): every file must carry the number of the row it lands
  on, and no file may match another track of the release. Only a source the user chose
  is zipped at all.
- **A pairing with no title in it says so** (`weak_pairing`): such a source is offered
  and can be fetched, but is never "recommended" (`_source_recommendable`).

`_source_coverage_summary` puts the pairing on a source row and `_source_files_view`
on the wire file by file, so a source whose files match no track is visibly wrong
before anything is downloaded. A search answer holds only the files that matched the
question; `GET /api/groups/<id>/sources/<n>/files` reads the peer's real folder once
(`slskd_expand_directory`) and recomputes the pairing over it.

**A track can be matched by hand.** `POST /api/groups/<id>/tracks/<n>/pick-file`
enqueues one specific peer file for one specific missing track. The binding is
recorded, and `_group_manual_pairs` hands it to placement, which consults it **first**
— otherwise placement would re-match and quietly undo the choice.

When nothing matches, the nearest rejected candidate and its score are recorded on the
track: a bare "no matching file" reads as "the source was empty" when the truth is
that the one candidate belongs to another track.

## How a file is placed

For each missing track the scan has already resolved the recording, its place on the
release and the canonical tracklist, so placement is deterministic:
`_deterministic_album_import` for the folder, `_place` for a file, `_place_file` for
the move.

1. **Which files.** A finished download imports the files *its own transfers landed*,
   by name (`_finalize_import_selection`) — not the folder, which slskd names after the
   peer folder's last component and which may hold what an earlier fill left. A
   transfer's file is the one that arrived for it (`_transfer_local_path`): never older
   than the transfer. A fill can span several download directories; a file picked by
   hand or rescued from another peer is placed too (`_place_group_downloads`).
2. **Find the album's folder** (`_nd_find_album_folder_by`): the folder of the Navidrome
   album that carries the wanted release id, else whose artist and title are the
   wanted ones — never just the best hit of a text search. None: `<music>/<artist>/<album>`
   is made. A placement that is not for an album gap is filed under the library's own
   spelling of its artist (`_library_artist_spelling`), so a new album cannot add a
   second spelling.
3. **Pair every slot before any file is touched** (`_match_slot`): a hand pick, then a
   recording id in the file's tags, then the row the file was downloaded for, then the
   title tiers. A length pairs a file only with the slot its own number names
   (`_pair_by_length`, `_file_own_number`), and by number and length it is all or
   nothing: one numbered file that does not fit its slot, and none is taken that way.
4. **Nothing is filed that has not shown the album.** An unattended import with no
   review group files nothing unless at least half of the tracks were found. A file
   with no slot is filed as an extra only when the album was shown *and* the file's own
   tags name this album (`_tags_name_album`); otherwise it stays where it is, with the
   reason. A hand route — the user naming the folder and the release — places each file
   it pairs.
5. **Guard** (`_reject_reason`) — see below.
6. **Tag** from the slot (`_mutagen_write_tags`; FLAC and Opus are Vorbis comments, MP3
   goes through ID3): number, disc and their totals from the slot and never from the
   downloaded file, the release's ids, the title. Writing a tag deletes its other names
   first (`TAG_ALIASES`), so a placed file cannot credit its album to two spellings of
   one artist. The file is named by its slot, with the disc once the release is more
   than one medium.
7. **Move** (`_place_file`): move, `chmod`, then a fresh mtime on the file and its
   folders — each step is load-bearing. A rename keeps the downloader's mode, and
   Navidrome sorts "recently added" by mtime.
8. **Verify.** A Navidrome scan is asked for and a background pass looks each placed
   track up; found tracks become `verified`, and one that never appears within
   `PLACEMENT_VERIFY_TIMEOUT` goes back to `pending`. The library, not the bookkeeping,
   is the oracle: a rescan that still reports a track missing clears an inherited
   `placed`.

A placement of a review group holds that group's claim (`_group_placement`) from before
the first file can move until its rows are written, so a cancel always knows whether it
came first. Every caller of the import that names a group is listed in a test that
reads the module's syntax tree; a new one fails there until it is listed.

### The placement guard

`_place` refuses — leaving the file in `/downloads` with a named reason on the track —
when one of these holds, in this order:

- **the audio is another recording.** With `ACOUSTID_API_KEY` set the file is
  fingerprinted and `_acoustid_verdict` gives one of four answers: *confirmed* (the
  slot's recording is among those named); *the same track in another recording*
  (`_same_track_recording`: the title, the album's artist, and a length within
  `LB_BOT_SAME_TRACK_LENGTH_SEC` of the track's) — placed, and said; *different* —
  refused, and remembered against the peer; or *no opinion*. **Silence is never
  evidence**: no key, no `fpcalc`, a fingerprint nobody knows, AcoustID down — all are
  "no opinion", and placement goes on as it would without the check.
- **the audio is already in the album**: `_audio_signature` compared with
  `_same_audio_exact` — FLAC's own MD5 of the *unencoded* audio, or an identical
  sample count at the same rate. Deliberately strict, because it gates a refusal; that
  hash proves two files are the same encode and says nothing about a different
  recording, which is what the fingerprint is for.
- **the file's own tags contradict the slot**: a recording id that is a real
  MusicBrainz id and is not the slot's — unless the title and the length are the
  slot's, which is what a file tagged with an id MusicBrainz has since merged looks
  like — or an own-title that is exactly another track of this release.

A refusal carries its kind, and `_note_force_place_offer` turns that into the offer:
**"Place anyway"** on the row, which overrules the refusal the row shows and no other
(`_place_track_anyway`). A hand pick skips the tag and fingerprint checks — that
judgement is the one being overridden — and keeps "already in the album", which is
what stops a duplicate being created. A file with no usable tags and no MD5 has
nothing to contradict and is placed: the guard must never turn a working fill into a
no-op.

### Duplicates

Library → Cleanup covers two different things.

**Duplicate albums** are bucketed by normalized artist + title, then optionally
by fuzzy title similarity, then structurally. The structural pass exists because a
release and its copy titled in another language share no text at all: candidates are
albums with the same track count whose total runtimes agree within a tolerance
that scales with the track count (`DUPLICATE_DURATION_TOLERANCE` floor,
`DUPLICATE_TRACK_TOLERANCE` per track). A candidate is confirmed by, in order: the
artist tag agreeing; both albums resolving to the same MusicBrainz release-group; or —
with "match across languages" (`{"deep": true}` on `POST /api/scan`) — their
**per-track runtimes agreeing track for track** (`_album_duration_fingerprint`), which
needs no MusicBrainz id on either side.

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

Nothing looser feeds a delete button: equal length, rate and channels is duration
alone, and two different songs of one length would be grouped.

Signatures are cached in `library_index.db` (`file_signatures`, keyed on path + size +
mtime), so a rescan costs one `stat` per file rather than a header read.

**A file is one file.** Identity is `(st_dev, st_ino)` (`_file_identity`), never the
path string: `os.path.normpath` does not case-fold, resolve symlinks or
unicode-normalize, while `_song_abs_path` forces NFC on Navidrome's path and
`os.walk` returns the filesystem's spelling. Compared as strings, one file is two rows
carrying the same stream md5 — the strongest evidence there is, with one of them
always the deletable row. Identity is used for the Navidrome/disk merge, the per-set
dedupe, the cross-set `claimed` ledger (one file appears in at most one set), the
delete endpoint's prune, and the SPA's row keys. The disk walk is non-recursive and
skipped for the library root or a directory holding implausibly more audio than the
album has tracks.

Sets are ranked best-copy-first by format → bitrate → size and deleted one at a time
through `POST /api/library/delete-file`, which requires `{"confirm": true}` and
refuses any path that does not resolve strictly inside `LB_BOT_MUSIC_DIR`. Two
further guards: it **refuses the last copy** (`409 last_copy`) unless some other
file that still exists is genuinely the same song, and it **moves to trash rather than
unlinking**. Trash is `LB_BOT_TRASH_DIR`, default `/music/.lb-bot-trash`, laid out as
`<YYYY-MM-DD>/<path relative to the library>` with a `manifest.json` beside the files;
same filesystem, so it is a rename, and dot-prefixed so Navidrome skips it.
`GET /api/library/trash`, `POST /api/library/trash/restore` (refuses an occupied
original path) and `POST /api/library/trash/empty` complete it. In the UI the row the
set is keeping has no Delete button, so emptying a set takes a deliberate change of
mind.

`GET /api/gaps/<id>/duplicate-files` answers for a single album live.
`_missing_for_album_records` also reports `extra` — files the canonical tracklist
can't account for — so an album reading 11/11 can still say something is off.

### Navidrome paths vs `/music`

Navidrome reports paths as **its own** container sees them, and its music root need
not be lb-bot's `LB_BOT_MUSIC_DIR`. `_song_abs_path` therefore normalizes
(backslashes, percent-encoding, NFC/NFD), tries the path as given, then rebases by
walking the trailing components against `MUSIC_LIBRARY_PATH` until one resolves —
never onto a bare filename, which would pair unrelated albums sharing a track name.
The prefix that worked is remembered for the process (`_nd_path_prefix`). Nothing needs
to know Navidrome's root.

That covers a *different root*. It does not cover a path Navidrome **made up from the
tags**, which is what a Subsonic client is handed unless "Report Real Path" is on for
its player: gap filling finds an album's folder all the same, but an album override, a
cover change and a name merge need every file's real path and refuse without it (see
"Overrides"). The System page's "Library path ↔ Navidrome" check samples across
several albums and reports a ratio, so one stale Navidrome row does not condemn the
mount.
