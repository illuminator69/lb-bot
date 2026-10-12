# lb-bot — deployment and configuration

How to run it, what it needs mounted, what it needs of Navidrome, and every environment
variable. The [README](../README.md) has the two-command quick start; this is the detail
behind it.

---

## Runtime & deployment

Docker, defined by `Dockerfile` and `docker-compose.yml`.

A prebuilt image is published to **GHCR** on every push to `main`
(`.github/workflows/docker.yml`), and `docker-compose.yml` pulls from there:

```bash
docker compose pull && docker compose up -d    # update to the latest build
```

To build locally instead, comment the `image:` line in the compose file and
uncomment `build: .`.

**What is in the image.** Two stages: `node:22-slim` builds the web UI (`npm ci`,
`npm run build`), then `python:3.11-slim` gets `libchromaprint-tools`, the Python
requirements, `listenbrainz_bot.py` and the built `web/dist`. It runs
`python -u listenbrainz_bot.py` and exposes `8899`. There is no init system: the
process is PID 1.

- **`libchromaprint-tools`** is the `fpcalc` binary the optional AcoustID check
  fingerprints with. On a base of your own without it the check has no opinion and
  placement goes on as it would without the check.
- **`LB_BOT_REVISION`** is a build argument baked into the image (CI passes the
  commit); System › Status shows it. Not something to set at run time.

**Mounts (host → container)**, as the reference compose file has them:

- `/mnt/user/appdata/lb-bot` → `/config` — everything lb-bot keeps (below)
- `/mnt/user/appdata/slskd/downloads` → `/downloads` (`SLSKD_DOWNLOAD_DIR`) — the
  folder slskd downloads into; the same files slskd sees
- `/mnt/user/Music` → `/music` (`LB_BOT_MUSIC_DIR`) — the library root, the folder
  Navidrome serves

The container runs as `user: "99:100"` (Unraid `nobody:users`) so placed tracks
aren't root-owned. **All three mounts must be writable by that uid** — including
`/downloads`, since placement moves the file out of it. The bot applies its own umask
at import (`LB_BOT_UMASK`).

**What `/config` holds:**

- `lb_bot_state.json` — transfers in flight, the fill ledger. It embeds the Telegram
  token and chat id: treat it as a secret.
- `library_index.db` (with its `-wal` and `-shm`) — the catalogue, the review, your
  overrides and what they changed, the wishlist; and `library_index.db.hwm.json` beside
  it. **Restoring a backup: restore the database and leave the current `.hwm.json` in
  place** — it is what tells mirroring clients that the index went backwards.
- `listens.db` — the ListenBrainz listens mirror. Deleting it with lb-bot stopped
  makes it import again from scratch.
- `missing_album_review.json` — tasks, searches and the last duplicate scan;
  `mbz_cache.json` — cached MusicBrainz answers, which can be deleted with lb-bot
  stopped at the cost of asking again.
- `cover-backups/` — the covers a cover change moved aside. They are the only copies:
  undoing the change puts them back.

> **Gotcha:** files left behind by an earlier root run (the SQLite index,
> `lb_bot_state.json`, album folders) stay root-owned and cause "attempt to write
> a readonly database" errors. One-time host fix:
> `chown -R 99:100 /mnt/user/appdata/lb-bot` (and `/mnt/user/Music` if placement
> into pre-existing folders fails).

Network: `media` (an external Docker network shared with Navidrome and slskd, so the
default URLs `http://navidrome:4533` and `http://slskd:5030` resolve). The web UI has
no login of its own — do not publish port `8899` beyond your own network.

### What Navidrome must be set to

