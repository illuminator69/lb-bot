# lb-bot

A self-hosted **music-library gap-filler**. It scans a Navidrome library for
missing tracks and incomplete albums, acquires the missing audio from Soulseek
(via slskd), and places it back into the library — correctly tagged, no retagging
of what's already there. It runs as a single long-lived Python process exposing
**both a Telegram bot and a React web UI**.

It also has a **second front end you don't run yourself**: through
[navi-connect](https://github.com/illuminator69/navi-connect), lb-bot's
discography and gap-filling surface appears *inside* the music players — the
albums you're missing show up on the artist page next to the ones you own, and a
download is reviewed and started from there. If that's what you came for, the
[navi-connect setup guide](https://github.com/illuminator69/navi-connect/blob/main/TESTING-SETUP.md)
is the place to start; it covers both halves.

<p align="center">
  <img src="docs/screenshots/lb-bot-fill-gaps.png" width="720"
       alt="lb-bot Fill gaps workspace showing an album with 14 of 16 present, 2 tracks missing, and the missing track list" />
</p>

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
placement) are the active work area — see [`docs/STATUS.md`](docs/STATUS.md) and `CLAUDE.md`.

**Format policy:** only `flac` and `opus` are accepted (`FORMAT_PRIORITY`).
Everything else is rejected at search time. Placement and retagging also handle
`mp3` (ID3), since a group can opt into mp3 as a last resort.

---

## Quick start

A prebuilt image is published to GHCR on every push to `main`, and
`docker-compose.yml` pulls from there:

```bash
cp .env.example .env     # then edit — Telegram token, Navidrome, slskd, MusicBrainz contact
docker compose pull && docker compose up -d
```

The web UI is on **:8899**. Three mounts have to be writable by the container's uid,
including the downloads directory — placement unlinks the source file after copying.
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) has the mounts, every environment variable,
the Telegram commands, and the one-time `chown` that fixes "attempt to write a readonly
database".

To run it directly instead:

```bash
pip install -r requirements.txt
cd web && npm ci && npm run build     # or `npm run dev` for HMR
python listenbrainz_bot.py            # bot + web UI on :8899
```

---

## Documentation

| | |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | What the process is doing: the three concurrent halves, the state files and index schema, the systems it talks to, the `/api/*` surface, and how placement actually works. |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Docker, mounts and ownership, every environment variable, local development, Telegram commands. |
| [`CLAUDE.md`](CLAUDE.md) | The deep, opinionated design notes — placement internals, beets removal, runtime chown quirks. **Read this before touching the gap-fill or repair pipeline.** |
| [`docs/STATUS.md`](docs/STATUS.md) | What has landed but not yet been run against the live stack. |

Line numbers in `CLAUDE.md` and in code comments are snapshot hints and drift —
navigate by function name.

---

## Repo map

- `listenbrainz_bot.py` — everything (bot, web server, API, workers, placement).
- `web/` — React SPA (Vite/Tailwind); `web/dist` is the built output served by Flask.
- `Dockerfile`, `docker-compose.yml` — build & runtime.
- `requirements.txt` — Python deps (`mutagen` declared explicitly
  post-beets-removal; `rapidfuzz` for folder ranking and file↔track matching —
  wheels only, and the code falls back to `difflib` if it is missing rather than
  failing to import).
- `docs/` — architecture, deployment and status.
- `CLAUDE.md` — authoritative design notes; **read before editing the pipeline.**
- `AGENTS.md` — agent instructions.
- `test_album_review.py` — test scaffolding for the album-review flow. Its
  baseline is **32 errors**, all `No module named 'mutagen'` in this
  environment; anything beyond that is yours.
