# Audit fixes — perf, navigation, permissions, gap-fill completeness

Session date: **2026-07-26**. Commit `ba8b83e` on
`claude/unraid-containers-handoff-egnevy`.

Implements all seven phases of the audit plan. Four reported problems, plus the
hardening and cleanup that came with them. Line numbers are deliberately absent
— navigate by function name.

---

## 1 — Read-only placed files

**Cause.** The single placement site is `_place()` inside
`_deterministic_album_import`. `shutil.move` on a same-mount move is an
`os.rename`, which preserves the source's mode bits *whatever `copy_function`
says* — so slskd's `0644`/`0444` came along with the file. No `os.chmod` existed
anywhere in the module, and `LB_BOT_UMASK` was a no-op because the env default
was `""`.

**Fix.**

- `os.chmod(dest, 0o664)` after the move in `_place()`, best-effort.
- `os.chmod(dir, 0o775)` after the album dir `os.makedirs`.
- `LB_BOT_UMASK` defaults to `"002"` so the umask applies without compose env.
- `_atomic_json_write` chmods the temp file to `0o664` before `os.replace` —
  `mkstemp` creates `0600` regardless of umask, which was leaving state files
  unwritable by a later run under a different uid in the `users` group.

**Verify on the host:** `ls -l` shows `-rw-rw-r--` owned `99:100`, and Navidrome
picks the file up after a rescan.

---

## 2 — Two or three tracks still missing after a gap fill

Four independent causes. All four had to go.

### 2a. The stall watchdog was killing queued files (primary cause)

`_poll_downloads_once` applied `STALL_TIMEOUT` (120s) to *any* transfer with no
byte progress. Soulseek peers serve one or two files at a time, so the tail of a
twelve-track album legitimately sits at 0 bytes in the peer's remote queue for
minutes — and got cancelled at ~180s.

The watchdog now distinguishes **stalled** from **waiting**:

| Transfer state | Timeout | Rationale |
|---|---|---|
| Running (has been `InProgress`) | `STALL_TIMEOUT` 120s | genuinely stuck |
| `Queued, Remotely` / `Initializing` | `QUEUE_TIMEOUT` 900s | waiting its turn; dead-peer net only |

A transfer promoted out of the queue gets a fresh clock (`_was_waiting`), so it
isn't judged on time it spent waiting.

### 2b. No per-file failover after partial success

Once *any* file in an album completed, the `ag["completed"] > 0` branch just
counted failures and finalized short. `_switch_album_source` exists but is the
wrong tool — it re-enqueues the whole album, discarding what already downloaded.

Added **`_retry_file_from_alt_source`**: walks `ag["alt_sources"]`, matches the
track via the existing `_best_file_for_missing_track`, and re-enqueues that one
file into the same group so the group's total still adds up. Capped at
`MAX_FILE_RETRIES` (2) per file via a `_tried_users` list carried onto each new
transfer. Folder listings are expanded once per alt source and cached on the
source dict (`_expanded`).

### 2c. Phantom "queued" tracks

`_enqueue_group_source` marked **all** approved tracks `queued` when `ok > 0`,
even though `slskd_enqueue_folder` had silently dropped the ones
`_album_file_pairs_for_missing_tracks` couldn't pair with a file. The
consequence was severe: the group stayed `downloading` forever, so
`api_gap_fetch`/`api_gap_auto` short-circuited on `alreadyActive` on every
retry, and `queued` isn't in `_RETRYABLE_DECISIONS` — the gap became permanently
unfillable.

- Only tracks with an actual file pair are marked `queued`; unmatched ones are
  `failed` with reason `"no matching file in source"`.
- New `_live_transfer_track_indexes(group_id)` checks `pending_downloads` for a
  real in-flight transfer. `_review_group_next_action` only reports
  `downloading` when one exists, and `_approve_pending_missing_tracks`
  re-approves `queued`/`downloading` tracks that have nothing behind them.

### 2d. Dishonest placement marking

