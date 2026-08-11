# lb-bot Web UI — Redesign Doc

## 1. What the bot actually does (ground truth from the backend)

There is exactly one real workflow, plus three ways to feed it and an admin layer:

1. **Find gaps** — scan the library for incomplete albums (`/api/scan`, `/api/scan-all`, `group.missing_tracks`).
2. **Decide what to get** — per-group canonical release pick, per-track approve/skip decisions, source search (`/api/groups/<id>/sources`, `/api/groups/<id>/decisions`).
3. **Acquire** — download via slskd, either automatically from a chosen source or via manual search/album download.
4. **Place** — once files land on disk, match them to the release tracklist and move them into the library (`/api/beets/import` → `_deterministic_album_import`), then verify against Navidrome.
5. **Admin/observability** — diagnostics, settings, logs, raw task/operation feed.

Everything in the product is one of these five things. The UI, however, has **12 top-level tabs**, several of which are different views onto the *same* underlying data, and the acquisition step is split across four disconnected tabs with no shared mental model.

## 2. Problems found, mapped to evidence

### 2.1 Flat over-tabbed navigation, no hierarchy
`TABS` in `App.jsx` is a single flat list: Import, Downloads, System, Home, Album Review, Playlist Scan, Album Download, Search, Spotify, Diagnostics, Settings, Logs. A first-time user has no way to tell that Album Review is the *primary* workflow and Logs is an escape hatch — they're rendered as equally-weighted buttons in one wrapping row.

### 2.2 Home and System are the same screen twice
`Home.jsx` renders Action Center cards + "Recent tasks" (`TaskCards limit={3}`) + "Recent operations" (`OperationCards limit={4}`).
`System.jsx` renders nav buttons to Diagnostics/Settings/Logs/Import + "Recent operations" (`limit={8}`) + "Recent tasks" (`limit={8}`).
These are the same two components at different slice sizes. A user has no reason to visit both, and it's not obvious which one is "home."

### 2.3 Four disconnected acquisition entry points
`Album Download` (MusicBrainz release group lookup → download), `Search` (raw slskd query → per-folder download), `Playlist Scan` (ListenBrainz), `Spotify` (playlist scan) are four separate tabs, each a bare input + button, none cross-linked, none showing how their output relates to Album Review's gap list. A new user has no way to know "I want album X" should start at Album Download, not Search.

### 2.4 Placement is (almost) unified but still shows two seams
The Repair Queue removal already consolidated placement into the Import tab. Two seams remain:
- Import tab's folder list comes from `/api/beets/folders` (disk scan + heuristic suggestion). Album Review's "Needs match" section comes from `group.match_items` (server-tracked, group-scoped). These are two different views of "a folder needs to be placed," and a folder can show up as a suggestion in Import *and* as a match_item in Album Review simultaneously, with no visual link other than the one-way "Match in Import →" button.
- `System.jsx` has a nav button labeled **"Imports"** that goes to the `Import` tab — but the Import tab is about *placing new downloads*, not viewing import history. Naming collision waiting to confuse someone.

### 2.5 Native `window.confirm()` / `window.alert()`-style blocking dialogs
`confirmAction()` (`App.jsx`) wraps `window.confirm`, used for: cancel transfer (Downloads), hide/retag/merge/logical-merge (Album Review), import-as-is (Downloads). Additionally `GroupDetail.batch()` and the inline "Retag & next" button call `window.confirm` directly, bypassing `confirmAction` entirely — two different confirm code paths for the same kind of action. These are OS-styled, block the whole tab, carry no visual connection to the button that triggered them, and can't be styled or queued. This is the single biggest thing standing between the current UI and "frictionless."

### 2.6 Dense, flat, unlabeled toolbars
`GroupDetail`'s top toolbar has **9 buttons in one row** (Previous, Next, Hide, Retag merge, Retag & next, Rescan group missing, Reconcile downloaded files, Find sources) with no grouping — navigation, destructive, and workflow actions sit side by side with identical styling except for one `.primary` class. "Rescan group missing" appears three times across the panel (top toolbar, "Missing tracks" section, "Advanced" section) for the same action, and "Logical merge" sits in an unlabeled "Advanced" `<details>` with a confirm dialog and no explanation of what it does.

