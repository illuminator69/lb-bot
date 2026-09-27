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

**Test baseline:** **32 errors, 0 failures** (441 tests on `index-mirror`, 2026-09-24 — the total drifts as
tests are added, so check the 32/0, not the count; all 32 are in `AlbumReviewTests`). The errors are all stale beets tests kept
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

**`core.fileMode` is `false` here** — another Windows-migration leftover. Git ignores the
executable bit on disk, so `git add` records a new script as `100644` however you chmod it, and
a fresh clone gets a `deploy.sh` that answers "Permission denied". Adding an executable means
`git update-index --chmod=+x <path>` as a separate step; check with `git ls-files -s <path>`.

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

New environment variables, all optional and all degrading to "feature off"
rather than to an error:

| var | default | what it gates |
|---|---|---|
| `ACOUSTID_API_KEY` | unset | fingerprint verification at placement. Unset = "no opinion", placement unchanged. Needs `fpcalc` too. |
| `ACOUSTID_MIN_SCORE` | `0.6` | below this an AcoustID answer is no opinion, not evidence against the file |
| `LB_BOT_WISHLIST_INTERVAL` | `21600` (6 h) | how often the wishlist sweep wakes |
| `LB_BOT_WISHLIST_COOLDOWN` | `43200` (12 h) | how long before one wishlist row is re-searched |
| `LB_BOT_SOURCE_FAILOVER_DEADLINE` | `45` | wall-clock ceiling on the fetch route's walk down the ranked source list |
| `LB_BOT_AUTO_INDEX` | `1` (on) | the background auto-index worker (`_auto_index_worker`): scans Navidrome artists with no, stub or stale index rows, yielding every MusicBrainz request to interactive callers. `0` turns it off without a rebuild. Like `index-push`, it only exists when the web UI does (`LB_BOT_WEB`, see § The index change feed) |

Deezer needs no configuration at all — the browse API is open.

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

### `GET /api/album/lookup` answers ownership, not just a ranking

Free-text MusicBrainz album search, and until 2026-09-23 that was all it was —
right for this repo's own SPA, where the Library panel is a download form and
every candidate is something to fetch. It is wrong the moment a *client's search
box* renders the same rows: "not in your library" said about a record the library
holds sends the tap to a download page for an album already on disk. That is the
Fresh tab's `releaseAlbumId` bug and the similar-albums shelf's bug, each paid
for once.

So `_album_lookup_marked` adds, per candidate, the same release-level pair
`/api/fresh-releases` established — `releaseOwned` and the `releaseAlbumId`
behind it — plus a `coverUrl` from the Cover Art Archive (this repo's own
`/api/cover` is Navidrome art keyed by a Navidrome album id, so it has nothing to
serve for a release the library lacks). Three things about it:

- **It marks; it does not filter.** Ownership is by release-group id, which is
  exact, so a client can safely render an owned hit as a library row. The sister
  rule on `/api/artist/lookup` — never drop a row on ownership — exists because
  *that* match would be by name, which hides the right artist when two share one.
- **`releaseAlbumId` may be empty on an owned row.** `_index_owned_rgids` counts
  any non-`missing` row, and a row flipped to `present` at placement carries no
  Navidrome ids until `_index_backfill_present_album_ids` runs. Owned with
  nowhere to send the tap is a real state, not an unowned one.
- **Marking costs no MusicBrainz request** — both maps come from the library
  index — so the route's `_mbz_lock` exposure is still the one search it always
  had. A test asserts exactly one `mbz_get`, because that is the property a
  careless refactor would quietly lose.

The existing keys are untouched, `primary_type`'s snake_case included: this
module's own SPA reads them at `web/src/panels/Library.jsx`.

**The ranking is MusicBrainz's text score, and a caller must not trust the order.**
This route sorts albums before non-albums and then by score, and that score is
about *text*. Measured 2026-09-23: `q=Daft Punk Discovery` returns "Daft Punk's
Discovery but it's in the SM64 Soundfont" by Pignickel **first**, a second parody
second, and the real `Discovery` third — because one title contains both search
words and the other contains one. A client taking the first hit opened the parody
for an album the user owned in full. `q` reaches MusicBrainz verbatim, so a
**fielded** query is a different question and a much better one:
`artist:"Daft Punk" AND releasegroup:"Discovery"` returns the right record first
and no parodies at all. Nothing changed in this route; it is written down because
the shape of the answer is easy to mistake for a ranking that already knows what
you meant.

**`ownership` here is index membership.** `_index_owned_rgids` covers only artists
whose discography has been **scanned**, so an album the user owns by an unscanned
artist is marked `releaseOwned: false` — correctly, for the question this module
can answer, and misleadingly for the question a client is actually asking. A
client with the whole library on hand should consult it *before* this route.

### `GET /api/album/releases` also names the artist

Variants and editions of a release-group, and — since 2026-09-23 — `artistMbid`.
The route always fetched the release-group with `inc=artist-credits` to build the
display credit and **dropped the id out of the same payload**.

That mattered because of where an album page is reached from. A Deezer browse row
carries no MBIDs at all, so a client arriving from one had the artist's *name* and
no way to open their page — and the artist page is the only route to "scan this
artist's discography", i.e. the thing most likely to be needed for exactly those
albums. `_artist_credit_mbid` takes the **first** credited artist rather than
merging: a tap has to land on somebody, the display credit still carries the whole
thing, and the lead credit is the only defensible answer for a collaboration.
Additive, and costs no MusicBrainz request.

**A failed lookup is an error, not an empty album** (2026-09-26). The fetch was
non-strict, so a MusicBrainz outage came back as `{}` and answered 200 with artist
`?` — which the SPA's album resolver read as "MusicBrainz names no artist for this
album" for as long as the failure cooldown lasted. It is strict now: **404**
`not_found` for a release-group MusicBrainz does not have, **503**
`musicbrainz_unavailable` for an outage. A hub client that treated the old empty
200 as "no variants" gets an error status instead.

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

### Deezer browse — `GET /api/deezer/{chart,editorial,genres}`, `/api/artist/related`

The browse half of Track C. Deezer is free and unauthenticated — no key, no
token refresh, no account — which is the whole reason it is here and Spotify is
not: Spotify's browse endpoints all need the client-credentials token and its
editorial ones need a *user* token.

Three rules the client (`_deezer_get` and friends) exists to keep:

1. **It never takes `_mbz_lock`.** That lock is the discography scanner's entire
   1 req/sec budget and `mbz_get` holds it across the pacing sleep, so a browse
   row that queued behind it would render when the scan finished. Same rule and
   same reason as `_similar_artists_marked` and `_wiki_get`. **Resolution to
   MBIDs therefore happens against the library index and never against
   MusicBrainz** — see `_index_release_group_directory`, the name-keyed view of
   `release_groups` that exists precisely because a Deezer row has only
   "Artist" and "Title" where every other caller already has an rgid.
