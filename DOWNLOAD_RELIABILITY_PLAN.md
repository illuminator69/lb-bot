# Download reliability: search, ranking & matching overhaul

*Plan drafted 2026-08-08. Informed by an audit of this repo's acquisition pipeline and of
Tubifarry (Lidarr Soulseek plugin, github.com/TypNull/Tubifarry) — file/class names cited below.*

## Context

Downloads fail ~50% of the time on first try, forcing manual file-picking in the web UI. The
audit found three root causes:

1. **Query building is naive.** `_group_source_plan` emits exactly two variants:
   `"{artist} {album}"` then bare `"{album}"` (`slskd_search_album_folders`). No cleaning of
   MusicBrainz canonical titles (punctuation, diacritics, `(Deluxe Edition)` suffixes), no term
   dedup — a self-titled album ("Led Zeppelin Led Zeppelin") degenerates to an artist-wide
   query. Tubifarry runs a tiered strategy chain (normalize → strip punctuation → volume/roman
   variants → trimmed words → distinctive words → track fallback) until enough results arrive
   (`SearchPipeline`, `QueryBuilder`, the `Search/Strategies/*` classes).
2. **Folder ranking ignores the album name.** `_score_folder` scores track-count proximity,
   availability, speed, queue, slots — but never checks whether the folder *is* the album
   searched for. For self-titled/ambiguous queries the entire discography ranks by upload speed
   ("3 pages of other albums"). Tubifarry fuzzy-matches the directory name against artist and
   album (FuzzySharp partial/token-sort ratios, `SlskdItemsParser.CreateAlbumData`).
3. **File↔track matching is too strict.** `_title_match_rank` accepts only exact / prefix /
   one-directional containment (`title_key in file_key`). Misses: MB title longer than filename
   ("Song (feat. X)" vs `04 - Song.flac` — containment never tested in reverse), any fuzzy
   variance ("Pt. 2" vs "Part II", typos), duration evidence (slskd sends `length`, MB has
   canonical durations — unused), and positional evidence (track numbers are only a tie-break
   bonus; the positional zip in `_album_file_pairs_for_missing_tracks` fires only when *zero*
   titles matched AND file count == missing count). This is "all files present in the source,
   bot still fails to match".

One matcher serves acquisition, failover and placement (`_best_file_for_missing_track` ←
`_enqueue_group_source_inner`, `_retry_file_from_alt_source`, `_match_slot`,
`_source_coverage_summary`), so fixing it propagates everywhere. Failover + stall watchdog are
already sound; they are not touched.

Decisions taken: **rapidfuzz** dependency approved; **up to 3 search passes**; must handle the
self-titled case and respect desired quality (bitrate/format) in ranking.

## Changes (all in `listenbrainz_bot.py` unless noted)

### 1. Query pipeline (recall)

New helper `_album_search_queries(artist, album, year, track_titles) -> list[str]` (place near
`slskd_search_album_folders`), yielding deduped, ordered variants:

1. `artist + cleaned_album` — cleaning strips bracketed edition/remaster metadata (port the
   useful subset of Tubifarry's `CleanComponentRegex`: `[FLAC|MP3|...]` tags,
   `(Deluxe Edition)`, `(2014 Remaster)` etc.), collapses whitespace, **dedupes repeated term
   sequences** (Tubifarry `QueryBuilder.DeduplicateTerms`) so self-titled → `"Led Zeppelin"` +
   append **year** when self-titled and year known (`"Led Zeppelin 1969"`).
2. Punctuation/diacritic-normalized variant, or distinctive-words variant for long titles (drop
   stopwords, keep longest words — Tubifarry `ExtractDistinctive`) — only if it differs from #1.
3. Existing album-only fallback; for self-titled albums instead `artist + most distinctive
   missing-track title` (Tubifarry `TrackFallbackStrategy`).

Rework `slskd_search_album_folders` to walk the list (max 3 passes), **merge** folder results
across passes keyed `(username, folder)` (keep the better-scored duplicate), and stop early once
a pass yields ≥2 usable folders whose album-match flag (see §2) is true. Keep the existing
`stats` publish-the-widest-pass semantics. Track-mode queries (`_group_source_plan` `"track"`
branch) get the same cleaning applied to `artist + title`.

Year availability: the review group already carries release metadata; thread `year` and
missing-track titles into the plan dict in `_group_source_plan`.

### 2. Album-aware folder ranking (the Led Zeppelin fix)

In the folder-assembly loop of `slskd_run_search`, compute with **rapidfuzz**
(`fuzz.partial_ratio`, `fuzz.token_sort_ratio`) the folder path's last one/two components vs the
*cleaned* album and artist strings — normalization mirroring Tubifarry's `NormalizeString`
(strip `._/`, non-alphanumerics, stopwords `the/a/an/feat/ft/with/and`). Store `album_match` /
`artist_match` scores on the folder dict.

`_score_folder` additions:

- Album-name match: up to **+3000** (scaled by ratio above threshold ~80); folders with no album
  resemblance get 0 bonus — peer metrics alone can no longer outrank the actual album.
