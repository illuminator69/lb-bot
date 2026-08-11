# Handoff: lb-bot frontend rewrite

## Overview
lb-bot is a self-hosted music-library gap-filler. It scans a Navidrome library against
canonical MusicBrainz tracklists, finds missing tracks, sources them from slskd (Soulseek),
downloads and files them, and reports health. This handoff covers a **ground-up rewrite of the
web frontend** — a single unified app with four screens (Fill gaps, Downloads, Library,
System), a live theming system, and fully optimistic interactions.

**This is a rewrite, not a reskin.** The current React frontend (in
`illuminator69/Lb-bot-missing`, branch `claude/unraid-containers-handoff-egnevy`) should be
restructured around the information architecture and interaction model described here — new
component boundaries, new state model, real API wiring — not merely recolored to match the
mock. Where this document and the existing code disagree on structure, this document wins.

## About the design files
The files in this bundle are **design references authored in HTML** (`.dc.html` — a
single-file component format with inline styles). They are prototypes that show the intended
**look, layout, copy, and behavior**. They are **not** production code to lift verbatim:

- The runtime is a custom template format (`<sc-if>`, `<sc-for>`, `{{ }}` holes,
  a `renderVals()` logic class). **Do not port this format.** Recreate the same UI in the
  target codebase's real framework (React) using its existing patterns, router, data layer,
  and component library.
- Styling in the mock is 100% inline for streaming reasons. In the rewrite, use whatever the
  codebase already uses (CSS Modules, Tailwind, styled-components, etc.) — but preserve the
  **token system** (see Design Tokens) as real CSS custom properties, because live theming
  depends on it.
- All data in the mock is hardcoded. The rewrite must wire to the real Flask API. A companion
  document, **`BACKEND_RECOMMENDATIONS.md`** (included in this bundle), specifies the endpoints
  and payload shapes the frontend should consume; build the data layer against that contract.

## Fidelity
**High-fidelity.** Colors, typography, spacing, copy, and interaction states are final and
should be reproduced faithfully. The two fonts (Space Grotesk, IBM Plex Mono) and the exact
token values below are the source of truth. Layout measurements (rail width, table columns,
card padding, radii) are intentional — match them.

The one area deliberately left flexible is **component decomposition**: the mock is a single
monolith for authoring reasons. The rewrite should break it into real components (below).

---

## Architecture the rewrite should adopt

The mock is one file with a flat `renderVals()`. Do **not** reproduce that shape. Target this
component tree instead:

```
<App>                         theme provider + router + live-data subscription
 ├─ <AppHeader>               logo, screen nav, live-status, <AppearanceMenu>
 ├─ <FillGapsScreen>
 │   ├─ <QueueRail>           search + filter chips + <QueueItem> list (collapses on narrow)
 │   └─ <AlbumWorkspace>      hero + one of: <SourceReady> / <SourcePicker> /
 │                             <DownloadingCard> / <FailedCard>, then <MissingTracks>
 ├─ <DownloadsScreen>         <TransferRow> list + <PlacementCard> grid
 ├─ <LibraryScreen>
 │   ├─ <AddMusicBar>
 │   ├─ <SearchResults>       (release picker + source list) — shown when a search is active
 │   └─ <LibraryTable>        header + <LibraryRow> (card layout on narrow)
 └─ <SystemScreen>
     ├─ <HealthGrid>          <HealthCard>
     ├─ <SourcePrefs>         <RankRow> reorder + fallback + <GuardRow> toggles
     └─ <LogViewer>           filter chips + streamed <LogLine>
```

Shared primitives to extract: `<Badge>` (format chip FLAC/OPUS), `<StatusChip>` (state pills),
`<SourceRow>` (used by both the Fill-gaps picker and the Library search results — same shape),
`<Toggle>` (guards), `<Pager>` (source pagination), `<CoverArt>` (see Assets).

Note `<SourceRow>` appears in two places with identical structure — build it once.

---

## Screens / Views

### 1. App header (shared)
- **Layout**: sticky top bar, `flex`, `align-items:center`, `gap:18px`, padding `14px 24px`,
  `background: var(--surface)`, `border-bottom: 1px solid var(--border)`. Wraps on narrow.
- **Components**:
  - Wordmark `lb-bot` — 20px / 700 / `var(--text)`, `letter-spacing:-.01em`.
  - **Screen nav**: 4 pill buttons in a segmented container (`var(--inset-track)` bg, 1px
    `var(--border)`, radius 10, 3px pad). Active pill: `var(--accent)` bg, `#17130f` text,
    600. Inactive: transparent, `var(--muted)`, 400. Labels: Fill gaps / Downloads / Library
    / System.
  - Spacer, then a context **stat** (13px `var(--muted)`) that hides below 560px, and a
    **live indicator**: 7px green dot + "Live · 3s" in `var(--green)`.
  - **Appearance button** (`◑`, 34×34, radius 9) opens `<AppearanceMenu>` popover (268px,
    `var(--surface)`, radius 14, drop shadow). Contains: Theme segmented (Dark/Light), Accent
    row (4 round 28px swatches — Amber/Sky/Sage/Rose), Background row (3 chips — Warm/Slate/
    Plum). Selecting any option re-themes the whole app live (see Theming).