2. **The 6 h cache is on the Deezer fetch, not on the ownership marking.**
   Ownership is re-derived per request, because a landed fill falsifies it.
   That split is what lets these routes sit in the hub's `LB_LIBRARY_ROUTES`
   and be invalidated by a fill at all.
3. **Marking is conservative.** Deezer publishes no MBIDs, so every resolution
   is a name match — exactly the case where a guess is worse than a blank. An
   unresolved row is `owned: false` with no id and no rgid, never a plausible
   one, and the client falls back to `/api/album/lookup` on tap: one
   MusicBrainz search for the one row the user chose, instead of a per-row fan
   out that would spend the whole budget on a shelf nobody touched.

The vocabulary is the existing one — artist rows carry `owned` + `artistId` +
`indexed`, release rows carry `releaseOwned` + `releaseAlbumId` + `coverUrl` —
so a client renders a Deezer row with the component it already has.

Two things measured against the live API on 2026-09-23 and easy to get wrong:

- **The editorial endpoints ignore `limit`.** A request for 5 came back with 10,
  so `deezer_editorial` cuts client-side. `editorial/0/selection` is the real
  editorial list and `editorial/0/releases` is the fallback, because `selection`
  has come back empty and an empty editorial row is indistinguishable from a
  broken one. `section` on the answer says which served it.
- **The chart is geolocated by the *server's* IP**, not the user's. Run from a
  French-routed host it is the French chart, and the open API has no country
  parameter, so this is a property of where lb-bot runs — and the reason a client
  is offered a **genre** picker and never a country one. A per-country control
  would silently do nothing.

`chart` and `editorial` take a `genre`, and `GET /api/deezer/genres` serves the
ids so the two clients do not each hardcode their own copy (the `MoodCharacter`
mistake, which has already drifted once). `chart/0` is exactly what bare `chart`
resolved to, so the default is byte-for-byte the old behaviour and a client that
sends nothing keeps it. `_deezer_genre_id` is the **one** place the value is
validated — digits or `"0"` — because it is interpolated into the upstream path,
so a caller cannot reach anything but a genre. The six-hour cache already keys on
`"path?query"`, so per-genre entries separate for free.

`deezer_search_artist_id` is **exact name match only**, deliberately: Deezer's
search happily answers a tribute band for a misspelling, and a near miss here is
not one wrong row but a whole shelf about the wrong artist.

### Paste-a-link — `POST /api/resolve-link`

One streaming URL in, MusicBrainz ids out. Before this the only URL parser in
the module was `spotify_playlist_id`, a `re.search` for `playlist/<id>`.

Three tiers, and `confidence` is on the wire precisely because they are not
equally good:

| conf | how | providers |
|---|---|---|
| 1.0 | the id *is* the answer, no network call | musicbrainz.org |
| ~0.9 | the provider's own API | Spotify (client credentials), Deezer |
| ~0.7 | a scraped `<title>` / `og:title` | Apple Music, YouTube Music, TIDAL, Qobuz |

An ISRC beats all three where the provider publishes one, because it identifies
the *recording* rather than a name two records might share. **`confidence` keys
off whether the ISRC leg actually answered, not off the ISRC existing** — that
distinction was a live bug: MusicBrainz 400'd the ISRC lookup, the text search
supplied the recording, and the answer still claimed 0.95.

This route *does* spend the `_mbz_lock` budget, one or two searches, and that is
fine: it is a single user-initiated action on a link the user just pasted,
exactly like `/api/album/lookup`. The browse rows above are the ones that must
never touch it.

**Search MusicBrainz fielded, not free-text, whenever you already know which
half is the artist.** `_mbz_release_group_for` exists for this. Measured
2026-09-23: the free-text query `Daft Punk Discovery` scored *"Daft Punk's
Discovery but it's in the SM64 Soundfont"* by Pignickel at 100 and returned it
first, because term density beats the record you meant. Free text stays as the
fallback, since a fielded query finds nothing when the store's spelling of the
artist and MusicBrainz's disagree.

Page-title formats, checked live on 2026-09-23 — they differ and a redesign
upstream breaks them silently, which is what the 0.7 cap is saying:

| store | tag | shape |
|---|---|---|
| Apple | `<title>` | `Discovery by Daft Punk on Apple Music` |
| TIDAL | `og:title` | `U2 - Achtung Baby` (artist first) |
| Qobuz | `og:title` | `Discovery, Daft Punk - Qobuz` (**artist last**) |
| YouTube | oembed | `author_name` + a title needing the artist prefix and `(Official Video)` stripped |

The comma rule is **Qobuz-only and must stay that way**: album titles contain
commas all the time, and applying it generally splits a title in half.

There is still **no Telegram plain-text handler** — `/spplaylist` remains the
only command that takes a link. The route is the deliverable; the paste UI is
each client's.

### Acquisition reliability

Three pieces, and the first two fix failures that looked like the feature
working.

**1. The ranked source list is actually walked.** `_source_failover_order` is
the one definition of "which sources, in what order", shared by
`/api/gaps/<id>/fetch` and `_gap_auto_task` — the two disagreeing about it is
how "auto picked a source manual wouldn't" happens. The fetch route used to
enqueue once and, on refusal, hand the client `nextSource` to click; but a
refusal is the *normal* answer from a peer whose free-slot flag went stale in
the seconds since the search, so the common case was a user clicking through
four sources by hand to reach one that worked. The walk is bounded by
`SOURCE_FAILOVER_MAX` (6) and, in the synchronous route, by
`SOURCE_FAILOVER_DEADLINE` — and it **re-acquires `_review_lock` per attempt**
rather than holding it across all of them, because each enqueue is a 30 s-timeout
slskd call and the whole UI polls through that lock.

**2. The stall watchdog now fails the *album* over, not just the file.** It has
always walked `info["candidates"]` for one file and never touched the group's
own ranked folder list, so a peer that accepted the enqueue and then stalled
ended the fill with five ranked sources sitting unused on the group.
`_switch_album_source` has been the right tool for that since it was written
and **was never called from anywhere**; it is now, before the give-up path.
Two things had to be fixed in it first, both of which are why it presumably was
never wired up:

- it re-enqueued the **whole folder**, ignoring `missing_tracks` — so a switched
  source fetched a whole album to fill two holes;
- it dropped the **review and repair linkage**, so a switched album downloaded
  correctly while every track sat on the `failed` the poller had just written.