`_mark_group_tracks_placed` flipped every `downloaded`/`needs_match` track to
`placed` unconditionally. It now takes the import `result` and is driven by
`per_file`:

- `_deterministic_album_import` tags each `per_file` row with the tracklist slot
  it was about (`recording_mbid`, `position`, `title`) via `_slot_ident`.
- `_placement_outcomes` indexes those rows by recording MBID *and* normalized
  title; a placed row wins over a failed one for the same key (the bonus-track
  fallback pass can emit a second row for a slot that already landed).
- Ambiguous / unmatched / move-failed tracks become `failed` with the reason.

Called with `result` at all seven call sites. Without a result the old
behaviour is preserved.

---

## 3 — Slow tabs (server side)

`/api/summary` is polled every 2–5s on every tab. It was doing five full JSON
deep-copies of the multi-MB review state under the global `_review_lock`, plus
an uncached synchronous slskd call with a 10s timeout. `/api/action-center`,
`/api/diagnostics` and `/api/settings` each ran four sequential network probes
inline — up to ~40s when a service was down.

- **slskd transfers cache.** `slskd_get_all_downloads(force=False)` with a 3s
  TTL; the background poller passes `force=True`. Benefits `/api/summary`,
  `/api/transfers` and `/api/downloads` at once.
- **Diagnostics never run inline.** `_diagnostics_cached(force)` sits behind the
  existing 60s `_HEALTH_CACHE` and serves all three routes; the four probes now
  run concurrently in a `ThreadPoolExecutor`. `?fresh=1` bypasses the cache for
  the explicit re-check affordances (`_fresh_requested()`).
- **Review snapshot memo.** `_review_snapshot()` is memoized per Flask request
  in `g`; `_save_review_state()` drops the memo, so a request that mutates and
  re-reads still sees its own write. Outside a request context, behaviour is
  unchanged.
- **`api_gap_detail`** copies the one group under the lock and builds the heavy
  view outside it.
- **`/api/beets/folders`** uses `_download_folders_cached` (30s) instead of a
  fresh `os.walk` + mutagen read per request; rows are copied since the cache is
  shared. `?fresh=1` supported.

Phase 3e (`_library_view` paging) was **not** done — the plan gates it on
"only if Library is still slow", which needs a live measurement.

---

## 4 — Back/forward jumped tabs

**Cause.** There was no router. Tab switches wrote `localStorage` and created
zero history entries, while three hash routes (`#/artist`, `#/gaps`, `#/import`)
did — so Back could only ever land on a stale hash left over from another
screen. `Artist.jsx` had a second, independent hash parser, and `Layout.jsx`
cleared the hash on tab change.

**The hash is now the single source of truth.**

```
#/gaps[/<groupId>]                          #/fresh
#/downloads                                 #/system[/<subtab>]
#/library[/duplicates]                      #/playlist
#/artist[/<artistId>[/<rgid>]]              #/import/<path>[/<groupId>[/<mode>]]
```

- One `parseHash()` → one `hashchange` listener → one `SET_ROUTE` action in
  `App.jsx`. Drill-down state (`selGap`, `sysTab`, `pendingImportTarget`,
  Library's duplicates view, Artist's `artistId`/`rgid`) derives from the route.
- `navigate(tab, ...params)` for real navigation (pushes history);
  `replaceRoute(...)` for transient corrections — it dispatches a synthetic
  `HashChangeEvent` because `replaceState` fires none, keeping one code path.
- `Artist.jsx`'s `parseArtistHash`/`artistHash` and its own listener deleted; it
  reads `state.routeParams`.
- `Layout.jsx`'s hash-clearing `replaceState` deleted; the tab bar navigates by
  hash.
- `localStorage.lbTab` is only a fallback when the hash is empty on first load.

**In history:** tab switches, gap detail, artist/album drill-down, library
duplicates, system sub-tabs, import sub-views.
**Not in history:** search keystrokes, filter chips, scroll, cursor re-homing.

---

## 5 — Slow tabs (client side)