### 2. Fill gaps
Two-column: **320px queue rail** + **1fr workspace**, each independently scrolling at
`calc(100vh - 57px)`.

- **QueueRail** (`var(--rail)` bg, right border):
  - Search input (full width, `var(--inset-deep)`, radius 9).
  - Filter chips: "Needs you · 46" (active — `var(--accent)` bg / `#17130f`), "Working · 12",
    "Done" (inactive — `var(--surface)` / `var(--muted)` / border).
  - Scrolling `<QueueItem>` list. Each: 44px cover thumb + album (13.5px/600) + artist
    (12px `var(--muted)`) + a state line with a colored dot. Active item: `var(--active-item)`
    bg + `var(--accent-bd)` border. State-word colors: need→accent, working→green,
    failed→danger, deciding→`var(--decide-fg)`.
- **AlbumWorkspace** (radial-gradient bg `var(--accent-tint)`→`var(--bg)`):
  - Nav row: Prev / position label / Next / spacer / "Skip this album".
  - **Hero**: 154px cover (radius 13, drop shadow) + artist (12px uppercase accent) + album
    (32px/700) + meta chips: "8 of 10 present", "canonical set" (with a tooltip explaining the
    MusicBrainz reference set — has a dashed underline), "2 tracks missing" (accent-tinted).
  - **One workspace card depending on album mode**:
    - **ready** → "Next step" card: shows the auto-picked source (format badge, bitrate, size,
      peer, coverage, speed, "✓ recommended"), a "Change source" button, "edit ranking" link,
      and a primary **"Get N tracks →"** button (`var(--accent)`).
    - **picking** → **SourcePicker**: header with count + Cancel, then paginated `<SourceRow>`
      list (4 per page) with a `<Pager>`. Each row: 44px format badge, bitrate · size, rec
      pill, peer · speed · coverage (partial coverage shows in accent-2, full in green),
      optional flag chips (slow peer / not flac / very large / missing track), and a
      "Use this →" / "Selected" button. Choosing a source returns to **ready** with it chosen.
    - **downloading** → progress card: "Working" label, "Getting N tracks from @peer",
      progress bar (accent gradient), mono stat line "36.1 / 78.4 MB · 46% · 4.2 MB/s · ~10s".
    - **failed** → recovery card (`var(--warn-tint)` bg, danger border): "!" badge, fail
      reason + detail, three actions — **"Try next best source →"** (green, primary),
      "Pick a source manually" (accent), "Retry same peer" (muted) — and a mono log tail
      showing the watchdog cancellation.
  - **Missing tracks** list: "MISSING TRACKS" header + "Reconcile downloaded files" action,
    then rows (mono position + title + artist + state chip). Chip state tracks the mode
    (ready / downloading / queued / failed).

### 3. Downloads
Centered `max-width:1200px`.
- Header: "Downloads" eyebrow + "Transfer queue" (28px/700), 3 right-aligned stat numbers
  (downloading / queued / need placement) + "Clear finished".
- **ACTIVE & QUEUED**: `<TransferRow>` cards. Downloading rows show a progress bar; queued/
  failed hide it. State chip + mono stat + action button (Cancel / Try next). A "Try next" on
  a failed transfer jumps to that album in Fill gaps.
- **NEEDS PLACEMENT** (count badge "2"): explanatory line, then a 2-col grid of
  `<PlacementCard>` (collapses to 1 col ≤768px). Each: mono source path, "Matches" + matched
  album (15px/600) + green detail line, then "Confirm & file" (optimistic → "✓ Filed") and
  "Not this — search".

### 4. Library
Centered `max-width:1200px`.
- Header: "Library" eyebrow + "3,142 albums · 187 with gaps".
- **AddMusicBar**: "Add music" label + query input + "Find album" (accent) + "Search slskd" +
  "Scan playlists". Wraps on narrow.