On exhaustion it pops the group and prompts but records nothing in the fill
ledger, and its `switching` flag is still set — so neither branch below it would
run and the fill would never report failure, which leaves every client polling
`downloading` forever with no retry to offer. The watchdog records that failure
explicitly.

**3. AcoustID/Chromaprint verification** (`ACOUSTID_API_KEY` + the `fpcalc`
binary, `libchromaprint-tools` in the Dockerfile). This is the one identity
check nothing else here can make. `_audio_signature`'s md5 is FLAC StreamInfo's
hash of the *unencoded* audio: it proves two files are the same **encode** of
the same master and says nothing at all about a different **recording** — a live
take, a radio edit, a cover or simply the wrong track under the right filename
all have perfectly good, perfectly different md5s and sail through.

It hooks into `_reject_reason` inside `_deterministic_album_import` and is the
only refusal there that survives `manual_pairs` and `skip_audio_guard_for`: a
user who picked a file by hand picked it from a filename too, and the override
they meant was "ignore the tags", not "place a different recording".

**Silence is never evidence.** No key, no `fpcalc`, an unknown fingerprint, a
down AcoustID — every one returns "no opinion" and placement proceeds exactly as
before. Only a positive contradiction rejects, and only above
`ACOUSTID_MIN_SCORE`. A guard that turns a working fill into a no-op because a
binary is missing is worse than no guard.

A rejection is remembered against **the peer, not the path** (`rejected_sources`
in `library_index.db`), because the file is about to be deleted or moved and
what must not happen again is re-fetching it from the same peer. `slskd_enqueue`
is the single choke point every acquisition path goes through, so that is where
the memory is consulted. `_download_origin` maps a local path back to its peer
and is deliberately in memory only: the window it must survive is one
download→finalize cycle, and a restart in the middle costs one bad peer one more
chance, never a wrong file placed.

**4. A cancel is terminal, and refused-over.** `cancelled` is a ledger state of its
own now (`_album_fill_set(..., "cancelled")`, `retryable: false`, no `failureKind`),
not `failed` + `failureKind: "cancelled"` — a cancel is the user's verdict, not a
failure, and both clients rendered the old shape with a Retry button. Three things
make it stick:

- **`_album_fill_set` refuses to write over a `cancelled` row** unless the caller
  passes `begin=True`, which only `_album_fill_begin` (a genuinely new fill) does.
  `_finalize_group`, the poller's failover branches and the verifier all used to
  overwrite it — the row flipped cancelled → placed while the files landed anyway.
- **`_cancel_album_fill` marks, detaches, then cancels — in that order, and the slskd
  calls off the request thread.** `ag["cancelled"]` is set, the group is popped and
  its transfers moved out of `pending_downloads` (`_detach_group_downloads`), all
  cheap and synchronous, so the poller can no longer find anything to finalize; the
  DELETEs (`_abandon_transfers_async`, with `?remove=true` so a retry does not
  collide with a stale Cancelled row) run on a daemon thread. The route answers in
  milliseconds — it used to run N × 10 s DELETEs on the event loop, behind the hub's
  20 s timeout, so a twelve-file album could not be cancelled at all. Files already
  on disk are left where slskd put them; the leftover-rescue view can place them.
- **Placement claims the row atomically** (`_album_fill_transition(fill_mbid,
  "placing", unless=("cancelled",))`) and the cancel route reads-and-writes under
  the same lock, so whichever side moves first wins and the other finds out. A
  cancel that arrives once the row says `placing` answers `cancelled: false` with
  the status — the clients show "too late" rather than a row claiming a cancel
  that did not happen. `_finalize_group` also checks `ag["cancelled"]` before
  doing anything, the poller's success branch drops a file whose group is gone
  instead of tagging it as a loose track, and `_switch_album_source_inner` checks
  after every `await` so a cancel mid-failover stops the next peer's enqueue.

`api_gap_cancel` mirrors all of this (`_gap_cancel`); it used to cancel by filename
with a slskd listing per file, never pop the album group and write nothing, so the
orphan sweep finalized — and placed — it minutes later. A cancel inside the 45 s
auto-retry back-off is honoured too (`retryAt` on the row is what makes the row
cancellable in that window; the retry worker checks the state before it fires).
A user cancelling ONE file in slskd's UI no longer cancels the album: that file is
counted lost with no failover, and the album is cancelled only when nothing has
landed and every remaining file is Cancelled in the same listing.

**5. Honest status, one clock, and a push.** Every counter a client reads off
`/api/album/status` moves on a poller tick, so `DOWNLOAD_POLL_INT = 60` made the
status up to a minute stale by construction while the bot's own SPA read slskd
through a 3 s cache and looked live. The poller is adaptive now
(`DOWNLOAD_POLL_ACTIVE_INT`, 5 s while any group or transfer is live, 60 s idle):
one slskd listing per tick whatever the number of clients or rows watching, and
the request path never touches slskd. What the view reports changed with it:

- `done` is files **completed** — never completed+failed; `failed` sits beside it.
- `percent` is byte-based when the transfers report sizes (`bytesDone`,
  `bytesTotal`, `speedBps`, `activeFiles` are on the view, from the `raw_transfer`
  the poller already stored and nothing read), and 100 on `placing`/`placed`/
  `verified` — it used to read 0 on a placed album, because `done` was written
  once at `queued` and never again. `_finalize_group` now writes the final counts.
- `cancellable` is the one Cancel rule, stated by the server: `searching`, `queued`
  or `downloading`, or a `failed` row whose `retryAt` is in the future.
- `updatedAt` moves on transfer progress too (the poller stamps `ag["progress_at"]`),
  and `serverTime` lets a client say "last checked Ns ago".
- `attempts` counts fills **started** (`_album_fill_begin`), not failures, and the
  one automatic retry is gated on `autoRetries` — it used to fire only on a row's
  first failure ever. A new fill drops every per-fill field
  (`_ALBUM_FILL_PER_FILL_FIELDS`) instead of merging over the old row.
- `_sweep_album_fill_zombies` runs each tick: `searching`/`queued` with no group and
  no task past `SEARCH_TIMEOUT + 60 s` → `transfer_failed`; `placing` with no group
  past 15 min → `placement_failed`; `placed` past the verifier's deadline →
  `verifyGaveUp`. The restore docstring used to promise this and nothing did it.
- Gap-fill rows reach `verified` (`_album_fill_mark_group_verified` from the
  review-group verifier); they used to sit on `placed` forever.