- **Poll errors surface.** `refresh()` dropped errors entirely when `silent`,
  so a tab that never loaded sat on "Loading…" forever with a dead backend.
  Adds `pollError` state, an offline banner with a Retry button, and a
  "Waiting for the backend…" gate message.
- **Debounced Library search** (300ms, local `draftSearch`) — it was firing one
  `/api/library` per keystroke.
- **Legacy bundle 6 → 2 requests.** Import/Playlist read only `review` and
  `tasks`; the bundle was also pulling `operations`, `downloads`, `searches` and
  `action-center` on every poll with nothing consuming them.
- **Skip refetch on a bare tab switch** when that tab's data is already in hand
  and <3s old (Fill gaps / Downloads / Library only — the tabs whose data lives
  in app state). Any parameter change still always refetches.
- **`LiveDot` isolated** into a `memo`'d component taking props, so its 1s
  staleness tick no longer re-renders the whole header nav.
- **Discography poll backs off** 2s → 15s (×1.4) — an MB-bound scan runs for
  minutes. Converted from `setInterval` to a self-scheduling `setTimeout`.
- **`IndexBuildControl`** likewise self-schedules from the value just fetched,
  so the poll can't tear down and restart on every status object.
- **`AlbumDetail.downloadBest`** resets its button outside the `try`, so nothing
  after the request can strand it latched. (`Fresh`'s `got()` already had a
  correct `finally`.)

---

## 6 — Gap-fill hardening + per-album MP3

- **Stale decisions don't survive a rescan.** `_inherited_decision` resets
  `placed`/`queued`/`downloading`/`verified`/`navidrome_*` to `pending` when a
  fresh scan still reports the track missing from Navidrome. User
  `skipped`/`dismissed` always survive — those are statements of intent, not
  claims about the library. Applied in `_merge_review_groups` and
  `refresh_group_missing`.
- **Re-approving sheds the old failure.** `_repair_job_track_from_group` clears
  `download_error`/`download_timeout` and recomputes status for
  `approved`/`source_pending`/`pending`;
  `_apply_repair_job_projection_to_group` is one-directional, so it stops
  writing the job's stale `failed` back over a freshly re-approved track (which
  is what made every re-approve silently undo itself).
- **Unicode-aware title matching.** `_match_key` NFKD-decomposes and strips
  combining marks before the `[a-z0-9]` filter, so `Björk` and `Bjork` produce
  the same key. Fully non-Latin titles (CJK, Cyrillic) previously stripped to
  `""` — they now fall back to a casefolded, punctuation-stripped form instead
  of matching everything (or nothing).
- **Placement is verified against Navidrome.** `_start_placement_verification`
  spawns one background pass per group after a successful placement; it polls
  `nd_track_present` for each `placed` track, promotes confirmed ones to
  `verified`, and resets anything still missing after
  `PLACEMENT_VERIFY_TIMEOUT` (600s) to `pending` with a reason. This makes the
  previously-dead `navidrome_pending`/`navidrome_verified` states real and
  catches every silent placement failure.
- **MP3 as a last resort, per album.** `FORMAT_PRIORITY` stays flac/opus
  globally. A review group can set `allow_mp3` (`POST /api/gaps/<id>/allow-mp3`,
  toggled from the Fill gaps action card), which widens the accepted formats for
  *that album's* search and enqueue only, with mp3 ranked strictly last.
  - Scoped with a **`ContextVar`**, not a thread-local: `asyncio.to_thread`
    copies the calling context, so a scope opened in a coroutine still covers
    the blocking slskd calls it offloads. Plain `threading.Thread` workers get
    no copy, which is why the album group also carries `allow_mp3` explicitly
    for the poller's failover path.
  - `_accepted_formats()` gates acquisition; `_placeable_formats()` always
    includes mp3, because refusing to file a track we deliberately downloaded
    would strand the audio in `/downloads`.
  - The UI shows the observed count of tracks the chosen source had no accepted
    file for (`noFileInSourceCount`), so the opt-in is an informed choice rather
    than a guess — derived from existing state, no extra search.

