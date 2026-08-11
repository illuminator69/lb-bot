# Handoff: Artist tab — implementation & wiring

**Scope of this doc.** The design pass on the Artist screen is settled in the mock
(`lb-bot.dc.html`, the **Artist** tab). This document tells a developer how to turn that mock
into working React in `illuminator69/Lb-bot-missing`, wire the real API, and implement the
design elements *properly* (tokens, components, states, responsive) — not by pasting HTML.

Read alongside:
- `README.md` — design tokens, fonts, the token generator, responsive breakpoints.
- `BACKEND_RECOMMENDATIONS.md` — API shapes; **§10** covers the new artist index + play counts.
- `ARTIST_PAGE_HANDOFF.md` — the original artist-feature handoff (product intent, existing
  `Artist.jsx` structure, endpoint list). This doc supersedes it where they differ, below.

> **The `.dc.html` template format is not production code.** `<sc-if>`/`<sc-for>`/`{{ }}` and the
> inline-everything styling exist for the mock's streaming runtime. Recreate the UI in React with
> the codebase's real patterns (components, router, data layer, CSS solution). Lift *values*
> (hex, spacing, radii, copy, interaction states), not markup.

---

## 1. What changed vs. the original Artist handoff

The original `ARTIST_PAGE_HANDOFF.md` described a **search-first** feature: `ArtistSearch`
(free-text MusicBrainz lookup) → `DiscographyView` (a `Library.jsx` table clone) → `AlbumDetail`.
Two product decisions changed that:

1. **Index-first, not search-first.** The Artist tab now **lands on an index of every artist in
   the library**, sorted **A–Z** by default with a **"Most listened"** toggle (Navidrome play
   counts). Picking an artist opens their discography. The free-text MB artist search is
   **removed** from this surface — you resolve by picking a known library artist. A filter box on
   the index just narrows the local list. (See `BACKEND_RECOMMENDATIONS.md` §10.)
2. **Discography is a cover-forward grid grouped by type** (Albums / EPs / Singles) — **not** a
   table, and **not** a layout the user toggles. The earlier "flat grid vs. grouped" choice was
   resolved to grouped-only (the flat grid was nearly identical but messier).

Everything else from the original handoff still holds: the four-status classification, the
missing→AlbumDetail / gaps→Fill-gaps deep-link routing, and reuse of shared primitives.

---

## 2. Component tree (target)

```
<ArtistScreen>                     route: /artist   (owns view state below)
 ├─ <ArtistIndex>                  default view — lands here
 │   ├─ filter <input> (local)     narrows the list client-side
 │   ├─ <SortToggle>               A–Z | Most listened  (segmented, reuse existing pill group)
 │   └─ grid of <ArtistTile>       circular cover + name + "N releases · N,NNN plays"
 ├─ <DiscographyView>              after an artist is picked
 │   ├─ <ArtistHeader>             round cover + name + summary line + "← All artists"
 │   ├─ <StatusFilterChips>        All / Missing / Has gaps / In library / Untagged (+ counts)
 │   ├─ <DiscographyScan>          skeleton grid while the discography task runs
 │   └─ <TypeSection>[]            "Albums" / "EPs" / "Singles" → grid of <ReleaseTile>
 │       └─ <ReleaseTile>          cover + corner status badge + title + year·type + status line
 └─ <AlbumDetail>                  after a release is opened
     ├─ <AlbumHero>                cover + eyebrow artist + title + status chip + primary action
     │      · missing  → "Get this album" + "Find sources" (collapsed) + auto-pick note
     │      · complete → read-only "in your library" confirmation strip
     │      · untagged → retag hint banner ("Retag with beets →")
     ├─ <SourcePanel>              collapsed until "Find sources"; reuses <SourceRow>
     └─ <TrackList>                position · title · duration (real tracklist, not a table row)
```

Shared primitives — **reuse, don't fork** (all in `web/src/components/ui.jsx` unless noted):
`Chip`, `StatusChip`, `PageTitle`, `EmptyState`, `SourceRow`, `Badge` (FLAC/OPUS), `Cover`
(`components/Cover.jsx`). **New primitives to add to `ui.jsx`** (so future screens get them):
`ReleaseTile`, `ArtistTile`, `TrackList`/`TrackRow`, `SortToggle` (or reuse the segmented-pill
component the header nav already uses).