`GET /api/fills?release_mbids=a,b&group_ids=g` answers every watched fill in one
read (`_fills_view`; 32 each, album views minus the per-file list, gap summaries
minus the sources), and **`_push_fill` POSTs `<hub>/lb/fill`** on every ledger
transition and on throttled progress (≥ 5 points or ≥ 10 s, `_push_fill_progress`),
coalesced on one worker thread. The hub relays it as a `fill` frame that touches
nothing in its library caches — a landing is still `/lb/notify`. A wishlist
landing is a `kind: "wishlist"` fill frame now rather than a second `albumPlaced`.
`_wishlist_note_outcome` records how a re-search ended on the row, which used to say
"re-searching" forever, and the automatic retry excludes the peer that just failed
(`lastSource`) plus anything the user excluded. `album/download` takes `allowMp3`
for the whole-album path — a `format_rejected` fill with no review group had
`mp3WouldHelp` on the wire and no route that could act on it.

**6. Artist evidence, and the search that stopped guessing** (2026-09-27, branch
`slskd-search-accuracy`). Short and self-titled album titles ("Zone" by Future,
"Led Zeppelin") used to download the wrong album: the album-only fallback query
flooded the pool, `_folder_name_score`'s old extra≤1 partial-ratio allowance
scored any "OtherArtist Zone" folder at 100, and artist agreement was a +250
bonus against ~4,000 points of peer metrics. Now:

- `_annotate_folder_match` decides `artist_verified` + `artist_evidence`
  (`path` / `files` / `tracks`): a complete credited sub-artist
  (`_artist_variants` — split on real separators, never "and") whole in any
  path component or the artist segment of a filename, or
  `_folder_track_matches` lining the folder's files up with the canonical
  tracklist (applied after scoring to the top unverified folders only —
  `_apply_track_evidence_pass` — because it is the expensive check). Non-Latin
  and stopword-only artists fall back to `_match_key` per component. Various
  Artists and empty artists are never gated.
- **Evidence is a tier, not a bonus**: `slskd_search_album_folders` sorts
  verified above unverified whatever the score, stamps `search_pass`
  provenance, and its early-stop needs *verified* album matches. **Absent flag
  means verified** — only explicit `False` demotes, so legacy dicts and the
  single-track path (still ungated, see backlog I-015) behave as before.
- **Unattended paths never guess** (`_auto_download_choice`, the one rule):
  the API download task fails `no_source` ("artist_unverified"), Telegram
  falls through to the picker with a caution, `_source_failover_order(...,
  folders=)` and `_switch_album_source_inner` skip unverified alternates. A
  user's explicit pick (`chosen`, `start_is_choice`) is consent — for that one
  source only.
- `_folder_name_score` classifies a candidate's extra tokens: packaging/years/
  bitrates and the artist's own name are stripped before scoring, sibling
  markers (roman numerals, small numbers, part/vol — symmetric, both
  directions, with the number exemptions off for numbered-series titles) cap
  the score below the 80 threshold. An all-ignorable candidate scores 0.
- Every album caller passes year + canonical tracklist now
  (`_album_search_context` / `_search_album_with_context`, cached, degrades to
  a bare search on a MusicBrainz outage). The year bonus was dead code before
  — `slskd_search_album_folders` never forwarded it.
- Wire: `artistVerified` / `artistEvidence` / `searchPass` on source rows,
  `verifiedCount` on `/api/album/sources`, `recommended` only when rank 1 is
  verified (both pickers). **`GET /api/debug/album-search?artist=&album=&year=|release_mbid=`**
  runs the real search and shows every folder's rank inputs — use it before
  theorising about this path, like the slskd probe before it.

### The wishlist — `GET/POST /api/wishlist`, `POST /api/wishlist/remove`

Where a `no_source` failure goes. `no_source` deliberately never auto-retries
(`FILL_AUTO_RETRY_KINDS`) and the reason is sound: lb-bot walks its entire
ranked source list before reporting it, so an automatic retry re-runs the
identical search against the identical peers. But *"don't retry now"* and
*"forget about it"* are different policies and only the first was ever argued
for — Soulseek's population turns over on the scale of days, so the retry worth
running is a **slow** one (`WISHLIST_RESEARCH_INTERVAL`, 6 h; per row
`WISHLIST_ROW_COOLDOWN`, 12 h) against a list the user curates.

Rows are a `wishlist` table in `library_index.db`, beside `review_groups` and
`meta` and for the same reasons; `INDEX_SCAN_VERSION` is deliberately **not**
bumped. Keyed by release-group id, so a landing is recognised without a name
match: a verified fill calls `_wishlist_landed`, which drops the row and pushes a
`kind: "wishlist"` fill frame (`_push_enqueue`) — a client showing the wishlist has
no other way to learn that what it is displaying is now in the library. Not a
second `albumPlaced`: the landing was already announced, and a library notify makes
every client refetch its whole album list.

The sweep does **one row per pass** on purpose: a source search fans out across
Soulseek and takes the better part of a minute (`SEARCH_TIMEOUT` is 75 s), so
running the whole list at once would saturate slskd and starve whatever the user
is actually doing.

Adding is idempotent and **does not reset `last_tried_at` or `attempts`** —
re-adding something already listed is not new information about who is sharing
it, and zeroing the clock would make a double-tap re-search immediately.

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

### The index change feed — seq triggers, epoch, high-water mark, auto-index

