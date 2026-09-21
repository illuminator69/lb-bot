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
