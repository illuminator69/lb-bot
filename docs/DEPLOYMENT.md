# lb-bot — deployment and configuration

How to run it, what it needs mounted, and every environment variable. The
[README](../README.md) has the two-command quick start; this is the detail behind it.

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

The runtime image installs **`libchromaprint-tools`** — the `fpcalc` binary the
optional AcoustID check fingerprints with. Nothing to do when you pull the image;
on a base of your own without it the check simply has no opinion and placement
goes on as before.

**Mounts (host → container):**

- `/mnt/user/appdata/lb-bot` → `/config` — state, the index DB (`library_index.db`,
  which also holds your overrides and what they changed), the listens mirror
  (`listens.db`), the covers a cover change moved aside (`cover-backups/`), the
  review file
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

### What Navidrome must be set to

- **"Report Real Path" for lb-bot's player** — needed for an album override, a
  cover change and an artist name merge. Navidrome hands a Subsonic client a path
  it *makes up from the tags* unless this is switched on for that client's player,
  and lb-bot rewrites tags only in files it has found for certain. In Navidrome:
  **Settings → Players →** the player whose name starts with
  **`listenbrainz-bot`** (it appears after lb-bot's first request) **→ Report
  Real Path**. Without it those three
  answer **409** with how many of the album's files lb-bot could reach, and write
  nothing; scanning, gap filling and placement are not affected, and an undo does
  not need it (it works from the paths it kept).
- **`ND_RECENTLYADDEDBYMODTIME=true`** on the Navidrome container, so a filled
  album counts as recently added (see ARCHITECTURE, "External systems").

### Local development

```bash
pip install -r requirements.txt          # python-telegram-bot, httpx, requests, Flask, mutagen, rapidfuzz
cd web && npm ci && npm run build          # build the SPA (or `npm run dev` for HMR)
python listenbrainz_bot.py                 # starts bot + web UI on :8899
```

`web/stub_server.py` exists for frontend work without the full backend.

---

## Configuration (environment variables)

Copy **`.env.example`** to `.env` beside `docker-compose.yml` and fill it in;
Compose picks it up automatically. `.env` is gitignored — nothing here belongs in
the repo, and every secret-bearing variable defaults to empty in the code.

```bash
cp .env.example .env    # then edit
docker compose up -d
```

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
- `ACOUSTID_API_KEY` — optional. With it (and `fpcalc`, which the image has) a
  downloaded file is fingerprinted before it is placed: a different song under
  the right name is refused, another recording of the same track is placed and
  said. Without it nothing is checked and nothing is refused on that ground
- `LB_BOT_HUB_URL`, `LB_BOT_HUB_TOKEN` — optional, for
  [navi-connect](https://github.com/illuminator69/navi-connect): the hub's
  *http* address and its token, so the players hear of a fill as it happens

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

**Optional checks and background work** (all default to sensible values; none is
needed to run)

- `ACOUSTID_MIN_SCORE` (`0.6`) — below this an AcoustID answer is no opinion,
  not evidence against a file
- `LB_BOT_SAME_TRACK_LENGTH_SEC` (`7`) — how many seconds a file may differ from
  its track's stated length and still be placed as *another recording of the
  same track* (a remix listed as its own recording); further off, it is refused
  as a different version
- `LB_BOT_LISTENS_SYNC` (on) — the ListenBrainz listens mirror. `0` stops the
  worker and leaves what is already stored
- `LB_BOT_AUTO_INDEX` (on) — the background worker that indexes artists nobody
  has opened yet, giving way to MusicBrainz requests a page is waiting for. `0`
  turns it off
- `LB_BOT_WISHLIST_INTERVAL` (`21600`, 6 h), `LB_BOT_WISHLIST_COOLDOWN` (`43200`,
  12 h) — how often the wishlist of albums nobody was sharing is looked at, and
  how long one album waits before it is searched again
- `LB_BOT_SOURCE_FAILOVER_DEADLINE` (`45`) — how many seconds one request may
  spend walking down the ranked source list after a peer refuses

**Web UI**

- `LB_BOT_WEB` (enable, default on), `LB_BOT_WEB_HOST` (`0.0.0.0`),
  `LB_BOT_WEB_PORT` (`8899`)

**Behavior toggles**

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