---

## 3. View / routing / state model

- **Routing** — put the artist selection and the opened release in the URL so back/forward and
  deep links work:
  - `/artist` → index
  - `/artist/:artistId` → discography
  - `/artist/:artistId/:rgid` → album detail
  Derive the current view from the route params, not ad-hoc booleans.
- **Local UI state** (not server): index `sortBy` (`az` | `plays`), index `filterText`,
  discography `statusFilter`, album-detail `sourcesOpen`, per-release optimistic `requested`.
- **Server state**: the artist list, the discography scan result, the tracklist, slskd sources.
  Fetch per view; don't preload everything.

### Data wiring (map each view to endpoints)
| View | Fetch | Notes |
|---|---|---|
| Index | `GET /api/artists` → `{ id, name, releaseCount, plays, coverUrl? }[]` | **new, §10.** Sort A–Z client-side; "Most listened" sorts by `plays`. If `plays` absent, hide that toggle and default to A–Z. |
| Discography | `POST /api/artist/discography` `{artist_id}` → `{task_id}`, then poll `GET /api/tasks/<id>` until `status==='complete'`, read `task.result.releases[]` | Show the **skeleton grid** while polling. Each release: `{rgid, title, year, primary_type, status, present?, total?, group_id?, navidrome_album_ids?}`. |
| Album detail | `GET /api/album/tracklist?release_mbid=` (+ `GET /api/album/releases?rgid=` if you keep an edition picker) | `tracklist` works for **any** release, so complete/untagged open a read-only tracklist too. |
| Sources (missing) | `POST /api/search`, poll `GET /api/searches`, render `<SourceRow>` | Collapsed behind "Find sources"; "Get this album →" calls `POST /api/album/download {rgid}`. |

**Row-click routing (unchanged contract):**
- `status === 'gaps'/'incomplete'` → dispatch `SET_SEL_GAP` + `SET_GAP_FILTER` + `SET_TAB('Fill gaps')`. Deep-link into the existing Fill-gaps screen for that review group — **do not** build new UI here.
- `status === 'missing'` → open `AlbumDetail`.
- `status === 'complete'` / `'untagged'` → open `AlbumDetail` **read-only** (see §5). This is the
  resolved answer to the original handoff's open question — yes, they're clickable.

---

## 4. Design tokens & the four-status vocabulary (implement properly)

- **Tokens are generated CSS custom properties** (`README.md` → "Design tokens"). Reproduce the
  `tokens()` generator (color-mix helper + anchor table) so the appearance menu keeps re-theming
  the Artist screen live. **Never hardcode hex** in the new components except album-cover gradient
  fallbacks and the constant `#17130f` (dark-on-accent text). Every color below is a token.
- **Add four dedicated album-state tones to `STATUS` in `ui.jsx`.** The original code reused
  transfer-state tones (`failed`→Missing red, etc.) as a shortcut — replace that with intentional
  tones. Nothing else depends on the old reuse.

  | State | Meaning | Tone / mark (from the mock) |
  |---|---|---|
  | **Complete** | every canonical track owned | `--green`; solid green **✓** corner badge; full-color cover |
  | **Has gaps** | owned but incomplete | `--decide-fg` (gold); mono **N** badge (missing count); routes to Fill gaps |
  | **Missing** | not in library | `--faint`; cover **dimmed** (`opacity:.5; filter:saturate(.6)`); hollow **+** badge — *not* danger red |
  | **Untagged** | owned, unverifiable | `--accent-2`; **dashed** `--accent-bd-sel` cover border; **?** badge |

- **Untagged gets a fix affordance.** In `AlbumDetail`, untagged shows a hint banner
  (`--warn-tint` bg, `--accent-bd` border) explaining the files lack MusicBrainz IDs, with a
  **"Retag with beets →"** button wired to whatever retag/beets flow exists (or a no-op + TODO if
  not yet built). Don't let untagged sit visually identical to the other three.