Since 2026-09-24 (branch `index-mirror` in all four repos, not yet deployed when
this was written; `PLAN-lbbot-index-mirror-2026-09-23` in `navi-connect/`, and the
round's `SESSION-*` note there for what was verified) both clients keep a **local copy of `library_index.db`'s
`artists` + `release_groups`** and pull deltas from it, so an artist page paints its
missing albums in the same frame as the owned ones instead of ~10 s later. **Only
lb-bot writes the index**; a client never writes a mirror row from a user action,
it asks lb-bot and waits for the change to come back through the feed. The wire
contract is `navi-connect/PROTOCOL.md` §15.3. What this repo owns:

**The sequence is maintained by SQLite triggers, and the index must never be
written around them.** `index_meta` holds one counter (`seq`) and the `epoch`;
seven triggers (`_INDEX_SEQ_TRIGGERS`, dropped and recreated on every open by
`_index_migrate_seq`) bump it on every row written to `artists` or
`release_groups` and stamp the new value on the **one** artist that row belongs
to (`artists.seq`). A deleted artist leaves a row in `index_tombstones` carrying
its own seq, and re-inserting the key deletes its tombstone. Why triggers and not
a helper each writer calls: several writers update by rgid or review group id
without knowing which artist keys they touch, and one collaboration release spans
several artists — only a row-level trigger sees the key. Why not `updated_at` or
rowid: a wall clock ties and is 0 on stubs, and rowid does not change on UPDATE.
Consequences that bind every future change:

- **Every index write must be ordinary SQL that fires the triggers** — in practice
  the shared `_index_db()` connection under `_index_lock`. No dropping or
  disabling the triggers for a bulk load "and restamping afterwards", and no
  `PRAGMA recursive_triggers=ON` (it is set OFF explicitly: with it on, the hidden
  delete inside `INSERT OR REPLACE` would tombstone the artist a rescan is
  rewriting). Anything that changes rows without firing the triggers is a change
  **no client will ever see** — a mirror only asks for seqs above its cursor, and
  the drift check only notices count/sum mismatches. A hand edit with the
  `sqlite3` CLI is fine; the triggers are in the schema and fire there too.
  Replacing the whole file is a different case and is covered: a fresh DB mints a
  new epoch, and an older copy of this one is caught by the high-water mark below,
  as long as the `.hwm.json` is left where it is.
- **The triggers have no failure path, deliberately.** `_index_mark_release_present`
  and `_index_set_release_album_ids` swallow exceptions, so a trigger that raised
  would silently lose the `present` mark and a filled album would list twice (the
  acquisition-coherence round). `index_meta`'s row is created before any trigger
  can fire and every read of it is `COALESCE`d. Keep it that way.
- **One increment per row write, not per artist** (ruling R1). A rescan of a
  1,000-row artist bumps the counter ~2,000 times; that is fine for a 64-bit
  integer and it guarantees every value belongs to exactly one key, so a page
  boundary can never split a shared value.
- The `artists` UPDATE trigger names every column **except** `seq`
  (`UPDATE OF artist_mbid, nd_artist_id, name, scanned_at, scan_version`), so the
  triggers' own stamp does not re-fire it. A new `artists` column that a client
  should see change must be added to that list.
- **Tombstones are never pruned.** Only an `nd:` → mbid key swap creates one, and
  there cannot be more of those than Navidrome has artists.

**Orphans are adopted, not deleted** (ruling R2). A `release_groups` row with no
`artists` parent counts as owned in `_index_owned_rgids` but could never reach a
mirror, which is keyed by artist; deleting it or excluding it from ownership
would un-own a filled album and invite a second download. `_index_adopt_orphans`
therefore gives every parentless key a **stub** parent on every open —
`scanned_at = 0, scan_version = 0`, mbid or `nd:` id inferred from the key
(`_index_stub_identity` is the same rule in Python) — and
`_index_mark_release_present`'s insert path creates the same stub in the same
transaction so no new orphan can appear. Know what a stub looks like to readers:
`_index_get_artist` returns it (so the discography read says `indexed: true,
stale: true`), `/api/library-index/status` counts it as indexed-and-stale, and an
mbid-keyed stub's mbid is in `_index_indexed_artist_mbids` (so a similar-artist
row reads `indexed: true` for an artist whose discography was never walked). The
auto-index worker picks stubs up second, right after artists with no row at all.

**The epoch, `WIRE_VERSION` and the high-water mark.** The epoch is
`f"{WIRE_VERSION}-{secrets.token_hex(8)}"`. A client whose epoch differs from the
server's wipes its mirror and pulls from zero, so the epoch changes exactly when
a seq stops meaning what a client's cursor assumes:

- **a new DB** (the `index_meta` row is created; existing artists are seeded
  `1..N` in key order on that first migration only);
- **a `WIRE_VERSION` bump** — the stored epoch's prefix no longer matches and is
  re-minted at the next open. `WIRE_VERSION` (currently `1`) covers the shape and
  meaning of a wire row, `_index_row_to_wire` **including the code-derived
  `effective_type`**: a change to `_effective_release_type` must bump it, or every
  mirror keeps rows classified under the old rule forever. It is unrelated to
  `INDEX_SCAN_VERSION`, which gates the *matcher* and forces MusicBrainz rescans;
- **the DB went backwards.** `synchronous=NORMAL` can lose the last commits to a
  power cut and a DB restored without its `-wal` rewinds; either way the counter
  would reissue values a client already holds for *different* changes, and the
  client would skip them. So the highest seq ever let out of the process is
  recorded in **`<index db>.hwm.json`** (`LIBRARY_INDEX_FILE + ".hwm.json"`, i.e.
  `/config/library_index.db.hwm.json`, shape `{"epoch", "hwm"}`) and
  `_index_check_hwm` rotates the epoch at the first open if the head is below it
  under the same epoch. It is its own file rather than a key in
  `lb_bot_state.json` (ruling R3): that file is rewritten whole by unrelated code on
  its own schedule, and this one must be durable *before* an answer goes out.
  Corollary for anyone restoring a backup: restore the DB and **leave the current
  `.hwm.json` in place**. Restoring the old HWM with the old DB disarms the check,
  and a client whose cursor sits between the two heads then silently skips the
  values the restored DB reissues.

The rule that makes the HWM worth anything: **a seq never leaves the process
before `_index_persist_hwm` has recorded it.** The three feed routes persist before
answering and answer `503 {"error": ...}` if the write fails
(`_index_persist_hwm_or_error`), which a client treats as busy and backs off with
its cursor untouched; the `index-push` sender persists before each POST and does
not send on a failure. `_index_persist_hwm` raises on purpose; only the boot-time
write inside `_index_check_hwm` swallows (ruling R12), because raising there would
fail `_index_db()` for whichever caller opened it first — possibly the
exception-swallowing present-mark. The rotation commit itself runs with
`synchronous=FULL` for that one commit so the new epoch is durable before the file
names it. Lock order is `_index_lock` then `_index_hwm_lock`, never the reverse.
A read-only `/config` therefore shows up as the two feed routes answering 503 and the
sender logging "could not persist the high-water mark" once — it then retries on its
own ladder, `INDEX_PUSH_PERSIST_BACKOFF_MIN` (2 s) doubling to `_MAX` (60 s), reset by
the first success, which it also logs once. It used to retry every 1 s poll with a
line each time. `/api/health` is deliberately *not* gated on the write (below). The
fix is the same chown as in § Runtime / Docker.

**The feed routes** (module-level views, unit-tested without Flask; thin wrappers
in `start_web_dashboard()`). No inbound auth, like every other `/api/*` route —
the hub is the only thing that reaches them.

- **`GET /api/index/changes?since=&epoch=`** (`_index_changes_view`): every artist
  and tombstone with `seq > since`, one `UNION ALL … ORDER BY seq`, each artist
  carrying all its rows in `_index_row_to_wire` shape. Pages are capped by
  **bytes** (`INDEX_CHANGES_MAX_BYTES`, 2 MB, under the hub's 4 MB
  `PROXY_MAX_RESPONSE`) and always carry at least one item. The envelope carries
  `scanVersion` and `ttlDays` so a client computes `stale` itself — nothing
  computed at read time goes on the wire as a fact. `artistCount` and `seqSum` are
  for the client's drift check. Head, items, count and sum are read in **one**
  `_index_lock` hold, which is what makes them one snapshot; a future second reader
  connection would need an explicit `BEGIN`. A wrong `epoch`, or `since > headSeq`,
  answers `{"resync": true, …}` with no items. A non-integer or negative `since` is
  400.
