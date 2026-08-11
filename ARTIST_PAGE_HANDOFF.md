# Handoff: Artist discography page — design pass

## Overview
lb-bot just gained a fifth screen, **Artist**, alongside Fill gaps / Downloads / Library /
System. Given an artist name, it pulls their full MusicBrainz discography and classifies every
release against the user's Navidrome library — complete / has gaps / missing entirely /
untagged — then lets the user drill into any missing album to see its tracklist and pull
Soulseek sources for it.

**The backend and wiring are done and working.** `web/src/panels/Artist.jsx` is a real,
functional implementation — search an artist, scan, filter, click through, download. What it is
**not** is designed. It was built by copy-adapting `Library.jsx`'s existing row-table pattern
(the fastest correct path to something working), not by treating this as the "browse an artist,
Spotify-style" screen the product intent actually calls for. That's the gap this handoff is for.

**This is a design/polish pass, not a rebuild.** Keep the component boundaries, the state flow,
and all API calls exactly as they are (see Current implementation below) — the job is visual and
interaction design on top of working plumbing, not new functionality.

## Product intent (why this screen exists)
> "an artist page (spotify-like) where I can see the albums and then click on each one, see the
> tracklist and then find the sources to download that album"

The operative comparison is a **Spotify artist page**: a visual browse of covers or a scannable
grid, not a dense operations table. `Library.jsx`'s table (Album / Status / Tracks / Action
columns) is right for "here are 3,000 albums, filter and act on them" — it is the wrong shape
for "here are one artist's 14 albums, look at them." The design pass should feel closer to a
cover-forward album grid with light status signaling, and a details/tracklist view that reads
like an album page, not a form.

## Current implementation (do not re-derive from scratch — read these first)
- `web/src/panels/Artist.jsx` — the whole feature, three states in one file:
  - `ArtistSearch` — text input, hits `GET /api/artist/lookup?q=`, renders a plain button list of
    candidates (name, disambiguation, area).
  - `DiscographyView` — on pick, `POST /api/artist/discography` (returns `{task_id}`), polls
    `GET /api/tasks/<task_id>` every 2s until `status === 'complete'`, then reads
    `task.result = {artist_mbid, artist_name, releases: [...], review_groups: [...]}`.
    Renders `releases` through a **copy of `Library.jsx`'s `.library-table`/`.library-row`
    markup** (Album / Status / Tracks / Action columns) with filter `<Chip>`s (All / Missing /
    Has gaps / Complete / Untagged). Each `release` row:
    `{rgid, title, year, primary_type, status, group_id?, present?, total?, navidrome_album_ids?}`.
  - `AlbumDetail` — shown when a "missing" row is clicked. Fetches
    `GET /api/album/releases?rgid=` (edition picker, existing endpoint) and
    `GET /api/album/tracklist?release_mbid=` (new, returns `{tracks: [{position, title, mbid,
    duration}]}`), kicks an slskd search via `POST /api/search`, polls `GET /api/searches` and
    renders results with the existing `<SourceRow>` component. "Get this album →" button calls
    `POST /api/album/download {rgid}` (existing, auto-picks best source).
- Row-click behavior already wired and **should not change**:
  - `status === 'incomplete'` → dispatches `SET_SEL_GAP` + `SET_GAP_FILTER` + `SET_TAB('Fill
    gaps')` — jumps into the existing Fill Gaps screen for that exact review group. This is a
    deep link into an existing screen, not a new UI to design.
  - `status === 'missing'` → opens `AlbumDetail` in place.
  - `status === 'complete'` / `'untagged'` → currently inert (no click handler). Open question
    for the design pass: should these be clickable to a read-only tracklist/info view? Nothing
    on the backend blocks it — `GET /api/album/tracklist` works for any release, not just
    missing ones — this is a product/design call, not a technical constraint.
- `web/src/App.jsx` — `'Artist'` added to `SCREEN_TABS`/`SECTIONS` (a full top-level tab, not
  buried in the Advanced dropdown) and the `panels` map. No further wiring needed.