---

## 7 — Dead code removal

**Backend**

- `WEB_INDEX_HTML` (~890 lines) deleted; `/` now serves a short "run
  `npm run build`" page when `web/dist` is absent.
- `/api/repair-jobs`, `/api/repair-jobs/<job_id>`, `/api/beets/diagnose` — the
  SPA calls none of them.
- The `"events"` legacy key in `api_logs`; `_repair_jobs_snapshot`; the unused
  `subprocess` import; the dead `blocked_beets_error` / `beets_registered`
  repair statuses.

**Frontend**

- `OperationCards`; reducer cases `SET_DOWNLOADS`, `PATCH_GROUP_FIELD`,
  `SET_SEL_GAP`; `setRefreshInterval`; dead state keys `syncMsg`, `searches`,
  `actionCenter`, `operations`, `downloads`; the empty `web/src/hooks/` dir.

**Accessibility**

- Bare `<a>`s with no href (edit ranking, search again, reconcile) → real
  `<button>`s with a `.link-inline` class. They were neither focusable nor
  announced as interactive, so those actions were keyboard-unreachable.
- Album rail rows: `role="option"`, `tabIndex`, `aria-selected`, Enter/Space
  handling, inside a `role="listbox"`.
- `role="status" aria-live="polite"` on the toast stack; `aria-label` on
  dismiss.
- `aria-current="page"` on tab-bar and Advanced-dropdown buttons.

---

## Verification

| Check | Result |
|---|---|
| `python test_album_review.py` | 67 tests, **32 errors** — unchanged stale-beets baseline, no new failures |
| `cd web && npm run build` | passes |
| Routing helpers | round-trip tested: encoded paths, `mb:<mbid>` ids, empty/foreign hashes |
| New Python helpers | smoke-tested under the suite's stub harness |
| `ast.parse` / import | clean |

**Five tests were removed, not just code.** They asserted on `WEB_INDEX_HTML`
markup, which phase 7 deletes:
`test_album_review_sidebar_is_bounded_and_windowed`,
`test_source_rows_render_risk_chips_and_coverage_progress`,
`test_dashboard_has_file_match_ui_and_no_mbid_prompt`,
`test_dashboard_fetches_operations_and_shows_feedback`,
`test_dashboard_has_repair_queue_workspace`.

### Still needs the live stack

Nothing below can be unit-tested without slskd + Navidrome + the real `/music`:

1. **Phase 1** — fill one track; check mode/owner on the Unraid host, confirm in
   Navidrome.
2. **Phase 2** — fill a 10+ track album from a slow peer. Confirm queued-remote
   files survive past 180s in `/api/transfers`, that forcing a rejection
   produces an alt-source enqueue in the logs, and that the final state has no
   phantom `queued` and no falsely-`placed` track.
3. **Phase 3** — `time curl` the hot endpoints (`/api/summary` <100ms warm,
   `/api/settings` <200ms warm, was up to 40s); watch the slskd request rate
   drop from ~1/s to ~1/3s.
4. **Phase 4–5** — browser-test Back/Forward across every tab and drill-down;
   check poll volume in the Network tab; kill the backend and confirm the banner
   appears and recovers.
5. **Phase 6** — split/duplicate album folder resolution, Opus vs FLAC tag keys,
   and Navidrome path vs container `/music` path reconciliation.

---

## Deferred (out of scope, from the plan)

- `_library_view` paging (phase 3e) — gated on a live measurement.
- `/api/album/sources` in-request 60s search → background job.
- MusicBrainz disk cache.
- Multi-disc `_finalize_group` majority-dir fix and `mbz_release_tracks` disc
  flattening.
- react-router adoption.

---

## Commit

One commit, `ba8b83e`, 14 files, +11430 / −11708. The seven phases interleave
heavily in `listenbrainz_bot.py`, and splitting a finished tree by hunks would
produce intermediate states that couldn't be built or tested — so the history is
one well-described change rather than seven unverified ones.