- **"Report Real Path" for lb-bot's player** — needed for an album override, a
  cover change and an artist name merge. Navidrome hands a Subsonic client a path
  it *makes up from the tags* unless this is switched on for that client's player,
  and lb-bot rewrites tags only in files it has found for certain. In Navidrome:
  **Settings → Players →** the player whose name starts with **`listenbrainz-bot`**
  (it appears after lb-bot's first request) **→ Report Real Path**. Without it those
  three answer **409** with how many of the album's files lb-bot could reach, and write
  nothing; scanning, gap filling and placement are not affected, and an undo does
  not need it (it works from the paths it kept).
- **`ND_RECENTLYADDEDBYMODTIME=true`** on the Navidrome container, so a filled
  album counts as recently added.
- **`.lb-bot-trash` must not be indexed.** Deleted duplicates are moved to
  `LB_BOT_TRASH_DIR`, by default a dot-folder inside the library, which Navidrome
  skips. If yours indexes dot-folders, point the variable somewhere on the same
  filesystem outside what Navidrome scans.

### Local development

```bash
pip install -r requirements.txt          # in a virtualenv
cd web && npm ci && npm run build          # build the SPA (or `npm run dev` for HMR)
python listenbrainz_bot.py                 # starts bot + web UI on :8899
```

`web/stub_server.py` exists for frontend work without the full backend. The tests:
`python -m unittest test_album_review` (0 errors, 0 failures; the tests that need
recorded search answers skip, as those are not published) and
`node --test web/src/lib/*.test.mjs`.

---

## Configuration (environment variables)

Copy **`.env.example`** to `.env` beside `docker-compose.yml` and fill it in;
Compose reads it. `.env` is gitignored, and every secret-bearing variable defaults to
empty in the code.

```bash
cp .env.example .env    # then edit
docker compose up -d
```

**How a variable reaches the container.** The reference `docker-compose.yml` lists its
variables one by one under `environment:` — the *Compose* column below. A variable it
does not list never reaches the container, whatever `.env` says: to set one, add a line
for it under `environment:` (for example `LB_BOT_LISTENS_SYNC: "0"`). And a variable it
*does* list is passed even when `.env` leaves it out, as an empty string — so the code's
own default does not apply to those: set `NAVIDROME_URL` and `SLSKD_URL` in `.env`, as
the example file does.

Defaults are the code's. "fixed" in the Compose column means the compose file sets the
value itself.

**Connections and credentials**

| Variable (default) | Compose | |
|---|---|---|
| `NAVIDROME_URL` (`http://navidrome:4533`) | from `.env` |  |
| `NAVIDROME_USER`, `NAVIDROME_PASSWORD` (empty) | from `.env` | A Navidrome login. Without one nothing can be checked against the library, and the background indexer does not run |
| `SLSKD_URL` (`http://slskd:5030`) | from `.env` |  |
| `SLSKD_API_KEY` (empty) | from `.env` | slskd UI → Options → API Keys |
| `LISTENBRAINZ_USER` (empty) | from `.env` | Whose playlists are scanned and whose listens are mirrored |
| `MBZ_CONTACT` (empty) | from `.env` | Contact email the MusicBrainz API terms require |
| `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` (empty) | from `.env` | The Telegram bot. Optional if you only use the web UI |
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` (empty) | from `.env` | Optional: Spotify playlist import, and resolving a pasted Spotify link |
| `LASTFM_API_KEY` (empty) | from `.env` | Optional: cross-checks the similar-artists lookup. Without it the ListenBrainz half runs alone |
| `ACOUSTID_API_KEY` (empty) | from `.env` | Optional: with it a downloaded file is fingerprinted before it is placed — a different song under the right name is refused, another recording of the same track is placed and said. Without it nothing is checked and nothing is refused on that ground |
| `LB_BOT_HUB_URL`, `LB_BOT_HUB_TOKEN` (empty) | from `.env` | Optional, for [navi-connect](https://github.com/illuminator69/navi-connect): the hub's *http* address and its token, so the players hear of a fill and of index changes as they happen |

**Paths and files**

| Variable (default) | Compose | |
|---|---|---|
| `LB_BOT_MUSIC_DIR` (`/music`) | fixed | The library root |
| `SLSKD_DOWNLOAD_DIR` (`/downloads`) | fixed | Where slskd's finished downloads are |
| `LB_BOT_STATE` (`lb_bot_state.json`) — relative to the working directory | fixed: `/config/lb_bot_state.json` | The state file. `mbz_cache.json` is kept beside it |
| `LB_BOT_REVIEW_FILE` (`/config/missing_album_review.json`) | fixed |  |
| `LB_BOT_LIBRARY_INDEX` (`/config/library_index.db`) | not listed | The index. `listens.db` and `cover-backups/` are kept beside it |
| `LB_BOT_MBZ_CACHE` — `mbz_cache.json` beside the state file | not listed |  |
| `LB_BOT_TRASH_DIR` (`.lb-bot-trash` under the library root) | not listed | Where deleted duplicates go instead of being unlinked; must be on the library's filesystem for the move to stay a rename |
| `LB_BOT_UMASK` (`002`) | fixed | Applied by the bot at import |

**Web UI**

| Variable (default) | Compose | |
|---|---|---|
| `LB_BOT_WEB` (`1`) | fixed | `0` runs the Telegram bot alone — and with it none of the workers the web side starts: no background indexing, no listens mirror, no index feed for a hub |
| `LB_BOT_WEB_HOST` (`0.0.0.0`), `LB_BOT_WEB_PORT` (`8899`) | fixed |  |

**Optional checks and background work** — none is needed to run, and none is listed in
the reference compose file except the first

| Variable (default) | Compose | |
|---|---|---|
| `LB_BOT_INDEX_TTL_DAYS` (`30`) | from `.env` | How old an artist's indexed discography may be before it is scanned again |
| `LB_BOT_AUTO_INDEX` (`1`) | not listed | The background worker that indexes artists nobody has opened yet, giving way to MusicBrainz requests a page is waiting for. `0` turns it off |
| `LB_BOT_LISTENS_SYNC` (`1`) | not listed | The ListenBrainz listens mirror. `0` stops the worker and leaves what is already stored |
| `ACOUSTID_MIN_SCORE` (`0.6`) | not listed | Below this an AcoustID answer is no opinion, not evidence against a file |
| `LB_BOT_SAME_TRACK_LENGTH_SEC` (`7`) | not listed | How many seconds a file may differ from its track's stated length and still be placed as *another recording of the same track*; further off, it is refused as a different version |
| `LB_BOT_WISHLIST_INTERVAL` (`21600`), `LB_BOT_WISHLIST_COOLDOWN` (`43200`) — seconds: 6 h, 12 h | not listed | How often the wishlist of albums nobody was sharing is looked at, and how long one album waits before it is searched again |
| `LB_BOT_SOURCE_FAILOVER_DEADLINE` (`45`) — seconds | not listed | How long one request may spend walking down the ranked source list after a peer refuses |
| `LB_BOT_FUZZY_DUPES` (`0`) | not listed | `1` makes the duplicate-album scan compare titles fuzzily by default |

**Search and polling** — the defaults are right for almost everyone; see
ARCHITECTURE, "Keeping the polled path cheap", before changing one. None is listed in
the reference compose file.

| Variable (default) | Unit | |
|---|---|---|
| `SLSKD_SEARCH_TIMEOUT` (`75`), `SLSKD_SEARCH_POLL_INT` (`1`) | seconds | Safety cap and poll interval for an slskd search. The cap must outlast slskd's own straggler timeout: results are unreadable until the search completes |
| `SLSKD_SEARCH_MIN_RESPONSES` (`5`), `SLSKD_SEARCH_MIN_WAIT` (`5`) | peers, seconds | When to ask slskd to wrap a search up. Raise for better recall, lower for speed |
| `SLSKD_SEARCH_SETTLE` (`25`) | seconds | How long to wait for slskd to publish responses it has already counted |
| `SOURCE_RESULTS_TTL` (`180`) | seconds | How long an album's source list is reused before a new search |
| `DOWNLOAD_POLL_ACTIVE_INT` (`5`) | seconds | How often slskd's transfers are read while a download is running |
| `LB_BOT_REVIEW_FLUSH_INT` (`2.0`) | seconds | The longest a change to the review waits before it is written to disk |

**Multi-user:** the `USERS` list near the top of `listenbrainz_bot.py` holds one entry
per user — Telegram token and chat, Navidrome login, ListenBrainz user, and which
playlists to pull (`playlist_sources`; Weekly Exploration and Weekly Jams by default).
The first entry is the one the environment variables fill; more are added in the
source.

---

## Telegram commands

| Command | Action |
|---|---|
| `/start`, `/help` | Help text |
| `/scan` | Scan your playlists now for missing tracks |
| `/status` | Active downloads and album progress |
| `/pending` | Albums in progress or needing review |
| `/diag` | What the bot can reach |
| `/album <Artist - Album>` | Download a whole album by name |
| `/search <Artist - Title>` | Manual Soulseek search with per-file and per-album download buttons |
| `/spplaylist <url>` | Import a public Spotify playlist |
| `/checkalbums` | Find incomplete albums in the library |
| `/beets` | List the download folders that hold audio and offer to file each into the library. The name is from when beets did the filing; lb-bot's own placement does it now, and nothing runs beets |
| `/rescan` | Trigger a Navidrome quick scan (`/rescan full` for a full one) |

A scan runs at start (throttled, so a restart loop does not repeat it) and every
Tuesday at 09:00, container time.
