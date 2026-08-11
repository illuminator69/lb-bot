# lb-bot — Backend recommendations

Written against the redesigned frontend (`lb-bot.dc.html`). The goal is to make the
Flask/React backend serve the exact shapes the UI already renders, so the mock data can be
swapped for live data with minimal glue. Everything below is framed as "what the frontend
needs" → "what the backend should expose."

---

## 1. Shape the API around the four screens, not around the DB

The UI is organized as **Fill gaps / Downloads / Library / System**. Give each a dedicated
read endpoint that returns view-ready objects (already joined, already labelled) rather than
raw ORM rows. This keeps optimistic UI honest and avoids N+1 fetches on the client.

| Screen | Endpoint | Returns |
|---|---|---|
| Fill gaps | `GET /api/gaps?filter=needs\|working\|done&cursor=…` | queue items + the focused album's canonical/missing/source data |
| Downloads | `GET /api/transfers` | active + queued + failed transfers, and `needs_placement[]` |
| Library | `GET /api/library?filter=…&q=…&cursor=…` | paginated album rows with status + track counts |
| System | `GET /api/system/health`, `/prefs`, `/logs` | checks, ranking/guards, log stream |

### Canonical shapes to standardize on
The frontend already assumes these fields — lock them into the API contract:

- **Album**: `{ id, artist, album, present, total, status, coverUrl }`
  where `status ∈ {ready, picking, downloading, failed, complete}` and
  `missingCount = total - present`.
- **Source**: `{ id, format, bitrate, size, peer, speed, coverage, flags[], rank }`.
  `coverage` should be structured (`{ haveTracks, totalTracks, bitDepth? }`) with a
  server-computed display string, so the client never re-derives "partial · 1/2 tracks".
- **Transfer**: `{ id, title, sub, state, bytesDone, bytesTotal, rate, pct, etaSeconds }`.
- **Placement**: `{ id, path, matchAlbumId, matchLabel, detail, confidence }`.

---

## 2. Cover art — first-class, cached, with a real fallback

The UI layers `url(covers/<id>.png)` over a tonal gradient. In production this maps to
Navidrome's `getCoverArt`. Recommendations:

- Expose `GET /api/cover/{albumId}?size=96|160|512` that **proxies + caches** Navidrome art
  (server-side disk cache keyed by album+size). Don't make the browser hit Navidrome's REST
  auth directly.
- Return `204` (not `404`) when there's no art, so the frontend cleanly shows the gradient
  fallback without a console error.
- Include `coverUrl` inline in every album payload so the client never has to guess the ID.

---

## 3. Make optimistic actions safe with idempotency + echoes

Every button in the UI acts optimistically (choose source, get tracks, confirm placement,
reorder ranking, toggle guard). The backend should make those safe to replay:

- Accept an **`Idempotency-Key`** header on all mutating POSTs (get-tracks, retry,
  confirm-placement). Re-sending the same key returns the same result instead of
  double-enqueuing a download.
- Every mutation returns the **new authoritative object**, so the client can reconcile its
  optimistic guess: `POST /api/gaps/{id}/fetch` → returns the created transfer;
  `POST /api/placements/{id}/confirm` → returns the updated library album.
- On failure, return a **structured error** the "recoverable failure" card already expects:
  `{ code, reason, detail, nextSource?, logTail[] }`. The Mezzanine "watchdog cancelled →
  try next best" flow depends on `nextSource` being populated by the server.

---

## 4. Real-time: replace the "Live · 3s" poll with a push channel

The header shows a live indicator; several screens (transfers, logs, gap states) change
continuously. Polling every 3s is wasteful and makes progress bars stutter.

- Add **`GET /api/events` (SSE)** — a single server-sent-events stream emitting
  `transfer.progress`, `transfer.state`, `gap.state`, `placement.new`, `log.line`,
  `health.change`. SSE is enough (one-way), survives proxies, and is trivial in Flask.
- The frontend can then patch individual objects by `id` instead of refetching whole screens.
- Keep a `GET /api/state/version` (monotonic) so a reconnecting client knows whether to do a
  full refetch or resume.

---

## 5. Source selection: move ranking + guards server-side (already modeled in System)

The System → Source preferences screen already models **ordered ranks** (FLAC16, OPUS256,
FLAC24, OPUS-any), a **fallback policy** (best / ask / skip), and **guards** (coverage cap,
size cap, min speed, queue limit). Make the server the single source of truth:

- `GET/PUT /api/prefs` — persist `{ ranks[], fallback, guards{} }`.
- The server applies guards **before** ranking and returns sources already sorted, with
  `rank` and a `recommended` flag set — the client should never re-sort. This guarantees the
  "auto-pick" the Fill-gaps card shows matches what an automated run would choose.
- Expose `POST /api/gaps/{id}/auto` that runs the same ranking headlessly (for the
  "scan all for missing" bulk action), so manual and automatic paths can't diverge.

---

## 6. Placement matching should ship a confidence + a diff

"Needs placement" promises a one-tap confirm with a match "ready." Back that with:

- Server computes the match (MusicBrainz release-group + fuzzy folder parse) and returns
  `confidence` plus a **track-level diff** (`filesFound`, `willFill`, `unmatched[]`), so the
  UI can show exactly what one tap will do and offer "Not this — search" with pre-filled query.
- Confirm is transactional: move/hardlink files → trigger Navidrome rescan of just that
  folder → return updated album. Never leave a half-filed folder.

---

## 7. Search (Add music) — paginate and dedupe releases

The add-music results view targets a MusicBrainz album, lets the user pick a **release**, then
lists slskd sources. Backend needs:

- `GET /api/search?q=…` → `{ artist, album, releases[], sources[] }` where releases carry
  `{ id, label, trackCount, year, format }` and sources are already guard-filtered + ranked.
- Cache MusicBrainz lookups aggressively (respect their rate limit — the health check already
  surfaces it) and **deduplicate** slskd hits by content hash so the same rip from three peers
  collapses into one row with multiple peer options.
- `POST /api/search/request` is idempotent (§3) and returns the transfer that appears under
  Downloads, so "✓ Requested — see Downloads" is truthful.

---

## 8. Health checks should be actionable, not just red/green

The System → Health cards already pair a status with a **Fix** action (e.g. "Fix
permissions"). Make that real:

- Each check returns `{ id, ok, detail, remediation? }` where `remediation` is a callable
  endpoint (`POST /api/system/fix/{id}`) — so the Fix button does something, not just links to
  docs.
- Include `lastRun` timestamps and let the client trigger `POST /api/system/recheck`.

---

## 9. Logs: filterable, structured, streamable

Logs are filtered by tag (slskd / task / errors) in the UI. Emit **structured** log lines,
not formatted strings:

- `{ ts, tag, severity, msg, refId? }` where `refId` links a log line back to a transfer or
  album — so a future "jump to this download from its log line" is trivial.
- Serve history via `GET /api/logs?tag=&severity=&cursor=` and live tail via the SSE channel
  (§4). Cap the returned window; the UI only shows the most recent.

---

## 10. Small correctness items that make the UI trustworthy

- **Watchdog semantics**: the failed-transfer copy claims "nothing was left half-downloaded."
  Ensure the stall watchdog actually cleans partial files and the API reports
  `partialCleaned: true`.
- **Counts must agree**: header stat, queue filter chips ("Needs you · 46"), and library
  totals ("3,142 albums · 187 with gaps") should all derive from one server-computed summary
  (`GET /api/summary`) rather than three independent queries that drift.
- **Stable IDs**: albums/transfers/sources need stable IDs across refetches so optimistic
  patches and SSE deltas land on the right object.
- **Pagination everywhere**: library, source picker, and search all paginate in the UI — use
  cursor-based paging (not offset) so live inserts don't shift pages under the user.
- **Gap-queue navigation after a pick**: choosing a source (or completing/failing an album)
  should advance the frontend's cursor to the *next* album in the "needs you" queue, not reset
  it to the first. This needs the queue endpoint to return a stable, ordered list (or an
  explicit `nextId`) the client can walk — right now the mock just re-picks index 0.

---

### Suggested rollout order
1. Summary + canonical Album/Source/Transfer shapes (unblocks all screens).
2. Cover proxy (§2) + idempotent fetch/confirm (§3) — makes Fill gaps + Downloads real.
3. Server-side ranking/guards/prefs (§5) — aligns manual and automatic behavior.
4. SSE event stream (§4) — replaces polling, smooths progress.
5. Placement diff (§6), search dedupe (§7), health remediation (§8), structured logs (§9).