- **`GET /api/index/keys`** (`_index_keys_view`): every artist's `{key, seq}`,
  ~120 KB, for the drift repair. Tombstones are not listed — a tombstoned key is
  simply absent.
- **`GET /api/health`** (`_index_health_view`): `{"ok": true, "epoch", "headSeq"}`,
  nothing else — no Navidrome, no MusicBrainz, no summary. It is the hub's liveness
  probe; before it existed the hub probed `/api/summary`, the heaviest read in the
  module. (Not `_health_view`: that name is the SPA's unrelated
  `/api/system/health`.) **It never writes and never answers 503.** Its `headSeq`
  is the head already recorded in `.hwm.json` for the current epoch (0 before one
  is), never the live head, which may not be durable yet. It used to persist and
  503 like the feed routes, and the hub read that as lb-bot down — every client hid
  every lb-bot feature over a disk fault that affects only the mirror.

**The `index-push` thread** (`_index_push_worker`, `_index_push_tick` is the pure
throttle) polls the head every `INDEX_PUSH_POLL_SECS` (1 s) and POSTs `{seq,
epoch}` to `<LB_BOT_HUB_URL>/lb/index` with `LB_BOT_HUB_TOKEN`: leading edge, at most
one attempt per `INDEX_PUSH_MIN_GAP_SECS` (2 s), retried until a 2xx, and once at
boot even with nothing new. A trailing debounce would never fire during a steady
bulk build, which is why it is leading-edge. It is **not** `/lb/notify` — that
flushes the hub's library caches and makes both clients refetch their whole
library, every 2 s during a build — and deliberately **not** a second use of
`hub-fill-push`/`_push_worker`, whose behaviour the acquisition round depends on.
It shares only the `_push_enabled()` gate.

**The auto-index worker** (`_auto_index_worker`, thread `auto-index`). The mirrors
are only as complete as the index, and until this round the index grew only when
someone pressed "Build library index" or opened an artist page. The worker scans
one Navidrome artist per tick — no row first, then stubs, then stale rows
(older than `_auto_index_stale_after(key)`, or an older `INDEX_SCAN_VERSION`) oldest
first — through
the same `_index_store_artist` / `_index_store_unresolved_artist` the manual build
uses. It re-reads Navidrome's artist list every `AUTO_INDEX_DIFF_SECS` (15 min) and
right after a Navidrome scan finishes, which it detects by polling
`getScanStatus` every `AUTO_INDEX_SCAN_POLL_SECS` (60 s): lb-bot had no
scan-finished signal. Six things about it:

- **It yields MusicBrainz to every page.** The whole scan runs under
  `mbz_background()`; `mbz_get`'s network path takes `_mbz_turn()`, where a
  background caller waits while any interactive caller is queued and for
  `MBZ_BACKGROUND_GRACE` (2 s) after the last one finished. The worst a page waits
  is the one worker request already in flight.
- **Backoff.** A failed artist is retried after `AUTO_INDEX_FAIL_BACKOFF_BASE`
  (30 min) doubling to `AUTO_INDEX_FAIL_BACKOFF_MAX` (1 day), in memory. A
  `MusicBrainzUnavailable` also pauses the whole worker for
  `AUTO_INDEX_MB_OUTAGE_PAUSE` (= `MBZ_FAIL_COOLDOWN`, 300 s), since the next artist
  would fail the same way. **`MusicBrainzNoSuchEntity`** — a subclass `mbz_get`
  raises for a strict caller on a permanent 400/404/410, live or from the cached
  `{}` marker — is caught first and backs off *only that artist*: one bad tag mbid
  must not stall the worker (ruling R14). Every existing `except
  MusicBrainzUnavailable` still catches it. The name search is strict too, so an
  outage is never written down as "no such artist" (R13). An empty release list for
  an artist with a stored discography keeps the stored one and counts as a failure.
- **A manual build pauses it** — checked before every artist; the overlap is at
  most the one artist in progress.
- **It fills the review like a manual build** (ruling R36). `_auto_index_scan`
  calls `_union_review_groups` on the scan's gap groups, same as
  `_library_index_task`, so **the Fill-gaps review fills up on its own** as the
  worker walks the library. That is load-bearing: an `incomplete` row's `group_id`
  is the clients' `/lb/gap` handle, and a brief R32 that skipped the union left it
  answering 404 "Group not found" until a manual build. A group the user **hid**
  stays hidden (`_merge_review_groups` carries `hidden` across a rescan); one the
  user **removed** can come back on that artist's next rescan, as it always could
  with the manual build. The stale jitter below spreads those rescans, so the
  re-merges do not arrive as one monthly burst.
