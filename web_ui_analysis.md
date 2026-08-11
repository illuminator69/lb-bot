# lb-bot Web UI Rewrite: Usability, Consistency & Friction Analysis

This report documents the inconsistencies, functional frictions, visual bugs, and architectural deviations discovered in the React/Vite SPA (`web/src`) compared to the HTML design mocks (`lb-bot.dc.html`) and project specifications.

---

## 1. Critical React State & Interaction Bugs

### ❌ Persistent state on Album selection change (`ActionCard`)
In `FillGaps.jsx`, the details and action card is rendered as:
```jsx
{focus ? <ActionCard detail={focus} transfers={transfers} /> : ...}
```
Because the component lacks a `key={focus.id}` prop, React reuses the same `ActionCard` instance when clicking between albums in the sidebar. This leads to several major bugs:
* **Source Picker Page Out-of-Bounds:** If the user is on page 3 of the source picker on massive-album A, and switches to album B (which has only 1 page of sources), `srcPage` remains at `2`, rendering a completely blank list of sources.
* **Sticky Picker State:** If the source picker was open on album A, it remains open on album B instead of resetting to the "ready" card.
* **Cluttered Error Cards:** If a download failed on album A (setting the local `lastError` state), that failure message will persist and show on album B, even if album B is healthy and ready.

> [!IMPORTANT]  
> **Fix:** Mount the card with a key: `<ActionCard key={focus.id} detail={focus} transfers={transfers} />`.

### ❌ Stale Source Details & Stuck Progress Bars during Download
When a download is started from a different source than the auto-picked suggestion (or when the server fails over to another peer), two severe bugs occur:
1. **Stale Metadata Display:** The UI displays metadata (format, bitrate, size) from the client-side optimistic `chosenSrc` state, rather than matching the active download's actual peer. If the server fails over from a 31MB OPUS source to a 78MB FLAC source, the UI will continue to claim it is downloading the OPUS file.
   * **Fix:** Resolve the active source dynamically by matching the active transfer's username against the source list:
     `const activeSrc = sources.find(s => s.peer === mine[0]?.username) || chosenSrc`.
2. **Stuck Progress Bar (0%):** On starting a download, the progress bar remains stuck at `0%` and doesn't move. This happens because [App.jsx](file:///c:/Users/icher/Lb-bot-missing/web/src/App.jsx#L175) only fetches `/api/transfers` if the *previous* state's summary indicated active downloads. Since `summary` is on a slow 5s loop, the transfers query is blocked on first load.
   * **Fix:** Unblock the transfers API fetch if any album in the gaps list has a status of `'downloading'`:
     `if (!ui.summary || ... || ui.gaps?.items?.some(g => g.status === 'downloading'))`.

---

## 2. Visual & Accessibility (Color Contrast) Bugs

