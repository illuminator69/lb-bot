# CLAUDE.md — lb-bot

Context for working on this repo with Claude Code. Read this before touching the
gap-fill pipeline.

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

**Test baseline:** **0 errors, 0 failures** (524 tests measured 2026-09-27 — the total drifts as tests are
added, so check the 0/0, not the count). Q-002 deleted the 32 stale beets tests that used to error (every one an
`AttributeError` for a function removed with beets, see Decision below); a branch cut before Q-002 still shows
those 32 errors. Anything failing on a branch that has Q-002 is yours.

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
| `LB_BOT_SAME_TRACK_LENGTH_SEC` | `7` | how many whole seconds a file's own length may be from the track's stated one for audio identified as **another recording of the same track** to be placed rather than refused (`SAME_TRACK_LENGTH_SEC`, § Acquisition reliability item 3). Not `DURATION_MATCH_SEC`, which is evidence for a pairing |
| `LB_BOT_WISHLIST_INTERVAL` | `21600` (6 h) | how often the wishlist sweep wakes |
| `LB_BOT_WISHLIST_COOLDOWN` | `43200` (12 h) | how long before one wishlist row is re-searched |
| `LB_BOT_SOURCE_FAILOVER_DEADLINE` | `45` | wall-clock ceiling on the fetch route's walk down the ranked source list |
| `LB_BOT_LISTENS_SYNC` | `1` (on) | the listens mirror (§ The listens mirror). `0`: the `listens-sync` worker does not start, `status.state` reads `off` and `artistPlays` is 0, whatever `LISTENBRAINZ_USER` says. What is already in `listens.db` is left there, and the summary routes still read it. The switch for a first live run |
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
  `web/node_modules`, `.git`, design bundles); with it, ~375 kB. `test_fixtures/`
  (the suite's recorded slskd answers) is in it for the same reason.
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
3. **An ordinary read is fail-soft, and mind the per-key failure memory.** "No
   Wikipedia article" is a legitimate answer and a blank result must not raise.
   But `mbz_get` caches failures against the exact `path?params` key, so
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
5. **A refresh must never destroy a good About** (I-020, 2026-10-04). With
   `refresh=1` the fail-soft legs built an About out of the `{}` a source
   answers when it did *not* answer, wrote it over the stored entry and said
   200 `found: false` — which the hub kept for six hours and both clients,
   which have a "Refresh About" button since this round, showed in place of
   what they had. On a refresh every leg is asked **strictly**
   (`_meta_mbz_get(..., strict)`, `_wiki_get(..., strict=True)`): the url-rels,
   Wikidata, Wikipedia, the members (`artist-rels`), and for an album the
   release list and the credits. One that did not answer raises
   `MetaUnavailable`; the views (`_meta_artist_view`, `_meta_album_view`)
   answer **502 `{"error", "retryable": true}`** and **nothing is stored**.
   `found: false` is stored and answered only when the sources answered and had
   nothing — "MusicBrainz has no such entity" (`MusicBrainzNoSuchEntity`) and
   Wikipedia's "no such page" are answers. A Wikimedia failure is an exception,
   a body that is not a non-empty object, or the API's own `{"error": …}` under
   a 200 (`maxlag`), and is never cached. A refresh by `name` that MusicBrainz
   refuses as a query (`MusicBrainzNoSuchEntity`, cached for good) is likewise
   an answer: 200 `found: false`, nothing touched — caught as an outage it was
   a 502 for good. The strict MusicBrainz call asks through the failure
   cooldown and retries after 3 s and 10 s: about 13 s per leg to give up
   against a 503, about 43 s against a timeout. The hub proxies `/lb/meta/*`
   on its 45 s timeout (not the 20 s of the ordinary routes), so a refresh
   during a timing-out outage can still outlast it: the client gets the hub's
   error, and the stored entry is just as untouched. The ordinary
   read is unchanged, **including that it stores an empty About when a source
   happens to be down** (negative TTL, 7 days) — that is where the refresh
   button earns its keep, and it is why the refresh had to be made safe.

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
- it dropped the **review linkage**, so a switched album downloaded
  correctly while every track sat on the `failed` the poller had just written.

On exhaustion it pops the group and prompts but records nothing in the fill
ledger, and its `switching` flag is still set — so neither branch below it would
run and the fill would never report failure, which leaves every client polling
`downloading` forever with no retry to offer. The watchdog records that failure
explicitly.

**What switches an album is its source having given the group nothing — and
that is read from the listing in hand, not from the group's counter** (B-084,
H1, 2026-10-11). The poller's failed branch tested `ag["completed"] == 0`, and
the counter lags the listing by the loop's order. The first real download on
the build: the clicked folder listed eleven files, the peer refused six at once
("File not shared") and served the five that were the EP; in the poller's
first listing a refused file came ahead of the finished ones, "nothing has
downloaded yet, the source is bad" switched the album, the five landed files
were let go of and the EP was downloaded a second time from the next source.
With no alternate listed the branch below would have failed the whole fill.
`_source_has_given` (in `_poll_downloads_once`) is both tests now — the switch,
and the abandon below it:

- **Landed.** Another transfer of the same group that this listing shows
  `Succeeded` counts as arrived, whether or not the loop has reached it — for
  **every** failure state.
- **Held by the peer — for a refusal only.** When the failed transfer is
  `Rejected` (the peer's own answer about that one file), another transfer of
  the group **from that same peer** (H1a) in a state that is neither failed nor
  finished counts too: the peer is serving the group and has refused this
  file. Another peer's transfer in the group — a gap fill's single-file retry
  from an alternate — vouches for nobody but itself.
- Then the per-file branch runs as it always did: a transfer that names its
  track is asked of an alternate (`_retry_file_from_alt_source`), one that names
  none — every file of a whole-album download — is counted lost, and the group
  finalizes when `completed + failed` reaches `total`.
- **Unchanged:** every file refused → switched, or failed where there is no
  alternate; a transfer the watchdog cancelled, or one that errored, timed out
  or was aborted, with nothing landed in the listing → switched **whatever the
  peer still holds in its queue** (`QUEUE_TIMEOUT` is 900 s: counting a queue as
  life there would hold a stalled source for a quarter of an hour). A transfer
  slskd's listing does not show, and another group's, count for nothing.
- **Cost:** a peer that takes some of an album's files and refuses the rest (a
  per-user queue limit) gets the album placed short — or, under half, refused
  by item 11's C4 — where the source used to be switched *if* the refusal was
  seen before the first file finished, which was a matter of which second the
  poll fell on. A refused file of a whole-album download is not asked of
  another peer: its transfer names no track (registered, not built).

**A group is not judged while its files are still being asked for** (B-084,
H7). `slskd_enqueue_folder` registers its group and then asks for the files
one POST at a time, and since H3 the poller's first look at a new group comes
within seconds — inside that loop. A refusal of the files asked so far read
as "the source has given nothing", because the rest were not registered yet:
the album was switched, or the fill failed, while the loop went on asking the
first source into the same group, and those late transfers were then counted
as the alternate's.

- **The enqueue marks its group while it asks**: `asking_at`, stamped before
  each POST and removed in a `finally` when the loop ends. `_load_state` drops
  it — no asker survives a restart.
- **A poll during the asking leaves that group's failed transfers where they
  are** (`_being_asked_for`, at the top of the failed branch): nothing is
  popped, counted, switched, failed over or written for them, and the next
  poll — the loop done, every file registered — judges them as above. A stamp
  older than `ENQUEUE_ASKING_SECS` (60 s, twice the POST's own timeout) is an
  asker that died: the group is then judged like any other. The success branch
  is untouched — a file that lands during the asking is counted as always — and
  so is the switch's own loop, which the poller awaits (no poll falls in it).
- **The loop stops asking once the group is no longer its own**: gone from
  `pending_album_groups` under that id, cancelled (as before), or with another
  `source_user` than the peer it asks. For a group that is gone or has moved
  on, what this peer was asked for under the id is **let go of** — detached,
  and cancelled in slskd off the asking thread — because nothing would own it,
  or it would be counted for the source the group has now; a refusal of the
  POST that was out is not counted on the group, and the group is not popped
  for "this source took nothing". A cancel's letting go stays its callers'.
- **Costs:** a refusal is acted on one poll later, after the last file has
  been asked for (seconds); an asker that hangs holds its group's refusals for
  up to a minute. **Known and left:** past that minute the group can be
  switched or failed under a loop that is still alive; the loop then stops and
  lets go, but its caller still reads "N files queued from the first source"
  and writes that on the fill's row; and a switch to *another folder of the
  same peer* is not seen by the loop at all.

**3. AcoustID/Chromaprint verification** (`ACOUSTID_API_KEY` + the `fpcalc`
binary, `libchromaprint-tools` in the Dockerfile). This is the one identity
check nothing else here can make. `_audio_signature`'s md5 is FLAC StreamInfo's
hash of the *unencoded* audio: it proves two files are the same **encode** of
the same master and says nothing at all about a different **recording** — a live
take, a radio edit, a cover or simply the wrong track under the right filename
all have perfectly good, perfectly different md5s and sail through.

It hooks into `_reject_reason` inside `_deterministic_album_import`, for a
slotted file that is **not a hand pairing**: a hand pairing (`manual_pairs`)
is asked neither identity check, the fingerprint included. (This paragraph
used to say the fingerprint refusal survives a hand pick; the call has always
sat under `if slot and not manual` — Q-059.)

**What "a different recording" may refuse** (B-086 / B-087, 2026-10-11). Two
gap fills were refused although the tracks were right. Kenny Beats – LOUIE:
AcoustID confirmed all fifteen files and the id-tag check then refused them,
because the peer's `musicbrainz_trackid` tags hold *Spotify ids* and any
non-empty string was an id. King Crimson – Beat: the folder was the 2016
remix, which MusicBrainz lists as recordings of their own; AcoustID said so,
correctly, and "the slot's id is not among the answers" refused it like a
wrong song, with a row that said only "it is not e063bd58-…". The rule now:

- **A tag is a MusicBrainz id only when it is one** (J1, `_real_mbids`):
  `_tag_recording_mbids`, and the signature cache's read of what an older
  build stored, keep **every UUID a value holds**, lower-cased (J18). J1
  first asked the whole value to be one UUID, which also made a real id
  written another way no id: `https://musicbrainz.org/recording/<uuid>`, or
  two ids in one value, "carried none" — not paired by its id, and nothing
  for the tag check to contradict. A UUID glued to more hex digits or dashes
  is part of some other token and is not taken; a value with no UUID in it
  (a Spotify id) is still no id. **Where such an id
  meets an id taken verbatim from somewhere else, both sides are compared
  lower-cased** (J11): the import's id tier (`_match_slot`) and the album
  override's mapping (`_map_files_to_slots`) against the slot's id, the
  duplicate report's grouping (`_duplicate_file_sets`) against the
  `musicBrainzId` Navidrome reports for a listed song. `_last_copy_refusal`
  compares two files' own ids, both read this way. Two files that share a
  value which is no id no longer match "by id": the delete guard then
  refuses where it allowed, and the title key still groups them. The
  import's id tier is pinned (J12): a file whose tags carry the slot's
  recording id is that slot's whatever its name and title say, and a value
  that is no id pairs a file exactly as no id tag does.
- **The fingerprint gives one of four verdicts** (J2, `_acoustid_verdict`;
  the lookup is `_acoustid_named`, one request per file per import,
  `meta="recordings"`): `none` — every "no opinion" there was; `confirmed`
  — the slot's id is among the recordings named; `same_track`; `different`
  — something was named and it is neither. It carries what was named (id,
  title, duration, artist names, score).
- **`same_track`: the same track in another recording is placed** (J3,
  `_same_track_recording`) — all of: a named recording has the slot's title
  (`_match_key` equal); *that same recording's* artist is the album's, by
  the rule an album-artist tag is held to (`_album_artist_folds`, D7c) —
  the slot's own artist counts where it states one (no slot does today:
  the tracklist is read without artist credits), and on a Various Artists
  album whose slot states none the artist is not held against it; and the
  file's own length is within `SAME_TRACK_LENGTH_SEC` whole seconds of the
  slot's stated one. **7**, from `LB_BOT_SAME_TRACK_LENGTH_SEC` — not
  `DURATION_MATCH_SEC` (3), which is evidence *for* a pairing; here the
  title, the artist and the fingerprint agree and the length is a veto
  against another version. The 2016 mixes are 3, 5, 2, 1 and 4 s from the
  1982 lengths; the other-version cases on record are 23, 66 and 345 s
  away. A recording with no title or no artist, and a slot with no stated
  length, are never this. **It is read from the best answer only** (J16,
  `_same_track_among`): a named recording can be this only when its score
  is the best score named — the best answer another song and a weaker one
  a same-titled recording by the album's artist is `different`. (`confirmed`
  reads every answer at or above the minimum, as it always has; all five
  Beat files have theirs in the best result.) **Costs:** a demo, a live
  take or an edit that MusicBrainz titles like the track — **or a different
  song of that title** (two "Intro"s: nothing the rule has can tell them
  apart, and the row then says "another recording of “Intro”") — by the
  same artist, within 7 s of it, is placed unattended (and said); a
  same-track link AcoustID has only on a weaker cluster is refused, with
  the override; a different mix lands beside tracks of
  the original mix; an album credited to a name the recording's artist
  does not fold to (another script) is refused, with the override. **On a
  Various Artists album `same_track` asks no artist at all**: no slot
  states one (the tracklist is read without credits), so a recording by
  anybody that has the track's title, in a file within 7 s of its length,
  is placed — and said.
- **What outranks what** (J4), in `_reject_reason`: (1) the fingerprint —
  `different` refuses and is remembered against the peer; (2) "this audio
  is already in the album", unchanged; (3) **the id-tag check, the
  fingerprint's junior**: asked when the verdict is `none`, and when it is
  `confirmed` *but AcoustID named the tag's own recording too* (it has not
  chosen between them) **or the file is known not to be as long as the
  slot** (J17, `_another_length_than_the_slot`: both lengths stated, more
  than the same 7 s apart — a confirmed fingerprint has no length veto of
  its own, and a file 66 s longer than its track, tagged as another
  recording AcoustID did not name, was filed with nothing said. "Known": a
  slot with no stated length is not another length, so a release that
  states none does not turn every confirmed file with a merged id into a
  refusal). `confirmed` without either, and `same_track`, skip
  it: the audio has spoken. When it runs — the file carries ids, none the
  slot's — the file is still this track when its title tag is the slot's
  title and it is as long as the slot (the same 7 s): which is what a file
  tagged with an id MusicBrainz has since *merged* looks like, with no
  request made to find out. No artist is asked there — a namesake's track
  of that title and length, carrying its own id, passes (the second
  placement that asks no artist, beside a Various Artists album's
  `same_track`); (4) the title-tag check, unchanged.