- Status → chip mapping is a **shortcut that should be reconsidered**, not a fixed contract:
  `Artist.jsx`'s `STATUS_MAP` reuses `ui.jsx`'s existing `StatusChip` tones (`complete`→
  Complete, `ready`→Has gaps, `failed`→Missing, `picking`→Untagged) purely because those tones
  already exist and happened to fit. There was no design intent behind "Missing renders in the
  same red as a failed transfer" — if a design pass wants dedicated tones/icons for these four
  album states (independent of transfer/track states), extend `STATUS` in `ui.jsx` directly;
  nothing else depends on the current reuse.
- Tracklist rows in `AlbumDetail` reuse the `.library-row` CSS class with an inline
  `gridTemplateColumns` override (3 columns: position / title / duration) and are **not**
  wrapped in `.library-table`, so they don't get its rounded-border container — a placeholder,
  not a considered layout.

## What needs real design work
1. **`DiscographyView`'s album list.** Currently a literal `Library.jsx` table clone. For an
   artist with, say, 12–20 releases, a cover-forward grid (à la Spotify's artist discography
   rail/grid) will read far better than a 4-column operations table. Cover art is already
   available for free at `https://coverartarchive.org/release-group/{rgid}/front-{size}`
   (used today at `front-96` in the table; `front-250`+ available for a larger grid tile).
   Status needs a lighter-weight treatment than a table-cell chip — a badge/corner-mark on the
   tile, or a subtle border tint, is more in keeping with a browse surface.
2. **`AlbumDetail`'s composition.** It currently stacks three unrelated-feeling sections (hero +
   edition picker, tracklist, slskd sources) copy-pasted from two different existing patterns
   (`Library.jsx`'s `SearchResults` hero, plus new tracklist rows). This is the screen closest to
   an actual "album page" and deserves the most intentional layout: cover + metadata should
   anchor it, the tracklist should read like a real tracklist (not a repurposed table row), and
   the source list is secondary/progressive (maybe collapsed until requested, since it's a
   Soulseek implementation detail the user doesn't need to see immediately).
3. **Empty/loading states.** `ArtistSearch` before a query, `DiscographyView` while the scan
   task is running ("this can take a minute for prolific artists" is the only feedback right
   now — no skeleton/progress treatment), and `AlbumDetail` while sources are still trickling in.
   All currently just `<p className="muted">`. `EmptyState` (`ui.jsx`) exists and is imported but
   underused — decide where it actually belongs vs. plainer inline copy.
4. **Responsive behavior.** Not addressed at all yet. Per `README.md`'s existing breakpoints
   (≤860 / ≤768 / ≤560), decide how a cover grid reflows (this is the one place a true grid,
   not a stacked-card fallback, might be the right narrow-width behavior too — worth a fresh
   look rather than copying Library's "collapse table to stacked cards" pattern verbatim).
5. **The four-status vocabulary itself.** Complete / Has gaps / Missing / Untagged is functional
   but flat. Consider whether "Untagged" (an owned album lb-bot can't verify) needs a distinct
   visual treatment that invites a fix (e.g. a hint pointing at whatever retag/beets flow
   already exists), rather than sitting visually identical in weight to the other three states.

## Design tokens & shared components (reuse, do not reinvent)
Everything in `README.md`'s "Design tokens" and "Assets" sections applies unchanged — same two
fonts, same CSS custom property token set, same shadow/radius/spacing scale. Reusable primitives
already imported by `Artist.jsx`: `Chip`, `StatusChip`, `PageTitle`, `EmptyState`, `SourceRow`
(all in `web/src/components/ui.jsx`), `Cover` (`web/src/components/Cover.jsx` — takes `albumId`
*or* a raw `url`, falls back to a deterministic gradient keyed on `name`). Extend these rather
than introducing parallel one-off components; if `Artist.jsx` needs a new primitive (e.g. an
album-grid `Tile`), it belongs in `ui.jsx` alongside the others so it's available to future
screens too.

## Non-goals for this pass
- No backend/API changes — the contract above (`/api/artist/lookup`, `/api/artist/discography`,
  `/api/album/tracklist`, plus the pre-existing `/api/album/releases`, `/api/search`,
  `/api/searches/<id>/download`, `/api/album/download`) is final; redesign against it as-is.
- No changes to the Fill Gaps deep-link behavior for incomplete albums — that screen is out of
  scope here; Artist should only navigate into it, not duplicate it.
- No bulk/batch download UI — deliberately out of scope (per-album only, by design decision).