- **It is bounded, whatever the DB says** (final review C1). Two untagged artists
  whose name search resolves to the same mbid ("Beyonce"/"Beyoncé") used to take the
  one `nd_artist_id` column in turns: each scan made the other "missing" — always
  ranked first — so the pair was rescanned live against MusicBrainz forever and the
  head moved every tick. Now a resolved mbid whose row is fresh and held by
  **another** Navidrome artist still in the library is `covered`: nothing is
  written, and `state["covered"]` remembers the mapping so selection judges that
  artist by the shared row. And any id scanned in this process
  (`state["scanned"]`) is not picked again inside the TTL unless its tag mbid
  changed (which keeps R15's "a new tag rescans at once"). Staleness is **jittered
  per key** as well: `_auto_index_stale_after` is the TTL × (1 + (crc32(key) %
  1000) / 4000), 0–25 % longer and stable across restarts, so one bulk build does
  not age out on one day and rescan (and re-merge the review for) the whole
  library in a burst. Missing
  artists, stubs and a scan-version bump are not jittered, and the clients still
  compute `stale` at the plain TTL.
- It also deletes, with plain SQL so the triggers tombstone them, `nd:X` miss-stubs
  shadowed by an mbid row holding the same Navidrome id when every `present` row
  they carry is present there too (`_index_drop_shadowed_nd_stubs`), and it never
  writes an `nd:` row over another row claiming that id (`_index_store_artist(...,
  unless_claimed=True)`, checked inside the write's own lock hold).

`LB_BOT_AUTO_INDEX=0` turns it off (it returns at thread start with one log line;
also off with no Navidrome login). **`auto-index`, `index-push` and
`index-backfill-sweep` are started from `start_web_dashboard()`, so with
`LB_BOT_WEB=0` none of them runs** — a Telegram-only process has no index push and
no background indexing, whatever `LB_BOT_AUTO_INDEX` says.

**The read path is pure SQLite now.** `GET /api/artist/discography` no longer runs
`_index_backfill_present_album_ids` (it did, per read, against Navidrome's 300 s
album cache, on the lock the feed routes need). The backfill runs after
`_announce_album_indexed` for the placed release's artist, and every
`INDEX_BACKFILL_SWEEP_INTERVAL` (300 s) from the `index-backfill-sweep` thread for
anything else with an unresolved `present` row — except an artist it can never
match (no name and no Navidrome id, i.e. an adopted orphan stub, or no `artists`
row at all), which used to cost a walk of the whole Navidrome album index every
pass. Its writes are plain SQL, so they reach the mirrors like any other.

**A rescan never forgets a fill.** `_index_store_artist` carries every `present`
row across a rewrite: onto the scan's own row where the scan says `missing`, and
appended as it stands where the scan did not produce that release-group at all. The
second half is from the final review (M4): a miss-stub's refresh stores no
releases, so an album filled under an unresolvable artist was un-owned — and
offered for download again — on the stub's next refresh.

Scans got cheaper in the same round: a strict release-group browse bypasses a
positive `mbz_get` cache hit (`bypass_cache`), otherwise a TTL rescan re-read the
old answer forever; and `mbz_release_full` fetches `inc=release-groups+recordings`
once and caches a projection under each narrower key (the release-group key without
`media`, the tracklist key without `release-group`; the combined key itself is
dropped), halving the MusicBrainz cost of every title-matched owned album in a scan.
When both narrower keys are already cached it answers from them with no request at
all — asking for the combined key anyway cost one new request per owned album per
rescan.

### Why a playlist scan was taking 40 minutes

`group_missing_by_album` is O(tracks in the matched *releases*), not O(missing
tracks): 33 playlist tracks resolved to 16 albums totalling ~180 tracks. Measured
on the live stack 2026-09-22 it ran at **~13 s per track**. Two causes, both
there since the first commit, neither of them a rate limit:

1. **`nd_track_present` was evaluated twice for every track** — once to count how
   many are present, once to build the missing list. Same arguments, same answer.
2. **Every miss paid `_nd_search`'s 3 s warm-up retry, twice.** That retry exists
   because a warming or rebuilding Navidrome index answers nothing for
   everything. In a gap scan a miss is the *expected* answer, and a missing track
   misses both the MBID probe and the text probe — so 6 s per absent track,
   doubled by (1).

The tell is in the timings: releases where some tracks *were* present ran at
~10 s/track against ~13 s for the 0-present ones, because a hit returns before
the sleep. This is also why the scan feels fast on a playlist you mostly own and
crawls on one you do not — **the more gaps it finds, the slower it gets**.

Fixed by resolving presence once per track into a list the count and the missing
list both read, and by probing the index once up front (`_nd_index_is_warm`):
ask Navidrome for a random album it definitely has, then search for it by name.
A hit means the index is serving, so every later miss is real and the scan runs
`retry=False`; no hit means it is still building and the slow, correct path
stays. Per missing track: 8 Navidrome requests and 12 s of `sleep` become 2
requests and none.

The same tax was in `scan_user`'s own loop (`nd_has_track`) and in
`_check_missing` (the Spotify path), so both take the flag too. Measured end to
end on the live stack, one playlist, 96 tracks / 33 missing / 31 release groups:

| phase | before | after |
|---|---|---|
| `scan_user` track check | 213 s | 12.7 s |
| `group_missing_by_album` | 75+ min (killed unfinished) | 46.6 s |
| whole scan | never observed finishing | **60 s** |

**The probe term must come from the library.** The first version searched for
`"a"` and got zero hits against a perfectly warm index — Navidrome does not
match single-character queries — so it would have reported "cold" forever and
the fix would have been a silent no-op. Caught only by running it against the
live server.

**Don't reintroduce the retry on a bulk path.** A one-off lookup should keep it.

### What the web UI relies on (the 2026-09-26 audit round)

The SPA was restructured on branch `ui-polish` (see `README.md` § The React web
frontend for the screens, `SESSION-2026-09-26-ui-audit-polish.md` for the audit).
Most of the round was frontend, but it found several places where the backend
handed the UI a claim that wasn't true, and the fixes are rules now:

- **A group stuck on `downloaded` stops claiming to be in progress.** The
  `downloaded` bucket (files fetched, placement to come) mapped to the gap status
  `downloading` forever; on 2026-09-26 eighteen repair-origin groups had read
  "Working" for 7–55 days with nothing transferring. `_gap_status_for_group` now
  reports `failed` once `_downloaded_is_stale` (no track has *entered*
  `downloaded` for `DOWNLOADED_STALE_SECS`, 30 min), and `_gap_detail_view` sets
  `failReason: "stalled_placement"` + `stalledPlacement` — over any older error
  message, which is not why the album is stuck — so the UI offers
  Reconcile/Rescan, not "try the next source". **The clock is the tracks'
  `downloaded_at` and nothing else**, stamped by `_stamp_downloaded` only on the
  transition into `downloaded` (from `_set_review_track_state`, the repair
  projection and the reconcile pass). It first read the group's `updated_at`
  too, which Skip, Unhide, allow-MP3, a rescan, a source search and the Stuck
  card's own Reconcile all bump without placing anything, so a stuck album read
  "working" for another half hour after each. On a stalled group
  `_approve_pending_missing_tracks` re-approves the downloaded tracks, so the
  Stuck card's "download again" and "search again" can actually fetch; the
  fetch route answers 400 `nothing_to_fetch` when nothing is approvable rather
  than reporting every source as a peer rejection. `refresh_group_missing` is a
  no-op on a group with no `albums` (repair projections, playlist and Spotify
  groups) — it used to blank `canonical_mbid` and `missing_tracks` and mark them
  complete.
- **One definition of "has gaps"**: `_group_needs_attention(status)` — anything
  not `complete`. The Fill-gaps counts, the Library title (`libraryTotals`) and
  `/api/summary`'s `library.withGaps` used to answer three different ways
  (3103 / 3012 / 3121). `_library_gap_album_count` is the library-album count.
- **`/api/library` and `_needs_placement_view` read the list snapshot**, not
  `_review_snapshot()`: the latter deep-copies the whole review (~14 MB live) and
  `_needs_placement_view` runs inside `/api/summary`, i.e. every poll of every
  screen. `_review_list_snapshot` rows now also carry `source_count`, album ids,
  `canonical_mbid`/`release_mbid` and downloaded tracks' `downloaded_at` — none of
  which are `review_groups` columns, so `_GAP_LIST_GROUP_FIELDS` is unchanged.
- **List rows say whether a search has run** (`sourceCount` on `_album_view`). The
  rail said "source ready" for every album.
- **Hidden groups are listable** (`/api/gaps?hidden=1`, and `counts.hidden`), so a
  skip can be undone; the counts still describe the visible list.
- **Placement suggestions must share an album word.** `_suggest_review_group_for_folder`
  used to accept artist-only overlap ("Led Zeppelin IV" → *Coda*) and one shared
  word in a long folder name ("Speakerboxxx _ The Love Below" → *Love — Love*).
  It now requires an album-specific token and ≥50 % of the folder's own words,
  and returns `score` + `basis` (tags / folder name). `_suggestion_confidence`
  grades it `likely` (one-tap confirm) or `possible` (look first); every group
  suggestion used to be `likely`. **Only a full match on the files' tags is
  `likely`**; a folder-name match never is. Edition and format words
  (`_MATCH_NOISE_TOKENS`: "Deluxe Edition", "[FLAC 24bit 96kHz]", years) count
  neither for nor against. A **self-titled** group has no album-only word to
  insist on, so any word the source leaves unexplained marks the suggestion
  `ambiguous` → `possible` ("Led Zeppelin IV" is not the debut), and a tie goes
  to the group explaining more of the source's words. `POST
  /api/placements/<id>/confirm` takes the card's `groupId` (or `releaseMbid`);
  with neither it files only into a suggestion that grades `likely`, as the
  identity branch always did. The whole match is one-tap *library damage* when
  wrong: `_place` retags every file with the target release and files leftovers
  as bonus tracks.
- **An `mb:` scan never runs over an owned artist.** `POST /api/artist/discography`
  with `external` (or an `mb:` nd id) skips the library, so every release reads
  `missing` — and `_index_store_artist` rewrote the owned artist's own row that
  way when the mbid was theirs (reached from a search row, a pasted link, or a
  credit spelled differently from the tag). `_owned_artist_for_mbid` checks
  Navidrome's tags and then the index row, and the route scans as that library
  artist instead. The SPA also redirects an `mb:` route to the owned page when
  an owned artist carries that mbid. `name` is optional now: an `mb:` page has
  none, and posting the mbid in its place stored the UUID as the artist's name
  (mirrors included); the task looks the real one up.
- **Review merges keep user state and never double a row.** `_merge_review_groups`
  carries `_REVIEW_GROUP_CARRIED_FIELDS` (allow-MP3, the last no-source verdict)
  and `_REVIEW_TRACK_CARRIED_FIELDS` (manual picks, force-place state) across a
  rescan — the auto-index worker unions every artist it walks, so they used to
  last until that artist's next scan. `_replace_review_groups` drops a repair-job
  projection whose id (or identity) another origin still holds live; keeping it
  made two rows with one id. Both union and replace fold a second origin's row
  for the same album into the **richer** one (`_fold_by_identity`); rows naming
  no album at all (`_identity_key` == "") never fold together.
- **`GET /api/album/lookup?artist=&album=`** runs `_mbz_release_group_for` —
  fielded, quoted, free-text fallback, exact-title re-rank — for a caller that
  knows which half is which. The SPA used to hand-build the fielded query with no
  quoting, so '"Heroes"' or an edition suffix found nothing.
- **`/api/placements?all=1`** adds folders already filed or dismissed that are
  still on disk (`filed` says which). The old Import tab listed these beside the
  real queue without telling them apart.
- **`WEB_BUILD` is `LB_BOT_REVISION`**, baked in by `ARG LB_BOT_REVISION` in the
  Dockerfile. `deploy.sh dev` passes `<commit>[+dirty]-dev`; **CI does not yet**
  (the workflow lives in the publish clone — add `build-args: LB_BOT_REVISION=${{ github.sha }}`
  to `docker/build-push-action` there at the next release). Unset reads `unknown`.
  It was a hand-edited constant three months stale.
- **stdout and stderr reach the Logs view.** `_LogTee` (installed at the top of
  `main()`, never at import, so tests that capture stdout are unaffected) wraps
  both streams — stderr is where `traceback.print_exc()` and every `logging`
  handler write; werkzeug's per-request access lines are skipped. **Two rings**:
  printed lines go to `_stdout_events` (`WEB_LOG_MAX` = 1000), deliberate
  `_web_log` events to `_web_events` (`WEB_EVENTS_MAX` = 200), which error
  envelopes' `logTail` and `/api/status` read (`_web_log_events`); `/api/logs`
  merges them (`_log_ring_merged`). One shared ring let a single scan (a line
  per track) evict every deliberate event. The tee buffers the partial line
  **per thread** — `print` is two writes, and a shared buffer glued lines from
  two threads — and `_web_log` suppresses its own echo with a thread-local flag,
  not a `web: ` prefix match. Severity is keyword-derived on word boundaries,
  except for per-track progress lines ("Checking: Refused - New Noise" is not an
  error); the tag comes from the raw text, before redaction rewrites the host.
  The lock is an `RLock`: the SIGTERM handler prints on the main thread.
  **Everything printed now reaches a served endpoint**, so `_redact_secrets` also
  strips Subsonic's `u`/`t`/`s`/`p` query parameters — `requests` prints whole
  Navidrome URLs in its exceptions, and `t`+`s` replay as a login. A secret
  shorter than 12 characters is replaced only where it stands alone, so a
  password like "music" no longer rewrites every `/music/…` path.
- **`GET /api/system/status`** (`_system_status_view`): revision, uptime, what the
  auto-index worker and index-push sender last did (`_auto_index_public`,
  `_index_push_public` — each worker publishes a copy after its tick), AcoustID
  (key, fpcalc, rejected-peer count), Last.fm, Spotify, ListenBrainz playlists,
  wishlist and search timings. Read-only; no secrets, hub URL redacted.
  **`POST /api/acoustid/rejected/clear`** `{confirm: true}` empties `rejected_sources`.
- **`GET /api/fills?recent=1`** prepends the 20 most recently touched ledger rows,
  so the SPA can list album requests it didn't start.
- **`/api/library?sort=`** `artist | album | year | missing` (the table paginates
  server-side, so it sorts there too).
- **Plain route names for placement**: `/api/place-folder`, `/api/download-folders`,
  `/api/place-folder/candidates` alias the `/api/beets/*` routes (beets is not
  involved). `/api/place-folder` with only `group_id` resolves the release from the group.
- **`_settings_cards` matches failed checks by exact label** — "Library path ↔
  Navidrome" failing used to flag the Navidrome credentials card too — and no
  longer serves the stale `docker run` example.
- **`_deezer_get` raises `DeezerError` for transport failures too.** A read
  timeout escaped as a bare `requests` exception, which no caller catches, so
  `/api/artist/related` answered 500 whenever Deezer was slow.

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
