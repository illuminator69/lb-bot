# lb-bot — status

How far to trust each part. "Confirmed on a live stack" means it was watched working
against a real slskd, a real Navidrome and a real library; "built, not confirmed" means
it is in the image, unit-tested, and nobody has a record of watching it there. The
design behind each item is in [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## What it does today

lb-bot scans a Navidrome library for missing tracks and missing albums, finds them on
Soulseek through slskd, and places them into the library tagged from MusicBrainz, with
Navidrome as the check that they arrived. It has a Telegram bot and a web UI, and through
a [navi-connect](https://github.com/illuminator69/navi-connect) hub the same surface
appears inside the music players. Around that loop it lets the user correct what the
library says — which MusicBrainz artist an artist is, which release an album is (with a
cover), that two artist names are one — and it mirrors the user's ListenBrainz listens.

It runs on its author's stack as a released image built by CI; between releases the
same line runs there as development builds, which is where most of the confirmations
below come from.

**Latest release.** Adds the three corrections and their undo, the listens mirror, the
audio-fingerprint check giving real verdicts, the stricter rules for what is downloaded
and filed unattended, multi-disc releases, and the web UI on a new frame (five sections,
artist and album pages on top). The release before it was 2026-10-02. Upgrading re-scans
every artist once in the background (the index's scan version went up), which takes
some hours on a large library; until an artist has been scanned again its page says it
may be out of date.

## Confirmed on a live stack

- **The core loop.** Albums and single gaps downloaded, placed into the album's own
  folder with the release's numbering and ids, and verified in Navidrome — files
  picked by hand for one track each among them, and a whole fill watched end to end on
  the build that pairs titles written in another language.
- **A cancel stays cancelled**: a gap fill cancelled mid-download placed nothing
  afterwards.
- **The fingerprint check.** A file that was a different recording was refused with
  what it was identified as; files that were another mix of the right tracks were
  placed and said so; files whose "MusicBrainz id" tag held some other service's id
  were placed instead of refused; and "Place anyway" on a refused row placed it.
- **Corrections applied to a real library**: an artist pointed at another MusicBrainz
  artist, albums retagged to the release picked and artist names merged, each read
  back as done after Navidrome's scan; a cover saved from the Cover Art Archive,
  including one whose original was over the size limit and was taken in the Archive's
  smaller version.
- **The listens mirror**: the import began at once, resumed after a restart in the
  middle of it without starting over, and a week's play count agreed with Navidrome's
  own to within one.
- **The web UI**, walked on a desktop and a phone after its rebuild: the override,
  cover and merge pages showing a real library's state; pages coming back as they were
  left by the page's Back, the browser's buttons, a touchpad swipe and on a phone; the
  layout items of the new frame at phone and desktop widths.
- **Through a navi-connect hub**: the override, merge, listening and fill routes answer
  as a client calls them, and a preview of an override writes nothing.

## Built, not confirmed

In the released image and covered by the unit suite, with no record of being watched on
a live stack:

- **Undoing** an album override, a cover change or a name merge on real files, and
  **removing** an artist override.
- **A correction started from lb-bot's own page** rather than from a player. The
  routes are the same, but the record does not say which of the corrections above was
  made from which.
- **The listens import running to its end**, its total against ListenBrainz's own
  count, and the "played most, not in your library" list against a real library.
- **"Download all"** and a source row that reports more files than the album has: no
  search sample has shown one.
- **The multi-disc rules on a real box set**, and the "one spelling" rule seen through
  to Navidrome listing one artist where it listed two.
- **A source being replaced only when it has given nothing**, on a peer that refuses
  some files and serves the rest.
- **What Navidrome carries across a retag** — favourites, ratings, play counts,
  playlist membership. A retag is expected to change the album's id; what follows the
  album to its new id was not measured, and the UI says nothing about it.
- **Older parts with no specific check on record**: the duplicate-file report with its
  trash and restore; the cross-language duplicate-album pass; the per-album quality
  preference; the wishlist's slow re-search; the paste-a-link resolver; Spotify
  playlist import.
- **Browsers other than Chromium** for the web UI, and how far apart a real touchpad's
  last inertia events come (the rule that keeps a swipe back from losing the place
  assumes under a quarter of a second, and ignores sideways wheel events altogether).

## Known limits, and things left on purpose

**Acquisition**

- Only FLAC and Opus are searched for; MP3 is a per-album opt-in, ranked last.
- Nothing is downloaded unattended from a folder that has not shown *the album*. The
  cost: a single whose folder does not name its artist exactly, and an album whose
  title has no word of its own shared with untitled file names, get no automatic
  choice — only the picker.
- A file that is no track of the release (a bonus track of another edition, a hidden
  track) is not fetched with the album when the folder holds the whole release under
  exact titles; the fill says how many it left, and "Download all" fetches them.
- slskd ends a search at its response limit, so for a title made of common words each
  search is a different sample of the peers. A second question is asked; a right
  folder can still be absent from both.

**Placement**

- An album whose files carry neither its titles nor usable track numbers is refused
  whole, and one stray numbered file among untitled ones stops all of them being
  placed by number and length. Pairing by hand is the way.
- A lone file that is no track of its release cannot be filed by any route.
- "Another recording of the same track" is judged by title, artist and length: a
  different song with the same title by the same artist within a few seconds of the
  track's length is placed, and said.
- On a Various Artists album that judgement asks no artist at all.

**Overrides and merges**

- lb-bot writes FLAC, Opus and MP3 tags. A file in another format is counted and
  reported, not rewritten — after a merge the old spelling is then still an artist.
- An undo restores the files that were rewritten. A file placed into the album after
  the change keeps what it was given; a folder renamed after the change cannot be
  undone until it is named back.
- An ID3v2.3 file does not come back byte for byte from an override and its undo: the
  values are the same, some frames are written in their v2.4 form.
- An album that shares its folder with files of another album takes no cover through
  lb-bot.
- An artist override stored for a name that a merge then removed stays in the list
  applying to nobody; whether undoing the merge brings it back into force depends on
  Navidrome giving the re-created artist the id it had.
- A collaboration credit is never offered as a spelling of its lead artist, and a band
  whose own name contains a separator is not offered a merge with an alias that has
  none.

**The rest**

- The web UI has no login; it is meant for a private network.
- The listens mirror reads ListenBrainz only, and stores the first artist of a listen:
  a collaboration counts for its lead artist.
- With `LB_BOT_WEB=0` the background indexer, the listens mirror and the index feed do
  not run.
- Line numbers quoted in older notes drift; navigate by function name.