- **Two mutually-exclusive bodies**:
  - **Browsing** (default): filter input + filter chips (All / Has gaps / Working / Needs
    decision / Complete) + "Scan all for missing", then the **LibraryTable**.
    - Table columns: **`1fr 150px 90px 150px`** = Album / Status / Tracks / Action.
    - Header row `var(--inset-warm)`; body rows zebra-striped (`var(--surface)` /
      `var(--surface-alt)`), 1px hairline separators.
    - Album cell: 34px cover + album (13.5px/600) + artist. Status: colored chip. Tracks: mono
      "8/10". Action: context button (Fill gaps / View / Choose / Place / Recover / Rescan) —
      "Fill gaps" rows use the accent button, others muted.
    - **Narrow (≤768px)**: hide the header row, collapse each row to a stacked card
      (single-column grid).
  - **Search results** (when a search is active): back link, a hero card (cover + artist +
    album + **release picker** buttons: Original/Remaster/Deluxe with track counts), then
    "AVAILABLE ON SLSKD" — a list of `<SourceRow>` with **"Request →"** buttons that optimistically
    become "✓ Requested — see Downloads". Coverage strings reflect the selected release's
    track count.

### 5. System
Centered `max-width:1000px`. Header + a 3-tab sub-nav (Health / Source preferences / Logs).
- **Health**: 2-col grid (1 col ≤768px) of `<HealthCard>`. Each: status dot, name, PASS/FAIL
  pill, detail line, and — when failing — a **Fix** action button. Failing cards use a danger
  border. (Checks: Navidrome, slskd, MusicBrainz, Music dir.)
- **Source preferences**:
  - **Source ranking**: explanatory copy, then reorderable `<RankRow>` list (number, format
    badge, label, description, up/down arrows). Order = the priority lb-bot walks top-down.
    Below it, a **fallback** row: "If nothing above is available:" + segmented
    (Pick best / Ask me / Skip the gap).
  - **Guards**: rows with label + description + on/off value + a `<Toggle>` switch. Guards:
    full-album coverage, size cap 500 MB, min speed 1.0 MB/s, skip long queues.
- **Logs**: filter chips (All / Errors / slskd / Tasks) + a mono, scrolling log pane
  (`var(--inset-deep)`). Each line: timestamp + colored tag + message; error lines in danger.

---

## Interactions & behavior

**Everything is optimistic** — the UI updates immediately on action and reconciles with the
server response (see `BACKEND_RECOMMENDATIONS.md` §3 for the idempotency + echo contract):

- **Choose source** → album flips to `ready` with that source marked chosen.
- **Get N tracks** → album flips to `downloading`; a transfer should appear under Downloads.
- **Try next best** → picks the next-ranked source and starts downloading.
- **Confirm & file** (placement) → button flips to "✓ Filed"; row leaves the queue on refetch.
- **Request** (search result) → button flips to "✓ Requested — see Downloads".
- **Reorder ranking / toggle guards / change fallback** → persist to prefs; affects future
  auto-picks.
- **Nav between screens**, **release picker**, **log/library filters**, **source pagination**,
  and the **appearance menu** are all local UI state.
- Cross-screen jumps: a failed transfer's "Try next" and Library action buttons navigate into
  Fill gaps for that album.

**Live updates**: the header shows "Live · 3s". Replace polling with the SSE stream from
`BACKEND_RECOMMENDATIONS.md` §4 and patch individual transfers/gaps/logs by id.

**Theming transition**: `body { transition: background .35s ease }`; changing theme/accent/bg
rewrites CSS variables on `:root` and the whole app re-themes smoothly.

## Responsive behavior
A single responsive layout (no separate mobile app, no phone frame). Breakpoints as built:
- **≤860px**: Fill-gaps grid → single column; the 320px rail becomes a **horizontal queue
  scroller** above the workspace (items become fixed-width horizontally-scrolling cards).
- **≤768px**: all 2-col grids → 1 col; hero rows stack vertically; the Library table hides its
  header and each row becomes a stacked card; button/filter rows wrap.
- **≤560px**: header condenses (tighter gap/padding, context stat hidden); page padding tightens.

Use intrinsic responsive CSS where possible (`clamp()`, `minmax()`, `flex-wrap`,
`auto-fit`); reserve media queries for the structural collapses above.