- **Type scale / radii / spacing / shadows**: per `README.md`. Tiles use `aspect-ratio:1/1`
  covers, radius ~11px, cover shadow `0 12px 30px -12px rgba(0,0,0,.55)`; artist tiles are the
  same but `border-radius:50%`. Section eyebrows: uppercase, `.02–.08em`, 600.

---

## 5. AlbumDetail composition (the real "album page")

Anchor it on **cover + metadata**, then tracklist, then sources — three coherent zones, not the
three copy-pasted blocks the original had.

- **Hero**: 200px cover (dashed border when untagged) + uppercase artist eyebrow + 32px title +
  status chip + `year · type · N tracks`. The **primary action lives in the hero** and depends on
  status:
  - *missing* → primary **"Get this album →"** (optimistic → "✓ Requested — see Downloads"),
    plus a secondary **"Find sources"** that expands the source panel; a small "auto-picks from
    your ranking" note.
  - *complete* → a quiet green "in your library — read-only" strip (optionally "Open in
    Navidrome").
  - *untagged* → the retag hint banner (§4).
- **SourcePanel is progressive** — collapsed by default; only missing albums have it. When
  expanded while the slskd search is still running, show a small loading/trickle state (reuse the
  Downloads/search loading pattern), not a dead empty list. Rows are `<SourceRow>` (same component
  as Fill-gaps picker and Library search — build once).
- **TrackList** reads like a tracklist: mono position, title (truncates), right-aligned mono
  duration, zebra rows in a single rounded container — **not** a repurposed `.library-row`. Source
  the tracklist from `GET /api/album/tracklist` on open (it may arrive after first paint — show a
  2–3 row skeleton, then fill).

---

## 6. Loading / empty states (don't leave `<p className="muted">`)

- **Index**: while `GET /api/artists` loads, a light skeleton of circular tiles. If the library
  has zero artists, `EmptyState` with a "scan your library" pointer.
- **Discography scan**: the **skeleton grid** (cover squares + text bars, gentle pulse) while the
  task polls — the mock shows the exact treatment. Keep the "this can take a minute for prolific
  artists" line as secondary text.
- **Album detail**: tracklist skeleton until `tracklist` resolves; source panel loading state
  while `searches` trickle.
- **Picked artist with no releases**: `EmptyState` ("No discography indexed yet for X").

---

## 7. Responsive

Per `README.md` breakpoints (≤860 / ≤768 / ≤560). The cover/artist grids should use intrinsic
`grid-template-columns: repeat(auto-fill, minmax(168px, 1fr))` (artist tiles `minmax(148px,1fr)`)
so they reflow to fewer columns without media queries — this is the one place a **true grid stays
a grid** at narrow widths (do not collapse to Library's stacked-card pattern). The discography and
album-detail hero rows stack (`hero-row`) below 768px; filter/sort rows wrap; page padding
tightens below 560px.

---

## 8. Acceptance criteria

- Artist tab lands on the **index**; artists are **A–Z** by default; **Most listened** re-sorts by
  `plays`; the filter box narrows the list live.
- Picking an artist runs the discography scan (skeleton → grouped grid). Releases are grouped
  **Albums / EPs / Singles**; status filter chips reflect real counts.
- Status badges use the **four dedicated tones**; missing covers are dimmed; untagged covers are
  dashed. Missing is **not** rendered in transfer-failure red.
- `gaps` deep-links to **Fill gaps**; `missing` opens detail with a working **Get this album** +
  progressive sources; `complete`/`untagged` open **read-only** detail; untagged shows the **retag
  hint**.
- Changing theme / accent / background from the appearance menu re-themes the whole Artist screen
  (tokens, not hardcoded hex).
- No `<p className="muted">` placeholders remain for loading/empty; skeletons + `EmptyState` used.
- `GET /api/artists` + Navidrome `plays` wired per `BACKEND_RECOMMENDATIONS.md` §10, with A–Z
  fallback if `plays` is unavailable.

---

## 9. Suggested order of work

1. **Backend `GET /api/artists`** (+ play-count polling, §10) — unblocks the index. Stub returns
   are fine to start the frontend in parallel.
2. **`ui.jsx`**: extend `STATUS` with the four album tones; add `ReleaseTile`, `ArtistTile`,
   `TrackList`, `SortToggle`.
3. **`ArtistScreen` routing** (`/artist`, `/artist/:artistId`, `/artist/:artistId/:rgid`) and the
   three views.
4. **ArtistIndex** (list + sort + filter) → **DiscographyView** (scan skeleton + grouped grid +
   status chips + routing) → **AlbumDetail** (hero states + progressive sources + tracklist).
5. **Loading/empty states**, then **responsive** pass, then wire the optimistic actions to the
   real endpoints and reconcile on refetch/SSE.

---

## 10. Claude Code kickoff prompt

Paste the block in `§11` into Claude Code from the repo root. It assumes the mock and these three
docs are available to reference.

## 11. — prompt —

```
You are implementing the redesigned **Artist** tab in this repo (React frontend for lb-bot).

Sources of truth (read first, in this order):
- design_handoff_lb_bot_frontend/lb-bot.dc.html  → the Artist tab is the visual/interaction spec.
  Ignore its <sc-if>/<sc-for>/{{ }} template syntax and inline styling — that's a mock runtime,
  not production. Recreate the UI in this repo's real React + styling conventions; lift only the
  values (hex, spacing, radii, copy, states).
- design_handoff_lb_bot_frontend/ARTIST_IMPLEMENTATION_HANDOFF.md  → component tree, routing,
  state, data wiring, tokens, acceptance criteria (§1–§9). Follow it.
- design_handoff_lb_bot_frontend/BACKEND_RECOMMENDATIONS.md §10 and README.md "Design tokens".

Do this, in order, opening a PR per step:

1. Add `GET /api/artists` returning `{ id, name, releaseCount, plays, coverUrl? }[]`. Source
   `plays` from Navidrome (Subsonic getArtists/getArtist playCount; sum album playCount), cached
   on the summary refresh cadence. If play data is unavailable, omit `plays`.
2. In web/src/components/ui.jsx, replace the borrowed transfer-state STATUS reuse with four
   dedicated album-state tones — complete (green ✓), gaps (gold, count), missing (faint, dimmed
   cover, hollow +), untagged (accent-2, dashed border, ?). Add ArtistTile, ReleaseTile,
   TrackList, and a SortToggle (or reuse the header's segmented pill group).
3. Restructure web/src/panels/Artist.jsx into an index-first flow with routes /artist,
   /artist/:artistId, /artist/:artistId/:rgid:
   - ArtistIndex: grid of ArtistTile from GET /api/artists; A–Z default + "Most listened" (plays)
     sort toggle + local filter box. REMOVE the old free-text MusicBrainz ArtistSearch entry.
   - DiscographyView: on pick, POST /api/artist/discography then poll GET /api/tasks/<id>; show a
     skeleton grid while scanning; render releases grouped by type (Albums/EPs/Singles) as a
     cover-forward grid with corner status badges and status filter chips (with counts). NO
     grid/by-type toggle — grouped only.
   - Routing: gaps → existing Fill-gaps deep link (SET_SEL_GAP + SET_GAP_FILTER + SET_TAB), do NOT
     rebuild it. missing → AlbumDetail. complete/untagged → AlbumDetail read-only.
   - AlbumDetail: hero (cover + meta + status chip + status-dependent primary action), a
     progressive SourcePanel collapsed behind "Find sources" (missing only, reuse SourceRow), and
     a real TrackList from GET /api/album/tracklist. Untagged shows a "Retag with beets →" hint.
4. Replace every loading/empty <p className="muted"> with skeletons + EmptyState. Add the
   responsive behavior from the handoff (§7): intrinsic auto-fill grids that stay grids at narrow
   widths; hero rows stack ≤768px.
5. Keep all styling on the generated CSS-variable token system (README) so the appearance menu
   re-themes the screen; no hardcoded hex except cover-gradient fallbacks and #17130f.

Do NOT change the API contract beyond adding GET /api/artists, and do NOT change the Fill-gaps
deep-link behavior. Verify against the acceptance criteria in ARTIST_IMPLEMENTATION_HANDOFF.md §8.
```
