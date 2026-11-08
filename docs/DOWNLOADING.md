# Downloading (optional)

lb-bot's first job is to show which albums your library is missing and where to find
them. This page is about the second, optional one: fetching a missing album for you
and placing it in the library. It is **off by default**. Nothing on the Worbus
install path needs it, and a server without it shows the gaps, the wishlist and the
find-it links instead.

## What it needs

- a running [slskd](https://github.com/slskd/slskd) (the Soulseek client lb-bot
  drives over its REST API), reachable from the lb-bot container;
- an API key for it (slskd UI → Options → API Keys);
- slskd's finished-downloads folder mounted into lb-bot at `/downloads`
  (`SLSKD_DOWNLOAD_DIR`), writable by the container's uid, because placement
  unlinks the source file after copying.

## Turning it on

Set `SLSKD_URL` (default `http://slskd:5030`) and `SLSKD_API_KEY` in `.env`, make sure
`docker-compose.yml` mounts the downloads folder at `/downloads`, and restart lb-bot.
Downloading is on **only when `SLSKD_API_KEY` is set** (read once at start). Without
it lb-bot makes no slskd call at all: every download route answers 409
`downloads_off`, and the web page and the apps leave the download controls out.
[`DEPLOYMENT.md`](DEPLOYMENT.md) lists every related variable (search timeouts, poll
intervals) and the mounts.

## How it works

With downloading on, the README's loop gains a step before Place:

1. **Acquire** — the user (or an auto mode) chooses what to download and from
   which Soulseek source. Source selection is failover-aware because peers
   frequently reject or stall.
2. **Place** — downloaded tracks are moved into the existing album folder,
   tagged from canonical MusicBrainz metadata, and verified present in Navidrome.
   No existing library files are retagged.

Both steps are where the care went: a peer's folder has
to show *the album* — not only the artist — before anything is taken from it, a
click on a source downloads that source and no other, and placement files
nothing that has not shown the album. [`ARCHITECTURE.md`](ARCHITECTURE.md)
has the rules; [`STATUS.md`](STATUS.md) says what has and has not been
confirmed on a live stack.

**Format policy:** only `flac` and `opus` are accepted (`FORMAT_PRIORITY`).
Everything else is rejected at search time. Placement and retagging also handle
`mp3` (ID3), since a group can opt into mp3 as a last resort.