### 2.7 Two independent polling loops
`App.jsx` polls everything (`/api/review`, `/api/tasks`, `/api/operations`, `/api/downloads`, `/api/searches`, `/api/action-center`) every 5s via `scheduleRefresh`. `Downloads.jsx` *also* runs its own 5s `setInterval` hitting `/api/downloads` independently. Two unsynchronized timers fetching overlapping data — not wrong, but not "reactive" either: everything is poll-and-replace, so a track that starts downloading can take up to 5s to visibly move, and a user watching a transfer sees no indication anything is live (no spinner, no "last updated Xs ago", no optimistic UI beyond the few `dispatch({type:'PATCH_...'})` calls in Album Review).

### 2.8 Diagnostics vs Settings largely overlap
Both are read-only card grids of `{title, ok, values/detail}` fetched once on mount. Diagnostics adds two rescan buttons; Settings adds a Docker/paths cheat sheet. Functionally these are "system health" and "system config," which are related enough that most users hit both back-to-back when something's wrong — currently two tab switches away from each other.

### 2.9 Dead reference from the Repair Queue removal
`Layout.jsx` still has:
```js
if (t !== 'Repair Queue') { history.replaceState(...) }
```
`'Repair Queue'` no longer exists in `TABS` — this condition is always true now, a harmless but literal leftover of the tab that was supposedly fully removed.

## 3. Proposed information architecture

Collapse 12 flat tabs into **4 sections**, each with a clear job:

```
┌─ lb-bot ───────────────────────────────────────────────┐
│  [Overview]  [Library]  [Get Music]  [System ▾]         │
└──────────────────────────────────────────────────────────┘
```

- **Overview** (was Home) — the only "dashboard." Action Center cards, live task/operation feed. `System.jsx` is deleted outright — its nav-button role is replaced by the System dropdown, its data role was a duplicate of Overview.
- **Library** (was Album Review) — the primary workflow, unchanged in scope, restructured internally (§4).
- **Get Music** — one section, four modes, replacing the four disconnected tabs:
  - **By release** (was Album Download): MusicBrainz lookup → download whole release.
  - **By search** (was Search): raw slskd query.
  - **From playlist**: merges Playlist Scan (ListenBrainz) and Spotify Scan as two source options in one form (`source: listenbrainz | spotify`, one input, one button) — same downstream effect (produces gap candidates), no reason for two tabs.
  - **Needs placement** (was Import): unchanged functionally, promoted to live inside Get Music since it's the tail end of acquisition, and because that's where a user naturally looks after downloading something. Cross-linked bidirectionally with Library's "needs match" items (§4.3) instead of the current one-way deep-link.
- **System ▾** (dropdown, not a tab row) — Diagnostics + Settings merged into one page with two tab-strip sections inside it (health checks on the left/top, config values + Docker examples below — one scroll, not one nav click), plus Logs as a separate dropdown item. This is deliberately de-emphasized (a dropdown menu, not equal-weight buttons) since it's not workflow — it's rarely visited.

This drops the top nav from 12 items to 4, with System's 3 destinations one click deeper instead of cluttering the primary row.

## 4. Library (Album Review) internal redesign

Keep the five-step mental model (canonical → missing → source → download → verify) — it's a correct model of the backend state machine (`next_action.bucket`) and works well as a stepper. Fix the execution:

### 4.1 Toolbar → grouped, iconed, reduced
Replace the 9-button flat toolbar with three visually separated clusters:
- **Navigation** (left): Previous / Next / position counter — unchanged, low visual weight (ghost buttons).
- **Primary workflow action** (center, one button, changes label/action based on `next_action.bucket` — i.e. exactly what the stepper says is next): "Find sources" when `source_pending`, "Retag & verify" when `downloading` is done, etc. This replaces having Find sources / Retag merge / Retag & next / Reconcile downloaded files all visible at once regardless of state — only the action relevant to the current step is primary; the rest move into a "…" overflow menu.
- **Secondary/meta** (right, overflow `⋯` menu): Hide/Unhide, Reconcile downloaded files, Rescan group missing, Logical merge. These are correct actions but not what most users do most of the time.