## State management
Model (names are illustrative — use the codebase's conventions):
- **Navigation**: `screen` (fill/downloads/library/system), `sysTab` (health/prefs/logs) — put
  these in the router.
- **Fill gaps**: `selectedAlbumId`; per-album `mode` (ready/picking/downloading/failed) and
  `chosenSourceId`; source-picker `page`.
- **Library**: `libraryFilter`; `searchActive` + `searchQuery` + `selectedRelease`; per-source
  `requested` flags; per-placement `filed` flags.
- **System prefs** (server-persisted): `ranks[]` order, `fallback`, `guards{}` map.
- **Appearance** (persist to localStorage): `theme`, `bgTone`, `accent`, menu open.
- **Logs**: `logFilter`.

Data fetching: one query per screen returning view-ready objects, plus an SSE subscription for
live deltas. See `BACKEND_RECOMMENDATIONS.md` for the full endpoint list and payload shapes.

---

## Design tokens

Two fonts (Google Fonts):
- **Space Grotesk** (400/500/600/700) — all UI text.
- **IBM Plex Mono** (400/500/600) — bitrates, sizes, byte counts, paths, timestamps, logs.

Tokens are **CSS custom properties on `:root`**, and are **generated**, not static — this is
what makes live theming work. A `tokens()` function derives the full set from three inputs:
`theme` (dark/light), `bgTone` (warm/slate/plum), `accent` (amber/sky/sage/rose), by mixing a
few anchor colors. Reproduce this generator (a small color-mix helper + anchor table) so the
appearance menu keeps working. **Never hardcode hex in components** except album-cover
gradients and the constant `#17130f` (dark-on-accent button text).

**Anchor inputs**
- Background anchors `{ bg, surf, text, shade }` per tone × theme:
  - warm/dark `#17130f #1c1611 #f2e9dd #0a0806`; warm/light `#efe7d8 #fbf7f0 #2a2118 #d3c6ae`
  - slate/dark `#14161a #1a1d22 #e8ecf2 #080a0c`; slate/light `#e8ebf0 #fdfdfe #1c2128 #c9cfd8`
  - plum/dark `#181218 #1e171e #efe6ee #0b070b`; plum/light `#efe6ee #fdf9fc #261e26 #d2c4d2`
- Accents: amber `#e0913f` · sky `#5aa0e0` · sage `#7bab6f` · rose `#d97a8c`
- Semantic (dark / light): green `#6fae8f` / `#3f8f66`; danger `#e88a80` / `#c0463a`;
  decide `#d9b45f` / `#9a7d2e`.

**Derived token set** (default warm/dark/amber values shown for reference):
```
--bg #17130f            --surface #1c1611        --surface-alt (bg↔surf .5)
--rail --inset-track --inset-deep --inset-warm   (bg↔shade at .35/.55/.80/.42)
--active-item --sel-row (surf↔accent .05/.10)
--border --border-warm --hairline               (surf↔text .085/.12/.045)
--text #f2e9dd  --text2 --muted --faint --dot    (text↔bg .22/.42/.60/.74)
--accent #e0913f  --accent-2 --accent-3          (accent lightened/darkened by theme)
--accent-tint --accent-btn --accent-bd-soft --accent-bd --accent-bd-sel --accent-chip
--green #6fae8f  --green-on #0f2019  --green-tint --green-tint-2 --green-bd
--danger #e88a80 --danger-text --danger-tint --danger-tint-2 --danger-bd
--decide-fg #d9b45f --decide-tint  --warn-tint
```
(Full mixing ratios are in the mock's `tokens()` method — copy them exactly.)

**Type scale**: 32px album title · 28px page title · 24px search album · 20px wordmark ·
18px card heading · 15/14.5/14px body · 13.5/13/12.5px controls · 12/11.5/11px meta &
eyebrows. Eyebrows: uppercase, `letter-spacing:.09–.12em`, 600.

**Radii**: pills 999px · cards 12–14px · thumbs 7–9px · badges 8–9px · inputs/buttons 8–10px.

**Spacing**: page padding `28px 34px` (tightens to `20px 16px` narrow); card padding 14–20px;
common gaps 6/8/10/14/18px. Rail 320px. Table columns `1fr 150px 90px 150px`.

**Shadows**: cover art `0 14px 34px -10px rgba(0,0,0,.65)`; popovers/menus
`0 20px 48px -14px rgba(0,0,0,.6)`.

## Assets
- **Cover art**: the mock layers local PNG placeholders (`covers/<id>.png`) over a per-album
  CSS gradient, so the gradient is the fallback. In production, `<CoverArt>` should load from
  the backend cover proxy (`GET /api/cover/{albumId}?size=…`, `204` when absent) and fall back
  to a deterministic gradient. Album gradient values are inline in the mock data
  (`linear-gradient(135deg, …)`), keep them as the fallback palette.
- **Icons**: none as image assets — a couple of unicode glyphs (`◑`, `←`, `→`, `↑`, `↓`, `✓`,
  `!`, `☾`, `☀`). Swap for the codebase's icon set.
- **Fonts**: Space Grotesk + IBM Plex Mono via Google Fonts (or self-host).

## Files in this bundle
- `lb-bot.dc.html` — the full unified app mock (all four screens, theming, responsive). Primary
  reference.
- `lb-bot Current.dc.html` — earlier snapshot, for diffing intent if useful.
- `Fill Gaps.dc.html` — standalone Fill-gaps exploration.
- `Album Review Redesign.dc.html` — related exploration.
- `covers/` — placeholder cover PNGs referenced by the mock.
- `BACKEND_RECOMMENDATIONS.md` — the API contract and usability recommendations the data layer
  should be built against. **Read this alongside the README** — it defines the shapes every
  screen consumes.

> The `.dc.html` files open in a browser to view the intended design, but their template format
> is not meant to be ported. Recreate the UI in the app's React environment.
