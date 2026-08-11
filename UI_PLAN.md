# UI_PLAN.md — lb-bot web UI rewrite

Scoping doc for replacing the current web UI with a modern, state-driven SPA.
**No code has been changed for this plan.** This is the agreed direction to pick
up after work items A and B (see `CLAUDE.md`) land.

## Why we're doing this

The current UI is `WEB_INDEX_HTML` — **~890 lines of vanilla HTML/CSS/JS embedded
in a single Python string** (`listenbrainz_bot.py`, ~line 6610–7500). It has no
framework, no build step, and **no state management**. The DOM is mutated
imperatively (`getElementById` + `innerHTML` + `fetch`).

That architecture is the root cause of "it crumbles once too many buttons are
pressed":

1. **No single source of truth.** Background work (scans, downloads, repair jobs,
   Navidrome verification) polls and writes straight into the DOM, racing against
   whatever the user is clicking.
2. **`innerHTML` rebuilds** destroy event listeners and in-progress input on every
   refresh; a poll landing mid-click silently drops the action.
3. **No component isolation** — one render error breaks the whole page.
4. **No types, no linting, no tests** — 890 lines of markup+style+logic in a
   string.

For a long-running tool with many concurrent operations, imperative DOM code is
the worst case. A state-driven framework fixes the class of bug, not just the
symptoms.

## Decision: keep the Python backend, rebuild only the frontend

The backend is the valuable part and it is **not** the problem. It already exposes
a clean JSON API — **59 `/api/*` routes** — that the new UI consumes unchanged.

- **Keep** `listenbrainz_bot.py`: the repair pipeline, the 59 API routes, the
  Telegram bot. Untouched by this work.
- **Replace** only `WEB_INDEX_HTML` with a real SPA built to static assets and
  served by the existing Flask app at `/`.

### We are NOT adopting Explo

Explo (the reference the team liked) is a **different application** — a
Discover-Weekly clone modelling playlists and scheduling. Its screens do not map
to lb-bot's missing-track review / per-track source selection / repair-job /
placement workflow, and its Go backend would mean porting the entire repair
pipeline for no benefit. We adopt Explo's **stack pattern**, not its app:

> React + Vite + Tailwind SPA, built to static, embedded/served by the existing
> backend, talking to the existing JSON API.

Explo is MIT-licensed, so its UI primitives (cards, toggles, modals, wizard) may
be lifted as a visual head-start **with attribution** (add to a `NOTICE`/credits).

## Stack

Mirrors Explo's frontend, minus anything Go-specific:

| Concern        | Choice                                  |
| -------------- | --------------------------------------- |
| Framework      | React 19                                |
| Build          | Vite 6                                  |
| Styling        | Tailwind v4 (`@tailwindcss/vite`)       |
| Animation      | Motion (optional, used sparingly)       |
| Data fetching  | `fetch` + a small polling hook; add TanStack Query only if polling/cache logic grows |
| State          | React state/reducer + context; no Redux unless it proves necessary |
| Server         | **unchanged** — Flask serves built assets at `/` and `/api/*` |

No TypeScript decision is forced; recommended (`.tsx`) because the API payloads
are non-trivial and types kill a whole class of the current bugs. Can start JS and
migrate.

## Repo layout

```
/                       listenbrainz_bot.py, Dockerfile, requirements.txt
/web/                   new frontend project (Vite root)
  package.json
  vite.config.js
  index.html
  src/
    main.jsx
    App.jsx
    lib/api.js          thin wrapper over /api/* (one place for all routes)
    hooks/usePolling.js the single, shared polling primitive
    components/...       panels + reusable UI
/web/dist/              build output (gitignored; produced in Docker)
```

Flask change is minimal: point the `/` route and a static handler at
`web/dist/` (serve `index.html` + hashed assets) instead of returning
`WEB_INDEX_HTML`. SPA fallback: unknown non-`/api` paths return `index.html`.

## The fragility fix (the whole point)

- **One state tree.** Server data lives in state; the UI is a *function of* that
  state. Buttons dispatch actions; they never touch the DOM.
- **One polling primitive** (`usePolling`) that updates state on an interval and
  on focus. React reconciles — no `innerHTML`, no lost listeners, no races.
- **Optimistic + reconciled** actions: a click updates local state immediately and
  is confirmed/rolled back by the next poll, so the UI never feels stuck or lies.
