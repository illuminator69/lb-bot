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

**Test baseline:** **32 errors, 0 failures** (319 tests as of 2026-09-23 — the total drifts as
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
