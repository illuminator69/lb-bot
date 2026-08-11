# Implementation Plan — Audit Fixes: Duplicate Files, Source Pairing, Across-Language Search & Format Filtering

A comprehensive audit of `listenbrainz_bot.py` identified critical root causes for reported bugs in album lookup duplicate detection, single-song gap filling filename mismatches, long cross-language duplicate searches, and FLAC-only filter search exclusions.

---

## User Review Required

> [!IMPORTANT]
> **Key Architectural Decisions for Approval:**
> 1. **Single-Song Source Pairing Enhancements**: When title-matching fails for a single missing song in an album gap fill, add a **track-position fallback** (matching `track.get("position")` against track numbers in filenames like `03 - ...` or `Track 03`) even if the folder contains full album audio files (`len(audio) > len(missing_tracks)`).
> 2. **UI & API Error Visualization for Mismatched Sources**: Enhance `/api/gaps/<id>` response and UI to report detailed source file pairing breakdowns (`unmatched_files`, `matched_files`) when `"no matching file in source"` occurs, allowing the user to view available files in the candidate Soulseek folder.
> 3. **Across-Language Duplicate Search Optimization**: Pre-fetch and bulk-cache `release-group` IDs for all library albums with MBIDs prior to pairwise comparison, cap deep MB lookups per scan pass, and skip pairwise deep MB queries when neither candidate has an MBID.
> 4. **FLAC-Only / Format Scoring Fix**: Fix `_score_folder` math so `locked_file_count` is evaluated against total files or capped, preventing accepted FLAC files from being discarded when locked/extra files exist in the peer's folder. Also fix `_effective_prefs()` so `_PREFS_DEFAULTS` ranks do not forcibly append deselected formats (like `opus`).

---

## Identified Bugs & Root Causes