- **A refusal says what the audio is, and can be overruled** (J5). The text
  names what was found (`_heard_refusal_text`: the best-scored recording
  AcoustID named, of several the one closest to the slot's title, its
  artist, and **the file's own length** — J13: the number the veto
  compared; AcoustID's stated one only where the file's could not be read —
  beside the slot's own title and length, with the confidence;
  `_tag_refusal_text`: what the tags say and the two lengths) —
  `m:ss`, at most 300 characters, through `_strip_host_paths`, an id only
  shortened and last. **The kind travels with the refusal**
  (`REFUSAL_DUPLICATE` / `REFUSAL_RECORDING`: `refusal_kind` on the
  `per_file` row, `kind` in `_placement_outcomes`) and **one function turns
  it into the offer** (`_note_force_place_offer`; no reading of the
  wording): "Place anyway" on a row refused as `recording`, beside the one
  case it was offered for (a hand-picked `duplicate`). The row keeps
  `force_place_kind`; the gap track row carries `forcePlaceKind`
  (`"duplicate"`, `"recording"`, `""`). **Pressing it overrules the refusal
  the row shows and no other** (`_place_track_anyway`): the file is placed
  as a hand pairing — so nothing is fingerprinted or looked up, under
  `_review_lock` — with the "already in the album" guard still on for a
  `recording` refusal; if that guard refuses, the row shows that refusal
  and its own offer. **It judges the row it was pressed on and no other**
  (J10), for both kinds: placed → `placed`, `imported_at`, the verifier and
  the scan, as `_place_group_downloads` leaves a row it placed; every other
  row of the group — one whose file waits for its own placement, one with
  an offer of its own — is left exactly as it was, and no album group is
  popped. (Until then it ran `_mark_group_tracks_placed` on the one-file
  import's result: every other `downloaded` row was failed, "no downloaded
  file matched", its offer gone.) **An override names the row it was shown
  for** (J19): the route takes a row's *index*, many more rows carry an
  offer now, and a rescan between the view and the press can put another
  offered row at that index. `POST …/tracks/<n>/place-anyway` takes an
  optional `{"recordingMbid", "title"}` — what the gap view shows for the
  row; when either is sent and the row at that index is not the one shown
  (`_row_is_the_one_shown`: ids in one case, titles by `_match_key`,
  against every tracklist row the view draws for it — `_gap_view_rows`, the
  view's own pairing, under which a missing row can appear with a library
  track's id — and against what the row carries itself) the answer is
  **409** `{"ok": false, "error": "This album's tracks were read again
  since the page was drawn — open it again.", "code": "row_moved"}`, with
  nothing placed and no row touched. A client that sends neither is
  answered by the index alone, as before; the web UI sends both
  (`placeAnywayBody`) and shows the 409's sentence. The other route that
  takes a row's index (`pick-file`) still goes by the index alone.
  Only lb-bot's own web UI has the button, on
  a gap's track row — in the queue under Needs you and on the album's own
  page (`TrackRow` in `GapDetail.jsx`; `web/src/lib/placeAnyway.js` words
  its confirmation by the kind, `node --test` matrix beside it); the hub
  proxies no such route.
- **A placement of another recording is said** (J6): one `_web_log` line
  per file (artist, album, track, what the audio was identified as, both
  lengths), `placement_note` on the review row — `placementNote` on the gap
  track row, shown muted under the track, also once the library lists it and
  the album page draws it as a plain row of the tracklist — dropped when the
  row is fetched, approved or overridden again and carried across a rescan;
  a `note` on the `per_file` row of a fill with no review group.
- **The peer memory follows the verdict** (J7): written for `different`
  only, and **taken back when the file is placed after all**
  (`_forgive_source_file`, in `_place`) — by the file's origin where
  `_download_origin` still has it, else by the entry's `expected` being the
  slot's recording and its file name's last component being the placed
  file's name (or slskd's renamed form of it).
- **Rows refused under the old rules were looked at again, once** (J8,
  `_recheck_old_recording_refusals`, the `migrations` row
  `b087_recheck_recording_refusals`): at the first start of this build, in
  every visible album-gap group, a `failed` or `downloaded` row whose error
  is one of the two old refusals and whose file is still on disk is either
  **set back** — to `downloaded`, with no placement miss — or **offered**
  (kind `recording`), and never both. **It is set back only if the
  unattended pass will judge it** (J15), asked of the pass's own tests,
  shared with it and not copied: `_unattended_group` (an album-gap group,
  not hidden, no `cancelled` row in it) and `_unattended_skips` (a row
  with its file's path, stamped `downloaded_at`, not reconcile-linked, and
  not a non-picked row of a group matched by hand) — the same two
  `_stranded_download_groups` and `_place_group_downloads` read. What only
  the pass can find out at each start (a live transfer, a verifier still
  watching, Navidrome's answer) is not asked. A hand pick is not set back
  either: the pass would file it as a hand pairing, asking neither identity
  check. **Every other such row keeps its text and gets the offer** — the
  unstamped one, the reconcile-linked one, the row of a hand-matched group,
  every row of a group holding a `cancelled` row. **A row that is set back
  keeps its old text until something judges it**: the placement that takes
  it clears it or writes the new refusal, so a group this start's pass
  cannot take still says why, and the next start's pass judges it. The log
  line counts the two classes as what they are. (Until J15 the test was
  `downloaded_at` alone: rows the pass leaves were set back, lost their
  text, got no offer, were counted "looked at again" — and J8 never runs
  twice.) **Cost:** a file refused days ago is filed at that start with
  nobody looking, by the rules a download finishing now is filed by.

**Silence is never evidence.** No key, no `fpcalc`, an unknown fingerprint, a
down AcoustID — every one returns "no opinion" (the verdict `none`) and
placement proceeds exactly as before. Only a positive contradiction rejects,
and only above `ACOUSTID_MIN_SCORE`. A guard that turns a working fill into a no-op because a
binary is missing is worse than no guard.

**The fingerprint is asked of `fpcalc` by name** (`_acoustid_fingerprint`,
B-062, 2026-10-07). pyacoustid chooses its own backend: the in-process one
(audioread plus the chromaprint library) whenever both import, `fpcalc` only
otherwise. In the image both import — `libchromaprint-tools` brings the library
with the binary — and audioread has nothing to decode with (no ffmpeg), so
every FLAC failed with "audio could not be decoded" and was never tried with
`fpcalc`. From the day the key was set until this fix the check therefore
answered "no opinion" for every file, which is exactly what silence is allowed
to look like. `fingerprint_file(path, force_fpcalc=True)` first; the library's
own choice is the fallback for a box with a decoder and no binary. The
`unavailable` note on `/api/system/status` is cleared by the next fingerprint
that works. **Read-only dry run before it went live** (110 library files, 90
albums, each checked against its own recording tag): 100 confirmed, 10 no
opinion, 0 refused — the sample is the evidence that strict "expected
recording among AcoustID's answers" does not refuse good files here; a
recording MusicBrainz lists twice is the case that could.

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

**A cancel is a verdict on the fill it stopped, and a new gap fill lifts it** (B-068,
2026-10-09). A Fill-gaps fill never begins a ledger row, so the `cancelled` row a gap
cancel wrote stayed on the release: the next fetch of the same gap downloaded, reached
`_finalize_group`, read "cancelled — not placing" and left every file in /downloads,
every row `downloaded`. It lasted until the row was evicted or a restart a day later.

- **`_album_fill_lift_cancel(release)` removes a `cancelled` row** — and only a
  cancelled one — when a new gap fill starts. It is called **only by the code that
  starts one**: `_enqueue_group_source_inner`'s album path once slskd has taken a file
  (its leftover rescue runs after that), and `_pick_file_for_track` once slskd has
  taken the pick. **Never the poller, `_finalize_group` or a sweep**: they act for a
  fill already running, and for them `_album_fill_set` still refuses to write over
  `cancelled`. The new fill then runs with no row, as every gap fill always has; a
  cancel that arrives during it cancels it, exactly as before.
- **Why a lift and not a begin.** `_album_fill_begin` is an album download's: it
  writes artist and album, which arms the one automatic retry
  (`_album_download_task`, the *whole album*), and it leaves a `searching` row that
  `_sweep_album_fill_zombies` fails after 135 s when no album group stands behind it
  — which a hand pick and a rescue never have.
- **Known cost, accepted:** what the removed row remembered about the release goes
  with it — `attempts`, `excludedUsers`, `lastSource`, `rgid`, `quality`. The release
  key reads `unknown` (or `downloading`, from the live group) afterwards; both clients
  watch a gap fill by its group id, not by the release.
- **What belonged to the cancelled fill still cannot place anything**, and three
  things see to it — none of them the ledger row, which a new fill lifts:
  1. Its album group is gone.
  2. Its single transfers — a rescue outlives `_cancel_album_fill`, which detaches
     only the album group's own — are **marked on the transfer**
     (`info["fill_cancelled"]`, the cancel's own reason text;
     `_mark_group_singles_cancelled`). When a marked transfer ends, the poller writes
     its row **`cancelled`** with that text — what `_gap_cancel` leaves for the same
     transfer — and does not fail it over to another peer (the failover enqueue finds
     the row through its `track` copy, so the next peer's file would be an unmarked
     transfer for the same row). The file stays in /downloads and its path on the
     row, for Placement. It must not read `downloaded`: a `downloaded` row is what the
     next fill's finalize imports and what the next fetch does not ask for again.
     `_gap_cancel` needs no mark: it detaches the singles with the rest.
  3. **The rows whose file had already landed are settled `cancelled` too**
     (`_landed_row_entries`, through `_mark_detached_entries_cancelled` and its
     rules), by both cancel routes and the slskd-side branches that go through
     `_cancel_album_fill`. Left `downloaded`, "cancel the wrong source, fetch a better
     one" imported the wrong source's files at the next finalize and never asked the
     better one for those tracks. Not a row with a current placement miss (a refused
     file keeps its reason and its "Place anyway"), not a settled one, and not a
     Loose-tracks group's. **And not while a placement of that review group is
     running** — next bullet.
- **Placement claims the review group's rows too, and the cancel finds out.** The
  ledger's `placing` settles "who moved first" for the album key; the landed rows
  needed the same. `_finalize_group` pops its album group *first* and then imports
  (AcoustID makes that long), and `_mark_group_tracks_placed` advances only rows
  that still read `downloaded` — so a gap cancel pressed during the import, finding
  no group, settled those rows `cancelled` while their files were being moved: in
  the library, reading cancelled, no verifier, re-downloaded by the next fetch.
  `_placing_groups` is the review groups a placement owns right now: the finalize
  from the moment it takes its group (`_take_group_for_finalize`) to the end of
  `_finalize_group`, whatever the exit; every other placement for the length of its
  `with _group_placement(group_id):` block. **The finalize's pop-and-claim, and each
  cancel path's pop-and-read, are one hold of `_placing_cond`**, so exactly one of the two gets
  an album group and the cancel knows which: a group the cancel found was not taken
  for placement; one it did not find may be being placed, and then
  `_landed_row_entries` is not asked. A row that reads `cancelled` therefore still
  means "not placed" — `_mark_group_tracks_placed` was deliberately not taught to
  resurrect one. What the routes answer is unchanged (`/api/album/cancel`:
  `cancelled: false` with state `placing`). The registry also replaces the helper's
  old process-wide lock: a second placement of the *same* group waits its turn (the
  finalize by leaving its group in the table, still cancellable), and other groups
  no longer wait for it. `_placing_cond` is a leaf lock.
- **Every caller of `_deterministic_album_import` that passes a review group id
  holds that group's claim** — from before the import can move a file until the
  group's rows are written — the placements a user starts by hand included: the
  Place task (`_deterministic_import_task`), the two download-match routes, the three
  `/api/imports/<id>/*retry` routes, "Place anyway", and a Loose-tracks row's
  auto-placement, beside the poller's and the startup pass's `_place_group_downloads`
  and the finalize. One context manager, `_group_placement`; an empty id claims
  nothing, so a caller passes whatever id it has ("Pick the release" with no group,
  an artist-page album, the Telegram re-import workers: none). Three things to know
  before adding a caller:
  - **Never wait for the claim under `_review_lock`.** The placement that holds it
    needs that lock to write its rows. `_place_track_anyway` imports under the lock,
    so its route claims first: `with _group_placement(group_id), _review_lock:` — in
    that order. The claim is not re-entrant; nothing inside a claimed stretch claims
    again.
  - **A hand placement now waits for a placement of the same group already running**
    (a finalize's import, say) instead of importing the same folder beside it; the
    request or the task stays open that long. A different group never waits.
  - **The tests hold a new caller to it, two ways.** `test_album_review.py` swaps the
    import for a checked one at the top of the file: any test that reaches an import
    naming a group with no claim held fails with that message — so a test calling the
    import directly claims like anyone else. And `PlacementClaimCensusTests` reads
    the module's syntax tree: every use of the import must be a call (or a
    `to_thread` hand-over), one that passes a group id must be listed and sit
    *inside* a `with _group_placement(...)` block, one that passes none must be
    listed as such. A new caller fails there until it is listed.
  - Still as built (ruled 2026-10-09): while a placement owns a group, a cancel
    settles **none** of that group's landed rows, the ones that placement is not
    touching included (a Place task on one folder, a stranded `downloaded` row in
    another). Such a row stays `downloaded`.
- **A cancel can land while the fetch is still queueing**, and then it sees only
  what is registered at that instant. The fetch runs inside the route's
  `_review_lock` hold — the album enqueue (the group is registered *before* its
  per-file POSTs) and then a rescue walking up to six alternates — and neither cancel
  route takes that lock before it pops the group. So `slskd_enqueue_folder` hands
  the group's dict out (`group_out`) and stops asking once it reads `cancelled`, and
  `_enqueue_group_source_inner` checks it after the album enqueue and after the
  rescue (which is told to stop, `stop=`): `_enqueue_cancelled_while_queueing`
  detaches and cancels the group's transfers registered under the popped id, marks
  the review group's singles, lifts nothing, and returns `ok` + `cancelled` so that
  neither the route nor `_gap_auto_task` walks on to the next source. The dict, not
  "the id is gone from the table": a group whose source failed is gone too, and its
  rescue must still be placed.
- **A review group's fill never arms the automatic retry.** `_album_fill_fail` takes
  `review_group_id` from every caller that fails a transfer group (`_finalize_group`,
  the poller's two source-failed branches) and does not schedule the retry for one.
  It used to arm whenever the release's row named an artist and an album, which a row
  left by an earlier artist-page download always does — so a gap fill failing over
  such a row re-downloaded the whole album 45 s later.

`api_gap_cancel` mirrors all of this (`_gap_cancel`); it used to cancel by filename
with a slskd listing per file, never pop the album group and write nothing, so the
orphan sweep finalized — and placed — it minutes later. A cancel inside the 45 s
auto-retry back-off is honoured too (`retryAt` on the row is what makes the row
cancellable in that window; the retry worker checks the state before it fires).
A user cancelling ONE file in slskd's UI no longer cancels the album: that file is
counted lost with no failover, and the album is cancelled only when nothing has
landed and every remaining file is Cancelled in the same listing.

**Every cancel path marks the gap's review tracks, not just `_gap_cancel`** (B-021).
`_mark_detached_entries_cancelled(entries, reason)` is the shared helper — every entry
a cancel detaches carries its own `review_group_id`/`review_track_index` from enqueue
(and `review_track_key`, so a rescan between the detach and the mark cannot move the
mark onto a neighbour), so the helper marks each behind track `cancelled` (grouped by
group id, one `_push_gap`
per affected group) and skips a track that already reads a settled decision
(`placed`/`verified`/`skipped`/`filed_extra`) instead of dragging it back. `_gap_cancel`
uses it unchanged; `_cancel_album_fill` (`/api/album/cancel`, the vanished-transfer
sweep, the "every file Cancelled in slskd" branch) and `/api/downloads/cancel` now call
it too, with the caller's own reason text. `_cancel_album_fill` marks **from the
detached `entries`, not from the ledger CAS's `applied` result** — the detach already
happened by the time the CAS can lose (row already `placing`/`placed`/`verified`/
`needs_match`), so those transfers are gone either way and their still-open tracks must
not keep reading `downloading`.

**5. Honest status, one clock, and a push.** Every counter a client reads off
`/api/album/status` moves on a poller tick, so `DOWNLOAD_POLL_INT = 60` made the
status up to a minute stale by construction while the bot's own SPA read slskd
through a 3 s cache and looked live. The poller is adaptive now
(`DOWNLOAD_POLL_ACTIVE_INT`, 5 s while any group or transfer is live, 60 s idle):
one slskd listing per tick whatever the number of clients or rows watching, and
the request path never touches slskd. **Idle, the wait ends as soon as a group
or a transfer exists** (B-084, H3, `_wait_for_the_next_poll`): the loop used to
decide how long to sleep before it slept, so a download queued a second into an
idle minute was not looked at for the rest of it — its "0 of N" stood that
long. The look is at the two tables in memory, every active interval; with
nothing live the poll still comes once a minute and asks slskd nothing more.
What the view reports changed with it:

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
  first failure ever — and is never armed for a review group's fill (item 4), nor
  for a fill that began with a click on a source (item 11). A new
  fill drops every per-fill field
  (`_ALBUM_FILL_PER_FILL_FIELDS`) instead of merging over the old row.
- `_sweep_album_fill_zombies` runs each tick: `searching`/`queued` with no group and
  no task past `SEARCH_TIMEOUT + 60 s` → `transfer_failed`; `placing` with no group
  past 15 min → `placement_failed`; `placed` past the verifier's deadline →
  `verifyGaveUp`. The restore docstring used to promise this and nothing did it.
- Gap-fill rows reach `verified` (`_album_fill_mark_group_verified` from the
  review-group verifier); they used to sit on `placed` forever.
- The review-group verifier's deadline is **per row**
  (`_placement_verify_deadline`: `PLACEMENT_VERIFY_TIMEOUT` after the row's
  `imported_at`, never sooner than that after the worker started) — one worker
  runs per group and a later placement joins it, so a row placed late used to be
  reset to `pending` seconds after landing (B-025). For "Loose tracks" rows
  (B-017 auto-placement) the linker records the slot the file went into
  (`placed_title`, `placed_recording_mbid` — the import rewrote the file's tags
  to those) and the verifier searches for that, not the playlist's spelling;
  each matched loose song's own album is announced (`albumIndexed` with the
  Navidrome song's artist/album), never one "Loose tracks" notice. A loose file
  filed as an extra track — it matched no slot, beside files that showed the
  album, and its own tags name that album (item 11) — settles its row as
  `filed_extra` (wire `done`, never re-approved, not verified) with a note
  naming the release as "artist – album", never a path. **A lone file that is no track of its tagged release is
  no longer filed at all** (item 11): its row keeps `downloaded`, its file and
  the reason, for "Pick the release".

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
  verified above unverified whatever the score (three tiers since item 11:
  artist and album evidence, artist evidence only, the rest —
  `_rank_album_folders`), stamps `search_pass` provenance, and its early-stop
  needs *verified* album matches. **Absent flag
  means verified** — only explicit `False` demotes, so legacy dicts and the
  single-track path (still ungated, see backlog I-015) behave as before.
- **Unattended paths never guess.** This item's half of the rule is artist
  evidence; **the one rule is `_unattended_folder_ok` — artist evidence, *and*
  album evidence for a folder of an album search, item 11** — and
  `_auto_download_choice` is the first folder
  that passes it: the API download task fails `no_source` (`artist_unverified`
  or `album_unverified`), Telegram falls through to the picker with a caution,
  `_source_failover_order(..., folders=)` and `_switch_album_source_inner`
  skip every alternate that does not pass. A user's explicit pick (`chosen`,
  `start_is_choice`) is consent — for that one source only.
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
  verified and shows album evidence (both pickers; and paired on titles —
  items 7 and 11). **`GET /api/debug/album-search?artist=&album=&year=|release_mbid=`**
  runs the real search and shows every folder's rank inputs (`albumEvidence`
  among them, and whether a pass was cut off) — use it before
  theorising about this path, like the slskd probe before it.

**7. One pairing, from the picker to placement — and translated titles**
(2026-10-06). A library tagged as one edition and a peer sharing another in a
different language ("惑星" against `01 - Planets.flac`) match on no title tier,
so the track number and the duration are the only evidence. Both fallbacks
existed and neither ran: only the check *after* the album enqueue passed
`release_total` to `_album_file_pairs_for_missing_tracks`, so it marked rows
`queued` that the enqueue never fetched; `slskd_expand_directory` dropped
`length`; scanned gaps carried no `duration`; placement read the duration from
the wrong key. A fill of nine queued seven. Now:

- `_album_file_pairs_for_missing_tracks` is **the** folder matcher — the
  picker's coverage (album path), the group enqueue, the leftover rescue, the
  album switch and the per-file retry all call it with `_release_context(group,
  tracks)`. Title tiers first; then, **only in a trusted folder**
  (`album_match_ok`, or a majority of the whole release's titles), the track
  number (whole-album folder only) and an unambiguous duration. `release_tracks`
  is the whole release, present songs included — a present song of similar
  length vetoes a duration guess — and the rows being paired must be the same
  objects (`_with_release_rows`), since `_duration_match` tells a track from its
  siblings by identity.
- **The exact-count zip is not blind any more** (2026-10-09). A folder holding
  exactly as many files as there are gaps, none matched by title, used to be
  paired to them in filename order on the count alone: Currents with two gaps
  (tracks 5 and 6) read 2/2 "Best match" on four two-file folders and fetched
  "Let It Happen" for "Eventually". `_exact_count_zip` pairs only when every
  file carries a track number equal to the `position` of the row it lands on
  (on that row's disc, where the release has several — item 9), no two rows
  share a place and no two files a number, and no file title-matches (any
  tier) another track of the release — a song already owned, or a gap not
  being paired. Unnumbered files are never zipped.
  **Only a source the user chose is zipped.** Everything unattended passes
  `blind_zip=False` and never zips at all: the leftover rescue, the per-file
  retry, the album switch — and, since the L1 review, the automatic pick
  (`_gap_auto_task`) and the fetch route's walk past a refusal
  (`_enqueue_group_source(..., blind_zip=False)`). The route's first attempt,
  at the source the user tapped, and `…/sources/<n>/download` keep the zip.
  The trusted-folder tiers (track number in a whole-album folder, an
  unambiguous duration) are not the zip and stay available to all of them.
- **The folder score expects the album, not the gaps.** `_group_source_plan`
  passes the release's `total` as `expected` (the gap count only when the total
  is unknown). `_score_folder` pays up to 2500 for a folder showing exactly
  `expected` files, so with two gaps every two-file folder outranked every
  whole thirteen-file copy — which is how the zip above got to run first.
- **A pairing with no title in it says so.** A coverage whose matched rows
  include a `position` or `duration` basis carries the risk `weak_pairing`
  ("paired by track number or length, not by title"), so it never reads "Best
  match" or "Complete but queued", and `_source_recommendable` — the one rule
  behind `recommended` on `/api/gaps/<id>` and `/api/album/sources` — refuses
  it. The source is still offered and still fetchable: it is the user's call.
  **In the gap view only** (`gap_pairing=True`), a source that pairs **none**
  of the rows being fetched is never `recommended` either, whatever its rank
  and artist evidence: when a search finds only partial folders, the first of
  them is not the one to take. The artist page's picker does not get that
  rule, because its coverage is a different count — a search's hits (only the
  files whose names matched the query) against the whole tracklist, where zero
  is ordinary for a folder that is the album.
- The group enqueue pairs **once** and queues that pairing (`file_pairs=`);
  a row reads `queued` only if slskd accepted its file (`accepted_out`).
- A row fetched on `position`/`duration` evidence carries `acquired_basis`
  (a carried field). Placement re-matches by tag and title, which cannot find
  such a file again, so `_group_acquired_pairs` hands it to its slot (after the
  MBID tag, before the title tiers) — with every `_reject_reason` refusal still
  in force: it is not a hand pick. Without it the file was filed as a bonus
  track under its own English tags.
- Gaps scanned before this have no durations: `_stamp_missing_durations` fills
  them from the **cached** tracklist only (no request, safe under the lock), at
  source search, on opening a source's files and at enqueue.

**8. A file that is its own transfer is placed too** (2026-10-09). A file
picked by hand for one gap (`_pick_file_for_track`) and a leftover rescued from
an alternate peer (`_rescue_leftover_tracks`) are enqueued with
`review_group_id` and **no `album_group_id`**. The poller's single-success
branch treated them as loose tracks: tagged where they lay, "✅ Download
completed", and nothing ever placed them — the row read `downloaded`, then
failed. And `_finalize_group`, importing only `ag["local_dirs"]`, marked a
rescue that had already landed "placement failed: no downloaded file matched".
Two hand-picked Currents files and a rescued Innerspeaker track sat in
/downloads that way.

- `_place_group_downloads(group_id, token, only_paths=)` is the one helper for
  an album-gap review group (`_is_album_gap_group`: not Loose-tracks, and it
  knows its release). It takes the rows that read `downloaded` with a file on
  disk and **no placement miss of their own**, imports exactly those files
  (`only_relpaths`; the import derives the hand picks and the
  `acquired_basis` pairings from the group), and judges **only those rows**,
  each by its own slot. Placed → `placed`, `imported_at`, the verifier.
  Refused → the row keeps `downloaded` and its file, with the refusal as
  `download_error` ("not placed: …") and `placement_missed_at`, so the group
  reads stalled at once; a hand pick refused as "already in the album" keeps
  `can_force_place` (`_note_force_place_offer`, shared with
  `_mark_group_tracks_placed`). **"Try again later" is not a refusal** (F5): an
  import that could not ask what it needed (`retryable` — MusicBrainz gave it
  no tracklist) judged no file, and its rows get "not placed yet: …" and **no**
  `placement_missed_at` (`deferred` in the outcome). Stamped, the row read
  stalled and nothing unattended ever looked at it again, over an outage of a
  few minutes; unstamped it is the next start's pass's, and reads "working"
  until `DOWNLOADED_STALE_SECS`. The scan runs only when something moved. It
  never calls `_mark_group_tracks_placed` — that fails every other in-flight
  row as "not included" and pops the review group's album groups.
  **Known and left:** a release MusicBrainz will *never* list is deferred
  again at every start (one cached lookup and one log line each time), where
  it used to be stamped a miss once. And the wait below covers the start, not
  the lookup: `_nd_find_album_folder_by` still takes "Navidrome did not answer"
  for "in no folder", so the pass — if Navidrome goes away after the ping — and
  the poller's single file at any time can still file into
  `<music>/<artist>/<album>` as the import would spell it.
- **Nothing unattended places a `downloaded` row that lacks `downloaded_at`**
  (`_download_stamped`, F1). The stamp is written whenever a row *enters*
  `downloaded` (`_stamp_downloaded`), by the poller and by Reconcile alike, so
  a row without it was left by something older — the retired repair pipeline's
  rows above all (below, "`repair` is retired"). The three unattended takers
  ask: the startup pass's selection, `_place_group_downloads` **with no file
  named**, and the extras of `_finalize_import_selection`. Such a row waits for
  Placement, where a person decides. A placement that names its file — a
  transfer that has just finished — is unchanged. The cost: a file this
  pipeline downloaded whose row lost its stamp is not placed at startup.
- **The poller** (`_finish_gap_single`): if an album group of the same review
  group is **arriving** (`_album_group_arriving`: pending, not cancelled, not
  matched by hand, and with a file of its own landed and found on disk — what
  `_finalize_group` needs before it imports), the file waits and that finalize
  imports it; otherwise the file is placed at once, off the event loop,
  **alone** (`only_paths`) — a transfer finishing does not sweep in whatever a
  cancelled fill left behind. A group that has landed nothing is not waited
  on: it can end without importing anything (source failed, every alternate
  refused), and the file would be stranded again. The file is **not** tagged
  in /downloads any more — its own tags are what placement judges it by.
  Telegram says which of the three happened ("Placed into …", "waiting for its
  album", "not placed (reason)").
- **`_finalize_group` imports those files, not their folders**
  (`_finalize_import_selection`). Of the album group's own folders
  (`ag["local_dirs"]`) it imports the files its transfers landed
  (`ag["local_files"]` — item 11, C1; whole only for a group an older build
  started). From anywhere it takes exactly the files the review group's
  stamped `downloaded` rows name. slskd names a
  local folder after the peer folder's **last path component**
  (`/downloads/Currents`), so two peers' downloads — or a cancelled fill's
  leftovers, or what a refused fill left — share one folder. A waiting
  file's folder is still **never added to `local_dirs`**: it is not the
  album group's.
- **Once at startup** (`_startup_placement` → `_place_stranded_downloads`, on a
  thread from `main()`). **It waits for Navidrome first** (F5): the import asks
  Navidrome which folder an album is in and takes "no answer" for "in none", so
  a pass started before Navidrome was up — a NAS reboot starts the containers
  together — filed into `<music>/<artist>/<album>` as it would spell it, beside
  the album's own folder. It pings every `STARTUP_PLACEMENT_POLL` (10 s) for up
  to `STARTUP_PLACEMENT_WAIT` (10 min); if Navidrome never answers with the
  login lb-bot uses, nothing is placed, the log says so, and the rows are as
  they were for the next start. (No Navidrome login configured: nothing to
  wait for.) **Each group it places files for is one `_web_log` line** — artist,
  album, how many (F6): nobody asked for this now. It takes
  groups holding such a row with no live transfer, no arriving album group, no
  running verifier, **not hidden** (the user set the album aside; the retired
  repair-job groups are hidden rows stuck on `downloaded`), and **no row
  reading `cancelled`**. **A row Reconcile linked is not such a row**
  (`download_state == "reconciled"`, `_reconcile_linked`): Reconcile matches
  any pending or failed row to any file with its title in any download folder
  named like the album or the artist, and pressing it is consent for a
  placement the user then starts — a fetch's finalize still takes it — not
  for the next restart to move and retag the file with nobody looking.
- **A cancel stays terminal** (item 4 has the whole of it). `_gap_cancel`
  detaches a review group's single transfers with the rest;
  `_cancel_album_fill` detaches only the album group's own, so a rescue runs
  on, marked `fill_cancelled`, and its row is written `cancelled` when it
  ends. A single's placement never reads the ledger. Rows whose file had
  already landed are settled `cancelled` by the cancel, so after one a group
  holds no `downloaded` row for anything to place. The startup pass's
  `cancelled`-row rule is for what an older build left — a cancelled fill
  with its landed rows still `downloaded` — and it is not a lasting record:
  the next fetch or pick re-approves a `cancelled` row.
- Side effect to know: `_approve_pending_missing_tracks` holds back a missed
  row whose file is still in /downloads while anything else is fetchable. That
  was written for Loose-tracks rows (the only ones with a miss stamp until
  now) and now applies to an album gap's refused file as well — it keeps its
  file, its reason and its "Place anyway" until it is the only thing left.
- A Loose-tracks row keeps `_auto_place_loose_track`, unchanged.

**9. A release is its media** (2026-10-09, B-066). `_release_tracks_from` used
to flatten a release into one list with the per-medium `position` and no medium
number, and placement numbered a file from the file. Lonerism's box (release
`d105e2cd`: the album on two LPs, again on a CD, and a 7") read as 26 tracks
with every title twice, a library holding the album read 24/26, and eight files
were filed as disc 1 tracks 1,2,2,3,4,4,5,6 beside the ones already there.

- **Rows carry their place.** `disc` (the medium's own number), `disc_total`
  (how many media MusicBrainz lists), `disc_tracks` (that medium's count) and
  `track_id` (the release-track id), beside `position` — which restarts at 1
  on every medium, so **a row is `(disc, position)`, never `position` alone**
  (`_row_slot`). Review rows keep `disc` and `disc_total` (`_row_disc`); a row
  scanned before this has neither and reads as disc 0, "not known".
  **On the wire**: `/api/album/tracklist` rows carry all four; a gap's track
  rows (`/api/gaps/<id>` → `tracks[]`) carry `disc` beside `position`, always
  present, **0 for "not known"** — an older row, or a song the library has
  (Navidrome's album record carries no disc). Additive; no `WIRE_VERSION`
  bump, which is about index rows.
- **A medium that is another medium again is dropped**: every recording on it
  is also on one other medium that is larger, or equal-sized and earlier. The
  LP sides go, the CD stays, an identical second CD goes; a track with no
  recording id is nobody's repeat. **The numbers that remain are
  MusicBrainz's** — discs 3 and 4 of 4 for Lonerism — never renumbered. This
  happens in the one function every reader goes through (the scan, the
  tracklist route, placement, `mbz_resolve_album`'s total, the search's title
  list), so they cannot disagree about how long an album is.
- **Present still means the recording, else the title** — never the medium. A
  library file tagged as an LP side makes its song present.
- **Placement numbers a file by its slot**: `tracknumber`/`tracktotal` (the
  medium's count, not the release's), `discnumber`/`disctotal`, from the slot
  and never from the downloaded file. The file is named `D-NN - Title.ext`
  once the release — after the drop — is more than one medium, `NN - Title.ext`
  otherwise. An extra track (no slot) keeps its own disc and track and is given
  no track total.
- **A track number is evidence only with its disc** (`_file_number_names_row`,
  behind the exact-count zip, the whole-album track-number tier and the title
  tiers' tiebreak). `_filename_disc_track` reads the disc from `D-NN` / `D.NN`
  or from a `CD2` / `Disc 1` folder. On a release of several media
  (`disc_total > 1`) a bare "03" names no row — it is track 3 of every disc —
  so the file must name the row's disc. The numbers compared are MusicBrainz's,
  so on a release that lost a medium no peer's numbering matches and the number
  is simply not used; titles and durations still pair.
- Known and left: `/api/album/releases` counts tracks from each release's
  `track-count` (the browse carries no recordings to compare), so a box picked
  there still shows and sends MusicBrainz's total (26); the two B-sides of a
  Lonerism-like box whose recordings also sit on *another kept* medium would
  still share one outcome key in `_placement_outcomes` (recording, else title).

**10. Placement writes one spelling** (2026-10-09, B-067). `_mutagen_write_tags`
set its twelve keys and deleted nothing. A peer's file names its album artist
under half a dozen keys and Navidrome reads them all, so a file placed with
`albumartist=Belle & Sebastian` still said `album artist`, `album_artist`,
`albumartists`, `artists`… `=Belle and Sebastian`: the album was credited to
both, and — Navidrome keying artists by **name** — the library listed two
artists for one MusicBrainz id. The numbering had the same residue.

- **`TAG_ALIASES` is the one table**: per Vorbis key written, every other name
  for the same fact (the sort and credit forms included; `discsubtitle` and
  `media` under `discnumber`). **Writing a key deletes its aliases first**,
  case-insensitively; a key the caller does not pass is not touched and nor are
  its aliases — the loose-track tagger writes three tags, and the album artist
  is not its business. `_id3_write_tags` mirrors it (`_ID3_ALIASES`: `TSO2`,
  `TSOP`, `TSST`, `TMED` and the TXXX frames with those descriptions). Every
  placement ends in `_place` → `_mutagen_write_tags`, so the poller's helper,
  the finalize and the hand placements all get it; add an alias to the table,
  never a second delete somewhere else. A tag restore (the album override)
  must snapshot the aliases with the key, since the write removes them.
- **`musicbrainz_releasetrackid` is the slot's** (`track_id`, item 9), and is
  **deleted** when the slot has none or the file has no slot: it names a track
  of one release, and the album id beside it has just been rewritten.
  `TAGS_DELETED_WHEN_EMPTY` is that rule — for those keys *present and empty*
  means "delete", where for every other key an empty value means "leave it".
  On an MP3 the recording id is now the `UFID` (Picard's convention, and what
  mutagen's easy reader calls `musicbrainz_trackid`); it used to be written
  into the release-track TXXX frame, which is why `_tag_recording_mbids` reads
  that frame as a recording id too — keep that reader, older files need it.
- **The track artist is the file's own unless it is the album artist spelled
  another way** (`_track_artist_tag`): a featured credit stays, "Belle and
  Sebastian" on an album by "Belle & Sebastian" becomes the album's spelling.
  The merge retag (`beets_merge_album_folders`) applies the same rule.
- **`_artist_fold` answers "the same name, written two ways?"** — NFKC,
  casefold, `&`/`+` read as "and", punctuation and spaces dropped, every letter
  and mark kept in any script. Never `_norm_album_text` for this: that one
  answers `''` for every non-Latin name (B-060), and an accent or a vowel sign
  is part of a name. It is a question about spelling; whether two entries are
  one *artist* is the MusicBrainz id's.
- **A placement with no album gap behind it is filed under the library's
  spelling of its artist** (`_library_artist_spelling`). **One place asks:
  `_deterministic_album_import`**, for every call that is not for an album
  gap's review group (`_placement_takes_the_librarys_spelling`: no group id, a
  group that is gone, or a Loose-tracks group) — an artist page's whole album,
  "Pick the release", a Loose-tracks row's auto-placement, the Place task, the
  `/api/imports/<id>/*retry` routes, the Telegram re-import workers. Until the
  L2 review only the album group's finalize asked, and each of the others could
  still file under MusicBrainz's credit or a peer's `albumartist` tag. A new
  caller of the import gets it without doing anything; do not resolve the name
  before calling, and **read the name that was written from the result**
  (`"artist"`) — the finalize does, for the index-key lookup, the hub
  notification and the fill verifier. **A placement for an album gap does not
  ask**: its review group's artist is the library's own name for the album
  being filled. ("Place anyway" is the one import made under `_review_lock`,
  and it never reaches the question: for an album gap it is not asked, and a
  Loose-tracks row has no release, which the import refuses first. So nothing
  slow was added under the lock — keep it that way.)
  Which name: the release's one credited artist is looked up by MusicBrainz id
  in `_nd_artist_index()` (override-aware). Among the entries carrying the id,
  an entry that is this name under another spelling (`_artist_fold` of the name
  handed in, or of the credit's own) with the most albums; failing that, the
  entry with the most albums **that is one act's name** — "Кино" for "Kino",
  "Ye" for "Kanye West". **A collaboration entry is not a spelling of the
  artist**: a name `_artist_variants` splits into several acts ("A feat. B",
  "A & B", "A with Friends"), filed under the lead artist's id, never takes a
  solo album; with no other candidate the name handed in stands. ("Belle &
  Sebastian" and "Tyler, The Creator" split as well, and are chosen by the
  first rule: the whole name is the credit.) A joint credit is left alone, and
  so is everything when MusicBrainz or the artist list cannot be read; an
  empty artist list asks MusicBrainz nothing.
- Not covered: a loose track that is *not* filed (its tags name no release) is
  tagged where it lies with the title and artist it was fetched for — that
  path names no album artist. Deleting `artists` loses a peer's multi-artist
  split on a featured track (the `artist` string is kept).

**11. A click is that source, and an album is shown before anything is taken
for it** (2026-10-10; an incident on the NAS build). After – "After EP" and
"After EP 2" were each asked for with a click on a source. lb-bot downloaded
OVERWERK's and Mitch Murder's "After Hours EP" and filed twelve files into the
library as After's, retagged, into another album's folder. Five things, each
of which had looked like the feature working:

- **A click downloads the source that was clicked** (A1). The click was a
  preference: the download ran a *new* search, moved the clicked peer to the
  front if that search returned it and otherwise took the automatic choice,
  while the page said "Queued from @<the clicked peer>".
  - **The list holds still.** `GET /api/album/sources` keeps what it answered
    per release (`_album_sources_kept`: the ranked folders with their files,
    when searched) for `ALBUM_SOURCES_TTL` (15 min), in memory, bounded
    (`ALBUM_SOURCES_MAX` 32 releases; `ALBUM_SOURCES_KEEP` 60 folders by rank
    plus the ones already answered). Asked again inside that time it answers
    the kept list (`cached: true`, `searchedAt`, `serverTime`) and searches
    nothing. **A search made while a list is alive is merged into it** —
    `refresh=1`, the automatic download's own search, a click's one search —
    by peer and folder, re-ranked (`_album_sources_keep`): the list only
    grows while it lives. **A folder both searches found is the newer copy's
    figures and both copies' files** (`_merged_source_copy`, C8): slskd
    returns of a folder only the files that matched the question, and "the
    newer copy wins" cost the right folders their track evidence when
    "Search again" found them by the track question, one file each. Its
    album evidence is worked out on the union against the release's titles
    and never ends lower than a copy had shown — **also when the newer copy
    has every file and no evidence of its own** (D7d: a search looks at the
    titles of its forty best-named folders only); its score is the better
    of the two where the older copy adds a file, the newer copy's
    otherwise. Two passes of one search are merged the same way
    (`_union_source_files`). The route
    answers the first twelve by rank **plus every folder it has answered
    before** (up to 36), so "Search again" takes nothing off the screen. A
    search that found nothing is not kept, and nor is one a download made
    under preferences of its own (`quality`, `allowMp3`): the list is the
    picker's, ranked by the standing ones.
  - **A row says how many files a plain download of it leaves out**
    (`extraFiles`, B-084 H5a): the audio files lb-bot has seen in the
    folder that are named for no track of the release (H2b, below) — and 0
    unless every track is paired, and by its exact title (H2a), since a
    plain download otherwise takes the folder whole. No key when the
    tracklist is not known. **One rule with what the
    download asks for** (`_files_beyond_the_release`, read off the row's own
    `coverage`; `_whole_album_files` goes through it), so a row and a fill
    cannot disagree about a folder both have seen whole. It counts the files
    **the search returned**, so it is a lower bound — until a download reads
    the folder: **the kept copy then takes the peer's own listing**
    (`_album_sources_learn`, H5d, through `_merged_source_copy`), and the
    next answer for that row (`fileCount`, `coverage`, `extraFiles`) is from
    the whole folder, with no new search. Only a folder the list holds, in a
    list that is alive; nothing else about the list changes (when it was
    searched, `sampled`, what was shown, the order); not from a download
    under preferences of its own; and the switch's reads of an alternate are
    not taught.
  - **`POST /api/album/download` with `sourceUsername` (+ `sourceFolder`)
    takes that folder from the kept list and searches nothing**
    (`_album_source_named`). The list gone (the TTL, a restart) or without the
    pick: one search, merged, and the same peer and folder looked for in it.
    Not there: the fill is `failed` `no_source`, retryable, "The source you
    picked (@peer) is no longer listed … search again", nothing queued —
    **never `_auto_download_choice`**.
  - **`allFiles: true` with it is "Download all"** (B-084 H5b): that folder
    is asked for every audio file, as every folder was before H2. Only JSON
    `true`, and only with `sourceUsername` — without one the route answers
    400 `{"error": "allFiles needs a picked source (sourceUsername)"}` and
    starts nothing. **Every 200 echoes `allFiles`**: for a new fill what was
    asked; for `existing: true` the running fill's own (the status view's),
    which the second request did not change. The echo is how a client behind
    a hub that drops the key finds out. **It is consent for the folder that
    was picked**: a folder the download moves on to — the next listed one
    when slskd takes no file of the pick, an alternate at a switch — is
    asked for the release's tracks, and the fill reads `allFiles` false from
    then on. What is *filed* is unchanged: a file that is no track of the
    release is an extra only by the import's own rules (below), and
    otherwise stays in the download folder for Placement. **Cost:**
    "Download all" on a folder that holds another release's files fetches
    them — and when the peer refuses them, they are counted lost one by one
    while it serves the rest (item 2).
  - **What a click may fail over to** is the other folders of that same list
    that pass `_unattended_folder_ok` (`_unattended_alternates`, the group's
    `alt_sources`): the poller's switch walks them as for any group, and a
    pick slskd takes no file of at the enqueue is followed by them there, in
    order. Past them the fill ends with its reason. **A fill that began with
    a click gets no automatic retry** (`_album_fill_fail` reads `pickedSource`
    on the row): the retry searches again and takes the automatic choice.
    **Nor does the retry an earlier automatic failure armed follow a click**
    (C7): the worker reads the row when it fires and does nothing when the
    fill that failed last began with one.
  - **The fill view says both**: `pickedSource` (the peer clicked, "" for an
    automatic fill; a per-fill field) beside `source` — which now follows a
    switch on the row too, so it is still right once the group is gone.
    **And what that source was asked for** (B-084 H5c, both per-fill
    fields, on `/api/album/status` and each album of `/api/fills`):
    `leftOut` — audio files of the current source's folder, the peer's own
    listing as the download read it, that were not asked for (0 when the
    folder was asked whole) — and `allFiles` — it was asked for every audio
    file at the user's request. Written at the enqueue and again at a
    switch, on the group (`left_out`, `all_files`) and on the row; read off
    the live group like `source`. 0 and false for a fill from before the
    fields. During a click's one search the row's `allFiles` is what the
    fill was started with.
  - With no source named the route is as before (search,
    `_auto_download_choice`), and its search is kept like any other.
  - **A folder that has the whole release is asked for the files that are
    its tracks', and not for the rest** (B-084, H2 and H2b,
    `_whole_album_files`). A whole-album download used to ask for every
    audio file of the folder: eleven for a five-track EP whose peer still
    listed six files of another release there — "0 of 11", and six
    refusals. Now, when every track of the release is paired with a file of
    its own: **every file named for a track of the release is asked for,
    and the files named for none are left out** (`_named_for_a_track`: any
    title tier of `_title_match_rank` — exact, prefix, containment, fuzzy —
    asked as the trusted-folder count asks it). `total` is the number
    asked, which can be more than the tracks. A track with no file by its
    title (untitled names, translated titles), or no tracklist to be had →
    the whole folder, as before, and the import's rules decide. **Not one
    file per track** (H2b): H2 asked only for the file the count preferred
    for each track, and where a second file bore a track's title — a
    re-recording, a live take under the plain name beside "(Album
    Version)", another format — the import no longer had both and filed the
    one it was given, where before it took the one whose tags carry the
    track's recording id (34 tracks of 29,735 in the review's differential
    went from right to wrong).
    **Paired as the picker's row counts** — `_source_coverage_summary` with
    the release's rows, by title, never by a track number or a length — so
    "5/5 tracks" and the files asked for are one pairing. **And "has them
    all" is every track by its exact title** (H2a,
    `_paired_by_its_exact_title`): the count also pairs on a prefix, a
    containment and a fuzzy match, which is right for a count and wrong for
    leaving files behind. "Home" was paired with "12 - Home (Demo).flac"
    beside an untitled "05.flac" that was the track, and the demo was
    fetched alone. One track paired below exact and the folder is taken
    whole, so the import has both files in hand — **which is all this rule
    does: it does not make the import file the right one.** With both, the
    import still takes "12 - Home (Demo)" for the slot by its name, as it
    did before this round, and the track's own file is filed beside it or
    stays in /downloads for a hand pairing (the import's matching;
    registered, not this round's). *Exact* is the count's own top tier (the
    title's key is the key of the name with its extension, a disc-track
    prefix and a leading track number off; spelling, spacing and
    punctuation aside) — **or the same equality with the name as it
    stands, or with only its first number off**, because the count takes a
    title's own leading number for the file's: "01 300 dreams" reads
    "dreams" to it, and the first track of the download this was written
    for was `contained`. The row's `coverage` and `coverageFull` are not
    this rule: they go on counting every tier. The tracklist is
    read where the search left it (`_release_tracks_cached`, never a
    request); the group keeps the rows (`release_tracks`) and
    `_switch_album_source_inner` asks an alternate the same way, a group
    that keeps none taking the folder whole. The group is a whole-album
    group in every other respect: no track on its transfers,
    `missing_tracks` empty, the finalize and its rules as they are. Callers:
    the album download task and the switch — Telegram's album paths and a
    bare folder download pass no rows and are as they were. **Costs:** a
    file of the folder whose name is no track's — a bonus track of another
    edition, a hidden track — is no longer fetched with the album when the
    folder holds the whole release by its exact titles: the fill says how
    many files it left (`leftOut`), and `allFiles` is the way to have them.
    A demo or a second take of a track *is* fetched with the album, as
    before H2 (the progress then counts files, not tracks), and so is a
    file whose name holds a short title by chance ("Go" in "Let It Go
    Now"). And a folder whose file names carry more than the
    title — a suffix the title lacks ("(Remastered 2011)", "(Album
    Version)"), **an artist before the title** ("01 - After - Obvious"; the
    count takes no artist prefix off), a misspelling, a disc before the
    track number of a title that begins with a number — is fetched whole,
    as before H2: its row reads `extraFiles` 0 and offers no second action.
  - **Web UI** (`web/src/lib/albumSources.js`, state matrix in
    `albumSources.test.mjs`, `node --test`; on the 2026-10 redesign's frame:
    `AlbumDetail` in `Artist.jsx`, `SourceRow` in `ui.jsx`, `requests.jsx`,
    `Activity.jsx`): the click toasts "Asked @peer for
    this album" — or that a download was already running — never "Queued
    from"; the request's own row on the album page is titled with the fill's
    `source` and says under it "lb-bot moved on from @peer, which you picked"
    when the two differ (`fillSourceBeyond`; a failed request says the same);
    the source list says its age beside its heading — "Kept from a search N
    min ago" for a list lb-bot answered out of what it kept, which nobody may
    have been shown, read again every half minute — "Search again" (the first
    button of the page's Next step; it sends `refresh=1` and takes nothing
    off the page), and one line when the list is a sample. Rows are keyed by
    peer and folder (a re-rank moves `id`). **A request is followed afresh,
    and an ended fill is believed only at the second look**
    (`useRequest(releaseMbid, made)`): the first poll after a new request
    can still read the row the request before it left behind. Once a request
    is made the list folds shut and the request's row takes its place, so no
    row is marked as the one asked. **A row is badged by `albumEvidence`** (`albumBadge`, C9), which
    source rows now carry — "name", "tracks", "", absent for a source nobody
    judged against an album: "matches album title", "its files are this
    album's tracks", "album not confirmed" for a name that scores and is no
    evidence, "different album?". `albumMatchOk` alone said "matches album
    title" for "2018 - EP - After 4AM". **The row is read as the unattended
    rule reads it** (`_source_summary` → `_album_evidence_as_read`, D3): a
    list stored before album evidence carries the field too, so a row the
    server would refuse is neither badged by its name nor `recommended`.
    **"Download all"** (B-084 H5e): a row whose `extraFiles` is above 0
    adds one quiet line, "N more files in this folder", and beside it a
    second, quieter action, "Download all M files" (M the row's
    `fileCount`) — in the row's text column, a text link, so the row's own
    action stays where it was; nothing is shown for 0 or an absent field,
    and nothing is counted from the files on the screen. It sends the row's
    request with `allFiles: true`; the toast says "Asked @peer for all M
    files in the folder" only on the answer's `allFiles` echo, and
    otherwise "lb-bot is downloading the album’s tracks only — it did not
    take “all files”". The request's row on the album page and the album's
    row under Activity add "N other files in the folder were left out" when
    the fill's `leftOut` is above 0. Wording in `albumSources.js`
    (`extraFilesText`, `downloadAllLabel`, `askedText`, `leftOutText`), its
    rows in the matrix.
- **Album evidence** (A2). Artist evidence is not album evidence: for an
  artist called "After" every "After Hours", "After Dark", "After School" is
  "by the artist" as far as a path can say, and nothing unattended asked
  more. `_stamp_album_evidence` puts `album_evidence` on every folder of an
  album search: `"name"` (`album_match_ok`), `"tracks"` (at least half of the
  release's titles, and at least two, have a file among the search's hits —
  `_folder_track_matches`), or `""`. **Counted against the release** (C6):
  - **a release known to have one track** is evidenced by that one title
    (it needed two, so a single had a folder's name or nothing, and
    "Obvious (Single) [2025]" scores 66.7) — **with all of** (D2): a file
    named exactly it (`_file_is_titled`: the whole title, or what follows
    the last " - "; "Obvious Child" is not "Obvious"); **the artist named
    exactly by that file or its folder** (`_artist_named_exactly`: a
    component of the folder's path, or a part of a component or of the
    file's name as split at " - ", is the full credit or a credited
    sub-artist, `_artist_fold` aside — "After School", "After Dark" and
    "After Hours Compilation" do not name "After"); and, where the peer
    states that file's length and MusicBrainz the track's
    (`release_lengths`, `_release_title_lengths`), the two within
    `DURATION_MATCH_SEC` — a stated length that disagrees is no evidence,
    one nobody states does not stand in the way. One exact title alone was
    enough for a day: with no right folder in the answer the automatic
    choice for After's single "Obvious" was "After School/First Love",
    downloaded, filed and retagged as After's. **"Known" is the release's
    own total** (`release_total`, from whoever holds MusicBrainz's
    tracklist: `_search_album_with_context`, `/api/album/sources`, the kept
    list's merge, a gap's plan), never the number of titles a caller handed
    in: with MusicBrainz down, the one title a caller held ("Nangs", of
    thirteen) is no single. **Costs:** a single filed under a folder that
    does not name its artist exactly has no automatic choice, only the
    picker; a namesake artist's track of the same title and length still
    passes;
  - **a gap fill** searches by the rows it is fetching and counts evidence
    against the whole release as the group knows it (`_group_source_plan`:
    `release_titles` from `_release_rows`, `release_total`). Counted against
    the rows alone, one missing row could never be evidence — "Auto-select
    best source" was dead for every one-row gap — and two were "half of the
    release" in any folder holding those two. **A group that knows its
    release and not its size — or its size and only its own rows, a
    playlist's — counts against the release's own tracklist** (D6):
    `_search_group_sources`, the slow half outside `_review_lock`, reads it
    (`_album_search_context`, one cached read) when the plan names a release
    and holds no total or fewer titles than its total. With MusicBrainz not
    answering, what the group knows stands. Only a group with no release at
    all has no track evidence — and its search is a track search.

  **For a title with no word of its own
  the name is not evidence** (`_album_own_words`: the artist's words,
  packaging, EP/LP/single and bare numbers taken out — "After EP 2", a
  self-titled album, "1989"): there "2018 - EP - After 4AM" scored 80 and
  passed. Only the tracks count then. The titles are checked for at most
  `_TRACK_EVIDENCE_FOLDER_CAP` (40) artist-verified folders, best-named
  first. **`_unattended_folder_ok` — artist evidence, and album evidence for
  a folder of an album search — is the one rule** behind
  `_auto_download_choice`, every `alt_sources` (`_unattended_alternates`),
  `_source_failover_order`, `_switch_album_source_inner`, `_gap_auto_task`,
  the leftover rescue and the Telegram auto paths. **A track search's folder
  is nobody's album and passes on artist evidence**, as it always did: a
  Loose-tracks row, a gap with no release, Telegram's `/search`, the
  single-track path. **Which search made a folder** (D3,
  `_album_evidence_as_read` — the one reading, for the rule and for the row
  a client is shown): its own `album_evidence`; else its `search_kind`,
  which every search stamps now (`"album"` / `"track"`); else, for a list
  from before the stamp, **what whoever holds the list says** (`album_list`)
  — a review group by what its last search was (`source_results.mode`),
  else by what its next would be (`_group_sources_are_album`); a transfer
  group by whether it names a release (`_album_group_holds_album_list`);
  Telegram's album prompt and the album download hold album lists. Only a
  stamp-less list held for an album is read by its name: `album_match_ok`,
  unless the title has no word of its own (`artist` and `album` are for
  that). **The keys cannot tell**: `_annotate_folder_match` stamps
  `album_match_ok` on every folder of every search, False when there is no
  album to compare with — for a day "no `album_evidence` but
  `album_match_ok`" was read as "an older build's album search", which
  refused every folder of every track search (a Loose-tracks group's
  "Auto-select best source" ended "none is clearly Playlist tracks by Loose
  tracks"). Refused: `no_source`, "Sources found, but none
  is clearly <album> by <artist> — pick one yourself" (`album_unverified` on
  the task row). `_source_recommendable` needs it too. **Cost:** an album
  whose title has no word of its own and whose peers' file names carry no
  track titles gets no automatic pick, only the picker.
- **The import files nothing that has not shown the album** (A3, C2–C4 and
  D1, D4 after the reviews). `_deterministic_album_import` pairs every slot
  before it touches a file — by a hand pick, a recording id, the row it was
  downloaded for or a title tier, per slot (`_match_slot`: the duration tier
  of `_best_file_match` is off there) — then the slots still open with the
  files nobody has, by length (`_pair_by_length`). Then, in this order:
  - **A length is evidence only for the slot the file's own number names**
    (D1). A file nothing better has paired is paired by its length only
    with the open slot at its own number, and only within
    `DURATION_MATCH_SEC` whole seconds of that slot's length
    (`_whole_seconds`: each rounded). **The number** (`_file_own_number`) is
    its track-number tag, else the number its name **leads with**, after an
    optional disc prefix (`_NAME_LEADS_WITH_RE`: `04 - …`, `2-04 - …`), **or
    the number of a name that is nothing but a word for "track" and a
    number** (`_NAME_IS_TRACK_WORD_RE`, E1a: `Track 01`, `track_07`, `Audio
    Track 12`, `Titel 3`, `Piste 07`, `Pista 4`, `Traccia 2`, `Spur 5` — the
    whole name, from a fixed list of words; it is what an untagged rip is
    called) — not the first digits anywhere in the name, which is the
    acquisition side's reading of a peer's file: "U2 - One", "Song (Part
    2)", "Title [24bit]", "Track 2 of my heart" and "Stück 3" have none.
    **Where the tag and the name both give
    a number and they differ, the file has none** (E1). On a release of
    several media the disc as well — tag, else a `D-NN` name or a `CD2`
    folder — and without one no pairing. For a day the files were paired
    with the nearest track of the whole release instead: After EP 2 as "01
    - Stück 1.flac"…, every file three seconds long, was filed with three
    or four of its six under their neighbours' titles, and three files of a
    foreign seven-file folder were filed as a five-track album.
  - **By number and length it is all or nothing** (E1, `_pair_by_length`).
    The rule cannot know that a number is wrong, so every spare file whose
    number names a track of the release has to be that track by its length,
    and nobody else's. **One that is not, and no file of this import is
    taken by its number and length**: its length is not that track's, a
    better pairing of this import has filled its slot, or two files claim
    one track. Each open slot's row says which file it was. **What
    contradicts nothing:** a file with no number; one whose number names no
    track of the release (track 7 of six; a track with no disc said on two
    media); one numbered for a track this import does not fill — the
    library has it — that is as long as that track (E1b: it is that track,
    and simply not wanted, so a whole untitled folder placed for a gap
    fills the rows that are missing); and one whose length cannot be
    checked, its own unreadable or MusicBrainz stating none for the track
    (E1c: not paired, and no witness either way). Before this the import filed whichever numbered files happened
    to fit: a peer's edition one track longer — every later file numbered
    one off — had two of its shifted files filed under the next track's
    title, and names numbered right with tags in another order had the tag
    win. **What it still files, as what it is:** a folder whose numbering
    differs from the release's *only* between tracks of nearly the same
    length (After EP 2 with 2↔4 and 3↔6 swapped by tag alone, 208 and 206 s,
    199 and 197 s) contradicts nothing, and those files go under each
    other's titles. Only a title, a recording id or AcoustID could tell.
  - **Such a pairing is placed only when the import has at least three
    pairings, by length or better, and they are more than half of the slots
    it may fill** (C3). One length was "half" of a single's one slot and two
    of a four-track EP's: a foreign folder was filed whole. A hand pairing
    and a row's `acquired_basis` are not "length alone"; the acquisition
    side's own matcher (item 7) is untouched.
  - **An import nobody is looking at, with no review group, files nothing
    until the album is shown** (C4, C4a): fewer than half of the slots paired
    → no file is filed, not the one whose name holds a track's title either,
    and the result is not ok ("only 1 of the 6 track(s) of … was found among
    these 6 file(s) — too few to take them for this album"). One
    coincidental title used to be one foreign file in the library, the fill
    `placed`, the release `present`. **The import is told by its caller**
    (`unattended=True`, a parameter — never a guess at who called), and one
    caller says it: the album group's finalize, i.e. the artist page's
    download, the wishlist, the automatic retry and Telegram's album paths.
    **A hand route is the user naming the folder and the release** — the
    Place task (`/api/place-folder`, "Pick the release"), the download-match
    and `/api/imports/<id>/*retry` routes, "Place anyway", Telegram's
    Re-import, Import as-is and `/beets` — and there each file paired by its
    recording id or a title is placed as it always was: one track of twelve,
    two of six. (C4 first held them to half the album too, which took away
    the only way a partial folder, or a Loose-tracks row's file, could be
    filed.) A review group says which tracks are wanted, so **an import for
    one places each of its rows that is paired** whoever started it; a hand
    pairing is always placed; an id that names no group is no group.
    **So does a fill that named the tracks it wanted itself** (D4): an
    album group with `missing_tracks` and no review group — Telegram's fill
    of an album the library partly has (`group_missing_by_album`,
    `_dlall_worker`, the album prompt). The finalize hands those rows over
    (`wanted_tracks`); they are the slots the import may fill, and each one
    paired is placed. Rows that name **every** track of the release are a
    whole-album fill and C4 stands (an album the library has none of is
    listed with all its tracks missing); rows that name none of them say
    nothing. `PlacementClaimCensusTests` holds the list: a new caller that
    passes `unattended` or `wanted_tracks` fails there until it is listed.
  - **A file with no slot is filed as an extra only when at least half of
    the slots this import may fill — and at least one — were filled**
    (`album_shown`) **and its own tags place it in the album**
    (`_tags_name_album`, C2): its release id tag is this release's — enough
    by itself — or its album tag is the album's title, as
    `_nd_album_title_is` compares, **and its album-artist tag, where it
    carries one, is the album's artist** (D7c): the name the album is filed
    under, the name it was asked for by, or the release's own credit as far
    as it is cached (`_release_credit_cached`, never a request), spelling
    aside. Another artist's "Greatest Hits" lying in the folder is not an
    extra of the wanted one. **And what the import would not take for a
    track, it does not file as an extra of it** (F2): a left-over whose own
    number (`_file_own_number`, its disc with it on several media) names a
    slot this import was to fill and did not is left where it is, its row
    saying "it may be track 11 (“…”), which is still missing, and was not
    taken for it" — untitled files that all-or-nothing did not believe were
    filed under their own names (`11 - Lied 11.flac`) beside the track that
    still read missing. A number of no such slot — a bonus track past the
    tracklist, a slot this import filled, a track it was not filling — is
    an extra as before.
    Otherwise it is a `rejected` row with the reason and stays where it is.
    The leftover pass used to file whatever it was handed; with half-a-slot
    enough, a single filled from the EP's folder took the EP's other four.
  Nothing placed is `ok: False`, "Nothing placed: none of the N file(s) is a
  track of <album>" (a slot's own refusal first, when there is one), and a
  folder made for the album and left empty is removed. Every caller already
  took `ok: False` for a failure: the finalize fails the fill
  `placement_failed` (no automatic retry, nothing marked `present`, no
  notice, no verifier); `_place_group_downloads` leaves the row `downloaded`
  with "not placed: …"; a Loose-tracks row's auto-placement is a miss; the
  Place task and the hand routes answer the error. **Costs:** a lone extra
  can no longer be filed by any route, a Loose-tracks file that is no track
  of its tagged release included; a file paired on a length, with fewer
  than three pairings, is left by every route — a gap fill's too (a row
  fetched on its length carries `acquired_basis` and is placed); **an album
  whose files carry neither its titles nor numbers is refused whole** (a
  rip named `Track 01.flac`… has its numbers, E1a; one named `Stück 1.flac`…
  with no track tags has none) **and
  so is one numbered in another edition's order as soon as one numbered
  file does not fit its slot**; one stray or off-length numbered file among
  untitled names (a hidden track numbered like the last, a duplicate of a
  number) and none of the untitled files is placed by length, while the
  titled ones are placed as always (hand pairing is the way).
  The extras rule and the length rule hold on a hand route as anywhere.
- **A finalize imports the files its own transfers landed, not the folder**
  (C1). slskd names a local folder after the peer folder's last path
  component, so `/downloads/After EP 2` is every download of that name:
  what an earlier fill left there refused was handed to the next fill's
  import with what had just arrived, and filed beside it. An album group
  records what lands (`local_files`, the poller; a source switch starts it
  over) and `_finalize_import_selection` selects exactly those, plus — as
  before — the files the review group's own `downloaded` rows name,
  wherever they lie. A group that recorded nothing (in flight across the
  upgrade) imports its folders whole; an empty selection is never handed to
  the import, to which it would mean "everything". **Those files reach the
  import by name** (D5): the selection asks for each, and the import takes
  the files it is named (`only_relpaths`) by name — a placeable file inside
  the folder — never from among the first `IMPORT_FOLDER_FILE_LIMIT` a walk
  of the folder reaches ("no longer in the download folder" was said of six
  files lying past 200 others). **A download folder is
  marked imported only once nothing placeable is left in it**
  (`_note_folder_imported`, every import's mark): one still holding refused
  or left-over files stays in Placement's list — **and a mark from an
  earlier import is taken back** when an import leaves one of its files
  there (D7a: the mark only ever grew, and hid a later fill's refused files).
- **A transfer's file is the one that arrived for it** (D5). What "the files
  its own transfers landed" are was `_resolve_local_path`'s answer: the first
  file of the transfer's base name an `os.walk` of /downloads reaches. slskd
  does not overwrite — where a file of the name lies, the new one is written
  beside it under another name — so six files an earlier fill had left under
  the album's own names were recorded as the next fill's, filed and
  retagged, and the six that had just arrived never reached the library.
  The poller asks `_transfer_local_path(remote, queued_at)`:
  - **never older than the transfer** — last written no earlier than the
    transfer was queued, less `TRANSFER_FILE_SLACK_SECS` (five seconds, for
    one host's clock — E3; with a minute, what a refused fill left a few
    seconds before the switch to the next source fitted the next fill's
    window). The rule everything rests on; it does not depend on how slskd
    names a file;
  - looked for first in the folder slskd files that peer folder under,
    under the transfer's own name or slskd's renamed form of it,
    `<stem>_<15 to 20 digits><ext>` — its clock's ticks; "Interlude_2.flac"
    is another file's name (E3) — then anywhere under /downloads, by the
    same names;
  - of several that fit, the newest; where none fits, none;
  - a transfer with no `queued_at` is resolved as it always was.
  **Measured on the NAS** (2026-10-11, read-only): a finished file's
  modification time is the moment slskd finished it, on the clock lb-bot
  reads, and the renamed form is `<stem>_<18 digits><ext>`.
  **A transfer read as finished before its file is in place** (E2): slskd
  reports the transfer and then moves the file out of its incomplete
  folder, and the poller looks once. The album group notes each such
  transfer (`unfound`), and the finalize looks for them again before it
  imports (`_look_again_for_landed_files`: at once, then every second for
  `FINALIZE_FILE_WAIT_SECS`, five in all — off the event loop's back, no
  lock held); what is found joins the group's own files. **That second look
  is only in the transfer's own folder** (F1, `own_folder_only`): it runs
  minutes after an early track's poll, and with the poller's fall-back walk
  of /downloads a same-named file of another download that had landed
  meanwhile was taken for a transfer whose own file never appeared. **What is still
  not found is a transfer that did not land**: counted in `failed`, not
  `completed`; a review row reads `failed` with "downloaded, but the file
  was not found where slskd downloads to" (retryable); and a fill that
  placed the rest says "N downloaded file(s) were not found where slskd
  downloads to" in its ledger row's `reason` — what the web UI and the
  clients read — as well as in Telegram. With none found at all the fill
  fails `placement_failed`, "could not be found", not retryable.
  **Known and left:** a single transfer with no album group (a hand pick,
  a rescue) is still looked for once; and two fills at once into one leaf
  with one file name in common, both landed between two polls, take each
  other's file ("the newest").
- **Telegram's "📥 Import as-is" files a folder as what it is** (C5):
  artist, album and release all from the folder's own identity
  (`_identity_for_path`), never the artist and album of the fill's record
  — on the message a refused fill leaves, that record names exactly what
  the folder was found not to be, and one tap filed OVERWERK's six as
  After's. A folder nobody can name is not placed, and the message says so
  — **nor one whose identity names a release and no artist** (D7b: the
  import then asked MusicBrainz for the credit fail-soft, and with
  MusicBrainz silent filed it under `Unknown Artist/`). After an as-is
  import the record names what the folder was filed as. "Re-import" is
  unchanged.
- **An album's folder is the album's own** (A4). `_nd_find_album_folder_by`
  asked Navidrome's `search3` for the title, which answers by words, and —
  when no hit carried the release id — kept every hit and took the one with
  the most songs: "After EP" was filed into `/music/After/300 dreams/`,
  another release of the same artist, found by the word "After". A hit is
  the album only when
  its `musicBrainzId` is the wanted release; else when its artist
  (`_artist_fold`) and its title (`_nd_album_title_is`: the index's two title
  keys, editions aside) are the wanted ones; then the review group's own
  albums, asked by id, largest first. None: `""`, and the import makes
  `<music>/<artist>/<album>` as it always could. ("Navidrome did not answer"
  is still read as "in no folder" — item 8.)
- **A search slskd cut off is a sample, and a sample gets a second question**
  (A5). slskd ends a search at its response limit (250 here): for "After EP
  2" nineteen searches in a row ended `Completed, ResponseLimitReached`
  within two seconds, the right folder in one sample of three.
  `slskd_run_search` reads the pass's `state` (`cut_off`: "LimitReached" in
  it — the file limit as well). In `slskd_search_album_folders`, **when a
  pass was cut off, or the title has no word of its own, a pass that names a
  track is run** — the artist plus the release's most distinctive title
  (`_album_track_query`, what the self-titled branch always picked) — next,
  merged, and the early stop does not skip it; `MAX_SEARCH_PASSES` bounds it
  as before. A title with words of its own that is not cut off is one pass,
  as before. `stats` gains `cut_off` (any pass) and `sampled` (every pass),
  per pass in `pass_stats`; `/api/album/sources` answers `sampled`, true only
  while every search merged into the list was one. slskd's own limit is not
  raised.
- The suite's evidence is four real slskd answers for those two albums, peer
  names replaced, in `test_fixtures/` (gzip; out of the image by
  `.dockerignore`; the tests that read them skip when the folder is absent).
- Not covered: the Telegram pickers and the Fill-gaps picker keep their own
  lists (a gap's `source_results`), and a click there was always for the
  listed folder. A kept list is not persisted. The merge keeps a folder for
  as long as its list is refreshed inside the TTL, with the figures of the
  last search that returned it. **The track question gives the automatic
  path no choice for the titles it is planned for**: it returns one file per
  folder, which is no track evidence, and for a title with no word of its
  own the name is none either — the second pass serves the picker (review
  M-3, registered). `_nd_find_album_folder_by` answers `""` for an album the
  library holds under another spelling of its artist or title when it
  carries no release id and no review group lists it (M-4), and "Navidrome
  did not answer" is still "in no folder". The per-file failover
  (`_retry_file_from_alt_source_inner`) walks `alt_sources` without asking
  the rule again. After the re-review (D1–D7): with MusicBrainz down, two
  or more titles a caller held are still counted against themselves — only
  the one-title case asks for a known total; an import that ends before it
  looks at a file (no tracklist from MusicBrainz) takes no "imported" mark
  back; Import as-is with an identity that names an artist and no album
  still lets the import ask MusicBrainz for the title; and the artist
  evidence by path still passes any folder that holds a one-word artist's
  word — D2 closes that for singles only.

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

**A library group follows its Navidrome album when the album is renamed** (F2,
`_follow_renamed_albums`, the first thing a union does). A library group's id is
built from its artist's name and its album's title, so a rename that leaves the
Navidrome album id alone — an artist merge of an album whose files carry a
release id, a retag of the artist or the title by hand — brought the album back
from the next scan under another id, and the union appended it beside the group
it had: two tiles, with the user's state on the one the index no longer named,
for ever. Now an incoming `library` group whose set of Navidrome album ids is
not empty and is that of **exactly one** existing `library` group under another
id takes that group's place: the existing row is given the incoming id in one
hold of `_review_lock` (the old id marked, so the flush drops its row), and the
merge carries everything as for a rescan. One `_web_log` line each. **Not with
work in flight** for the existing group — a live transfer, an album group of its
own not cancelled, a placement claim, a verifier, each of which names the group
by the id it has: then nothing is done, the incoming group is *not added
either*, and the scan after the work has ended moves it. **That look is made
inside the lock hold that moves the group**, not before it: a fetch or a hand
pick holds `_review_lock` across its slskd calls and registers what it queued
before letting go, so a union that waited for the lock looks once it has it.
**A move held back is one `_web_log` line too** (the album as the group that
stays has it, "will follow its album at the artist's next scan"): the scan
stores its index row under the incoming id all the same, so until the artist
is scanned again — the Rescan button, or the auto-indexer weeks on — a
client's gap for that album answers "Group not found" while lb-bot's own list
shows the tile under the old name. Rescan the artist once the download has
finished. Not when a group with the incoming id already exists, and not when
two groups list those albums. It runs in every artist's rescan: on a copy of
the live index, of 3,042 library groups 3,038 keep their id and 4 move, each
an album whose *title* in Navidrome changed with its Navidrome id kept (Björk
– Post, D'Angelo – Voodoo, Led Zeppelin – [Led Zeppelin IV], Mac Miller –
Circles; the re-reviewer's count — every group's id against the saved
Navidrome album table, probe `lbfinal2/r1_rekey_bound.py`, method in the
round's `final-rereview-lbbot.md`). What it can carry from the wrong group:
nothing, unless two `library` groups list the very same Navidrome albums.
**Known and left:** the re-key, the merge and the swap are three holds of
`_review_lock`, so two unions for one album under *different* ids that overlap
within milliseconds (a scan on the 5-minute album cache beside one on a fresh
list) can leave two groups again — one moves it there, the other back, and
the first then appends its own — and "not when the incoming id exists" then
keeps them apart. One album listed twice, the user's state on one; no file is
touched.

This is the fix for a real data loss. Until 2026-09-22 the entry point was
`_store_review_groups`, which replaced the **whole** list, with a comment stating
that was fine because "callers are full-library rebuilds where `groups` is the
complete truth". That was true of scan-all and of neither other caller. On
2026-09-20 a ListenBrainz playlist scan therefore replaced a ~3000-group library
review with its own 97, silently and with no way back — `gaps.needs` went from
~3000 to 78 and stayed there. Nothing in the UI or the logs said a thing. If you
add a scan, give it an origin and use `_replace_review_groups`; if it only covers
part of a set, union instead.

**`repair` is retired (B-022), and so is the whole repair-job pipeline.** Its own
steps went in `c5bcb77` (2026-07-03: the matcher/stager/importer, the
`/api/repair-jobs/*` routes, `RepairQueue.jsx`); what survived was a shadow copy
of the review — a `repair_jobs` dict created on every fetch/search/decision route
and updated from the enqueue, the poller and `_set_review_track_state`, which
nothing read except two things that did harm. `_merge_review_groups` turned
every active job whose group a scan no longer listed into an origin-`repair`
group — no albums, no sources, tracks stuck on `downloaded`, nothing that could
ever clear it (58 on the NAS) — and `_apply_repair_job_projection_to_group`
copied each job's track statuses back over the review on every rescan. All of it
is deleted, `LB_BOT_REPAIR_JOBS` with it. The state file's `repair_jobs` key is
still read, ignored, and written back unchanged (`_retired_repair_jobs`) so the
previous release keeps its jobs on a rollback. `_review_track_id` (the old
`_repair_track_id`, same hash) survives as `track_id` on
`_canonical_tracklist_from_group`'s rows. Operations no longer carry a `job_id`.

The origin-`repair` rows that exist are hidden once at startup by
`_hide_retired_repair_groups`, not deleted: unhide is one tap, and their files are
still in /downloads for Placement. It is
recorded in `library_index.db`'s **`migrations`** table (`name`, `applied_at`,
`detail`), written only after the rows landed — a failed flush leaves no row and
the next start retries — and never runs again, so an unhide sticks. That table is
the place for any future one-time data migration that the data alone cannot say
has run; an older release ignores it. `repair` stays in `REVIEW_ORIGINS` because
those rows still carry it.

A retired row carries **the id of the group its job was for**, so the real album
can come back under that id — and since `INDEX_SCAN_VERSION` 3 re-indexes every
artist, it does. **It comes back visible, and clean** (F1): `_merge_review_groups`
inherits **no track state** from a `repair` row into a non-`repair` one, except
for a row a transfer is running for now. A retired row's tracks are stuck on
`downloaded`, each naming a file in /downloads that nobody chose for that slot;
carried over, the album was visible again with them, and the startup pass
places a visible group's `downloaded` rows at the next restart. On a copy of the
live index that was Cocteau Twins twice and six alternate mixes of "Houses of
the Holy", which the placement matcher takes for the track whose title they
contain (that matcher's containment tier is registered, and older than this).
The files stay in /downloads for Placement; `_download_stamped` (above) is the
second lock on the same door.

Three more rules keep a hidden zombie from shadowing the album:
`_merge_review_groups` does not carry `hidden` from a `repair` row into a
non-`repair` one (the zombie's `hidden` is the migration's, not the user's);
`_replace_review_groups` lets a fresh non-`repair` row with the zombie's id
displace it rather than skipping the fresh row as a duplicate id; and both union
and replace leave `repair` rows out of identity folding, since folding into one
hid the album and folding its tracks into a real row would list a placed track
as missing.

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
  there cannot be more of those than Navidrome has artists — plus one per key an
  artist leaves when its MBID override is set or removed (below), which is one
  per change the user makes by hand.

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

**The worker says how far it is** (ruling U1, 2026-10-10). The bump to
`INDEX_SCAN_VERSION` 3 made every artist stale and the worker walked all of them
for hours; lb-bot's own web UI showed that as text rows in Settings and nowhere
else. After every tick `_auto_index_public` also carries `total` (the library
artists the worker holds), `remaining` (how many of them are not up to date by
its own rule — no row, a stub, stale, an older scan version; **an artist waiting
out a back-off counts, one judged by a fresh shared row (`covered`) does not**)
and `current` (the artist being scanned, for the length of its scan, "" otherwise).
All from what the tick has in hand (`_auto_index_select(..., backed_off=)`,
`_auto_index_progress`): no request and no index read is added. **`total` and
`remaining` are absent until the worker has read the artist list once — never
0.** One log line when a backlog starts and one when it is done.
`GET /api/system/status` spreads them in `autoIndex`. **`GET /api/summary` has
`index`** (`_index_progress_view`): `{enabled, running, outcome, pausedUntil,
current}`, `total` / `remaining` once known, and `build` (`{done, total,
current}`) only while a manual build runs. It is a copy of memory — that route
is on every screen's poll — so it reads no index and asks nobody; a test holds
it to that. In the web UI **one function says what may be said**
(`web/src/lib/indexProgress.js`; `node --test web/src/lib/indexProgress.test.mjs`
is its state matrix, no dependency): the **sidebar** shows a box on every page
while a build runs, the worker has artists it can scan now, or MusicBrainz is
not answering — and nothing when the numbers are absent, the worker is off,
nothing is left, or all that is left waits for a retry (a sign that could
never leave). It is a box of the download tray's own kind under the tray
(`IndexAtWork` in `Shell.jsx`, since the 2026-10 redesign of the web UI): the
short label over a meter — the sidebar is narrower than the long label, which
is the box's `title` — clipped, never wrapped, and it opens System › Status.
**On a phone the sidebar is a bottom bar and its foot is not shown** — the
tray and the connection status with it — so there the numbers are on the
System page only. **System › Status › Background work** shows the same
numbers: "Up to date" with a meter while artists are left, and a "Now" row;
"Up to date" is the worker's count, and the older "Artists indexed" row
(`/api/library-index/status`: has an index row at all) is shown only when the
worker has no numbers or a manual build is running — one definition per count,
each with a `title` that says what it counts. (Before the redesign the
indicator sat in the header's spacer, sized by container queries, and the rows
were Settings → Status → Library index.)

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

### Artist MBID overrides — `GET /api/overrides`, `POST /api/artist/override`, `…/remove`

Since I-019 (2026-10-03, branch `feat/substreamer-parity`, not deployed when this
was written). Every consumer keyed a library artist by the MusicBrainz id
Navidrome reports from the file tags, or by lb-bot's own name-search guess when
there is no tag, and nothing could correct a wrong one. An override is the
user's own pick: one row of `artist_overrides` (`nd_artist_id` PK, `artist_mbid`,
`name`, `tag_mbid`, `set_at`) in `library_index.db`. The table is **outside the
change feed** — no seq trigger names it, it is not in a mirror, and clients read
it whole. No `INDEX_SCAN_VERSION` / `WIRE_VERSION` bump. The section comment
above `_artist_overrides_cache` is the long form; what binds a future change:

- **One choke point: `_nd_artist_index()`.** It hands each overridden artist out
  with `musicBrainzId` = the override and Navidrome's own kept as
  `tagMusicBrainzId`, applied on the way *out of* the cache
  (`_apply_artist_overrides`), so a set or a remove is seen at once. Anything
  that needs a Navidrome artist's MBID must read it from there (or from
  `_artist_index_rows()`, whose overridden rows also carry `tagMbid`); a new
  direct `nd_get_all_artists` caller would bypass every override. The two
  callers that keep a list for longer re-apply at each use: the auto-index
  worker's 15-minute copy (in `_auto_index_tick`) and a manual build's rows (in
  `_library_index_task`). The function is reversible for that reason — a row
  that already carries the tag key is read back through it.
- **The read swap.** `_index_get_artist` looks an overridden artist up under the
  override and nowhere else, ignoring the caller's `mbid` and never falling back
  to the Navidrome id. `POST /api/artist/discography` and `POST
  /api/artist/release` swap a client's `mbid` the same way
  (`_effective_artist_mbid`), so a client with a stale copy of the overrides can
  neither read nor write the artist under its tag.
- **A set or a remove re-homes the index rows** (`_index_rehome_nd_artist`), in
  the same transaction as the table write: every row the artist *holds*
  (`nd_artist_id`, or the `nd:<id>` key) under another key is deleted with plain
  SQL, so the triggers tombstone it. A row another library artist holds is never
  touched. **`present` rows move with the artist** rather than being deleted —
  onto the new key's `missing` row, else as a row of their own under a stub
  parent — because `present` is the only record of a fill a scan cannot see
  (the same reason `_index_store_artist` carries them across a rescan). **The
  one exception: when another artist still in the library holds the destination
  row, nothing is moved into it** — whether that row is fresh (so no scan follows)
  or stale (so the override's scan takes it over) — and the fill marks are deleted
  with the row they were on. That row is the other artist's, and a moved mark
  would be carried across every rescan of theirs.
- **The rescan** is the ordinary `_artist_discography_task`, under Navidrome's own
  name for the artist, so the review is handled by what was already there: a
  review group's id is built from the artist **name**, album title and Navidrome
  album ids — never the artist MBID — so the union replaces groups in place, and
  `_reconcile_artist_review_groups` refreshes or retires the rest.
- **One bucket per MusicBrainz id** (`_artist_library_albums`, 2026-10-09,
  `INDEX_SCAN_VERSION` 3 — B-061, and B-060 with it). A scan is matched against
  the albums of every library entry that **is** the scanned artist: an entry
  whose override-aware MBID (`_nd_artist_index()`) is the scanned one, plus the
  entry the scan was started for (`nd_artist_id`, which every scanner passes —
  for an artist with no tag and no override it is the only way in by id). An
  album belongs by its `artistId`; the old name match is asked only for an
  album no id speaks for, never for a name that normalises to `''`, and for a
  name with letters outside ASCII only when the spelling agrees too
  (`_artist_names_match` — "DJ Грув" and "DJ Смэш" both normalise to "dj").
  Until then the bucket was the albums whose artist *name* normalised like the
  scanned one's: two spellings of one artist were two buckets, the row was
  matched against whichever entry scanned last, the other's albums read
  `missing` (a second copy of an album owned under "King Gizzard and the Lizard
  Wizard" was downloaded for "King Gizzard & the Lizard Wizard"), and every
  non-Latin artist shared the `''` bucket and skipped the reconcile.
  What follows from it:
  - **An override changes what is matched**: an overridden artist joins the
    bucket of the MBID it is overridden to, and leaves its tag's.
  - **A scan from either entry stores the same row**, so who holds a row no
    longer decides what is in it — for entries that *carry* the id. Entries
    that only **guess** the same id (untagged, no override, the name searches
    agree) are not known to be one artist and do not share; "Keep this match"
    is how one joins. The holder rules below are unchanged.
  - **A namesake with an id of its own is somebody else**: two tagged
    "Nirvana"s used to share a bucket by name.
  - The scan result carries what it was matched against (`library`: album ids,
    entry ids, entry names), and `_reconcile_artist_review_groups` selects its
    groups by that album set — plus, for a group whose album is gone and so in
    no bucket, by the entries' exact names or the old name match. So both
    spellings' groups are reconciled from either page, and a non-Latin artist's
    are reconciled at all.
  - `POST /api/artist/release` classifies its one release against the same
    bucket, from the same function.
  - Not changed: `_norm_album_text` itself (the review-group id and the album
    keys are built on it), and who may *store* (`_index_store_artist`, its
    override refusals, the `covered` rule).
- **No scan, and `task_id: ""`, when the row is someone else's**
  (`_artist_scan_covered`, the auto-indexer's `covered` rule, on a set, on a
  remove **and, for an overridden artist, on the rescan route**): the MBID the artist now resolves to has a
  fresh row held by another live library artist. The scan would store under
  that key and take the row — for nothing when both entries carry the id (one
  bucket, so the same row with a new holder), and to store a row with its own
  albums added, which the holder's next scan takes out again, when this artist
  only guesses the id. (Until the shared bucket it re-matched the row against
  this artist's albums alone.) The artist reads the shared row through the read
  swap or its tag instead. A client reads `task_id: ""` as "do not start a scan
  either". **A set's and a remove's answer then carries `covered: true`**
  (I-029, 2026-10-11 — additive, nothing else in either answer changed): a
  blank `task_id` has other reasons too — no Navidrome login, a remove that
  resolves to nobody, an id Navidrome's list does not carry — and lb-bot's own
  web UI says "shares its discography with another artist" after a set or a
  remove, which it may say only of this one. As on the rescan route, `covered`
  is only ever beside a blank `task_id`.
- **`POST /api/artist/discography` asks the same function for an artist that
  has an override** (`_artist_rescan_view`) and answers `{"ok": true,
  "task_id": "", "covered": true}` with nothing started. Until 2026-10-04 it
  had no such check, so after a set that answered `task_id: ""` the page's
  ordinary Rescan — the natural next tap — ran the very scan the override had
  declined. `covered` is always paired with the blank `task_id`: a client says
  "shared with another library artist" only from `covered`.
  **Only for an overridden artist.** Two library entries merely tagged with
  one MusicBrainz id share one index row too — measured read-only on the NAS on
  2026-10-04, **114 such groups, 246 of 2,425 album artists** (collaboration
  credits, renames, transliterations), the holder not always the entry with the
  most albums — and a Rescan from the entry that does not hold it re-files the
  row under that entry, as it always has. Since the shared bucket that is the
  same row either way, so the Rescan is simply how the row is refreshed from
  either page; before it, the other entry's albums read `missing` until it was
  rescanned in turn (B-061), and the Rescan was the only remedy there was. For
  a day the check applied to every artist, which took it away.
  A scan already running is still answered first, and an `mb:` page is
  unaffected: a row an external scan stores is held by no library artist.
  **With Navidrome's artist list unavailable nobody is known to be live, so
  the rule cannot apply and the scan runs**, as it always did (a set and a
  remove answer 502 there; this route was left as it was).
- **A set needs Navidrome's artist list** (502 `retryable`, nothing stored, when a
  login is configured and the list is empty), exactly as a remove does. Without
  it the tag recorded beside the override is unknown, nobody is known to be live
  (so neither the covered rule nor the `present`-move exception can apply), and
  the scan would match an empty library and store every album as missing.
- **409 `busy` while a fill for the artist is in flight** (`_artist_fill_in_flight`).
  "For the artist" = the fill names a release-group or review group of an index
  row the artist holds, or its artist text is the library artist's name. "In
  flight" = an album-fill ledger row in `searching`/`queued`/`downloading`/
  `placing`, `placed` until its verifier is done (`verifyGaveUp` ends it),
  or `failed` with the automatic retry pending; a live `pending_album_groups`
  entry; a review group with a live transfer in `pending_downloads` or a running
  placement verifier. A `queued`/`downloading` *decision* with no transfer
  behind it is deliberately not counted — it is a phantom, and it would lock the
  artist until a rescan the override itself is waiting to start.
- **A scan already running cannot undo a change** (`_artist_override_refusal`,
  inside `_index_store_artist`'s own lock hold, and checked by the scan before
  it unions its groups). Refused: any MBID other than the override for an
  overridden artist (the unresolved `nd:` stub included), and the removed
  override's MBID from a scan that began before the remove —
  `build_artist_discography` stamps `scan_started_at` on its result for this.
  The removed-override memory is in-process only; a restart has no scan to
  refuse.
- **Remove resolves before it deletes.** The artist goes back to the tag, else a
  strict name search; a MusicBrainz outage (or Navidrome's artist list being
  unavailable) answers 502 `retryable` with the override still in place, never
  an unresolved stub (R13). The 409 fill check runs *after* that search, right
  before the write, because the search can take tens of seconds.
- **The scan state follows the override too.** `_artist_discography_read_view`
  looks the scan up under the effective MBID, and a dropped scan reports under
  `nd:<id>` only while that key still names it — otherwise a client still sending
  the tag is shown "failed: this scan was dropped" while the override's scan runs.
- **All three scanners drop an overtaken scan before the union**: the scan task,
  the auto-index worker and the manual build (`_artist_scan_superseded`), and the
  manual build's summary counts an artist only when its row was stored.
- **The picker's candidates are asked for strictly** (`GET /api/artist/lookup`,
  `_artist_lookup_view`): 502 `retryable` when MusicBrainz did not answer, so
  that an empty list under a 200 is only ever a real no-match. Fail-soft, `[]`
  meant both — and for five minutes after any failure of that search the
  fail-soft call does not even ask — so both clients' pickers said "No
  MusicBrainz artists found" for a search that never ran. **Asked `once`**
  (`mbz_get(..., strict=True, once=True)`): one attempt, and for a query that
  failed inside the last `MBZ_FAIL_COOLDOWN` (five minutes) none at all — the
  502 comes from that memory. A plain strict call retries after 3 s and 10 s
  and asks through the cooldown: 13 s to its 502 against a 503, 43 s against
  a timeout, for every query, each queued behind the last on `_mbz_lock` —
  on a picker that searches at every pause in typing, through the hub's four
  proxy slots. The memory is per request, as everywhere in `mbz_get`: another
  spelling during an outage still costs one attempt (up to the 10 s request
  timeout). Every other strict caller insists as before.
- **lb-bot's own web UI** (`web/src/panels/Artist.jsx`, `useDiscography`;
  `Search.jsx`). After `POST /api/artist/discography` a blank `task_id` means
  no scan was started, so there is nothing to poll: the page re-reads `GET
  /api/artist/discography` — under the mbid it just posted, which for an
  untagged artist is the looked-up one, not the empty `artist.mbid` — shows
  what is stored, and when the answer said `covered` adds one muted line ("This
  artist shares its discography with another artist in your library; there is
  nothing to rescan from here."). With nothing stored it shows the error. It
  used to poll `/api/tasks/` — a 404 — every few seconds for as long as the
  page was open, with the skeleton on screen. The same poll now ends on a 404
  from `/api/tasks/<id>` (lb-bot restarted mid-scan and forgot the task): it
  shows what is stored with a line saying the scan did not finish, or "The
  scan was lost". And the search screen renders the lookup's error ("Couldn’t
  search MusicBrainz for artists: …") instead of "No other artists found."
  The album search beside it still swallows its failures.
- **The match picker in lb-bot's own web UI** (I-029, U2 — 2026-10-11;
  `web/src/panels/ArtistMatch.jsx`, one component in two homes). Under **Needs
  you › To fix** it is the pane of an artist to match (`ArtistDetail` in
  `NeedsYou.jsx`); on the **artist page** a quiet line by the hero —
  "MusicBrainz match: <name> · Fix match" — unfolds it (`DiscographyView` in
  `Artist.jsx`; not on an `mb:` page). **What may be said is one file**,
  `web/src/lib/artistMatch.js` (pure; `artistMatch.test.mjs` is its matrix).
  - **What lb-bot uses now** (`matchNow`): an override in `GET /api/overrides`'
    `artists` is "your choice on <date>"; else the artist list's `mbid` is
    "from your files' tags" (a row that carries `tagMbid` is an override — and
    since that list is kept for a minute, once lb-bot's own answer is read it
    is the one believed, and the row's `tagMbid` is then the tag); else a
    `toCheck` entry — `unmatched` is "lb-bot's own guess" that holds none of
    the albums, `unresolved` is no match; else the id the artist page's own
    discography read names (a guess that holds albums); else "lb-bot goes by
    its own search for this artist". `toCheck` absent is not "nothing to
    check". **Neither answer carries the MusicBrainz artist's own name**
    (`artists[].name` and `toCheck[].name` are the library's): it is read from
    `GET /api/meta/artist?mbid=` — once per id, only while the picker is on
    show, and not at all where the artist page has read it already or the user
    has just picked the candidate. When it cannot be had the row says
    "MusicBrainz artist" and still links out.
  - **"Keep this match"** beside "Choose another" for an `unmatched` guess: an
    ordinary set to the guess. **"Go back to the files' tag" / "… lb-bot's own
    search"** for an override, by whether it recorded a tag.
  - **The search** holds the artist's name. It is asked when "Choose another"
    is pressed, on Enter, and `SEARCH_PAUSE_MS` after the last keystroke — one
    request at a time (another search, the search shutting or the pane being
    left ends the one that is out), never for a blank field, never twice for
    the same text on a pause; for an artist with no match the field is simply
    there and nothing is asked until Search is pressed. A pasted MusicBrainz
    artist id or address is that artist (named by the meta route; offered even
    when it cannot be named — the set is what asks MusicBrainz whether it is
    one). **A search that did not run is never "No MusicBrainz artist by that
    name."**: 502, a timeout (20 s) and anything else that stopped it are
    "Couldn't search MusicBrainz" with Try again. Candidates already on the
    page stay, dimmed, while another search is out.
  - **A set or a remove, then the scan.** One press is one request. On the
    answer the picker becomes the job: `task_id` is polled at
    `GET /api/tasks/<id>` every 2 s — "Reading <artist>'s discography again…"
    with the whole scan's meter — and ends "N of M albums line up": of the
    albums the scan bucketed for the artist (`result.library.album_ids`), how
    many a release that is in the library holds (`linedUp`). A blank `task_id`
    with `covered` is the artist page's own muted sentence (`SHARED_NOTE`,
    which now lives in the lib); a blank one without it says only that no scan
    was started. A scan that failed or that lb-bot forgot (404) says so *and*
    that the match itself is set. lb-bot not answering a poll is asked again
    for three minutes since it last answered (`SILENT_MS`), then said, and the
    asking stops. 409 is lb-bot's text; 502 offers Try again.
  - **The scan is a job of the one watch** (kind `scan`, U2 fix round;
    `scanMachine` in the lib, a reader in `lib/jobs.js`): followed by the
    artist's id — the two homes know that, not the task's — and asked about
    by its task, once however many homes show it. Leaving the pane stops
    nothing; coming back shows it where it is (the artist page opens the
    picker on arrival for it); and a scan that ends with nobody showing it is
    one toast, "<artist>: 4 of 5 albums line up." or what the card would have
    said. An answer that starts no scan has nothing to follow and is the
    pane's own.
  - **Not known is said as not known.** `toCheck` absent — lb-bot could not
    work its list out, likely right after a set while Navidrome is busy — is
    "no list": the pane says so with Try again (`why: 'nolist'`), never "its
    own search", and the To fix rows stay as they were (`keepListed`: an
    artist is "Matched" when it left a list lb-bot *gave*, or when the answer
    itself says it has an override). While the artist page is still reading
    its discography the line says nothing and the picker is a skeleton; once
    it has read and names nobody both say "No MusicBrainz match" (`indexMbid`,
    from `pageRead`: an id, `''`, `undefined` while that read is out, `false`
    when it failed, `null` where there is no such read). **A read that failed
    is not known either** (U3 fix rounds, R3 and R3a — `why: 'noread'`):
    nothing in a failure says there is no match, so it is not "No MusicBrainz
    match", and not a skeleton that waits for an answer nobody is fetching.
    **And it never takes the way to the picker away**: the line says
    "MusicBrainz match: not known" with "Fix match" beside it (nothing while
    the read is still out), and the picker says "lb-bot could not read this
    artist's discography just now." with Try again — the page's own retry of
    that read — *and* "Choose another" (`nowView`'s state `noread`): choosing
    an artist is the one thing that mends a name MusicBrainz's search does not
    find. For a day (R3) the line said nothing and the picker hid its search,
    so an artist whose scan failed could not be matched at all. **The page's
    own lookup answering with no candidate is an answer, not a failure**
    (`nobody` in `useDiscography`, the third argument of `pageRead`): the page
    shows it as "Discography scan failed · No MusicBrainz match for …", and
    "No MusicBrainz match · Fix match" is true then and is said, the picker's
    search simply there. `noread` is only a request that did not answer, or
    answered an error.
    The sentences are `NOT_KNOWN` in the lib. **Under To fix, a list
    lb-bot did not give is said as that** (R5, `toFixNote` in
    `lib/merges.js`): an answer without `toCheck` or without `siblings`, or no
    answer at all, puts a note with Try again in the list — never "Nothing
    needs a decision", which is a claim about a list nobody gave. **The note
    says what is missing** (M2): the artists to match, or both lists — "lb-bot
    could not work out which artists need a look just now."; the sets alone —
    "lb-bot could not work out which artists are under two names just now."
    **A skeleton stands where the two groups will be only while a read is
    out, the lists are not both held, and the page draws neither group** (Z5,
    then F3 of the final fix wave; `toFixWaiting(state, drawn)`): the first
    read, and a Try again from a page that has neither — a heading and three
    rows (the groups come after the other lists, and with nothing in their
    place they pushed "Left on disk" down by their whole height in one frame),
    with neither the note nor "Nothing needs a decision" said beside it.
    **With a group on the page, a read that is out moves nothing**: the page
    keeps the rows it has through an answer that left a list out
    (`keepListed`, `keepSets`), so the note stays where it is and as it is
    until that read's answer replaces it. **Its Try again shows that it was
    pressed**, for as long as the read the press asked for is out
    (`aria-busy`, the frame's busy look; `aria-disabled`, not `disabled`, so
    the keyboard stays on it; a second press sends nothing). A read nobody
    asked for leaves the button as it is too: the spinner is eighteen pixels
    in a sentence, enough to wrap the line under it. The press is kept with
    the read it asked for (`read({ asked: true })`, `asked` in
    `useOverridesRead`'s state: set when that read starts, gone when a read
    ends; a read nobody asked for that starts behind it keeps it, since its
    answer is the one the press gets) — nothing beside it remembers the
    press, so nothing can forget it late or keep it past its answer.
    For a round (Z5 alone) the skeleton came up for any read while a list was
    missing — under groups that were drawn, with the note taken away, for the
    length of a read nobody had asked for. The skeleton's rule is a function
    of what is drawn and whether a read is out (`reading` in that state: a
    Try again after an answer keeps `status` as it was); nobody has to
    remember who asked. A `musicbrainz.org/artist/`
    address with no id in it is a wrong address, said as one.
  - **Under To fix the artist stays**: the list and the picker read one
    `GET /api/overrides` (`useOverridesRead`), read again behind the page when
    lb-bot has stored a change and when the scan is over; the artist leaves
    lb-bot's list and stays in the page's — in its place, its row saying
    "Matched" (`fixRowView`) — until the tab is left, whatever else is picked
    meanwhile (`arts` in `FixTab`: every artist listed on this visit; the
    count beside the heading is lb-bot's, the ones still to match). A re-read
    that fails keeps the list it had and says so in the pane.
  - **What the artist page kept is let go of wherever else the change is
    made** (F1, final fix wave). The artist page keeps each scanned
    discography for the session (`discoCache` in `Artist.jsx`) and shows a
    kept one with no request at all. Only that page's own surfaces used to let
    go of it: a match kept under To fix, its scan over, "Open artist page" —
    and the page showed what it had read before ("scanned yesterday", the old
    match's releases). **`forgetDiscography(ids)`**, exported beside the
    cache, is the one way in (no page reaches into the `Map`): the ids of the
    artists a change touches (`artistsTouched` in `lib/merges.js`: an artist
    match's own artist, both names of a merge), or — with no list — everything
    kept, where the artist's id is not in hand (an album's row names its artist
    in words). Called for every change **answered or seen to end outside the
    artist page**: under To fix, a set or a remove when lb-bot has stored it
    and again when its scan is over (so also at the POST's own answer when no
    scan is started), and a merge or its undo at its end; on System ›
    Overrides, a Remove of a match at its answer, and every followed job at
    its end (an undo of a merge, of an album's edition or cover; a scan); a
    job that went silent and that a later answer of lb-bot's list says is
    over (nobody saw it end); and — the shell's part, `onJobEnded` in
    `App.jsx` — a job that ended with no page showing it, before its toast.
    The cache is a convenience of the session: a page that reads once more is
    right, one that shows the match from before is not.
  - **On the artist page** a stored change reads the artist list again at once
    (`reloadArtists` — every view that holds the list follows), so the page
    goes by the id the artist has now; the discography is read again, behind
    what is shown and under that id, once nothing more will change — the scan
    is over, or none was started (`onChanged`: `stored`, then `ended`).
    **The page reads `GET /api/overrides` once on arrival** (since U3 — it
    says whether the artist is in the library under another name too, below
    § Artist name merge); the picker reads that same answer when it opens,
    and its own requests (the search, the MusicBrainz name) still wait for
    "Fix match". An `mb:` page asks nothing.
- `_artist_overrides()` never raises (it sits under pages that have nothing to
  do with the index DB) and answers "none" for an unreadable table;
  `GET /api/overrides` reads strictly and answers 503, because a client replaces
  its own copy with that answer.
- **`toCheck` on `GET /api/overrides`** (I-021, `_artist_matches_to_check`) lists
  the artists an override is for, so they need not be found by opening artists
  one by one: `{ndArtistId, name, kind, mbid, albums}`, most albums first. An
  artist is listed when Navidrome reports at least one album for it, carries no
  MBID tag for it, it has no override, and lb-bot's own match holds nothing —
  `unmatched` (an MBID-keyed index row, the name search's guess, with no
  release-group other than `missing`; `mbid` is the guess) or `unresolved` (no
  MBID-keyed row at all: never indexed, or only the `nd:` stub; `mbid` is "").
  Computed at read time from the override-aware artist list and one read of
  `artists` ⋈ `release_groups`. `[]` means "nothing to check"; the field is
  **absent** when that could not be worked out — Navidrome's artist list
  unavailable, or any failure of its own — because a client reads a missing field
  as "no list" and an `[]` nobody can vouch for as "nothing to check". Its failure
  never costs the override map, which both clients need. It means "worth a
  look", not "wrong": an artist whose match is another library artist's row
  (`covered`) holds no row and reads `unresolved`. Do not work around that here
  — "Keep this match" in the clients is an ordinary set to the guess, which takes
  the artist off the list and, now, puts its albums in the shared row's bucket.
  (Until the shared bucket a non-Latin artist's correct guess often matched none
  of its albums — B-060 — and was listed for that.)

Two limits that are known and **not** fixed (confirmed in the I-019 review):

1. **The review is reconciled only by the scan task.** `_reconcile_artist_review_groups`
   runs inside `_artist_discography_task` and nowhere else, so when the override's
   scan does not run to its end — lb-bot restarted and the auto-indexer picked the
   artist up instead, the scan was overtaken and dropped, or a set **or a remove**
   was `covered` and started none — the review groups built under the old MBID are
   left as they were. Stale, not corrupted: each still describes a real Navidrome
   album against the release it was built for. For the first two cases a manual
   re-index of the artist (the Rescan button, `POST /api/artist/discography`)
   reconciles them. **For a covered artist there is no such remedy, and the
   route says so**: it answers `covered: true` and starts nothing, where it used
   to run the very scan the override had declined and take the other artist's
   row. Its stale groups stay for as long as the artist is covered.
2. **A row two library artists read belongs to whichever holds it** (the
   shared-row model). The shared bucket made the row's *content* the same
   whoever holds it; it is still one row with one holder. If the
   overridden artist holds a row a second library artist also reads (same tag, or
   the same name-search guess — the auto-indexer's `covered` case), the set
   deletes that row, since this artist no longer is that MBID. The second artist
   reads "not indexed" until the auto-indexer (or its own page) rescans it, and the
   rebuilt row has no fill marks: the `present` rows went with the artist that
   was overridden (or were dropped, by the exception above).

### Album overrides — `GET /api/album/override`, `…/covers`, `POST /api/album/override[/preview|/remove]`, `POST /api/album/cover`

Since I-022 (2026-10-10, branch `feat/album-override`, not deployed when this was
written). For an album the library owns, the user picks which MusicBrainz release
it is, and lb-bot **retags the album's files** to it: release and release-group
ids, disc and track numbers and their totals, titles, the album title, the date.
A Cover Art Archive image can be saved as the album's cover with it, or on its
own. Everything can be undone. The wire is the round's `contract.md`, frozen with
the hub and both clients; the section comment above `class NavidromeUnavailable`
is the long form. What binds a future change:

- **The tag is the pin.** Nothing remembers "this album is release R" and
  applies it at read time, the way an artist override is applied to Navidrome's
  artist list. The files say R, so Navidrome says R, so the scan, the gap
  detector and the clients agree without knowing an override exists. The
  `album_overrides` row is the record of the change and the handle on its undo.
  Do not add a read-time lookup of that table to a matcher.
- **What it needs from Navidrome: the files' real paths.** Navidrome hands a
  Subsonic client a path it *makes up from the tags* — `<album artist>/<album>/
  <DD>-<TT> - <title>.<ext>`, every `/` in a name a `_` — unless **"Report Real
  Path" is switched on for that client's player** (Navidrome's settings →
  Players → the `listenbrainz-bot` player, `ND_CLIENT_NAME`). Measured on the
  live server on 2026-10-10 with it off: 74 of 15,084 songs happen to be named
  that way on disk, and the made-up folder exists for 87% of the albums. So
  `preview`, `apply` and `cover` answer **409 `files_unreachable`** with
  `reachable` and `total` unless lb-bot finds **every one** of the album's
  files inside its music folder (`_album_files_refusal`), and the `error` text
  says where the setting is. Two rules in it:
  - **All or nothing.** A retag of the files that happen to be found would
    split the album in two (the rest keep the old release), and the cover's
    folder would be a guess.
  - **lb-bot does not guess which file is which**, and does not change
    Navidrome's settings. The right folder plus a title or a size would often
    find the file — and when wrong would rewrite another song's tags for good,
    with a snapshot that says the wrong song was always that. The path is
    Navidrome's to give.

  `GET /api/album/override` is not affected: what an album is and what it holds
  is Navidrome's to say. **The undo does not ask Navidrome at all** — it works
  from the paths it kept — so an override applied with the setting on can be
  undone with it off. Its own 409 `files_unreachable` (other words, same code)
  is for a folder that is no longer there; see the undo below.
  Every test fakes a Navidrome that reports real paths unless it says
  otherwise (`_FakeNavidrome.synthesized`, `AlbumFilesUnreachableTests`).
- **Tags only, and not the artists.** Nothing is renamed or moved — a retagged
  file keeps its name, which after an override may say another track number
  than its tags do. `ALBUM_OVERRIDE_TAGS` is what is written (through
  `_mutagen_write_tags`, so L2's alias deletion applies); the album artist and
  the track artist are not in it. An override says which release an album is,
  not whose; that is the artist merge's business.
- **An MP3 keeps its ID3 version** (`own_id3_version=True`, `_id3_own_version`).
  Placement saves v2.3 whatever a file was, which is its right over a file out
  of /downloads. For a library file it is not harmless: saving a v2.4 tag as
  v2.3 joins every multi-valued text frame into one value (`TPE1` "Artist A",
  "Artist B" → "Artist A/Artist B", `TCON` likewise) and drops the frames
  mutagen does not know — frames the override does not write and no snapshot
  holds. So the override writes, and the undo restores, in the version the
  file has (the snapshot records it); a file with no tag gets v2.3.
  **And a v2.3 tag keeps a frame's several values** (`v23_sep=None` in those
  two saves): v2.3 has no way to say "two values", taggers that want one write
  them NUL-separated all the same, mutagen reads those as two and — saving
  v2.3 the ordinary way — joins them with "/" (`TPE1`, `TCON`, `TCOM`,
  `TXXX:ARTISTS`, `TXXX:MusicBrainz Artist Id` → "art-1/art-2"), again in
  frames nobody named. Placement still joins.
- **The row has its own id, and the album is found by its folder and its
  files.** A retag is expected to change the album's Navidrome id — its id rule
  follows the release — but that is **expected from Navidrome's id rule, not yet
  watched on the live server**; everything here is built so that it holds
  either way. If it does, the id a client opened the album by is dead before
  the job ends. A job is polled and an override undone by the row's `id`.
  `nd_album_id` is the id it was applied to, `album_id` the id Navidrome gave
  the album afterwards (`""` until known — `albumId` on the wire), and `folder`
  is what does not change: `_album_override_of` matches an album to its row by
  either id, else by folder and release — **and by folder only for an album
  whose files say a release** (F3): two untagged albums in one folder (singles
  kept loose in an artist's folder) have the same release too, none, and each
  read the other's row as its own.
- **First write wins in `tag_changes`.** A file's tags are kept before the file
  is first written and never again (`INSERT OR IGNORE`), so a second apply on
  the same row keeps the earliest snapshot and an undo always goes back to
  before the first. `_tag_snapshot` keeps **every key `ALBUM_OVERRIDE_TAGS` can
  write and every alias the write deletes**, whatever a particular release
  fills in — a release with no date writes none, and the next one's date would
  otherwise overwrite a value nothing kept. `_tag_restore` puts back every value
  of every key and deletes a key that was not there; the order of comments in
  the tag block and the case of key names are not preserved. An MP3's frames
  are kept the same way (`_ID3_FRAME_OF`, `_ID3_ALIASES`); a format the writer
  cannot write has no snapshot and is never touched.
- **Every file is kept, on disk, before any file is written**
  (`_tag_changes_apply` → `_tag_changes_keep_all`, the loop an artist merge
  writes with too): one commit for all the snapshots of an apply, made
  with `synchronous=FULL` (`_index_commit_durably` — the index is WAL +
  `synchronous=NORMAL`, under which a commit reaches the disk at the next
  checkpoint and a power cut takes the last ones back). The row itself is made
  the same way, and so is a cover change's. A cover's "snapshot" is the image
  moved out: `_keep_file` syncs the copy before the original goes (the library
  and /config are different filesystems, so a move is a copy and a delete).
  If the snapshots cannot be committed, nothing is written.
- A file whose tags cannot be read is not written. A file that cannot be
  written is a `failures` row (`{title, reason}`, never a path) and the rest go
  on: `applied` with failures is a partial result. `done` counts files written;
  `total` is the files that **change** (`changed` in the preview: a slot's
  numbers, title or release id differ from the file's own). A file that is
  already right is not rewritten, so re-applying the same release writes
  nothing. **A file whose write failed and that is as it was loses its
  snapshot there and then** — kept, it is what the undo fails on for ever (a
  file that cannot be written cannot be restored either). Only the job's own
  snapshot, and only if the file really is unchanged.
- **One mapping, in tiers across all the files** (`_map_files_to_slots`, shared
  by the preview and the apply): every file whose recording id names a slot,
  then the title tiers over what is left — exact before prefix before
  containment before fuzzy, each across every unplaced slot, with the sibling
  tighter-claim check — then a duration that names one slot and one file.
  Placement's `_match_slot` walks the *slots* and tries every tier for each,
  which is how a duration match for an early slot can take a file titled for a
  later one; do not reuse it here. The title is the file's title **tag**: the
  filename helpers strip a leading track number, wrong for a song called
  "1979". A file with no slot is left untouched (Navidrome then lists it as an
  album of its own, under the old release). The plan gives no slot to a file
  with no path either, though the routes refuse before one is made.
- **An apply, a remove and a cover change are placements of the album.** Each
  holds the placement claim (`_group_placement`, through `_album_claim_groups`)
  of every review group on the album while it changes files, so no fill places
  into the folder and no cancel settles its rows meanwhile. Each is refused 409
  `busy` while a fill is in flight (`_album_fill_in_flight`: the ledger for
  either release via `_fill_entry_moving`, a live transfer group, and for a
  review group on the album a live transfer, a row still queued or downloading,
  a running verifier, or a placement that owns it) — and **asks again once it
  holds the claim**, about the same album the request asked about: every id
  and both releases it has gone by. If the wait for the claim was a real one
  (another placement had it, or it took `ALBUM_OVERRIDE_REPLAN_AFTER`), **the
  apply plans its writes again** from the files as they are now
  (`_album_override_replan`, with the request's own refusals).
- **A running job holds the album, and Fill gaps starts nothing for it.**
  `_album_override_jobs` (in memory, `_album_override_job_begin` / `_end`) is
  the albums a job has right now: an apply or a remove from the request's
  answer until the album is listed again and the index told (or the minute's
  wait is over, or the job died), a cover change for the length of its
  request. It is what 409 `in_progress` reads besides the row's state — a
  cover change gives the row no state of its own — and what the Fill-gaps
  routes read: **fetch, auto, pick-file and source download answer 409
  `{"ok": false, "error": …}`, no `code`, and queue nothing** for a review
  group that holds such an album (`_album_override_holds_group`, which returns
  the job's own words: `ALBUM_OVERRIDE_FILL_REFUSAL`, or an artist merge's). The same look is in `_enqueue_group_source`
  and `_pick_file_for_track`, **inside the `_review_lock` hold that queues**;
  the job, for its part, reads the group's rows under that lock once it has
  the claim. So one of the two always sees the other, which is what closes the
  hole a fill used to fall through: started while the files were being
  rewritten, it waited for the claim and then placed under the release the
  album had just been moved off. The look reads memory only — nothing slow was
  added under `_review_lock`. A job's end is its own (a token), because a
  job's look for the album can outlast it into the next job.
- **"Is a job running? Then this one is" is one step**
  (`_album_override_claim_lock`): the look and the row write that answers it,
  for an apply, a remove and a cover change. 409 `in_progress` otherwise. The
  lock is not held across a file or a request: the cover's files are moved
  after it is released, under the job registry and the placement claims.
- **No row is left `applying` or `removing` by a job that died, and no album
  left held**: an exception in the task, a task that could not be started
  (`_album_task`) and a restart (`_album_overrides_interrupted`, at startup)
  all leave the row `failed` with the way out as a failure row, and the
  snapshots are still there for the undo.
- **A job that wrote nothing ends `failed`, at once** (ruling X14 — the
  merge's too). An apply whose every write failed (`done` 0 of N), or a forced
  one for a release none of the album's files has a place on, does not wait
  for a scan, asks for none, tells nobody, and never reads `applied`: the
  album would be shown as overridden to a release no file of it carries. Its
  failures say why (the files' own reasons, or "none of this album's files is
  on that release"). **Not a no-op**: applying the release the files already
  carry is `applied` 0 of 0 as before — every file is on it. One file written
  is an apply (`applied`, with the failures).
- **On a second press `total` is the files that press tries** (F7,
  `_undo_total`): a file it gives up on is neither restored nor a failure, and
  with the total still counting it the clients' "the writes are over: `done +
  failures >= total`" (X4) never came true — a stuck bar instead of "waiting
  for Navidrome". `done` stays "files restored".
- **An undo that put back part does everything a whole one does, and then
  reads `failed`** (X14; `failed=` on `_album_override_follow` /
  `_artist_merge_follow`). Some files restored and others not — or all of them
  and the cover not — is a library that changed on disk: the row stays
  `removing` through the same wait for Navidrome, the album that holds the
  restored files is looked for, the old review groups are retired, the release
  refreshed, the hub told; where a whole remove deletes the row, this one
  writes `failed` with its failures — at that same point, so a client that
  re-reads on the notice reads how it ended. **An undo that put nothing back
  is `failed` at once** (it only asks Navidrome for a scan): a failure seen
  before the first poll is the clients' to notice (they seed "was active" from
  the POST's own `state`), not something lb-bot holds a state for.
- **`applied` waits for Navidrome, and "listed again" is after a scan and by
  the files** (`_album_override_follow`). After the last write the row stays
  `applying` until Navidrome lists the album again — then `applied` carries
  its new id — or `ALBUM_OVERRIDE_INDEX_WAIT` (60 s) has passed, then
  `applied` with `albumId` "". Two conditions for "again", because looking
  sooner found the wrong album (half of a split album that carried the
  release all along) and looked at the old album's id while it was still
  listed:
  - **A scan that began after the last write has ended** (`_ScanAfterWrites`).
    Navidrome does not say when a scan began, so: a look that reads *not
    scanning* after the last write (a scan found running is waited out — it
    may have passed the folder already); a scan asked for at that look; and
    then either that request was taken and a later look reads not scanning, or
    `lastScan` has moved (a scan of Navidrome's own, when `startScan` — an
    admin's — is refused; it is asked for again at every look until then). A
    status that cannot be read is no answer.
  - **The album holds the files this job wrote** — for a remove, the ones it
    put back (`_nd_find_album_in_folder(..., expect)`): for an apply every
    file that is now on the release, written or already right, less the ones
    that failed.

  A remove's row is deleted on the same rule (404 `no_override` is how a poll
  learns it finished). lb-bot goes on looking for ten minutes to fill the id
  in. **The look belongs to its job**: a row since undone or applied again
  (`set_at`, which every apply stamps anew) ends it unwritten — it would
  otherwise announce the album a second time for the next job.
- **Once Navidrome lists the album** (`_album_reindexed`): review groups keyed
  on the id it had are retired as `_reconcile_artist_review_groups` retires a
  group whose album is gone — only for an id Navidrome really no longer lists
  (files with no slot keep the old album alive under the old id), asked after
  the scan has ended, and never one with work in flight; the release is
  refreshed through `_artist_release_refresh_view` (the code `POST
  /api/artist/release` runs), and the release it was when that is another
  group; the hub gets the existing `albumIndexed` notification with the new
  album id.
- **An undo can always be finished — and never gives up on a file at one
  press** (`_tag_changes_put_back`, the loop an artist merge's undo runs too).
  What stands in its way is a
  `failures` row on a row left `failed`. Per file, by what one look at it
  says (`_file_look`: `os.lstat`, and **only "no such file" — ENOENT, ENOTDIR
  — means the file is not there**; permission denied, an I/O error, a stale
  handle say nothing about the file, and `os.path.isfile` is False for all of
  them):
  - there, and already as its snapshot says: not written again;
  - there, and cannot be written: its snapshot is kept — the one case the next
    try may fare better with;
  - **not found: two presses.** The **first** undo keeps the snapshot, notes
    on its row that the file was missing (`missing` in the row's JSON,
    `_tag_changes_note_missing`), restores what it can and ends `failed` with
    a line that says the file was not found and what pressing Undo again will
    do. An undo run **again** with the file still not found gives up on it:
    the snapshot is dropped without another failure, and the row can go. **Why
    not at once: the library is a union mount.** With one array disk away, an
    album's folder is still there (from the other disks) and the files that
    lived on the missing disk answer "no such file" — one press during such an
    outage would have thrown away the only record of what they were. A file
    seen in between — by an undo, or by an apply that writes it again — is no
    longer one that was reported missing, so the next time counts as a first;
  - could not be looked at, for any other reason: always kept;
  - **a folder that is not there keeps everything.** The route answers 409
    `files_unreachable` (`reachable` of `total` files on record) and starts
    nothing (`_album_override_reach`: the folder of every file on record, and
    the album's own when there is a cover to undo); the task, for a folder
    that goes missing in between, fails each file with its snapshot kept;
  - a snapshot that is **not usable JSON** can be restored by nothing: dropped,
    and said. One whose `format` this build does not know is **kept**, and
    said — it is another build's (a newer one's, after a rollback), which can
    still restore it.
- **The cover.** `cover_url` must be https on `coverartarchive.org`,
  `archive.org` or `*.archive.org` — it is a URL a client sends and lb-bot then
  requests. `_cover_url_checked` reads the host **with the parser that sends
  the request** (urllib3's) and requires Python's to agree; refuses any URL
  holding a backslash, whitespace or a control character; lets a host hold
  only `[a-z0-9.-]`; and **returns the URL rebuilt from the checked parts,
  which is what is asked for**. (`https://evil.example\.archive.org/x` is a
  host ending `.archive.org` to `urlsplit` and a request to `evil.example` to
  the library.) `_fetch_cover_image` follows redirects **by hand**, each hop
  through the same check, caps the size and takes only a JPEG or a PNG.
  **An original over the cap (`ALBUM_COVER_MAX_BYTES`, 20 MB) is taken in the
  Archive's 1200 px version of the same image** — its 500 px one when the
  Archive has no 1200 px — and refused only when neither can be had (B-083,
  `_fetch_cover`, which both cover routes call; Wet Leg – moisturizer's
  front is a 27.6 MB PNG). The smaller version's address is **derived by
  lb-bot from the checked address of the original** (`_cover_renditions`:
  `…/<image id>.<ext>` → `…/<image id>-1200.jpg` on `coverartarchive.org`,
  nothing else) and fetched like any cover address; an answer that states a
  `Content-Length` over the cap is not read at all. The wire is unchanged:
  the row keeps the `cover_url` that was asked for, and what is written is a
  `cover.jpg` — one `_web_log` line says the smaller version was used.
  **A hop the Archive fails is asked again** (G2, `_cover_hop`): a 500, 502,
  503 or 504 up to three tries in all, a request that failed without an
  answer (a refused or reset connection) once more — **never one that timed
  out** (G2a: it has cost its 20 s already, and a repeat outlasts the hub's
  45) — `ALBUM_COVER_RETRY_PAUSE` (1 s) apart — its storage host
  answered 500 for about one request in three when measured, and an
  original and its smaller version both end there. Only that hop, never the
  chain; a 404, any other status and a redirect off the allowlist are
  answers, and fail at once as they did.
  `GET /api/album/covers` reads the Archive strictly (`_caa_fronts`): `[]` only
  for a real none, 502 `retryable` otherwise, and every URL it answers is https
  and one `cover_url` accepts.
- **A cover belongs to one album: it goes into the one folder that holds the
  album's files and no other album's, or nowhere** (`_album_cover_place`, F3).
  The common folder of an album kept in disc folders may be the artist's, or
  the library's: its `folder.jpg` is not this album's to move. And a folder
  that holds **audio files that are not this album's** — singles kept loose in
  an artist's folder are an album each to Navidrome — is every one of those
  albums' folder: `cover.jpg` there showed on all of them, and the folder's
  own picture was what got moved out. The cover route answers a plain 409 in
  both cases, before the Archive is asked; an apply retags and leaves a
  `Cover image` failure row. A name that begins with a dot is not counted
  (AppleDouble's `._01.flac`: Navidrome does not list it). The cost: an album
  that shares its folder with one stray track of another release — or with
  the same song kept beside itself as a `.wav` — takes no cover through
  lb-bot.
- **What was there is never lost** (`_album_cover_write`,
  `_album_cover_restore`). Every image Navidrome would prefer (`cover.*`,
  `folder.*`, `front.*`, any case) is **moved** to `<dir of
  library_index.db>/cover-backups/<override id>/` — never left in the album's
  folder under a name that could be the cover again — except the cover an
  earlier change of the same override wrote, which is replaced. The change is
  all or none: the new image is written beside its place first, then the
  earlier ones are moved, then it takes its place; a failure anywhere moves
  them back and removes the partial file, and a row made for the change is
  taken back. **Unless the moving back failed too** (`CoverNotPutBack`): the
  earlier cover is then in lb-bot's keeping and nowhere else, so the row
  stays — made for this change or not — with a `Cover image` failure that
  says so, because its undo is the only thing that puts that image back and
  the data panel the only place anybody would find it.
  On the undo a kept image is the only copy of a cover that was
  the user's: one that cannot be put back (the folder has an image by its
  name now, or the move failed) **stays kept** and is a failure row, the
  backup folder is removed only when empty (by the undo and by
  `_album_override_delete` alike), and with the album's folder gone nothing is
  touched. **`cover_file` is forgotten exactly when lb-bot's own cover is out
  of the folder** — not before (the undo could then never remove it) and not
  later (a `cover.jpg` the user puts there afterwards would be taken for
  lb-bot's and deleted).
- **Read strictly.** Navidrome through `_nd_album` / `_nd_search_albums`
  (`NavidromeUnavailable`, never `[]`) and MusicBrainz with `strict=True`: a
  dependency that did not answer is a 502 `retryable` with nothing written,
  never "no such album" or "nothing matches". A release is **one** MusicBrainz
  request (`_album_release`: its tracklist and its release group are one
  document) — two strict ones took past the hub's timeout against an unhappy
  MusicBrainz, and an apply that passed late started a job nobody followed.
  Release ids are compared in one case (`_nd_find_album_in_folder`, and
  `_album_override_of` when it finds a row by folder and release): a row keeps
  them lower-cased, Navidrome hands on what the files say. The polled route is the
  exception by design: asked by `id` it answers from the row when Navidrome is
  away.
- **Paths are stored, never sent**: `folder`, `tag_changes.path` and the backup
  folder do not leave the process — a refusal about where the files are says
  how many, not where. `GET /api/overrides` gains `albums` (every row, newest
  first). **It is read on its own and left out when it cannot be**, like
  `toCheck` (and `merges` and `siblings`, below): read with the artist map it
  took the map down with it, and both clients resolve every artist through
  that map. `_album_override_wire` reads
  a `failures` value that is not a list as none.
- The two tables are outside the change feed, like `artist_overrides`: no seq
  trigger, no `INDEX_SCAN_VERSION` / `WIRE_VERSION` bump. `tag_changes` is shared
  with the artist merge (a batch id is an override's or a merge's).

- **lb-bot's own web UI is a third client of these routes** (I-029, U1 —
  2026-10-11; `web/src/panels/AlbumOverride.jsx` on `AlbumDetail` in
  `Artist.jsx`). Same origin, no hub, and bound by the round's contract as
  Feishin and Navic are (§ "Client behaviour both clients mirror", rulings
  X1–X10). **What may be said is one file**, `web/src/lib/overrides.js` (pure;
  `node --test web/src/lib/overrides.test.mjs` is its state matrix): a job's
  sentence from its row, the preview's counts and rows, what the commit button
  does, a refusal's words. No code reaches the page — a refusal is lb-bot's
  own `error` text, `poor_match` alone is a question that names both numbers
  and is sent again with `force` for that release only — and nothing is said
  about favourites, ratings or play counts (item 1 below).
  - **Which Navidrome album.** The override routes take `album_id`;
    `/api/album/releases` marks the *release* the library holds, not the
    album. The page takes the ids from what it already reads: the index row's
    `navidrome_album_ids` and, for an album with gaps, the gap's `albumId`. No
    field was added. Each is read once (`GET /api/album/override?album_id=`).
    Several ids in one release group — two pressings of one album — and the
    block asks which before anything else is asked. **An id can be dead on
    arrival**: the page keeps an artist's discography for the session
    (`discoCache`), and a retag from another client moves the id under it. A
    404 `unknown_album` takes that album off the page and the index is read
    again without blanking the page (`useDiscography().reread`). **An id is
    asked for when the index's list of ids changes and names one that no
    album on the page has** — never twice for the same list — so an album an
    undo gives its old id back is found again, and so is what an apply left
    under the old id (files with no place on the release stay an album of
    their own there).
  - **The page's parts.** Under the title one quiet line (`ChangedLine`):
    nothing for an album lb-bot has not changed; "Tags changed by lb-bot · when
    · Undo" ("Cover changed…" when the row's release is the one it was, X7);
    the job's own line while one runs — the state word, the whole job's meter,
    "3 of 6". Between the edition chips and the tracklist a block
    (`EditionBlock`, a `Roll` that is there from the start) that opens when the
    chip selected is not the edition the files carry (`carriesSelection` — a
    pressing folded into an edition of the same label is that edition's chip):
    the edition's line, "My files are this edition", and a field for a release
    MusicBrainz lists that the chips do not (`parseReleaseRef`; also behind
    "Edition not listed?" in the Tracks heading). The action asks for the
    preview — **one request at a time: picking another chip aborts the one
    that is out** — and "What changes" unfolds: the release, the three counts,
    the from → to rows (files that only change the release they name are one
    sentence with the rows behind it; a "01 X → 01 X" row says nothing), the
    files with no place under their own sentence, the cover grid ("Current
    cover" by default; asked by `coverQuery`, X8), the one fixed
    sentence, "Retag N files". Under the cover, "Change cover": the same grid,
    and `POST /api/album/cover`. **On the page in this order** (fix round 1,
    2026-10-11): the edition's line · "My files are this edition" with "Not
    listed? Paste…" at the end of that same row · the paste field · "What
    changes" — so what a press opened follows the thing pressed. The first
    cover tile is "Current cover" (its ring is what keeping means); a count of
    nothing is not said ("8 files change · 2 tracks still missing
    afterwards"); "Try again" is offered on a refused commit or cover save
    when lb-bot's answer is one to retry; while a job runs the block says
    nothing but the job.
  - **An album lb-bot filled is `present` in the index, and that is in the
    library** (B-091). The index has five words for a release, not four, and
    the pages knew four: a `present` album had no chip, was in no group of its
    artist's page, and — `inLibrary` being false — had none of this surface,
    on exactly the albums a wrong edition shows up on. One file reads the
    word, `web/src/lib/indexStatus.js` (`albumState`, `inLibrary`,
    `countsMeasured`; matrix in `indexStatus.test.mjs`), for every reader of
    an index row's `status`: `Artist.jsx` (the artist page's rows, groups and
    "N of M releases", the album page) and `ui.jsx` (`AlbumStateChip`,
    `ReleaseTile`). A `present` row's own `present`/`total` are from before
    the fill, so they are not shown as its counts; the album page counts off
    the tracklist. (`Library.jsx` and `lib/needs.js` read the *review's*
    statuses, which have no such word.)
  - **On arrival the chips select the release the album itself names**
    (`selectionOfRelease`): with exactly one album read, a release it carries
    that is among the chips, and no chip picked on this visit. The release
    list's `owned` mark is lb-bot's index, which is behind the files for a
    while after a retag; a pick of the user's is never overridden.
  - **What a press opens comes into view, and a height that changes is a
    transition** — `lib/reveal.js` and `Fluid` in `components/Page.jsx`, both
    of the frame and described in `README.md` § The React web frontend. The
    preview's skeleton is the size of the common answer. The secondary lines
    (the counts, "N files keep their number and title", tile labels, the
    title line's "when") are in the frame's secondary style — `muted`,
    `text-caption`; `.s` is styled only inside a `.row` and is used nowhere
    else. The title line's decision is `lineAtRest` in the lib, with its
    matrix case for a row state the page does not know.
  - **One status speaks, on a change of state word** (`spoken`, an `sr-only`
    `role="status"` on the page): the line under the title and the job's card
    are not live regions, so a poll's count is not announced. The keyboard
    goes back to the control that opened what has just shut. **A refusal is
    announced when it appears** (F7, final fix wave): the notice a refused
    Retag, cover save, Undo, Merge or "Use this artist" puts on the page is a
    `role="alert"` (`Notice`'s `role`, `components/ui.jsx`) — the button came
    back to life and nothing else said why — and a preview that could not be
    had is a `role="status"`. Not every notice: the "why" notes and a read
    that failed on arrival are part of the page, not interruptions. **Each
    Undo says what it undoes** (`undoSays`): the line's and the card's carry
    the album's name as their label — a page that holds the album more than
    once has several buttons that all say "Undo" (the tab's rows have had
    theirs since R4).
  - **A cover-only change on an album with no release tag is a cover row**
    (F2). X7's "the release is the one it was" asked for two equal, named
    ids; lb-bot's cover route writes `release_mbid = prev_release_mbid = ""`
    for an untagged album, so that row read "Tags and cover changed by lb-bot"
    and was listed under "Album editions" with no tag touched. A row lb-bot
    gives with **both ids present and both blank** is the cover route's — an
    apply always names its release — and is "Cover changed by lb-bot", group
    `covers`. Blank before and named now is an untagged album that was given a
    release (tags changed); a row that does not carry the two fields is not
    known, never "unchanged" (`releaseUnchanged`; `_album_override_wire`
    always sends both keys, as strings — the columns are `NOT NULL DEFAULT
    ''`).
  - **A write's answer that names no job is not a job** (F5; `startedId`,
    `notSaid` in the lib). A 200 with no `id` used to be followed as
    `…?id=undefined`: lb-bot's 404 read as the job's end, and an undo toasted
    "restored." Now nothing is followed, nothing is toasted, and the surface
    says "lb-bot did not say what it started. See System › Overrides." where
    its refusals go — in the open preview for a Retag, in the fold for a
    Merge, in the row on the tab (there without the pointer: it is that tab).
    The album page's own Undo keeps its fallback to the row's id, which is
    the job's id by contract. lb-bot sends the id today; this is a guard.
  - **While an album's job runs or has gone silent, nothing that starts
    another job for it is offered** (F6; `albumQuiet` — Z3 on the album
    page). The three-minute give-up is no end: `canAct` asked for a job that
    was `polling`, so beside "Still working — see System › Overrides" the
    block offered "My files are this edition" and the hero "Change cover",
    and lb-bot answered either `in_progress`. A silent job's card holds the
    block like a running one, has no Done (it is not an ended one — a
    merge's has none either), and "Change cover" cannot be pressed while
    lb-bot is at the album (a running job included: it could be, there too).
    **The link keeps its place** (G1; `changeCoverLink` in the lib). It is
    the last line of the cover's column, and that column sets the hero's
    height: taken out of the page, as F6 first had it, everything under the
    hero moved a line in one frame when a job started and again when it
    ended. So it stays, `aria-disabled` — not `disabled`, which would take
    the keyboard's place from someone who is on it — with the reason as its
    description and its `title` ("Not while lb-bot is changing this album's
    files."), and a press opens nothing, then or when the job is over
    (`openCover` asks). It can be pressed again when the job is taken up — a
    read that finds the row still running adopts it — and ends, or when the
    album's own state, read again, says nothing is running: the silent job
    is then let go of (`take`), and the album says how it ended. Nothing
    reads on a timer for this: within a visit that is the page's own
    five-second look for an album Navidrome had not listed yet, and otherwise
    the next arrival.
  - **A job is followed in one place, outside the components**
    (`web/src/lib/jobWatch.js`, `createJobWatch`; the app's instance and its
    hook are `lib/jobs.js`; `jobWatch.test.mjs` drives it with a fake clock and
    a scripted lb-bot). By the row's id, every 2 s, from the POST's own `state`
    (X1); it gives up after three minutes with no change of `state` or `done`
    (X2) and is then not settled later (X3); "the writes are over" (X4) is a
    sentence with no bar. Every surface reads the watch, so two of them never
    poll one job twice; **leaving the page stops nothing**, and a job that ends
    with nobody showing it is one toast that names the album. A running row
    found by any read is adopted (`jobs.adopt`) — which is how a reload, or a
    job another client started, is picked up. A job's end is remembered for
    30 s against a read that is older than it — by the row the job began from
    when no poll ever read one (an undo whose row was gone at its first
    poll). A poll gives up on its read after 15 s (`POLL_TIMEOUT_MS` in
    `lib/jobs.js`): a read that timed out is no answer, and the silence rule
    goes on counting. An artist name merge and its undo are jobs of the same
    watch (kind `merge`, by the merge row's id; a reader and a machine per
    kind — § Artist name merge).
  - **After a job the page stays, and reads the album under the id the row now
    carries** (`afterJobSteps`). `applied` is written *before*
    `_album_reindexed` has refreshed the release in the index, so the page
    cannot ask the index what changed: the held mark on the chips and the ids
    the tracklist reads presence by come from the album's own state
    (`heldVariants`, `liveAlbumIds`), and the index and the release list are
    read again at once, 6 s and 20 s later, each time leaving what is shown in
    place. A row's own `albumId` is blank from the moment an apply starts, so
    it is believed only once the apply has settled with one; until Navidrome
    lists the album the page says so and looks again every 5 s for two minutes
    (lb-bot looks for ten). After an undo the row is gone and the album's id
    with it: the page finds the album again by the id the index then names.
    A pasted release can belong to another release group; the album is then
    no longer this page's, the page still stays, and the job's card offers
    "Open the album it is now" (the row's `rgid`). "Navidrome has not listed
    the album again yet" is said, and the row looked for again, for an
    `applied` row only; a row in a state the page does not know says nothing.
    While the page holds no album of its own (after an undo, until the index
    names the id) the tracklist reads presence by the index's ids. Nothing is
    read, and no timer set, for a page that has been left.
  - **The hero shows the cover lb-bot wrote** (`coverThumbUrl`: the Archive's
    own 500 px version of the row's `coverUrl`, the rendition `_cover_renditions`
    falls back to), not `/api/cover/<album id>`, which browsers keep for a day.
  - **From a gap**: "Wrong edition?" beside "N of M tracks in the library"
    asks `GET /api/album/override?album_id=` for the release group and opens
    `#/album/<artist>/<rgid>/edition`, the album page with the chips in view.
    The flow is not built twice.
  - **"Still working — see System › Overrides" is a way there** (U3): the
    words stay, and the tab's name is a button to `#/system/overrides`
    (`JobLine`; `splitStillWorking` in `lib/merges.js`) — in the job's card,
    in the line under the title, and in a merge's card. The toast's text stays
    text. On that tab itself a silent job says "Still working" and points
    nowhere.
  - Known and left: a row at rest that is `failed`, or `applied` with failures, shows
    its problems on every visit until it is undone or applied again; "Change
    cover" and the block need lb-bot to have a Navidrome id for the album, so
    an owned album whose index row carries none (item "`releaseAlbumId` may be
    empty on an owned row" above) has neither until the backfill has run.

Known and left:

1. **Whether a favourite, a rating or a play count survives the id change is not
   measured.** The clients say nothing about it for that reason.
2. **A folder renamed after an apply cannot be undone until it is named back**:
   the undo answers 409 `files_unreachable` for as long as the folder it kept is
   not there. Deliberately — the same answer protects a mount that is down — but
   it is a row that stays.
3. **Snapshots are by path.** A file renamed between two applies of one override
   is, to the second, a file it has never seen: its "first" snapshot is the first
   apply's tags, and the undo puts that back and — at the second press — gives up
   on the old path's (the file is not found). Renaming within an overridden album
   costs the undo for that file.
4. **Placements a user starts by hand are not refused while a job runs** — the
   Place task, "Place anyway", the `/api/imports/<id>/*retry` routes. They wait
   for the album's claim like any placement and then place under the group's
   release as it stood. The four Fill-gaps routes are the ones that are held.
5. **An undo does not reach a file placed into the album after the apply** (or,
   for an artist merge, after the merge). It has no snapshot — it was never
   rewritten — so it keeps the release the fill gave it (the override's), or the
   kept name, while the files around it go back: Navidrome then lists it apart
   from the album it was placed into, until it is retagged by hand or by
   another apply.
6. `/api/album/releases` still counts a box's tracks from `track-count` (item 9
   of "Acquisition reliability"); the preview's `trackCount` is the tracklist's
   own, so the two can differ for a release that repeats a medium.
7. **An ID3v2.3 file does not come back byte for byte** (registered, not fixed).
   mutagen translates a v2.3 tag to v2.4 frames when it loads one and does not
   translate back when it saves, so a v2.3 MP3 that goes through an apply — and
   through its undo — has `TDRC` for `TYER`/`TDAT`/`TIME`, `TDOR` for `TORY`,
   `TIPL` for `IPLS`, and has lost `RVAD` and `TSIZ`. The values are the same;
   the frames are v2.4's inside a v2.3 tag. Placement has always done the same to
   what it tags. (v2.4 files, and v2.3 files without those frames, are exact —
   multi-valued frames included.) An MP3 that had no tag at all ends, after the
   undo, with an empty one.

### Artist name merge — `GET /api/artist/merge/preview`, `POST /api/artist/merge`, `GET /api/artist/merge`, `POST /api/artist/merge/undo`

Since I-023 (2026-10-10, branch `feat/album-override`, not deployed when this was
written). Navidrome keys an artist by **name**, so "Belle & Sebastian" and "Belle
and Sebastian" are two artists there whatever MusicBrainz id both carry. For two
library artists whose effective MusicBrainz id is the same, lb-bot rewrites the
files of one spelling (`from_nd_id`) to the other's name (`nd_id`, the name that
is **kept**): the album-artist tag where it is that spelling, the track-artist
tag where it is. Tags only — no folder is renamed. It can be undone. The wire is
the round's `contract.md`; the section comment above `ARTIST_MERGE_TAGS` is the
long form.

**It is the album override's machinery pointed at an artist**, and the rules in
the section above are its rules — read them there, they are not repeated here:
the snapshots (`tag_changes` under the merge's id, all on disk before the first
write, first write wins, an MP3 saved in the ID3 version it has), the undo's
per-file rules (one look, a file not found kept by the first undo and given up
on by the second, an unknown snapshot format kept), the placement claim on every
review group that holds one of the artist's albums, the hold on Fill gaps for
the length of the job, `in_progress` and `busy` asked in one step and again by
the task, lb-bot having to reach **every** file or nothing is written (409
`files_unreachable`), `merged` waiting for a scan that began after the last
write, a dead or interrupted job leaving its row `failed`. The two features run
the same functions: `_tag_changes_apply`, `_tag_changes_put_back`,
`_tag_changes_reach`, `_album_claim_groups`, `_album_override_jobs`,
`_ScanAfterWrites`, `_album_task`. **A change to one of those is a change to
both.** What is the merge's own:

- **Who may be merged** (`_artist_merge_pair`). Both entries are read from
  `_nd_artist_index()`, so with the user's overrides applied: their MusicBrainz
  id must be the same and not empty, else 409 `different_artists`. (An artist
  override is therefore how two differently-tagged entries become mergeable.)
  404 `unknown_artist` when Navidrome lists either of them no more; 502
  `retryable` when its artist list cannot be read.
- **A collaboration credit is not a spelling** (`_artist_is_collab`, 409
  `collaboration`). A credit to several acts is filed under its first artist's
  id, so it carries that artist's id: on the live library 61 of the 114 same-id
  groups are an artist plus such credits. An entry is a credit **of another
  entry** when its name names several acts (`_artist_credit_acts`), **that
  entry's whole name is a run of consecutive parts of it** that is not the whole
  of it (`_artist_credit_runs`; by `_artist_fold`, a leading "The" on either
  side set aside), and the two whole names do not fold alike. The second
  condition is the point: "King Gizzard & the Lizard Wizard", "Belle &
  Sebastian" and "Joey Valence & Brae" split as well — they are the very entries
  a merge exists for — and no run of theirs is anybody. **A run, not only one
  part** (ruling X15): the lead act's own name may hold a separator, and then
  no single part of the credit is the other entry — "Earth, Wind & Fire with
  The Emotions" beside "Earth, Wind & Fire", "Belle & Sebastian & Monica Queen"
  beside "Belle and Sebastian". A run is read **as it stands in the name**,
  separators and all, and folded: "Crosby, Stills, Nash" out of "Crosby,
  Stills, Nash & Young" does not fold to "Crosby, Stills & Nash", so that pair
  is no credit. The third condition: two names that fold alike are never each
  other's credit.
- **Who may be merged with whom is one definition — the merge sets**
  (`_artist_merge_classes` → `_artist_merge_sets`, rulings X11, X16 and X20),
  and `siblings` lists by it what `_artist_merge_pair` refuses by. Of every
  named entry that carries one MusicBrainz id, album or not:
  1. entries whose names **fold alike are one class**, never apart ("Tyler, The
     Creator" and "Tyler the Creator" are one name, whichever of them splits);
  2. a class any member of which is a **credit** of an entry outside it is a
     set of its own — "Prince and The Revolution" with "Prince & The
     Revolution", and neither with "Prince";
  3. of the rest, the classes that name **one act** are together one set. **A
     name of several acts (`_artist_names_several`) and a name of one are
     never in one set unless they fold alike**;
  4. the classes that name **several** are sets among themselves: **two that
     are different credits are never in one** (`_artist_credits_differ`, X20
     — next point), and the classes hang together through the pairs that are
     not.

  The third rule is for the credit no string rule can see — its lead spelled
  otherwise than the lead's own entry (another script, an alias, a typo). A dry
  run over a copy of the live artist list (8,601 artists, 238 ids with two or
  more named entries) found three wrong offers among the 55 groups the first
  version listed: "Akira Inoue, Masataka Matsutoya & Hiroshi Sato" with
  "井上鑑"; "細野晴臣、鈴木茂、山下達郎" with "細野晴臣"; "流線形と比屋定篤子" with
  "流線形". It lists 53 now. **Costs, accepted:** a band whose name holds a
  separator is not offered a merge with an alias that holds none ("Earth, Wind
  & Fire" with "EWF", "Belle and Sebastian" with "ベル・アンド・セバスチャン" —
  none in the live library).

- **Two different collaborations of one artist are not two spellings**
  (`_artist_credits_differ`, ruling X20, B-073). The dry run above said the
  open case — two *different* credits of an artist who has no entry of their
  own, both plain several-act names, offered to each other — was "none in the
  live library". **It was wrong, and the dry run was why.** Deployed
  (2026-10-10), `GET /api/overrides` listed **58** groups, not 53, five of
  them wrong: "Roc Marciano & DJ Muggs" with "Roc Marciano & The Alchemist";
  "Sam Gendel & Sam Wilkes" with "Sam Gendel & Ruth Garbus"; "Larry June &
  The Alchemist" with "Larry June, 2 Chainz & The Alchemist"; "Brian Bennett
  & Alan Hawkshaw" with the seven-name Brian Bennett credit; and "Hailu
  Mergia & Dahlak Band" with the two Walias spellings. A name is a credit
  only *of another entry*, and **lb-bot's artist list is Subsonic
  `getArtists`: album artists only.** Each of those leads has no album of
  their own, so there is no entry to be a credit of. The dry run had fed the
  rule Navidrome's whole `artist` table (8,601 rows, the album-less leads
  among them); production holds 2,426. **A dry run of these rules is made
  over the artists that have an album** (the round's
  `dry_g_sets_production_view.py`, which reproduces the live 58 exactly) —
  and so is every fixture that says "production shape"
  (`ArtistMergeDifferentCreditsTests`).

  The rule, for two names of several acts that do not fold alike. Both are
  split into acts by the merge's own rules (`_artist_credit_acts`) and the
  acts that fold alike (`_act_fold`, a leading "The" set aside) are paired
  off **wherever they stand** — "A & B" and "B & A" are one credit:
  1. no act shared → **no evidence, not different**. This keeps "Haruomi
     Hosono & The Yellow Magic Band" with "細野晴臣 & イエロー・マジック・バンド",
     and "稲垣次郎とソウル・メディア" with its romanised spellings;
  2. something shared, nothing left over → the same credit;
  3. left-over acts on one side only → different ("A & B" beside "A, B & C");
  4. left-over acts on both sides → different unless every one of them is
     **related** (`_acts_related`) to a left-over act of the other name: the
     words of one are all among the words of the other, case-folded,
     combining marks stripped, "the", "his", "her", "their", "and" not
     counted ("His Soul Media" / "Soul Media"; "The Walias Band" / "the
     Walias"). An act with no words left is related to nothing. **Two acts
     in different scripts are related** — no evidence either way; "different
     scripts" is: the sets of scripts their letters are written in differ
     (`_act_scripts`, by Unicode name: kanji and kana are two).

  A class is judged by every spelling it has (two classes differ when any
  name of one differs from any name of the other). `_artist_merge_pair` asks
  the rule of the two names directly as well, and its third text says so:
  "these two names are different collaborations". Over the production view
  it lists **54**: the 58 less the four pairs, the Hailu Mergia group down to
  its two Walias spellings, no other group changed; over the whole artist
  table still the same 53. Nothing outside the merge reads the rule (a test
  reads the module's syntax tree for its callers).
  **Costs, accepted:** of three names where A–B and B–C are not different and
  A–C is, all three are one group, and A with C is refused when asked. Two
  credits whose left-over acts are in different scripts stay offered. A typo
  in a left-over act makes two spellings of one credit "different", and they
  are no longer offered.

  A merge is refused 409 `collaboration` unless both entries are in one set —
  in either direction; merging an artist's own albums *into* a credit is the
  same mistake — **and the two names are asked directly as well**
  (`_artist_is_collab`, each of the other), so the refusal never depends on what
  the sets were built from. The text says which it is: "one of these is a
  collaboration credit", "these two names are different collaborations", or
  "one of these names several artists and the other does not".

  It reads the artist list and nothing else: an entry no album is counted for
  is in no `siblings` group, and a credit of it is refused all the same. Both
  entries are judged in **one** read of that list — when the cached one does
  not have the `from` entry yet, the fresh one that does is the list, and a
  kept artist it no longer has is 404 `unknown_artist`.
  - **Judged against each other entry on its own, never against the group.**
    The L4 rule asked "is its whole name no other entry's" of the group, so
    the credit's second spelling excused the first and the credit was offered
    for a merge into the artist (the review's probe: "Prince and The
    Revolution" → "Prince", 200). A flag per entry cannot say "these two are
    each other's spellings and nobody else's"; a set can.

  What to know about how a name is split — **for the merge's rules only;
  `_artist_variants` and its `_ARTIST_SPLIT_RE` read none of the merge's own
  joiners** (`_credit_split_re`, `_CREDIT_TO_RE`):
  - **One act named twice is one act** (ruling X18), whatever joins it: "Liquid
    & Liquid", "A and A", "HUNTER×HUNTER" name one act and are no credit of it.
  - **"、", "，" and "＆" split a name, and "と" joins two acts as "and" does**
    (ruling X17) — "と" only with two or more characters on each side, since it
    is also a letter of many words. The katakana middle dot "・" joins nothing:
    "セキトオ・シゲオ" is one name. Costs: "稲垣次郎とソウル・メディア" is a credit
    of the album-less entry "ソウル・メディア" and is not listed with "Jiro
    Inagaki & Soul Media"; a name with "と" inside a word and two characters on
    each side ("やまとなでしこ") reads as two acts and is not offered with a
    one-act alias.
  - **"×" is a separator for the merge's rules, and for no other**: "JPEGMAFIA
    × Danny Brown" is how MusicBrainz joins that credit. It was in the shared
    `_ARTIST_SPLIT_RE` for a day (L4) and is out again (ruling X13): there it
    made "SPY×FAMILY" two acts, so the Soulseek search's artist-evidence gate
    took a folder naming only "Family" — or "I Spy" — for the artist, and
    placement's library-spelling rule no longer counted such an entry as one
    act's name. The letter "x" is a separator nowhere.
  - **A standalone "and" is one too, on the same terms** (the word, any case):
    "Prince and The Revolution" beside "Prince" and "Frank Zappa and The Mothers
    of Invention" beside "Frank Zappa" are credits, and no merge of them into the
    artist is offered. `_artist_variants` itself still never splits on "and" —
    "Florence and the Machine" is one artist — so the Soulseek search's artist
    evidence and placement's library spelling read such a name as one act, as
    before. "Belle and Sebastian & Monica Queen" names "Belle and Sebastian" as
    a run of two parts. What a split on "and" does cost, since X16: "Belle and
    Sebastian" *names several acts*, and so is never in one set with a one-act
    alias.
  - **A leading "The" does not hide a credit** (`_LEADING_THE_RE`): an act is
    compared with another entry's whole name without either's leading "The", so
    "Pastels/Tenniscoats" beside "The Pastels" is a credit. **Only there.**
    `_artist_fold` is unchanged, and "is this whole name another entry's" stays
    exact: "The Emotions" beside "Emotions" is two single acts — neither is a
    credit, and they are not one name spelled two ways either.
  - Two spellings of one credit ("Simon & Garfunkel" / "Simon and Garfunkel")
    are each other's spellings — and beside a "Simon" each is a credit of that
    entry, so the two are a set of their own.
- **A merge is a substitution of values, nothing else** (ruling X12;
  `_artist_merge_substitute`, and the comment above it). Every song of every
  album Navidrome lists for the `from` artist (`getArtist`, read strictly). In
  each file, in **every key that names its album artist or its track artist —
  the key the writers use and each of its aliases alike** (`_tag_snapshot` of
  `ARTIST_MERGE_TAGS`: `albumartist`, `album artist`, `album_artist`,
  `albumartists`, `artists`, the sort and credit forms…; for an MP3 `TPE2`,
  `TPE1`, `TSO2`, `TSOP` and the `TXXX` frames of those names): each value that
  is the `from` spelling (`_artist_fold`) and not already the kept name becomes
  the kept name. Every other value stays where it is — a co-credited artist in a
  multi-valued tag, a sort name, a featured credit ("… feat. X"), a
  compilation's track artist; a duplicate of the kept name that the
  substitution makes is dropped; **no key is added and none is deleted**.
  - **A file is in the plan when any of those keys holds the spelling**
    (`_artist_merge_plan`, the one function the preview, the merge and its task
    share; `files`, `total`). A file with nothing to rewrite is not written and
    not counted.
  - **It is written by `_tag_restore`** — the machinery the undo restores with
    — given only the keys that change (`_tag_changes_apply` takes, for a merge,
    a function of the snapshot it has just kept; for an album override, a dict
    of tags for `_mutagen_write_tags`). So what is written is derived from
    exactly what was kept, and an MP3 keeps its ID3 version. A frame keeps its
    text encoding unless that cannot hold the kept name (Latin-1 and "’"): then
    UTF-8, which a v2.3 save writes as UTF-16.
  - **Not L2b's rule.** Placement's writer deletes a key's aliases when it
    writes the key, and the merge used to go through it — deciding by "the
    canonical key, else its first alias with a value", reading the first value
    of a tag and writing one. On real files (L4 review) that left the old
    spelling in `album artist` / `albumartists`… whenever `albumartist` already
    said the kept name — **the files of the brief's bug 4, the ones that made
    the second artist** — so the merge reported `merged` and the artist stayed
    listed; and it collapsed `["Belle and Sebastian", "Monica Queen"]` to the
    kept name alone while leaving the same tag with the spelling second
    untouched.
  - **A format lb-bot does not write** (`_TAG_WRITABLE_EXTS` is flac, opus,
    mp3 — so not m4a, not Ogg Vorbis) is counted when what can be read of it
    says the spelling, and fails saying so: "lb-bot cannot write the tags of a
    .m4a file" (`_tag_unkept_reason`; the album override's failures say it the
    same way). The old spelling is then still an artist, with those files.
  - Not rewritten: the MusicBrainz artist ids. A sort name that *is* the
    spelling (`artistsort` = "Belle and Sebastian") is a value like any other
    and becomes the kept name.
- **A pair that already has a row keeps it.** Merging again — after a `failed`
  one, or a part-done one, or because files in the old spelling appeared since —
  writes into the same row, and the snapshots under it stay the files' first: the
  undo goes back past every merge of that pair.
- **Nothing to retag is a plain 409**, not a job: a `from` entry Navidrome lists
  no album for (a track artist only) previews as 0 / 0 and cannot be merged.
- **Fill gaps is told why in the job's own words**
  (`ARTIST_MERGE_FILL_REFUSAL`): `_album_override_job_begin(album_ids,
  refusal)` carries the text, `_album_override_holds_group` returns it.
- **`busy` asks two questions** (`_artist_merge_busy`): the artist's
  (`_artist_fill_in_flight`, for the kept artist and the rewritten one) and the
  albums' (`_album_fill_in_flight`, for every album whose files are rewritten).
  The second is the one that reads the review rows under `_review_lock`, and so
  sees a fetch that is still queueing — which is what makes the Fill-gaps hold
  airtight in both directions.
- **After the scan** (`_artist_merge_scanned`). The `from` artist's Navidrome id
  is dead, and an album whose files carry no MusicBrainz release id has another
  id — Navidrome knows such an album by its album artist. So: review groups keyed
  on an album id Navidrome no longer lists are retired
  (`_retire_review_groups_of_albums`, never one with work in flight); the row
  learns the albums' ids as they are now (`album_ids` — what its undo claims and
  holds; before the scan they are the ids the albums had); both Navidrome caches
  are dropped; the hub gets `albumIndexed` for the kept artist with those album
  ids, 16 to a notice. For an undo, the notice names the spelling again, under
  whatever id Navidrome gives it now — **the row's two artist ids are history
  once Navidrome has scanned; nothing matches on them afterwards.**
- **Then the artist is scanned** (`_artist_merge_rescan`, the last thing
  `_artist_merge_scanned` does, after the notice — for a merge and for an undo).
  The ordinary discography scan, started through the entry point an artist
  override uses (`_artist_scan_task`) and **under an override set's rules**: not
  for an entry that is `covered` (`_artist_scan_covered`), not beside a scan
  already running for it (`_artist_scan_running`), not without a Navidrome login
  and not when Navidrome's artist list — read afresh, the merge has just changed
  it — is empty; what the scan stores is `_index_store_artist`'s to refuse
  (`_artist_override_refusal`) as for any scan. **One scan for the artist**,
  from whichever of the merge's two entries Navidrome lists now, the kept one
  first: after a merge that is the kept entry; after an undo it is the kept
  entry unless the spelling that is back holds the artist's fresh row, in which
  case that one is scanned (found by name). It is a task of its own: the row is
  `merged` (or gone) before it is asked for, no route's answer waits for it, and
  a scan that is not started — or that cannot be — is a log line (`no scan
  started…`, `could not start the artist's scan…`). **No scan when Navidrome
  never finished one** (`ALBUM_OVERRIDE_FOLLOW_UP` ran out): it would read the
  library as it was.
- **`merged` with failures is a partial result**: the old spelling is then still
  an artist of its own, with the files that could not be written. **`merged`
  with nothing written and a failure does not exist** (X14, in the album
  section; X19): a merge that wrote **no file** and has a failure — or that,
  planned again after a real wait, found no file saying the spelling any more —
  is `failed` at once, with no scan asked. "Wrote no file" is what
  `_tag_changes_apply` counts as written, not `done`: `done` also counts a
  planned file that already said the kept name by the time of the write. (No
  file written and no failure is every file as the merge wants it: `merged`.)
- **A partial undo closes like a whole one** (X14): `undoing` through the scan
  wait, then `failed` where a whole undo's row would go, then the same
  `_artist_merge_scanned` — groups retired, both Navidrome caches dropped, the
  notice (naming the spelling again), the artist's scan — and the row's
  `album_ids` brought up to the albums as they are, for the next press to
  claim and hold.
- **An entry with no name is not merged, either way** (plain 409), and is in
  no `siblings` group: Navidrome keeps one for files that name no artist. As
  `from` its spelling is "nothing", which is what every empty tag says; as the
  kept name it would blank what it replaced. (`_artist_fold` of a name that is
  all punctuation — "!!!" — is the name: only an empty or blank one folds to
  "".)
- **At startup each kind of job is checked in its own `try`**
  (`_mark_interrupted_jobs`): they shared one, and the album check raising left
  every merge row `merging` until the next restart.

**What a merge does not do itself — on purpose, and worth knowing.** The first
two are the work of the scan above, so they are the state of things *between
the notice and that scan's end*, and for as long as no scan was started:

1. **The index.** Both spellings already read one `artists` row (the shared
   bucket, L2c). If the dead entry was that row's *holder* (`nd_artist_id`), the
   row goes on naming it — and its name — until the artist is scanned. It is
   read by its MusicBrainz id, so the kept artist's page has it throughout. The
   scan is not `covered` then (the holder is nobody in the library), runs, and
   files the row under the kept entry; `_artist_fill_in_flight` for the kept
   artist finds no row by its Navidrome id meanwhile and falls back to its name.
   Left alone, the auto-indexer would find the artist indexed and fresh and do
   nothing until the row is stale — which is why the merge asks.
2. **The review.** Groups keyed on a dead album id are retired; the groups for
   those albums under their new ids come with the artist's scan. **A group
   whose album id did not change follows its album** (F2,
   `_follow_renamed_albums`): a group's id is built from the artist's *name*,
   so the scan builds that album a group under another id, and the union gives
   the existing group that id instead of appending a second one beside it —
   the skip, "allow MP3", hand picks and landed rows stay on the one group.
   The same after the undo, back under the id it had. Until the scan has run
   those gaps are listed under the old name; a group with work in flight is
   moved by the scan after the work has ended.

   **The one case no scan is started for, with everything in order:** a group
   of three where a *third* entry — in practice a collaboration credit — holds
   the artist's fresh row. The kept entry is `covered`, as it would be for an
   override set, and the row and the review wait for that holder's own next
   scan (or a Rescan from its page).
3. **An artist override stored for the dead entry** stays in `artist_overrides`
   (it is keyed by Navidrome artist id) and in `GET /api/overrides`' `artists`,
   by its stored name, applying to nobody. Whether an undo brings it back into
   force depends on Navidrome giving the re-created artist the id it had, which
   was **not checked on a live Navidrome** (the tests' fake derives the id from
   the name and does); if it gets a new id, the override stays an entry for an
   artist that is gone, to be removed by hand.
4. **A featured credit keeps the old spelling** ("Belle and Sebastian feat. …"
   is another artist to Navidrome, and stays one) — as does a v2.3 MP3's
   `TPE1` that joins two artists in one value ("Belle and Sebastian/Monica
   Queen"): that is one value, and not the spelling.

**The merge in lb-bot's own web UI** (I-029, U3 — 2026-10-12;
`web/src/panels/ArtistMerge.jsx`, one component in two homes, and
`web/src/panels/Overrides.jsx`). Same origin, no hub, and bound by the round's
contract as the two clients are. **What may be said is one file**,
`web/src/lib/merges.js` (pure; `node --test web/src/lib/merges.test.mjs` is its
matrix): the set an artist can be merged within, which name is kept, the
preview's sentence, the one fixed sentence, a job's words, where the page goes
afterwards, and a row of System › Overrides. No code reaches the page — a
refusal (`collaboration`, `different_artists`, `busy`, `in_progress`,
`files_unreachable`, a 502) is lb-bot's own `error` text, with "Try again" only
when lb-bot says it can be retried.

- **A name in a sentence is isolated** (U3 fix round, Z1; `iso` in
  `web/src/lib/text.js`, with its test). The library has Hebrew, Japanese,
  Cyrillic and Latin names side by side, and a right-to-left name reorders
  the neutral characters and digits beside it: "also להקת הדוגמה · 2 albums"
  was drawn "also 2 · להקת הדוגמה albums". Every artist, album or track name
  that is **joined with other words in one string or one line** is wrapped in
  U+2068 FIRST STRONG ISOLATE … U+2069 POP DIRECTIONAL ISOLATE (nothing for an
  empty name) — in the three override surfaces' own sentences: a set's "also
  …" line, "From → Into", "merged into X", "N files in M albums will say X",
  "Which spelling is merged into X?", the scan's and the merge's lines and
  toasts, an album job's toast, "MusicBrainz match: X", "Album · Artist", a
  release's line, a problem's "title — reason", and the aria-labels that carry
  a name. A name that is an element of its own needs nothing. **Never in what
  is sent to lb-bot, in a key, or in anything compared or searched** — it is
  two characters more: `setBody`, `mergeBody`, `rowKey`, the search field and
  every list as lb-bot gave it are untouched, and a test holds the POST bodies
  to that. What an album's files are tagged as ("your files are tagged as
  …", `variantInLine` in `lib/overrides.js`) is the same: a MusicBrainz
  disambiguation is isolated, the page's own word for a release that has none
  ("Original", a year) is a word (M5). Written as escapes in the source, never
  as the characters — **and a matrix case holds the source to it** (T1,
  `web/src/lib/source.test.mjs`): it reads every `.js`, `.jsx`, `.mjs`, `.css`
  and `.html` file under `web/src` and fails on a literal U+00A0, U+200B,
  U+200E/F, U+202A–E, U+2066–2069 or U+FEFF, naming the file, line and column
  (an escape is fine). They get there through a tool that decodes an escape on
  its way into the file, and no diff shows them.
- **Which artists.** `setOf(overrides, artistId)` reads `siblings`: the set
  that holds the artist — found by the artist, never by the `mbid` (two sets
  may carry one) — less any entry lb-bot marks `collab`, and only when two or
  more are left. `siblings` absent is no list: no block, never "no other
  spelling".
- **On the artist page** (`DiscographyView` in `Artist.jsx`; not on an `mb:`
  page): the page reads `GET /api/overrides` once on arrival. In a set, a
  block "Also in your library as" lists the *other* spellings — portrait,
  album count, each a way to its page — and "Merge into one name…" unfolds:
  which name is kept (the one with the most albums, which lb-bot lists first,
  is chosen), in a set of three or more which spelling is merged into it (the
  route takes a pair), and "What changes" — `GET /api/artist/merge/preview`,
  asked when the fold opens and again when the pair changes, **one request at
  a time** (the one that is out is ended): "N files in M albums will say
  <name>", the sample albums, "…and K more albums", the one fixed sentence,
  Merge and Cancel. A preview of no files says what lb-bot counted and offers
  no Merge.
- **Merge is one request**, and then the block is the job: a merge is a job of
  the one watch (`lib/jobWatch.js`, kind `merge`; `mergeMachine`, and
  `readMerge` in `lib/jobs.js` — `GET /api/artist/merge?id=`). The X-rules are
  the album job's, from the same `jobStep`: the POST's own `state` seeds it
  (X1), three minutes with no change of `state` or `done` end the asking (X2,
  and it is not settled later — X3), `done + failures >= total` while the row
  is still active is "Waiting for Navidrome to read the albums again…" with no
  bar (X4), and the coded 404 `no_merge` is how a successful undo ends.
  Leaving the page stops nothing; a merge lb-bot lists as running is
  followed at **every answer** of `merges` the home reads — a reload, another
  client, and one this home had given up on, which a fresh answer that still
  lists it running takes up again (X3 allows it; the give-up itself reads
  nothing, or it would be followed again for ever); one that ends with nobody
  showing it is one toast, "From → Into: …".
- **While lb-bot is still merging these names, nothing is offered for them**
  (Z3, `mergeQuiet`): by the list last read — a row of these names in a
  running state — or by the job followed here, running or gone silent while it
  ran. After the three-minute give-up as on arrival: the card ("Still working
  — see System › Overrides"), and no "Merge into one name…" under it — lb-bot
  would refuse it, `in_progress`. A silent job's card has no Done (it is not
  an ended one); it is let go of when a later answer no longer lists its row
  as running, and the list then says how it ended.
  **A merge this page has itself seen end is over** (M1, `mergeRowsNow`): the
  read its end starts can fail, and the list kept through it still says
  "merging" of that row. A row in a running state, of a job seen to end since
  the list in hand was read, neither holds the names quiet nor draws a running
  card (`ended` in `useArtistMerge`, tied to that very list: the next answer
  is believed as it stands). **A fold that shut because lb-bot began merging
  these names does not open again by itself** (M4): `open` is cleared when
  the names go quiet, so the end of a merge another client started — found by
  a read while "Merge into one name…" was unfolded — asks for no preview
  nobody asked for.
- **When it ends** (`afterMerge`): lb-bot's lists are read again. On the page
  of the spelling that is gone — a merge that settled with every file
  rewritten — the page moves to the kept artist's, **replacing** the history
  entry, with a toast ("<from> is now <into>."); what the session kept of
  both names' discographies is let go of first, so that page does not open on
  what was read before the merge. Any other page stays: the artist list is
  read again, and the discography at once, 6 s and 20 s later (lb-bot scans
  the artist after the row says `merged`), each time behind what is shown. A
  merge with failures leaves the old spelling an artist, and the page where
  it is.
- **At rest** the artist page shows a merge of its names only when it needs a
  look: left running, `failed`, or `merged` with failures — the card, with
  lb-bot's lines and Undo. A merge that simply went through is listed where
  every change is, under System › Overrides.
- **Needs you › To fix: "One artist, two names"** (`FixTab` in
  `NeedsYou.jsx`), under the artists to match: one row per set (`keepSets`,
  `setRowView`; the first six, then "Show all"), listed and **not counted in
  the section's badge** (`lib/needs.js` says why for the artists; the same
  holds). The pane is the same merge component with every name of the set;
  nothing is asked on selection. A set merged from here leaves lb-bot's list
  and stays in the page's, in its place, saying "Merged" — its pane the merge
  that made it so, with its Undo — until the tab is left; an answer without
  `siblings` calls nothing merged. The list and the pane read one
  `GET /api/overrides`. **An address that names an artist or a set
  (`art:<id>`, `two:<id>`) is waited for** until that list is read: it used to
  be dropped for the first item of the tab, because lb-bot's list arrives
  after the others.
- **System › Overrides** (`OverridesTab`, the tab after "Source rules"): one
  read of `GET /api/overrides` (503: said, with Try again), four groups each
  from its own list — Album editions and Covers from `albums` (`rowGroup`: the
  release is the one it was → Covers), Artist matches from `artists`, Name
  merges from `merges`. A list lb-bot left out "could not be read"; an empty
  one is not shown; with nothing at all the tab says where a change is made
  from. **What a row says is `listRowView`**: "Album · Artist" / "Name → the
  MusicBrainz artist" / "From → Into", when, a link to the release on
  MusicBrainz, the state word (`Applied`, `Merged`, `Did not finish` at rest —
  with lb-bot's lines folded behind their count; the job's own word, sentence
  and whole-job meter while one is followed) and Undo (Remove for an artist
  match). A state the tab does not know says nothing and offers nothing.
  Every row's button says "Undo", so it is labelled with its row for a screen
  reader (`undo.says`: "Undo <Album> · <Artist>", R4).
  - **A row opens its page only by an id that is live now** (`rowOpens`): an
    album by its own answer — `GET /api/album/override?album_id=<the id the
    row carries>`, one request at a time in the list's order; 200 gives the
    release group to open, 404 `unknown_album` is a plain row, a row with no
    id is not asked about — an artist match and a merge (its kept artist) by
    the artist list. Never while a job is changing the row's files.
  - **A matched artist's own name** is read from `GET /api/meta/artist?mbid=`,
    once per id (neither list carries it — `name` is the library's); a
    skeleton while it is asked for, "its MusicBrainz artist" when it cannot be
    had, and the link out either way.
  - **Undo runs in its row**, through the watch (an album's by kind `album`,
    a merge's by `merge`; an artist match's Remove is stored at once and the
    scan that follows is the row's job, kind `scan`): one press is one
    request; the row is held while the undo runs (`heldRows` — also one found
    running, whoever started it), so it is still there when lb-bot lists it no
    more; when the undo is through (`undoEnded`) the row folds away with one
    toast and its group with its last row; an undo that failed stays, "Not
    restored", with lb-bot's lines and "Undo again". Whenever a followed job
    ends, the list is read again and the album's id asked about again.
    lb-bot's refusal is said in the row it was asked from.
  - **A held row keeps its place** (Z2, `holdRow` / `rowsShown`). At an
    undo's end the fold starts and the list is read again in the same moment;
    lb-bot no longer lists the row, and re-added after the listed rows it
    jumped to the bottom of its group in the middle of its fold (every undone
    row but a group's last; an artist match at once, since its remove is
    stored at once). A row is held with the index it has among its group's
    rows as they are *shown* and the row it stands before, and is drawn there:
    before that neighbour while it is still listed, else at that index. The
    heading counts the rows shown (a row that is folding away is on its way
    out).
  - **Every answer of the list is followed** (R2): on arrival, after Try
    again, at a job's end, the rows lb-bot lists as running are adopted — one
    this tab gave up on included. The give-up itself reads nothing and its row
    keeps its hold and its place ("Still working"); the next answer says more:
    still running → followed again; listed at rest → the silent job is let go
    of and the row says what the list says; listed no more → the undo went
    through, and the row folds away with its toast. **"Listed no more" is
    said only by an answer that gave that row's own list** (F4,
    `listedNoMore`): `albums` for an album row, `merges` for a merge. An
    answer that left the list out says nothing of the row — it stays held —
    where it used to fold away with "…: restored.": absent read as gone.
  - **An Undo answered without an id is not a job** (F5, above): said in the
    row, nothing followed or held, nothing toasted; the list, which the tab
    reads after every answered press, says what is so.
- **Known and left.** A favourite, a rating or a play count is not spoken of
  (as for an album). The artist page's block is not offered for an entry in no
  set — a collaboration credit, or an artist whose other spelling has no album.
  A row that folds away takes the
  keyboard's place with it (it goes to the page) — and so does the To fix
  note when its Try again is pressed from a page that draws neither group
  (the skeleton stands there while lb-bot is asked; with a group drawn the
  note and its button stay — F3). Under To fix a set of three
  merged into a name that is not its first is listed again under that name,
  beside the row that says "Merged". M1 is about a row that says *running*:
  under To fix, an undo this page saw end whose re-read failed still shows the
  merge as "Merged", with its Undo, until a read succeeds (lb-bot answers
  that press "no such merge", and says so).

### `GET /api/overrides` — `merges` and `siblings`

- `merges`: every row of `artist_merges`, newest first, as `MergeRow` — a row is
  there until its merge is undone, so this is "every merge that can still be
  undone". `from.ndArtistId` is the rewritten spelling's Navidrome id at the
  time of the merge.
- `siblings` (`_artist_sibling_groups`): **the sets of two or more library
  artists that can be merged into one another** — the merge sets of each
  MusicBrainz id (`_artist_merge_sets`, in the section above; ruling X11) —
  each entry `{ndArtistId, name, albums, collab}`, the entry with the most
  albums first. **A group is a set, not an id**: an artist id is in at most one
  group, several groups may carry the same `mbid` (the artist's one-act names,
  each of its several-act credits, a credit's spellings — X16, X20), and a set of one is not
  listed — so a lone collaboration credit appears nowhere. **`collab` is in the shape and false on every entry**: a
  credit is never listed beside what it is a credit of. Both clients find a
  group by the artist id it holds, which is why disjoint groups needed no client
  change; their `collab` filter stays for a build that says true. `albums` is
  counted off the album index by the album's own artist, and **an entry with no
  album is not listed** (a track artist is an artist to Navidrome, and nothing a
  merge could rewrite an album of) — though it still counts when the sets are
  made: a credit of an album-less artist is a credit.
- **Each of `albums`, `merges`, `siblings` and `toCheck` is read on its own and
  left out when it could not be**: `siblings` when Navidrome's artist list or
  album list did not answer, any of them on a failure of its own. `[]` is "there
  is none". `artists` — the map both clients resolve every artist through —
  answers regardless.
- **lb-bot's own web UI reads it in three places** (one hook,
  `useOverridesRead` in `ArtistMatch.jsx`): Needs you › To fix (the artists to
  match and the merge sets), the artist page on arrival (the match picker and
  "Also in your library as"), and System › Overrides (every change, with its
  undo). Each treats an absent list as "no list" — nothing is called matched,
  merged or empty by it.

### The listens mirror — `GET /api/listening/{status,summary,unowned,recent}`, `POST /api/listening/sync`

Since I-006 (2026-10-03, branch `feat/substreamer-parity`, not deployed when this
was written). lb-bot keeps a copy of the user's ListenBrainz listens and serves
the aggregates both clients' "My Listening" screens show; the wire shapes are in
the round's contract and in `navi-connect/PROTOCOL.md` once that is updated. The
section comment above `LISTENS_PAGE_SIZE` is the long form; what binds a change:

- **`listens.db` is its own file with its own lock** (`_listens_lock`), beside
  `library_index.db` (`_listens_path()` derives it from `LIBRARY_INDEX_FILE` on
  every call). Two tables: `listens`, keyed `(listened_at, recording_msid)` as
  ListenBrainz keys a listen, and `meta` (`user`, `backfill_cursor`,
  `backfill_done`, `catchup_at`, `last_sync_at`, `last_error`, `lb_total`, and a
  stalled backfill's `stall_cursor` / `stall_since` / `stall_count`). No
  credentials, no paths. **Deleting
  the file with lb-bot stopped is the supported rebuild.** Text keys are grouped
  with a registered `fold()` (Python `casefold`), because SQLite's `lower()` only
  folds ASCII.
- **One writer.** The `listens-sync` thread (`_listens_sync_worker`, started in
  `start_web_dashboard()` like the others, so not with `LB_BOT_WEB=0`) is the only
  code that writes rows. `_listens_sync_tick(state, now, user, fetch, wake,
  clock)` is the whole decision with the fetch and the clock passed in, and the
  loop around it (`_listens_sync_worker(clock, fetch, waiter)`) takes its clock,
  its fetch and its sleep as arguments too, so the tests drive both with no
  thread and no real sleep. The routes only read; `POST /api/listening/sync`
  sets `_listens_wake` and answers at once.
- **The backfill must survive a restart and must not end early.** It pages back
  from now with `max_ts=<cursor + 1>` (`max_ts` is exclusive and a page can end
  mid-second, so the boundary second is re-read and `INSERT OR IGNORE` absorbs
  it), and the cursor is written **in the same transaction** as the page's rows.
  It ends on a page shorter than `LISTENS_PAGE_SIZE`, checked against
  ListenBrainz's own `oldest_listen_ts`: a short page that still moved the cursor
  with that floor below it is followed by another, and a page that brought
  **nothing** older while the floor is below the cursor is a **stall**, a failure
  to retry — ending there left a partial history that said `ready`. An answer
  with no `payload.listens` list is a failure too; one listen that cannot be
  read is skipped (anything with a timestamp is kept, with what could be read of
  it), never its page.
- **A stall has three ways out, and none waits on ListenBrainz correcting its
  own numbers.** A retry that brings older listens goes on importing. As many
  listens stored as ListenBrainz counts means there is nothing more. And once
  the same answer has come back at the same cursor `LISTENS_STALL_MIN_REPEATS`
  (6) times over `LISTENS_STALL_MIN_SECS` (6 h), the import **ends with what it
  has** (`backfill_done`, `catchup_at`, `ready`) and logs the floor ListenBrainz
  reported and the cursor reached: a floor and a count that lag behind a
  deletion never catch up, one listen skipped or played since the import began
  keeps the count out of reach, and a truncated answer does not repeat for six
  hours. The record (`stall_cursor`, `stall_since`, `stall_count`) is in `meta`,
  so a restart counts on from it; it is cleared whenever a backfill page is not
  a stalled one. Before this the count was the only way out and was unreachable
  in the ordinary case: 300 `error` ticks over 49 simulated hours, the same page
  asked for every time, and deleting `listens.db` replayed into the same stall.
- **While stalled the mirror still takes new listens.** After each stalled
  answer the next request is the ordinary incremental pull
  (`state["alongside"]`), and the retry follows it at the pace: one retry and
  one pull per back-off, so at least every ten minutes. That pull does not
  clear `last_error` (the import is still what is wrong — `state` stays `error`
  rather than flickering) and does not reset the back-off. **The two take
  turns whether or not the other one worked**: the turn is handed over before
  the request, not after it succeeded. Handed over only by a pull that worked,
  a `min_ts` request that kept failing was all the worker asked for — the
  stall's count stayed at 1 and its bounded exit never fired.
- **Then two pulls.** Every `LISTENS_SYNC_INTERVAL_SECS` (10 min), or on a wake:
  `min_ts=<newest stored>`, which ListenBrainz answers with the listens nearest
  *above* it, so a full page means "more" and the next pull follows at once from
  that page's last second. And **once a day** (`LISTENS_CATCHUP_INTERVAL_SECS`,
  `meta.catchup_at`) the pull starts `LISTENS_CATCHUP_DAYS` (14) back instead and
  pages forward to the present the same way: a listen submitted late — one client
  queues scrobbles offline — arrives with its original time, below the newest
  stored, where the ordinary pull never looks. **It starts at the older of that
  and the newest stored listen** (with nothing stored, the end of the last
  catch-up): started fourteen days back whatever was stored, a mirror that had
  not pulled for three weeks never asked for the first of them, and afterwards
  its newest listen was the present, so nothing ever went back for the hole.
- **Pacing counts from the end of a request, and a wake is a flag in `state`.**
  `floor_at` (2 s after the last request ended) holds for everything; `next_at`
  is the pace, the interval or the back-off, also from the request's end — a
  request that timed out twice took two minutes, and counted from its start the
  retry followed at once. A sync request makes the next pull due at the floor
  and stays pending until a pull starts, so several are one. The worker clears
  the event only when it saw it set; checked and cleared unconditionally, a
  request landing in between vanished.
- **A time no listen can have is kept out** (`_listen_row`, `LISTENS_TS_CEILING`):
  at or before the epoch, or from 2100 on — seconds given as milliseconds is
  the year 56,000. Python can make no date of such a time, so one stored row
  made every summary answer 500 until the file was deleted; and it would be
  the "newest" listen, above which nothing is asked for again. A fixed range
  on purpose, not "later than now": on a box that boots with its clock at 1970
  that rule would throw every listen away. The calendar scan also skips a row
  it can make no date of, however it got there.
- **`LB_BOT_LISTENS_SYNC=0` switches the mirror off**: the worker returns at
  thread start with one log line, and `_listens_user()` answers "" so that
  `status.state` reads `off` and the Fresh rider counts nothing. The stored
  listens stay, and `summary` / `unowned` / `recent` still answer from them.
- **`meta.user` names whose listens these are.** Under another
  `LISTENBRAINZ_USER` the mirror is emptied and started afresh, with a log line.
  The same name in another letter case is the same user (ListenBrainz does not
  tell them apart) and changes nothing.
- **`state`** (`_listens_state`): `off` with no `listenbrainz_user`; `error` when
  the last sync failed, in whichever phase (the text is `last_error`; nothing is
  written but that, the cursor stays, stored rows are still served, and the next
  try waits 30 s doubling to 10 min) — a stalled import reads `error` for as
  long as it is stalled, six hours at most; else `importing` until
  `backfill_done`, then `ready`. The clients re-read their summary when it leaves `importing`.
  `total` (ListenBrainz's own count, asked for at most hourly) is best-effort and
  never decides the state.
- **An aggregate never carries a library fact.** The aggregates are memoized
  (`_listens_memoized`) on the listens' own `(file, count, newest)`. `owned`,
  `ndAlbumId`, `ndArtistId` and the genres are joined for **every** answer by
  `_listens_library()`. Putting an ownership field inside a memoized aggregate
  would serve a stale "not in your library" after a fill, and lb-bot is the last
  place that could come from: the hub neither caches nor coalesces the summary
  or the unowned list, so every client request arrives here.
- **`owned` is "the library holds it", not "lb-bot's index holds it"**
  (`_listens_album_held`). The Fresh feed's index-only rule was copied here at
  first and is wrong here: nearly everything in a listening history is owned,
  and an artist lb-bot never scanned, guessed wrongly or could not bucket (B-060, until 2026-10-09)
  had its held albums listed under "most played, not in your library" — where
  one client offers to acquire them. An album is held when **any** of four looks
  says so: the index holds its release-group; Navidrome has an album whose
  `musicBrainzId` is a release it was played as; Navidrome has an album by the
  same artist and title, both **casefolded** (`_listens_fold`, so a non-Latin
  name compares as itself — `_norm_album_text` would make it nothing); or the
  index holds that artist and title under its normalized or edition-stripped
  key (`_listens_title_key_pair`). That key is the index's own for an ASCII
  name; for any other, `_listens_key_text` normalises the same way but keeps
  every letter, mark and digit of every script, because `_norm_album_text`
  keeps `[a-z0-9]` only (B-060, not touched): "Икра 1" and "Морская 1" were
  both "1", and the one the library lacks read as owned because the other is
  held. Equality only, never on an empty half. The index's side is built from
  **held rows only** (`_listens_read_held`, `status != 'missing'`): an album and
  its title-track single share a name, and a name map over both let whichever
  came first decide. Artists come from the override-aware `_nd_artist_index()`,
  by MBID and then by name.
- **A list that cannot be read is a 503, never "nothing".** `_listens_library(need=…)`
  raises `ListeningUnavailable` when the index read fails or, with a Navidrome
  login configured, a list the answer needs is empty: `summary` needs the album
  list (for `owned`), `unowned` needs albums and artists, `recent` needs neither
  and leaves the ids blank. `unowned?kind=artist` needs the artist list and
  reads nothing else (`_listens_library(albums=False)`): not the index, not
  Navidrome's albums. A failed read is not stored, so it is asked again rather
  than remembered as an empty library.
- **One entity, one row, one count** (`_listens_groups`). Listens are grouped by
  MBID when they have one and by folded text when they have none, and a text
  group is then folded into the MBID group of the same folded text — otherwise
  an album whose listens only partly carry a release-group id is two rows and
  two albums in `totals`. Artists and albums are grouped in full (`unowned` and
  the totals need every one); tracks only among the `LISTENS_TRACK_CANDIDATES`
  most played, since every distinct recording is by far the longest list and
  ten are shown.
- **Lock order.** `_listens_compute_lock` → `_listens_lock` (the reader's
  set-up) and → `_listens_memo_lock`; `_listens_library_lock` → `_index_lock`
  (the one read of held rows). `_listens_lock` and `_listens_memo_lock` are
  leaves. **Never a listens lock taken while `_index_lock` or `_review_lock`
  is held**, and no network call under any of them: the worker fetches with
  no lock held, and Navidrome's lists are read before the library lock is
  taken.
- **A request must be cheap: four proxy slots, 20 s.** Aggregations run on a
  connection of their own (`_listens_reader`; holding `_listens_lock` across a
  multi-second GROUP BY made `status` wait for it) and one at a time
  (`_listens_compute_lock`), so requests for the same thing share it. **Storing
  an aggregate drops every entry of another stamp**: a new listen makes them
  all unreachable, and an all-time calendar with its album groups is tens of
  megabytes that otherwise sat in the memo until 24 newer entries pushed it
  out. The
  library's side lives in three one-slot caches rebuilt under
  `_listens_library_lock` — concurrent requests share one rebuild — keyed on the
  index head and on the very list Navidrome's cache handed out; the title keys
  are remembered (`_listens_title_key_pair`), because after every index write
  the same held albums were normalized again. Genres are loaded for `summary`
  only, and one scan of the history (`_listens_calendar_scan`, keyed on zone and
  listens, not on the period) backs every period's calendar. The grouping SQL
  is written for the planner — `+` on the first GROUP BY column, and no time
  range for all of history (`_listens_groups` says why). Measured at 180,000
  listens and 69,000 release-groups on the workstation: a request right after an
  index write went from about 1 s to about 0.1 s, four at once from 4–5 s to
  0.1–0.3 s, a period's first summary from ~0.3 s to under 0.1 s, and a cold
  all-time summary — which every new listen makes cold again — from ~3.5 s to
  1.5–2.5 s.
- **Local days come from the client.** The container is UTC, so `summary` takes
  `tz` (an IANA name; unknown is 400, missing is UTC and needs no zone
  database). `tzdata` is in `requirements.txt` as a safeguard, not because the
  image lacks zones: checked in the running container on 2026-10-04,
  `zoneinfo.ZoneInfo("Europe/Berlin")` resolves there without the package (the
  base image carries a system zone database). The pin keeps a named zone
  working on a base image that does not. Day and hour buckets are one `GROUP BY listened_at / 900`, each
  bucket given the local date and hour of its first second. That is exact
  while the zone's offset is a whole number of quarter-hours and changes on a
  quarter-hour of UTC: checked against the zone database (2026c, 598 zones),
  every offset since 2002 is one and every rule in force since 2023 changes on
  one. The exceptions are changes made at one minute past (Newfoundland and
  Labrador until 2011, Moncton until 2006, Gaza and Hebron in 2010–11, Casey
  station in 2020–22): there the listens of the one bucket straddling the
  change land on the side its first second falls — up to fourteen minutes'
  listening per change an hour (at midnight, a day) off, and never in the
  totals. A period is whole local days ending today. `unowned` takes no `tz`
  and uses UTC days.
- **`unowned` album rows always have an `rgid`** (it is what a client opens), so
  a played album ListenBrainz named no release-group for is left out.
- **`recent` never splits a second across two pages**: `before` is exclusive and
  `nextBefore` is the last row's second, so a page stops before a second it
  cannot finish. `genres` is the 20 most played; `genrePlays` covers every genre
  of a held album, shown or not. `status.error` is redacted (`_redact_secrets`)
  before it is stored.
- **`artistPlays` on `/api/fresh-releases`** is the sum of the row's artists'
  all-time plays, `0` until the first import has finished (a partial count is not
  shown as the count). `_fresh_apply_limit` keeps `artistPlays > 0` rows through
  the cut the way it keeps owned-artist rows. Only the **first** artist MBID of a
  listen is stored, so a collaboration counts for its lead artist. **A listen
  carries the MBID its file is tagged with**, so for an artist overridden
  because that tag is wrong every play is under the tag: a Fresh row, which
  names the real artist, counts the plays under the override and those under
  each `tagMbid` it stands in for (`_listens_tags_of_overrides`), each MBID
  once. The tag's own artist, if it is a real other one, keeps those plays too:
  the two cannot be told apart.

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

The SPA was restructured on branch `ui-polish` (`SESSION-2026-09-26-ui-audit-polish.md`
has the audit), and rebuilt on a new frame in 2026-10 (`feat/web-ui-redesign`,
merged 2026-10-11): `README.md` § The React web frontend describes the screens
as they are now — Needs you, Activity, Discover, Library, System, with artist
and album pages opening on top.
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
  transition into `downloaded` (from `_set_review_track_state` and the reconcile
  pass). It first read the group's `updated_at`
  too, which Skip, Unhide, allow-MP3, a rescan, a source search and the Stuck
  card's own Reconcile all bump without placing anything, so a stuck album read
  "working" for another half hour after each. One exception (B-025): a
  Loose-tracks row whose auto-placement already ran and missed
  (`placement_missed_at` ≥ its `downloaded_at`, stamped by
  `_auto_place_loose_track`) is waiting on nothing, so it does not count as
  recent — a loose group whose only downloaded rows missed is stalled at once
  instead of reading "downloading" (and answering `alreadyActive`) for 30 min.
  The list snapshot carries that stamp too. On a stalled group
  `_approve_pending_missing_tracks` re-approves the downloaded tracks, so the
  Stuck card's "download again" and "search again" can actually fetch — except
  a missed loose row whose file is still in /downloads while any other row is
  approvable: it keeps its file and its "Pick the release" hint, and it is
  re-approved only when it is all there is to fetch (a miss whose file is gone is
  fetched with the rest — nothing is left to keep). A loose group's Stuck card
  offers Pick the release, never Reconcile (it has no release to reconcile
  against); the SPA reads that from the gap view's `loose`, not from an empty
  `releaseMbid`, which an album gap with no MBID has too. The
  fetch route answers 400 `nothing_to_fetch` when nothing is approvable rather
  than reporting every source as a peer rejection. `refresh_group_missing` is a
  no-op on a group with no `albums` (playlist and Spotify groups, and the retired
  `repair` ones) — it used to blank `canonical_mbid` and `missing_tracks` and mark
  them complete.
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
  wrong: `_place` retags every file it pairs with the target release — a
  hand route places each file it pairs by title, as it always did. What
  bounds it since item 11 of § Acquisition reliability: a file with no slot
  is filed as a bonus track only when half of the release was found among
  the files and its own tags name the album, and a file is paired by its
  length only with the slot its own number names, three such pairings at
  the least.
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
  last until that artist's next scan. **A rescan keeps a `queued`/`downloading`
  row only when a live transfer backs it** (B-022): `_STALE_ON_RESCAN_DECISIONS`
  resets both (ba8b83e — a phantom with nothing behind it must be retryable), and
  `_inherit_track_state` exempts a row whose previous index is in
  `_live_transfer_track_indexes`, the same test `_review_group_next_action` and
  `_approve_pending_missing_tracks` apply, carrying its transfer fields
  (`_IN_FLIGHT_TRACK_FIELDS`) with it. Without that, a rescan read a running fill as
  `ready`, the fetch route's `alreadyActive` dedupe stopped firing and both
  transfers were approved for a second download. Every rebuild — the merge's swap
  in `_union_review_groups` / `_replace_review_groups`, and `refresh_group_missing`
  — then calls `_repoint_transfer_indexes` **under the same `_review_lock` hold as
  the swap**: the poller, the source switch and the cancel paths address a row by
  `review_track_index` (on the transfer, its track dict, and the album fill's
  `review_track_indexes`/`missing_tracks`), so a rescan that lists a new track
  first would otherwise have the poller's "downloaded" land on the neighbour. A
  row the rebuild drops gets index `None`, which `_set_review_track_state` ignores.
  **The re-point reaches only what is registered**, so a writer that reads an index
  and then waits names its row as well: `_set_review_track_state(...,
  expect_key=_rescan_track_key(row))` writes the index only while the row there is
  still that row, else follows the row by key within the group, else drops the write
  (the group lists it nowhere, or twice) with one log line (`_review_row_index`).
  The verifier (Navidrome round trip), the poller (its `_resolve_local_path` await,
  after which it also re-reads the transfer's index), `_loose_place_miss` (the
  transfer is deleted before auto-placement), `_mark_detached_entries_cancelled`
  (entries carry `review_track_key` from `_detach_transfer`) and the loose-track
  linker pass it; the key comes from the row copy the writer holds
  (`_review_row_identity` — a transfer's `track` is the enqueue's `dict(row)`). And
  `slskd_enqueue`, which registers only after its slskd call, re-resolves the index
  by that key and registers in one `_review_lock` hold (`_current_review_index`),
  so no rescan falls between. Without it the verifier turned a never-downloaded
  row `verified` — and `verified` survives every rescan.
  **`placed` goes stale; `verified` only once the song is gone; `filed_extra` never.** A `placed` row a
  scan still reports missing resets to `pending` (ba8b83e: an unchecked claim) —
  except while a verifier runs for its group (`_placement_verifiers`): until
  Navidrome indexes the file every scan reports it missing, and a reset took it
  off the verifier and offered it for a second download; the verifier verifies it
  or resets it itself at the deadline. A verifier is a thread and a restart ends it,
  so `main()` starts one for every group holding `placed` rows right after loading
  the review, before anything can rescan (`_resume_placement_verification`; each row
  gets its window again from boot) — otherwise the first rescan after a deploy reset
  every freshly placed row for re-download. A `verified` row (and the legacy
  `navidrome_verified`) was matched in Navidrome and a `filed_extra` row is a file
  filed into the library, so they are kept whatever a rescan says: when the two
  disagree it is the rescan's matching that missed. The verifier searches
  library-wide while a rescan reads only the group's own album records — so an
  album Navidrome splits under another record reads missing, including in the
  verifier's own closing `_refresh_group_counts_after_fill` — and a playlist scan
  uses the playlist's spelling. Resetting them re-downloaded a file the library
  has. That is also what production did until B-022: the repair-job projection
  kept both settled in every group that could hold them, so ba8b83e's rule for
  them only ever existed on paper. **A disputed `verified` row is re-checked, not
  kept blindly** (Q-023): the verifier records the Navidrome song it matched
  (`nd_song_id`, carried by both rebuild paths through `_REVIEW_TRACK_CARRIED_FIELDS`
  — one list now; `refresh_group_missing` kept its own inline copy, which had
  drifted), and every rebuild that makes a `verified` row with a song id live —
  the swaps in `_union_review_groups` / `_replace_review_groups`, and
  `refresh_group_missing` — queues it under the swap's own `_review_lock` hold
  (`_queue_disputed_verified`). One `verified-recheck` thread asks Navidrome for
  that song off every lock (`nd_get_song`, `getSong`) and resets the row to
  `pending` with `_set_review_track_state(..., expect_key=…)` — only while it still
  reads `verified` with that song — **only on a positive "no such song" (Subsonic
  error 70)**, and even then only once the verifier's own library-wide match
  (`nd_track_match`, the check that verified the row) also finds nothing while
  Navidrome answers and is not scanning (`_recheck_gone_song`): error 70 means "no
  song with that id", and a re-id (a rescan of moved files) answers it too, so a
  song the match finds is followed to its new id instead. An outage, a refused
  login or any other answer keeps the row; so does a song Navidrome still has on
  another album record (the split case above). A pass stops at the first
  transport error and leaves the rest queued for the next one. Still kept unchecked, as before: `filed_extra` rows (nothing records a
  song for them) and verified rows with no song id (verified before this,
  including the legacy `navidrome_verified`).
  `_replace_review_groups` still refuses two rows with
  one id. Both union and replace fold a second origin's row
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
  Dockerfile. `deploy.sh dev` passes `<commit>[+dirty]-dev`; **CI bakes it too**
  since S-012 (the workflow lives in the publish clone —
  `build-args: LB_BOT_REVISION=${{ github.sha }}` on `docker/build-push-action`,
  `.github/workflows/docker.yml`). Unset reads `unknown`. It was a hand-edited
  constant three months stale before this.
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
- **`POST /api/artist/release` answers 409** `{"error", "code": "artist_conflict",
  "artistKey"}` when the request's artist mbid conflicts with the same-name
  library artist's — Navidrome keys artists by name, so an mbid plus a same-name
  artist's nd id used to file the release under the wrong artist's discography
  (B-018, `_index_artist_mbid_conflict`). A key with no mbid on record is not a
  conflict.
- **Gap `tracks[].state` can be `"cancelled"`**, alongside the existing states,
  and `downloadError` carries the `error` text for `failed`/`cancelled` rows
  (B-004) — a cancel is the user's verdict, not a failure, and both clients used
  to render it with a Retry button.

### The web frame holds its place (B-094, 2026-10-12)

Four things from the user's first look at the override UI on a phone; none is
backend. The pure decisions are `web/src/lib/frame.js` (`frame.test.mjs`).

- **A hero's text beside something else has a floor** (N4). `.hero` is a
  wrapping flex row and `.grow` is `flex: 1; min-width: 0` — a basis of
  nothing. On a gap's detail the pager (219 px) is a third item: wherever it
  just fitted beside the text, the text got what was left — 18 px at a 412 px
  viewport, 74 px in a two-pane window of 1,100. The column's `flex-basis` is
  now `heroTextBasis(cover)`: 160 px, but never more than the row has beside
  the cover, so the cover and the text stay side by side however narrow, and
  the pager goes to a row of its own wherever the row cannot give the text
  its floor beside it. It goes by the pane, not the viewport — do not turn it
  into a media query. Of the seven `.hero` uses only `GapDetail`'s has a
  third item; a new one that gets one needs the same basis.
- **The "Can't reach lb-bot" banner folds** (N2). It was rendered only while
  `pollError` was set: everything under it went down 46 px in one frame, and
  back in one. It sits in the frame's `Roll` now, inside `.banner-fold`,
  mounted from the start; `Roll` keeps what it last held, so while it shuts
  it says what it last said. The arrival animations select `.main`'s direct
  children (`:not(.top):not(.banner-fold)`): the wrapper is that child now,
  and `.banner` itself is two levels down. Its role, its Retry and when it
  shows are as they were.
- **Nothing above a page's body changes height after its first frame** (N3).
  - *The frame's second title line.* `Page` drew `sub` only when it was not
    empty, and Needs you and Activity give theirs once their data is in: the
    head went 59 → 71 px on a first visit. A page that passes `sub` now has
    the line from its first frame, one line high (`subLine`: an empty string
    is drawn as a no-break space); a page that leaves `sub` out has no such
    line. **So `sub=""` is not "no line": it is "a line is coming"** — every
    section page passes a string, and all of them have a head of one height.
    A page passes `sub` only where a line comes (X4): Search with no query,
    and an artist or album page whose read failed, pass none.
  - *Library › Artists' "N of M indexed"* (`IndexLine` in `Artist.jsx`). It
    returned nothing until `/api/library-index/status` answered; on a phone
    the filter row wraps, so the row went 53 → 90 px a moment after every
    arrival and the grid dropped 37 px. The same button now stands there from
    the first frame — `inert`, carrying the line it will most likely read
    (counted from the page's own artist list: as many digits, in the tabular
    font) but not shown, a skeleton over it — and takes lb-bot's line in the
    same box. The last answer is kept for the session (`indexStatusKept`, a
    module variable): a later visit draws it at once and reads again behind
    it. A read that fails with nothing kept folds the place away (a `Roll`).
    Which count it shows is as it was. **Known and left:** a line longer than
    the stand-in (it says "· N stale", or "· building…") is wider than the box
    was, which at a width where the short one just fitted beside the filter
    can still wrap the row once, on the session's first visit; and on a row
    that has wrapped, the fold's end takes the row gap (10 px) in one step.
- **A page comes back as it was left** (N1). The app mounts one page
  (`PAGES[tab]`) and unmounts it when left, and the router kept `{hash, tab,
  key, title}` per history entry: every scroller's offset and every page's
  own `useState` was gone after a visit elsewhere. **View state belongs to
  the history entry.** `web/src/lib/view.js` is the decisions (pure,
  `view.test.mjs`), `lib/router.js` holds the bags, `lib/useView.js` is the
  hooks. What binds a change:
  - **The bag** (`view` on an entry): scroll offsets and small scalars only
    (`boundBag`: finite numbers, booleans, strings of at most 200 characters,
    24 names) — never a list or an answer. Stored with the entries in
    `sessionStorage` `lb.nav` (a quarter second after a write, and on
    `pagehide`), so a reload inside a session keeps it. **Additive**: the
    build before reads these entries and ignores `view`; this one reads
    entries without it (`readStored`).
  - **Which arrival restores** (`restores`, the router's `arrival`: `{ n,
    restore }`): Back and Forward (`pop`), a reload (`boot`), and the bar
    going back to where a section was left (`return` — `navigateSection`
    opens that entry's hash as a new entry, which takes a copy of its bag).
    A push is a fresh page at its top: a link, a tab, a row, an address
    typed. **The bar pressed for the section you are in resets nothing**
    (Y1): it goes to the section's front address, which from the section's
    first tab is the same page under another entry — no arrival, the page
    as it is, and Back finds it as it was left; pressed on the front
    address itself it does nothing. (That is what it did before B-094. For
    a day it gave a fresh page, and one stray tap on the bar cost the
    filter and the place for good.) From another tab of the section it is a
    tab press: a fresh page, as it always was.
  - **The same page under another entry is no arrival.** A row selected
    (`kindOf` → `soft`: the same page, the same tab) leaves the page as it
    is. **A section's front address and its first tab are one tab** (X1;
    `tabOf` / `sameTab` / `FIRST_TAB` in `lib/view.js` — the one place that
    says which tab an address is, asked by `kindOf` and by the pane's Back).
    Comparing the raw first parameter made `#/needs`, where the bottom bar
    lands, and `#/needs/gaps` two tabs: a row opened from the first was an
    arrival, and everything kept with the entry started over while the
    detail was open. Of such an entry: a pushed one starts with a copy of the bag (`bagForPush`),
    `replaceRoute` keeps the bag itself, and on every such change the page
    writes what it shows now into the entry it is now on. `arrival.n` does
    not move.
  - **Scrollers are the frame's** (`useKeptScroll` in `Page`): `.body`, and
    inside it `.two .list` and `.two .detail` — the detail's place with the
    address it was saved under, and restored only there. Saved as they are
    scrolled (one capturing listener on `.body`). Restored in a layout
    effect, so a page whose content is in hand at mount is at its place in
    its first painted frame — **which needs as many rows or tiles drawn as
    it had: keep that number too** (`gaps.rows`, `lib.tiles`). Content that
    is fetched is looked at again until it is tall enough, four seconds at
    most (`RESTORE_MS`) — and then **left where it is: nothing moves late**
    (X3; it used to go to the bottom of what there was); the user's
    own wheel, touch, press or key ends the wait at once — **but not input
    that was already under way when the page arrived** (W1): going back by a
    two-finger swipe is a stream of wheel events whose inertia goes on
    arriving over the page that has just mounted, and one of them ended the
    restore (the place was lost only when going back by a swipe). The wheel
    streams are heard for the whole app (`wheelStream`, at the window); a
    wheel is the user's only when its stream began after the page arrived
    (`wheelIsUsers`) **and it is mainly vertical** (W1b, `wheelScrolls`:
    `|deltaY| > |deltaX|` — the frame's scrollers go up and down and nowhere
    else, so a sideways wheel, a zero one and a tie never end a restore,
    whatever stream they belong to; a swipe may pause before the fingers
    lift, and what is sent at the release is a new stream, sideways like
    the swipe. Cost: someone scrolling a sideways strip while the page's
    content is still on its way is moved when the page takes its place), a
    new stream being one that comes `WHEEL_QUIET_MS`
    (250 ms) after the last event — wider than a decaying stream's steps,
    shorter than lifting the fingers and starting again. A key that was
    already down (`repeat`) and a modifier by itself are not the user
    either (`keyIsUsers`); a press and a touch always are; a scroller that
    is not laid out (the list behind an open detail on a narrow screen) is
    waited for until it is shown, by a `ResizeObserver`, however long
    (`scrollLook`). While a scroller waits its scroll events are not saved.
    For `lib/reveal.js` a restore is not the user scrolling (`forgetPane`).
    **A scroll event is written into the bag of the entry the frame was
    last drawn for** (`drawnFor`, `saveViewIn` — X6): one that lands after
    the route has changed, before the page for it is drawn, is the page
    before's.
  - **A page's own state**: `useViewState(name, initial)` in place of
    `useState`. Kept now: Library › Artists' filter, order and tiles drawn
    (`lib.q`, `lib.sort`, `lib.tiles`); the gaps queue's rows drawn
    (`gaps.rows`); To fix's two "Show all"
    (`fix.allArtists`, `fix.allSets`); the artist page's type and order
    (`artist.type`, `artist.sort`); Discover's "all genres"
    (`discover.allGenres`). **Not for** anything that sends a write or
    starts a job, nor a fold whose opening asks lb-bot for something (the
    match picker, a merge's preview, an edition's preview, the source list):
    a return does not re-open those, and a page still reads again what it
    reads on arrival. **A component that outlives its entry starts over for
    the next one** (a tab of the same section, the same kind of page for
    another artist): its kept state takes the new entry's value — the
    page's own on a push — and the frame puts its scrollers at their top.
    **A queue's filter is not this kind of state** (X2): the gaps queue's
    "Found by" is kept for the session — `useSessionState('gaps.origin')`,
    a module-level store in `lib/view.js` — like the queue's search, so a
    tab press to To fix and back leaves it alone. For a day it was the
    entry's, and a tab press reset it. **A kept "Found by" that the queue no
    longer offers is "any"** (Y2, `offeredOrAny`): its last album of that
    origin gone, or one origin left and the menu not shown, the queue would
    be filtered to nothing with nothing to clear it by. The kept value is
    not touched — it is in force again if the queue offers it again — and
    while the queue is not read yet the choice stands.
    A "reset when the filter changes" effect must not
    run at mount (`lib.tiles`, `gaps.rows` were reset to their first chunk
    by one).
  - **Needs you on a narrow screen** (N1f, N1g). The pane's Back asks the
    router whether the entry before this one is the list's own
    (`entryBefore`, `isListEntry`) and goes back to it; otherwise it
    replaces, as it did. It was a ref set by the tap, false again after the
    page had been left and come back, so Back then *replaced* the detail's
    entry with a second list entry at the list's top. The list's
    scroll-to-selected does not move a list that has a saved place on this
    arrival (`hasKeptPlace`), and a list with none brings its selected row
    into view when it is shown — it is `display: none` while a detail is
    open, where nothing can be scrolled. **A kept place is for the arrival
    it was kept for** (X5, `showsSelected`): once another album is selected
    than on that arrival — Next pressed in the pane — the list, when it is
    shown, lets go of the place (`letGoOfPlace`) and shows the selected row.
  - **Known and left.** Pages are never kept mounted
    while hidden. A kept "tiles drawn" of thousands paints them all on the
    way back. Nothing here was seen in a browser by its author: the matrix
    holds the decisions, and one scratch flow drove the router and the two
    hooks with a probe.

> **The sections from here down are the 2026-06 plan, kept as history.** The
> per-track "repair path" they build on is gone — `c5bcb77` removed its steps and
> B-022 (2026-09-28) the rest of the `repair_jobs` pipeline — so
> `repair_import_matched_tracks`, `_repair_track_metadata`, `repair_job_id` and
> `_repair_update_download` below name code that no longer exists. Placement is
> `_deterministic_album_import` / `_place_file` now, and source failover is
> § Acquisition reliability.

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