Delete the duplicate "Rescan group missing" from the top toolbar and the "Missing tracks" section — keep exactly one copy of each idempotent action, in the overflow menu, since it's not something a user does per-track.

### 4.2 In-app confirmation instead of `window.confirm`
Introduce one shared `<ConfirmBar>` / inline confirmation pattern (styled, dismissible, appears attached to the button that triggered it — e.g. button becomes "Cancel transfer? [Yes] [No]" inline, or a small popover) and route every destructive action (`confirmAction`, the two raw `window.confirm` call sites in `GroupDetail.batch()` and the retag-and-next handler) through it. This is the highest-leverage single change for "frictionless" — it removes every native dialog interruption in the app (Downloads cancel, Album Review hide/retag/merge/batch-decision, Diagnostics rescans if any are added later).

### 4.3 Unify "needs match" with Import's folder list
`group.match_items` and `/api/beets/folders`' folder list both describe "a downloaded folder needs placement." Make Import's folder list the single source of truth: have the backend attach `review_group_id` (already partially done via `suggested_match.group_id`) to every folder, and have Library's "Needs match" section render live folder cards *inline* (reusing Import's `FolderCard`) instead of a separate list that only deep-links out. A user should be able to confirm a match without leaving Library, and see the same card whether they arrived via Library or via Get Music → Needs placement.

## 5. Reactive/live-update pass

- **Single polling authority.** Delete `Downloads.jsx`'s独立 `setInterval`; instead let it opt into a faster tick of the *same* `scheduleRefresh` chain (e.g. `scheduleRefresh(2000)` while the Downloads tab is active, restored to 5000 on unmount) so there's one clock, not two racing fetch cycles.
- **Visible liveness.** Add a small "updated Xs ago" indicator next to the existing `syncMsg` text (already computed, just needs a ticking display) so polling feels alive instead of static.
- **Optimistic transitions** for the two actions that already fake it locally (`PATCH_MISSING_TRACK`, `PATCH_GROUP_FIELD`) — extend the same optimistic-dispatch-then-POST pattern to hide/unhide and match-mode toggle consistently (hide already does this; match-mode already does this — audit for the rest, e.g. cancel-transfer currently waits for the round trip with no immediate UI feedback).
- **Toasts instead of buried text.** Replace the single muted `syncMsg` header string (which errors and successes both silently overwrite) with a small toast stack, so an error doesn't get erased by the next successful poll 5 seconds later before anyone reads it.

## 6. Non-goals for this pass

- No visual redesign of the CSS system (`--panel`, `--accent`, `.card`, `.pill`) — the doc above assumes the same visual language, just reorganized and de-duplicated.
- No new backend endpoints beyond attaching `review_group_id` to folder objects (§4.3) — everything else is a frontend information-architecture and interaction change against the existing API surface from §1.
- No websocket/SSE push layer — "more reactive" here means fixing the polling story (one clock, visible liveness, optimistic UI), not replacing polling with push. Worth revisiting later, out of scope now.

## 7. Suggested implementation order

1. Delete `System.jsx`, fold its two "recent" grids into Overview (`Home.jsx`), fix the dead `'Repair Queue'` check in `Layout.jsx` while touching nav.
2. Collapse nav into 4 sections + System dropdown (`App.jsx` `TABS` restructure, `Layout.jsx` nav rendering).
3. Merge `Diagnostics.jsx` + `Settings.jsx` into one System page.
4. Merge `PlaylistScan.jsx` + `SpotifyScan.jsx` into one "From playlist" form under Get Music.
5. Build `<ConfirmBar>` and migrate every `window.confirm` call site (Downloads, Album Review ×3) — highest user-facing impact, do this early.
6. Album Review toolbar regroup (nav / primary / overflow) — depends on step 5 for the destructive items moving to overflow.
7. Unify Import folder cards with Library's match_items (backend `review_group_id` field + shared `FolderCard` component).
8. Polling consolidation + toast stack + liveness indicator.

Steps 1–4 are pure deletions/merges and safe to do first. Step 5 is the biggest UX win per unit of effort. Steps 6–8 are the deeper interaction work.