### 1. Duplicate files in album lookup (fails to see 3 identical songs in an album)
* **Root Cause 1**: `_song_relpath` and `_song_abs_path` do not normalize backslashes `\` to `/` on Linux POSIX environments before running `os.path.normpath`. Navidrome paths containing backslashes are misclassified as relative or fail path equality comparisons in `by_path` inside `_duplicate_file_sets`.
* **Root Cause 2**: `_duplicate_file_sets` groups intra-album files solely by title match key and MBID. Files lacking MBIDs or titles with track-position metadata (e.g. `01.flac`, `Track 01`) were not grouping by track number position.
* **Root Cause 3**: `_missing_for_album_records` reduces present tracks to `set(present_titles)` and `set(present_mbids)`. If an album has 3 identical copies of Track 1 and zero copies of Tracks 2..10, `present` is computed as `total - len(missing)` = 1, masking the duplicate file copies from album lookup summaries.

### 2. Persistent slskd source filename mismatch for a single song gap fill
* **Root Cause 1 (Prefix Stripping)**: `_file_title_key` strips leading track numbers (`04`, `1-04`), but fails when filenames start with Artist, Album, or Year (e.g. `Artist - 03 - Song.flac`, `2020 - Album - 03 - Song.flac`). `_match_key` produces `artist03song`, which fails exact/prefix title match against `song` and gets rejected by sibling title checks.
* **Root Cause 2 (Positional Fallback Gate)**: Positional fallback (`if not pairs and len(audio) == len(missing_tracks)`) is hard-gated on `len(audio) == len(missing_tracks)`. When filling 1 missing song out of a 10-track album, `len(audio)` (10) != `len(missing_tracks)` (1), so positional fallback never executes.
* **Root Cause 3 (Error Handling/Visualization)**: Unmatched tracks silently fail with `"no matching file in source"`. The API and UI do not expose what files were in the Soulseek folder or why pairing failed.

### 3. Very long across-language duplicate search
* **Root Cause 1 (Rate-Limited MB Calls in Loop)**: In `_duplicate_albums_by_signature` (`deep=True`), candidate pairs with matching track counts and similar durations execute `same_release(left, right)`. If artist names differ (e.g. localized artist names "The Beatles" vs "ビートルズ"), `_album_artist_key` match fails, triggering `rgid(left)` and `rgid(right)` which issue synchronous rate-limited HTTP calls (`mbz_get`) to MusicBrainz with `time.sleep(1.0)`. Evaluating hundreds of candidate pairs causes long delays.
* **Root Cause 2 (Missing MBID Retries)**: For untagged albums lacking `musicBrainzId`, `rgid()` returns `""` after checking cache, but candidate comparisons still re-evaluate `same_release` without early exit.

### 4. FLAC-only filter removing every search result even if source was flac-only
* **Root Cause 1 (`_score_folder` Locked File Math)**: In `slskd_run_search`, files are pre-filtered by `_score_file` to accepted formats (e.g. FLAC), so `folder["files"]` contains ONLY accepted FLAC files (`file_count`). `_score_folder` then reads `locked_count = folder.get("locked_file_count", 0)` from the raw peer response (which includes locked files across all formats/types). `avail_ratio = (file_count - locked_count) / file_count` becomes `<= 0.5` or negative when locked files exist, returning `score = 0` and dropping the source.
* **Root Cause 2 (`_effective_prefs` Format Preference Override)**: `_effective_prefs()` forces `ranks += [f for f in _PREFS_DEFAULTS["ranks"] if f not in ranks]`, appending `"opus"` back even when the user configures FLAC-only in preferences.

---

## Proposed Changes

### Core Backend (`listenbrainz_bot.py`)

#### [MODIFY] [listenbrainz_bot.py](file:///c:/Users/icher/Lb-bot-missing/listenbrainz_bot.py)

1. **Path & Track Normalization (`_song_relpath`, `_song_abs_path`, `_duplicate_file_sets`)**
   - Normalize all backslashes `\` to `/` in `_song_relpath` and `_song_abs_path`.
   - In `_duplicate_file_sets`, add track position (`track` number) as a union key factor alongside title and MBID so identical track numbers with distinct filenames are grouped.

2. **Single-Song Source Pairing & Positional Fallback (`_best_file_for_missing_track`, `_album_file_pairs_for_missing_tracks`, `_file_title_key`)**
   - Enhance `_file_title_key` to strip `Artist - `, `Album - `, `Year - ` prefixes prior to title matching.
   - In `_album_file_pairs_for_missing_tracks`, allow track-position matching (`_filename_track_number(f) == track.get("position")`) as a fallback when title matching fails, even when `len(audio) > len(missing_tracks)`.
   - Pass unmatched files into track download error details (`unmatched_source_files`) for API consumption.

3. **Cross-Language Duplicate Search Optimization (`_duplicate_albums_by_signature`)**
   - Pre-fetch release-group IDs in batch for albums that possess `musicBrainzId`.
   - Skip `same_release` deep MB checks if neither album in a candidate pair has a `musicBrainzId`.
   - Add a safety cap / timeout on deep MB lookups per scan iteration.

4. **FLAC-Only & Format Scoring Fix (`_score_folder`, `_effective_prefs`)**
   - Fix `_score_folder` math: calculate `avail_ratio` using `raw_total_files` from peer response or cap `locked_count` relative to total peer files rather than accepted FLAC files alone.
   - Fix `_effective_prefs()`: do not forcefully append default ranks if `ranks` override is explicitly set in preferences.

5. **Error Visualization & API Surface (`/api/gaps/<group_id>`)**
   - Expose detailed source file matching status (`unmatched_files`, `candidate_files`) in gap detail endpoint when a source pairing failure occurs.

---

## Verification Plan

### Automated Tests
- Run existing test suite: `python test_album_review.py`
- Add unit tests verifying:
  - `_score_folder` with FLAC-only files and non-zero `locked_file_count`.
  - `_file_title_key` with `Artist - 03 - Song.flac`.
  - Single-song positional pairing when `len(audio) > len(missing_tracks)`.
  - `_effective_prefs()` with FLAC-only ranks.
  - `_duplicate_file_sets` path normalization with Windows backslashes on POSIX.

### Manual Verification
- Test duplicate scan with deep cross-language matching.
- Test gap fill for single missing track using Soulseek source folders containing full album files.
- Verify FLAC-only setting in System preferences preserves FLAC-only filter without dropping valid FLAC search results.
