# UI Polish + Data-Truth Implementation Plan

## Summary

Fix the known UI issues in the most efficient order: first misleading data labels, then broken visual fallbacks/overflow, then interaction polish and saved tab state. Keep the current dense dashboard style; this is a targeted polish pass, not a redesign.

## Bug Map

### Fresh incorrectly says `in library` for albums the user does not own

- Backend source: `listenbrainz_bot.py:9501`, especially `owned` assignment at `listenbrainz_bot.py:9522-9526`.
- UI misuse: `web/src/panels/Fresh.jsx:66`, action swap at `web/src/panels/Fresh.jsx:69-80`, filtering at `web/src/panels/Fresh.jsx:104-107`, artist navigation at `web/src/panels/Fresh.jsx:133`, `Your artists` count at `web/src/panels/Fresh.jsx:157`.

### Downloads shows `(unnamed)` for active songs

- UI fallback: `web/src/panels/Transfers.jsx:30`, title/subtitle rendering at `web/src/panels/Transfers.jsx:30-49`.
- Album grouping: `web/src/panels/Transfers.jsx:113`, grouping logic at `web/src/panels/Transfers.jsx:135-147`.
- Backend transfer fields: `listenbrainz_bot.py:8625`, especially title construction at `listenbrainz_bot.py:8637-8659`.

### Source picker bitrate/size does not reliably fit

- Shared source row: `web/src/components/ui.jsx:175`, metadata/action layout at `web/src/components/ui.jsx:175-224`.
- Fill Gaps chosen-source card: `web/src/panels/FillGaps.jsx:195`, bitrate/size span at `web/src/panels/FillGaps.jsx:199-203` and right-side buttons at `web/src/panels/FillGaps.jsx:239-251`.

### Button hierarchy/placement inconsistencies

- Fill Gaps failure actions: `web/src/panels/FillGaps.jsx:159`.
- Artist album actions: `web/src/panels/Artist.jsx:633`.
- Downloads placement actions: `web/src/panels/Transfers.jsx:202`.
- Library search/action row: `web/src/panels/Library.jsx:436`.
- Global button styling: `web/src/index.css:155`.

### Tab switching/state persistence/refresh flicker

- Refresh race: `web/src/App.jsx:164`, especially abort/finally logic at `web/src/App.jsx:167-226`.
- Initial refresh behavior: `web/src/App.jsx:230`.
- Hash/deep-link clearing: `web/src/components/Layout.jsx:75`.

## Key Changes

### Fresh releases API/UI

- Change `/api/fresh-releases` to return `artistOwned`, `artistId`, and `releaseOwned`.
- Keep `owned` as a compatibility alias for `artistOwned`, but stop using it for the `in library` chip.
- `Your artists` filters/counts by `artistOwned`; `in library` and `View in library` use only `releaseOwned`.
- Add Fresh type filters: `Albums`, `EPs`, `Singles`, `Other`, based on existing `type`/`secondaryType`.

### Downloads display

- Add normalized backend fields: `displayTitle`, `trackTitle`, `album`, `artist`, `displaySubtitle`.
- Backend fallback order for track title: explicit row title, then filename basename, then empty.
- Frontend fallback order: `displayTitle`, `trackTitle`, `title`, filename basename, then `(unknown track)`.
- Keep album name in album group header; nested rows show track names.

### Source picker layout

- Refactor `SourceRow` into a stable grid: badge, wrapping metadata, action/status.
- Move the action/status to its own row on narrow widths.
- Apply the same wrapping metadata treatment to the Fill Gaps chosen-source card.
- Remove truncation from meaningful source stats; use wrapping tokens for bitrate, size, peer, speed, queue, and coverage.

### Interaction polish

- Add global `focus-visible`, pressed, disabled, and async feedback styles in `index.css`.
- Use one primary action per card/section.
- Make manual source picking secondary beside automatic download actions.
- Separate destructive actions like delete from normal placement actions.
- Add or reuse a small async button pattern for queue/download/search actions.

### Navigation/state

- Fix refresh race with a monotonically increasing request ID so stale requests cannot clear newer busy state.
- Avoid double-fetch on initial mount.
- Persist Fresh scope/sort/days/type, System subtab, Library page/search/filter, Fill Gaps selected album/filter/page, and source picker filters.
- Do not clear URL hash/deep links on ordinary tab changes unless intentionally returning to a top-level tab.

## Implementation Order

1. Fresh ownership split in `listenbrainz_bot.py` around `/api/fresh-releases`, then update `web/src/panels/Fresh.jsx`.
2. Downloads normalized display fields in `_transfers_view`, then update `web/src/panels/Transfers.jsx`.
3. Source metadata wrapping in `web/src/components/ui.jsx` and the chosen-source card in `web/src/panels/FillGaps.jsx`.
4. Button hierarchy and feedback in the listed page files plus `web/src/index.css`.
5. Refresh/state persistence fixes in `web/src/App.jsx` and `web/src/components/Layout.jsx`.
6. Fresh type filters/sorting polish and final copy cleanup.
7. Final visual QA pass for mojibake glyphs, overflow, focus states, and small-width layouts.

## Test Plan

- Fresh: owned artist with new album still shows `Get this album`; exact owned release shows `in library`; `Your artists` still means artists already in library.
- Fresh filters: type filters combine correctly with date/artist sorting and empty states.
- Downloads: active transfer with missing title uses filename fallback; grouped downloads show album header plus readable track rows; no normal `(unnamed)` display.
- Source picker: long bitrate/size/peer strings fit without horizontal overflow at narrow and desktop widths.
- Buttons: primary/secondary/destructive hierarchy is consistent across Fill Gaps, Artist, Fresh, Library, and Downloads.
- Navigation: fast tab switching does not flicker stale busy state; subtab/filter choices survive tab changes and reload.
- Accessibility: keyboard focus is visible, disabled states are semantic, compact buttons remain usable.

## Assumptions

- `releaseOwned` should only be true for reliable release/release-group MBID matches. Do not fuzzy-match album titles to hide download actions.
- If exact release ownership cannot be determined from current metadata, return `releaseOwned: false` and keep `Get this album` available.
- Keep the current visual density and token system.

## Coding Agent Prompt

You are working in `the lb-bot repo`. Implement the UI polish plan above. Start from the bug map paths and line numbers; do not spend time rediscovering patterns.

Fix in this order:

1. Fresh ownership split in `listenbrainz_bot.py` around `/api/fresh-releases`, then update `web/src/panels/Fresh.jsx`.
2. Downloads normalized display fields in `_transfers_view`, then update `web/src/panels/Transfers.jsx`.
3. Source metadata wrapping in `web/src/components/ui.jsx` and the chosen-source card in `web/src/panels/FillGaps.jsx`.
4. Button hierarchy and feedback in the listed page files plus `web/src/index.css`.
5. Refresh/state persistence fixes in `web/src/App.jsx` and `web/src/components/Layout.jsx`.

Preserve existing design tokens and app structure. Keep edits scoped, avoid broad refactors, and verify with a production build plus targeted manual checks for Fresh, Downloads, Source picker, and tab switching.
