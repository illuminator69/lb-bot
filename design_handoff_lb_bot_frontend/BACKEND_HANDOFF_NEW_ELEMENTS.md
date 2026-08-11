# Backend handoff — new elements (this round)

Companion to `BACKEND_RECOMMENDATIONS.md`. That doc covers the four screens generally;
this one is scoped to the UI elements added/changed in this round of review-comment fixes,
in `lb-bot.dc.html`. Right now every one of these is **client-only mock state** (a plain
`this.state` field with no server round-trip) — this is the list of what needs a real
endpoint behind it before it's more than a prototype.

---

## 1. Header logo → "Fill gaps" shortcut
Pure navigation, no backend involved. No action needed.

## 2. "Live · 3s" → manual refresh button
`refreshNow()` currently just flips a `refreshingLive` boolean for 700ms — cosmetic only,
nothing is actually re-fetched. Once real data is wired (see §4 of `BACKEND_RECOMMENDATIONS.md`
— SSE stream), this button's honest job is: **force a resync** if the SSE connection has
gone stale. Wire it to whatever your reconnect/refetch path is (e.g. re-fire the
`GET /api/state/version` check and full refetch if the version disagrees), not a fake timer.

## 3. Floating "jump to current album" button (Fill-gaps queue rail)
Frontend-only (scroll position math against a ref). No backend dependency — **but** it's
only trustworthy if the queue list order is stable across refetches (see the "queue
navigation" bullet already in `BACKEND_RECOMMENDATIONS.md` §10). If the server reorders the
"needs you" queue between polls, the button will jump the rail to the wrong row.

## 4. Gap-album tiles now open the album detail page (not the Fill-gaps tab)
This was the main functional fix: clicking a "2 missing" tile in an artist's discography now
opens the same detail view used for albums you don't own, instead of hijacking the global
Fill-gaps tab/cursor. Backend implication:

- The **release detail endpoint** (`GET /api/releases/{rgid}` or equivalent) must return
  **track-level presence**, not just `{present, total}` counts. The mock currently *guesses*
  which tracks are missing by marking the last N as absent (`detTrk` in the logic class) —
  that's a placeholder and will look wrong for real albums where gaps aren't trailing tracks.
  Return each track's own `{ position, title, duration, present: bool }`.
- The detail view's action row ("Find sources on Soulseek" / "Get missing tracks") must work
  for **both** `missing` and `gaps` status releases (`detActionable` in the logic class covers
  both) — make sure the fetch/source-search endpoint accepts a partial-album request (some
  tracks already present) and only requests the missing ones, not the whole release.

## 5. Compilations now show as their own discography group
Frontend just added `Compilation` to the type grouping — it was previously invisible because
no mock data had that type. For real data: MusicBrainz release-groups expose `primary-type`
(Album/EP/Single) and `secondary-types` (Compilation, Live, Remix, Soundtrack, …). The
discography endpoint should pass the **effective type label** through as-is (don't collapse
secondary types into "Album") so the frontend's grouping stays correct without a lookup table.

## 6. Release / Edition switcher (on album detail)
New popover next to the album title: pick a **release variant** (Original / Remaster /
Deluxe) and a **format/edition** (CD / Digital / Vinyl). Right now this is entirely cosmetic —
it's mock chip data, doesn't change the cover art or the tracklist, just appends a note to the
meta line. To make this real:

- The detail endpoint needs to return **all sibling releases** for a release-group (MusicBrainz
  models this directly — one release-group has many releases, each with its own date/label/
  packaging), not just the single release the UI currently treats as canonical.
- Each release needs its own **cover art asset** (this is the actual point of the feature —
  the comment that drove it was "the vinyl-tagged copy shows a disc photo instead of the real
  album art"). Cover lookup should be keyed by `releaseId`, not just `releaseGroupId`, and the
  "Digital" edition should be the safest default (most likely to have correct front-cover art).
  Same `GET /api/cover/{id}` proxy from `BACKEND_RECOMMENDATIONS.md` §2 applies, keyed at the
  release level.
- Switching edition should also swap which **source guards/coverage** apply — a vinyl rip has
  different expected bitrate/format profile than a digital FLAC. Not urgent, but don't hardcode
  guard assumptions to one format.

## 7. Soulseek button now visually primary (swapped with "Get album")
Styling-only change, no backend impact — both buttons still call the same two actions
(`getAlbum` = auto-pick-and-fetch, `toggleSrc` = open manual source list).

## 8. "Show files" toggle on the chosen source (Fill-gaps hero panel)
Currently synthesizes fake filenames from the track list (`chosenFiles` in the logic class) —
there's no real per-file data. This needs the **file manifest** from whatever your search
backend already has when it lists a slskd (or other) source — file name, size, and (ideally)
per-file match confidence against the canonical tracklist — surfaced without triggering a
download. If your existing source-search response doesn't carry a file list today, add one;
this button is useless once it just re-displays the track title with an assumed extension.

## 9. Per-album "Rescan album" button (Fill-gaps hero panel)
Sits next to "Skip this album." Currently a 1.2s fake spinner (`rescanningAlbum`), no request
sent. Needs a **scoped rescan** endpoint distinct from the existing "rescan all"/"scan-all for
missing" bulk action already in `BACKEND_RECOMMENDATIONS.md` §9's log lines —
`POST /api/gaps/{id}/rescan` that re-walks just that album's folder for newly-added files the
watcher missed, then returns the updated `{present, total, status}` so the UI can reconcile
without a full library rescan.

## 10. "Similar albums" shelf (album detail page)
New shelf below the album header, above the tracklist — parallel to the existing "Fans also
like" artist shelf, but at the album level: one representative album per similar artist, with
the same attribution rule (justified by the artist you're viewing, never an unattributed
popularity claim). Mock picks each similar artist's first "complete" album from your own
library — a real implementation should follow the same source stack as artist similarity
(ListenBrainz similar-artists + Last.fm artist.getSimilar, cross-checked) and can reuse that
same cached lookup; no new external dependency, just a slightly different roll-up (pick one
album per similar artist rather than listing the artists themselves). Clicking an entry must
navigate to *that* artist's discography + that release's detail — same `selRel`/`artistPicked`
pair the rest of the app already uses, so no new route shape is needed.

---

### Net-new client state to replace with real data
`relRelease`, `relFormat`, `editionOpen` (edition switcher), `filesOpen`/`chosenFiles` (file
list), `pickedOutOfView` (jump-button visibility — stays client-only, no backend needed),
`rescanningAlbum`, `refreshingLive`. All are plain `useState`-style fields with no persistence
today — expect to swap most of them for query state (react-query/SWR cache, or your
equivalent) once the endpoints above exist, rather than local component state.