- **Component isolation** with error boundaries: a broken panel doesn't take down
  the page.

## API surface (already exists — consume as-is)

The 59 routes group cleanly into the panels below (see `listenbrainz_bot.py`
~line 9199+). No backend changes needed for the rewrite; if a screen needs a
shape the API doesn't return, add a read-only endpoint rather than reshaping in
the client.

- **Review / missing tracks:** `/api/review`, `/api/scan`, `/api/scan-all`
- **Repair jobs:** `/api/repair-jobs[...]` (match, manual-match, stage, import,
  verify, defer/resume)
- **Groups / sources / downloads:** `/api/groups/<id>/...` (sources, reconcile,
  match, merge, missing, retag, decisions, download), `/api/downloads`,
  `/api/downloads/cancel`
- **Search:** `/api/search`, `/api/searches`, `/api/searches/<id>/download`
- **Albums:** `/api/album/lookup`, `/api/album/download`
- **Beets (placement, present-day):** `/api/beets/*`
- **Ops / status:** `/api/tasks`, `/api/operations`, `/api/action-center`,
  `/api/status`, `/api/diagnostics`, `/api/logs`, `/api/settings`,
  `/api/rescan`, `/api/playlists/scan`, `/api/spotify/scan`

## Screen-by-screen migration order

The SPA serves `/`; panels migrate independently behind it, so this is
incremental, not big-bang. Suggested order (highest pain first):

1. **App shell + nav + status bar** (`/api/status`, `/api/action-center`) — the
   always-on chrome and the live operation indicator.
2. **Missing-tracks / Review** (`/api/review`, scan triggers) — the primary view.
3. **Repair jobs** — the most stateful, race-prone screen; biggest payoff from a
   proper state tree.
4. **Sources / downloads** (groups, `/api/downloads`) — directly benefits from
   work item B's failover surfacing.
5. **Search + Albums.**
6. **Diagnostics / Logs / Settings.**

Each step ships behind the same `/` and can be validated against the live stack
on the LAN before moving on.

## Docker / build impact (and keeping the 30-min build in check)

Adding a frontend means a **Node build stage**. Done naively this *adds* to build
time; done right it doesn't touch the slow Python layers. Use a **multi-stage**
build so the frontend and the Python runtime cache independently:

```dockerfile
# --- stage 1: build frontend ---
FROM node:22-slim AS web
WORKDIR /web
COPY web/package*.json ./
RUN npm ci                      # cached unless deps change
COPY web/ ./
RUN npm run build               # -> /web/dist

# --- stage 2: python runtime (largely as today) ---
FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY listenbrainz_bot.py .
COPY --from=web /web/dist ./web/dist
EXPOSE 8899
CMD ["python", "-u", "listenbrainz_bot.py"]
```

Key points:
- The Node stage is **parallelizable** and cached on `package*.json`; it does not
  block or invalidate the Python layers.
- Frontend source changes only rebuild stage 1 + the final `COPY`; Python deps
  stay cached.
- This is **independent of** the build-time wins tracked separately (dropping
  unused Python deps, BuildKit pip cache, and the longer-term beets removal — see
  the build notes in chat / a future `BUILD_NOTES.md`).

## Sequencing relative to work items A & B

The UI rewrite is **orthogonal** to A (placement) and B (source failover): those
are backend pipeline fixes and touch none of the frontend. Order:

1. Land **A** then **B** — make the tool actually work (no stalls; files land in
   the library). A pretty UI on a stalling pipeline is the wrong order.
2. Then the UI rewrite as a self-contained phase — or in parallel, since it can't
   conflict with pipeline code.

## Risks / open questions

- **TypeScript or not** — recommended; decide before scaffolding.
- **Polling vs. push** — start with polling (matches today). Consider SSE/WebSocket
  later for live download/repair progress if polling feels laggy; backend would
  need one new endpoint.
- **Auth** — current UI appears unauthenticated on the LAN. If the SPA is ever
  exposed beyond the LAN, add auth (Explo has a session/login pattern to copy).
- **Settings writes** — confirm which `/api/settings` fields are writable vs.
  display-only before building forms.
- **No live testing without the stack** — validate each panel against the running
  Navidrome/slskd/library on the LAN, same caveat as the pipeline work.