### ❌ Unreadable Green Buttons under Light Theme
The theme generator in [tokens.js](file:///c:/Users/icher/Lb-bot-missing/web/src/lib/tokens.js#L62) hardcodes `--green-on` to `#0f2019` (a very dark green) for both Light and Dark themes:
```javascript
'--green': green, '--green-on': '#0f2019', ...
```
In `ActionCard` (Fill Gaps failure recovery), the "Try next best source" button style is:
```jsx
style={{ background: 'var(--green)', color: 'var(--green-on)' }}
```
* **Dark Theme:** Background is `#6fae8f` (light green), text is `#0f2019` (dark) $\rightarrow$ **High Contrast (6:1)** $\rightarrow$ **Pass**.
* **Light Theme:** Background is `#3f8f66` (dark green), text is `#0f2019` (dark) $\rightarrow$ **Very Low Contrast (~2.5:1)** $\rightarrow$ **Fail (Unreadable)**.

> [!TIP]  
> **Fix:** Make `--green-on` dynamic in [tokens.js](file:///c:/Users/icher/Lb-bot-missing/web/src/lib/tokens.js):
> `theme === 'dark' ? '#0f2019' : '#ffffff'`.

### ❌ Monospace Bitrate Text Overflow in `SourceRow`
In `SourceRow` ([ui.jsx](file:///c:/Users/icher/Lb-bot-missing/web/src/components/ui.jsx#L161)), the bitrate and size details are rendered as a single monospace string:
```jsx
<span className="font-mono text-[14px] font-semibold">
  {[src.bitrate, src.size].filter(Boolean).join(' · ') || '—'}
</span>
```
Monospace characters are wide and do not wrap easily. When the viewport is narrow (such as inside the Fill Gaps picker rail or on mobile), the long text string (e.g. `16-bit 44.1 kHz · 612.9 MB`) overflows the bounding borders of the source box, clipping text or pushing the "Use this →" button out of alignment.
* **Fix:** Use a smaller font size (`text-[12px]` or `text-xs`) and split the bitrate and size into separate inline-block spans so they can wrap cleanly on small screens.

### 🟡 Semantic Color Mismatches (Queued State)
In the Downloads/Transfers screen statistics:
* The "Queued" count is colored with `var(--accent-2)` (Amber/Orange derivative).
* However, in the status dictionary `STATUS.queued` and the status chips, the queued state dot color is `var(--decide-fg)` (Yellow/Gold).
* This creates a small visual discrepancy where the same state ("Queued") is represented by two different colors on the same screen. Using `var(--decide-fg)` consistently would improve semantic harmony.

---

## 3. Functional & Architectural Inconsistencies

### ❌ Obsolete Beets Integration (Work Item A Deviation)
A primary decision in the project roadmap ([CLAUDE.md](file:///c:/Users/icher/Lb-bot-missing/CLAUDE.md#L45)) is the complete removal of **beets** from the placement path, replaced by mutagen-based tagging and direct folder moves. 
However, the UI still heavily references Beets:
* The Advanced tab contains an **Import** panel which directly hits `/api/beets/folders` and `/api/beets/import`.
* When an album is in the `untagged` state, the Artist view displays a button: `Retag with beets →`.
* Since the backend is dropping beets support and will not perform existing library retagging, this UI is obsolete. It should be renamed to general "Placement" and point to the new beets-free matching endpoints.

### ❌ Polling Performance issues in Search
In `Library.jsx`, [SearchResults](file:///c:/Users/icher/Lb-bot-missing/web/src/panels/Library.jsx#L64-L82) sets up a 3-second interval polling the broad `/api/searches` endpoint:
```javascript
const all = await api('/api/searches')
const entries = Object.values(all || {}).filter(s => s.query === query)
```
This fetches the history of all searches from the backend on every poll tick, causing high network overhead when search histories grow.
* **Fix:** The API should expose `/api/searches/{id}` to poll a single search, or the frontend should consume the SSE event stream as planned in [BACKEND_RECOMMENDATIONS.md](file:///c:/Users/icher/Lb-bot-missing/BACKEND_RECOMMENDATIONS.md).

---

## 4. General UX & Flow Frictions

### 🟡 Redundant "Add Music" Actions
In [Library.jsx](file:///c:/Users/icher/Lb-bot-missing/web/src/panels/Library.jsx#L428-L431), the "Find album" and "Search slskd" buttons perform the exact same action:
```jsx
<button className="primary" disabled={!addQuery.trim()}
  onClick={() => setActiveSearch(addQuery.trim())}>Find album</button>
<button disabled={!addQuery.trim()}
  onClick={() => setActiveSearch(addQuery.trim())}>Search slskd</button>
```
Having two distinct buttons that execute the identical query results flow creates interface clutter and confusion. They should be unified into a single "Search" button or properly split into their respective backend search modes.

### 🟡 Fragmented Album Progress (Active vs. Finished)
In `Transfers.jsx`, finished tracks are separated into a "Finished" section at the bottom, while active/queued tracks are grouped by album at the top.
* If a user downloads an album with 10 tracks, and 5 finish:
  - 5 active tracks are grouped under the Album Group card at the top.
  - 5 finished tracks are listed under "Finished" at the bottom.
* This splits the progress of a single album across two distinct areas, preventing a unified view of the album's status. Keeping finished tracks in the Album Group until the *entire* group is finished would offer a much better UX.

### 🟡 Laggy Tab Switching (Active Request Block)
In `App.jsx`, when switching screens rapidly:
```javascript
if (refreshingRef.current) return;
```
If a request is already in progress, any rapid tab clicks are silently ignored, forcing the user to wait up to 5 seconds for the automatic background poll to fetch the new screen's data. Adding an `AbortController` to cancel in-flight requests on navigation would make tab switching feel instantaneous.

---

## 5. Comprehension, Usability & Workflow Analysis

### ❌ Fresh Tab: Flat Date Organization and Lack of Grouping
In [Fresh.jsx](file:///c:/Users/icher/Lb-bot-missing/web/src/panels/Fresh.jsx), releases are rendered as a flat grid without temporal division. In a list spanning 90 days, it is highly difficult to distinguish releases from this week, last week, or earlier weeks.
* **Improvement:** Group the items dynamically in a `useMemo` call by age (e.g., "This Week", "Last Week", "Two Weeks Ago", "Older") and render them under clear weekly divider headers (`SectionRule`).

### ❌ Fresh Tab: Double-Download Friction for Owned Albums
When a release is already present in the user's music library, the card renders an `in library` chip. However:
* The primary CTA button still reads **`Get this album →`** and remains fully enabled.
* Users can easily misclick and trigger a duplicate download process for music they already own.
* **Improvement:** If `r.owned` is true, the button should be disabled, hidden, or change to a non-mutating action like "View in Library".

### ❌ Fill Gaps Tab: Missing Auto-Scroll on Left Rail Selection
When clicking "Fill gaps" on an album in the Library table, the app correctly updates the active gap ID (`selGap`) and switches to the "Fill gaps" tab. However:
* The left-hand queue rail does **not** auto-scroll to show the active album. If the album is deep down in the list, it remains hidden out of viewport bounds.
* **Fix:** Use a React `ref` coupled with a `useEffect` watching the active album ID, calling `scrollIntoView({ behavior: 'smooth', block: 'nearest' })` on the matching queue element.

### ❌ Fill Gaps Tab: No Headless Auto-Select Button
In `ActionCard`, when an album is missing sources, the user is presented with a "Find sources on slskd" button which triggers a manual search.
* **Friction:** There is no quick "Auto-assign best source" button to headlessly fetch and automatically apply the backend's preference-ranking algorithm (e.g. hitting the `/api/gaps/{id}/auto` route).

### 🟡 System Tab: Reverse Log Direction
In `System.jsx`, the logs panel displays entries in reverse chronological order:
```javascript
{logEntries.slice().reverse().map((e, i) => ...)}
```
* While placing the newest logs at the top helps with instant debugging, standard command-line and server log viewers scroll downwards (newest at the bottom). A toggle to select log scroll direction (ascending/descending) would improve developer familiarity.

### 🟡 System Tab: Static Health Checks and Arrows Reordering
* **Checks Remediation:** Failing health checks only display informational guides (`howToFix`). Integrating actionable buttons that link to a backend remediation route (e.g. `POST /api/system/fix/{id}`) would make the dashboard actionable.
* **Source Priorities:** Reordering preferred audio formats (FLAC vs. OPUS) uses simple up/down arrow buttons. While functional, it feels outdated. Upgrading this to a modern Drag-and-Drop list would make the preferences screen feel considerably more premium.