- Year-in-path agreement: small bonus (+200).
- **Quality preference**: new pref in `_effective_prefs()` (System → Source preferences, default
  `flac-any`): `flac-any | flac-16-44 | highest-bitrate | prefer-opus`. Folder's dominant
  codec/bitrate/bit-depth (from its file list) scored for proximity to the preference (up to
  ~+600) instead of the current implicit higher-bitrate-always at file level (`_score_file` gets
  the same preference awareness). Surface the folder's dominant quality string in
  `_source_summary` so the picker shows it.

Expose `album_match` in `_source_summary` rows so the UI can badge "matches album title" /
"different album?".

### 3. Matcher upgrade (`_title_match_rank`, `_best_file_for_missing_track`)

Extend rank tiers (lower = tighter; the sibling tighter-claim logic in
`_best_file_for_missing_track` keeps working over the extended scale, comparing fuzzy scores
within a tier):

- **0** exact key match (unchanged)
- **1** prefix (unchanged)
- **2** containment — now **bidirectional**: also `file_key in title_key` with `len(file_key) >= 5`
- **3** fuzzy: rapidfuzz `token_sort_ratio >= 87` or `partial_ratio >= 92` over word-preserving
  normalized forms (new `_match_words(value) -> str` helper: same NFKD/casefold as `_match_key`
  but keeps single spaces between tokens; also map textual/roman numbers "Part II"→"part 2" à la
  Tubifarry `NormalizeVolume`)
- **4** duration (last resort, only when title tiers all miss): file `length` within ±3s of the
  canonical track duration, **and** unambiguous — no other canonical track of the release within
  ±5s of that file, no other unclaimed file within ±3s of the track.
- **positional fallback** (in `_album_file_pairs_for_missing_tracks` and at the end of
  `_best_file_for_missing_track` for the single-track paths): when the expanded folder's audio
  count equals the release's **total** track count and the folder's `album_match` flag is set
  (or a majority of the release's titles already matched files in it), pair remaining unmatched
  tracks by `_filename_track_number` == canonical `position`, unclaimed files only. Basis
  "position". This is the localized-filenames / full-album-folder fix.

Return the **match basis** (`exact|prefix|contained|fuzzy|duration|position`) alongside the file
(attach as `_match_basis` on the returned dict copy, or widen the return — adjust the ~5 call
sites). Consumers:

- `_match_slot` maps basis → confidence shown in placement rows (duration/position → "medium",
  named honestly).
- `_source_coverage_summary` / `_source_files_view` pass basis to `SourceRow`
  (`web/src/components/ui.jsx`) — one small UI touch: show a basis chip per matched file.
- On no-match, record the **nearest rejected candidate + score** in the track's
  `download_error` ("closest: '05 - Singularity.flac' (74%)") so a residual failure is
  diagnosable without the debug endpoint.

The regression the current strictness was built for (missing "Sing" stealing
`05 - Singularity.flac`) must stay refused: sibling tighter-claim applies to the fuzzy tier too,
and the **placement guard** (`_reject_reason` — audio already in album / contradicting tags) is
intentionally untouched as the safety net behind the looser matcher.

### 4. Album-mode enqueue: no track left behind

In `_enqueue_group_source_inner` (album branch): after `slskd_enqueue_folder`, tracks that got
"no matching file in source" currently fail immediately. Add a second pass reusing the
track-branch `ordered_sources` loop: try to match+enqueue each leftover track from the alternate
sources (expanded listings, shared claim ledger) before marking it `failed`.

### 5. Dependency + tests

- `requirements.txt`: add `rapidfuzz` (wheels only, no build step; Docker image rebuild picks it
  up).
- `test_album_review.py`: add matcher unit tests — reverse containment ("Song (feat. X)" ↔
  `04 - Song.flac`), Pt./Part/roman variants, fuzzy typo, duration-unambiguous accept +
  ambiguous refuse, positional fallback on full folder, and the Sing/Singularity refusal as a
  pinned regression. Query-builder tests: self-titled dedupe + year, edition stripping,
  distinctive-track fallback. (Dev-sandbox baseline: 32 pre-existing `No module named 'mutagen'`
  errors — anything beyond that is new.)

## Verification

1. Unit tests above pass, respecting the 32-error mutagen baseline.
2. Live stack:
   - **Led Zeppelin – Led Zeppelin**: source list must lead with folders matching the debut
     (year/tracklist), not the discography-by-upload-speed; picker shows album-match + quality
     badges.
   - An album that previously needed manual picks with all files present in the source: auto
     match should now pair them (check basis chips).
   - A known localized-filename album: positional/duration matching pairs it.
   - A deliberately wrong folder still refuses (placement guard) rather than mis-filing.
3. `GET /api/debug/slskd-search` unaffected; `/api/summary` poll path untouched (all new work
   sits in the existing background task path, no `_review_lock` held over network I/O — merge
   results via the existing re-find-and-merge shape in `_run_group_source_search`).

## Non-goals

- No changes to slskd polling/settle logic (recently fixed, traced, working).
- No changes to the placement guard or duplicate detection.
- No beets-path work.
- navi-connect client integration (see `DESIGN-lbbot-client-integration.md` in the navi-connect
  repo) is a separate effort layered on top of this one.
