import { createContext, useContext, useReducer, useEffect, useCallback, useRef, useState } from 'react'
import { api, post } from './lib/api.js'
import Layout from './components/Layout.jsx'
import ConfirmBar from './components/ConfirmBar.jsx'
import FillGaps from './panels/FillGaps.jsx'
import Transfers from './panels/Transfers.jsx'
import Library from './panels/Library.jsx'
import Artist from './panels/Artist.jsx'
import Fresh from './panels/Fresh.jsx'
import System from './panels/System.jsx'
import Import from './panels/Import.jsx'
import Playlist from './panels/Playlist.jsx'
import Toasts from './components/Toasts.jsx'

// ── Context ──────────────────────────────────────────────────────────────────
export const AppContext = createContext(null)
export const useApp = () => useContext(AppContext)

// ── Nav ──────────────────────────────────────────────────────────────────────
// Four primary screens (the redesigned IA), plus an Advanced dropdown for the
// deep-control panels the main screens intentionally keep simple.
export const SCREEN_TABS = ['Fill gaps', 'Downloads', 'Library', 'Artist', 'Fresh', 'System']
export const ADVANCED_TABS = ['Import', 'Playlist']
export const SECTIONS = [
  { key: 'Fill gaps', tab: 'Fill gaps' },
  { key: 'Downloads', tab: 'Downloads' },
  { key: 'Library', tab: 'Library' },
  { key: 'Artist', tab: 'Artist' },
  { key: 'Fresh', tab: 'Fresh' },
  { key: 'System', tab: 'System' },
  { key: 'Advanced', tabs: ADVANCED_TABS, dropdown: true },
]
export const TABS = [...SCREEN_TABS, ...ADVANCED_TABS]

const LEGACY_TABS = new Set(ADVANCED_TABS)

// ── Routing ───────────────────────────────────────────────────────────────────
// The hash is the single source of truth for "which view am I on". Every tab and
// every drill-down has one, so every navigation is a history entry and Back
// returns to the previous *view*. Before this, tab switches wrote localStorage
// only and created no history at all, while three routes (#/artist, #/gaps,
// #/import) did — so Back from any tab jumped to whatever stale hash was left
// over from another screen.
//
// Not in history: search keystrokes, filter chips, scroll. Those stay in
// localStorage-backed reducer state.
export const TAB_ROUTES = {
  'Fill gaps': 'gaps',
  'Downloads': 'downloads',
  'Library': 'library',
  'Artist': 'artist',
  'Fresh': 'fresh',
  'System': 'system',
  'Import': 'import',
  'Playlist': 'playlist',
}
const ROUTE_TABS = Object.fromEntries(
  Object.entries(TAB_ROUTES).map(([tab, route]) => [route, tab]))

const enc = s => encodeURIComponent(s)

/** Build a hash for a tab plus positional params. Empty params are dropped. */
export function routeHash(tab, ...params) {
  const base = TAB_ROUTES[tab]
  if (!base) return '#/'
  const parts = []
  for (const p of params) {
    if (p == null || p === '') break
    parts.push(enc(String(p)))
  }
  return `#/${base}${parts.length ? '/' + parts.join('/') : ''}`
}

/** Parse location.hash into { tab, params } — params are already decoded. */
export function parseHash(hash = location.hash) {
  const raw = (hash || '').replace(/^#\/?/, '')
  if (!raw) return { tab: null, params: [] }
  const segs = raw.split('/').filter(s => s !== '')
  const tab = ROUTE_TABS[segs[0]]
  if (!tab) return { tab: null, params: [] }
  return { tab, params: segs.slice(1).map(s => { try { return decodeURIComponent(s) } catch { return s } }) }
}

/** Push a route as a new history entry (the normal navigation verb). */
export function navigate(tab, ...params) {
  const next = routeHash(tab, ...params)
  if (location.hash !== next) location.hash = next
}

/** Rewrite the current route without adding an entry (transient sync only). */
export function replaceRoute(tab, ...params) {
  const next = routeHash(tab, ...params)
  if (location.hash === next) return
  history.replaceState(null, '', location.pathname + location.search + next)
  // replaceState fires no hashchange, and the router is the only thing that
  // turns a route into state — so raise it ourselves rather than teaching the
  // app a second way to change screens.
  window.dispatchEvent(new HashChangeEvent('hashchange'))
}

// ── State ─────────────────────────────────────────────────────────────────────
function initialTab() {
  // The hash wins on load (deep links, refresh, restored session); localStorage
  // is only the fallback for a bare URL.
  const fromHash = parseHash().tab
  if (fromHash) return fromHash
  const stored = localStorage.getItem('lbTab')
  return stored && TABS.includes(stored) ? stored : 'Fill gaps'
}

// Sticky UI bits (filters, subtab, selection) persist across tab switches and
// reloads so a chosen view survives navigation, per the polish plan.
function lsGet(key, fallback) {
  const v = localStorage.getItem(key)
  return v == null ? fallback : v
}
function lsSet(key, value) {
  if (value == null || value === '') localStorage.removeItem(key)
  else localStorage.setItem(key, String(value))
}

const initialState = {
  // new-screen server data
  summary: null,
  gaps: null,          // { items, counts, scanStatus, scanMessage }
  // The gaps list in hand can't confirm the selected album — set when a route
  // names an id the list doesn't contain (deep link, or a group a later scan
  // created). FillGaps uses it to hold the cursor instead of re-homing onto
  // items[0]. Cheaper than nulling `gaps`, which blanked the whole screen.
  gapsStale: false,
  gapDetail: null,     // focused album (tracks + sources)
  transfers: null,     // { transfers, needsPlacement, counts }
  library: null,       // { items, total, page, pages, libraryTotals }
  health: null,
  prefs: null,
  logEntries: null,
  // legacy server data (Advanced panels: Import, Playlist)
  review: null,
  tasks: {},
  // ui
  tab: initialTab(),
  routeParams: parseHash().params,         // positional params of the current route
  selGap: parseHash().tab === 'Fill gaps'
    ? (parseHash().params[0] || null)
    : lsGet('lb.selGap', null),            // focused gap group id
  gapFilter: lsGet('lb.gapFilter', 'needs'),
  gapSearch: '',
  libFilter: lsGet('lb.libFilter', 'all'),
  libSearch: lsGet('lb.libSearch', ''),
  libPage: Number(lsGet('lb.libPage', 0)) || 0,
  sysTab: lsGet('lb.sysTab', 'health'),
  logFilter: '',
  // Last poll error, shown as an offline banner. Silent polls used to swallow
  // failures entirely, so a tab that never loaded sat on "Loading…" forever
  // with no indication the backend was down.
  pollError: null,
  lastSyncAt: 0,
  toasts: [],
  // legacy ui
  pendingImportTarget: null,
}

function reducer(state, action) {
  switch (action.type) {
    case 'LOAD_SCREEN': {
      const next = { ...state, ...action.data }
      // A fresh list can confirm or deny the selection, so the hold is over.
      if (action.data.gaps) next.gapsStale = false
      return next
    }
    case 'LOAD_DATA': {
      const { review, tasks } = action
      return { ...state,
        review: review ?? state.review,
        tasks: tasks ?? state.tasks }
    }
    // The one place the hash turns into UI state. Everything else navigates by
    // assigning location.hash; the hashchange listener funnels back through here.
    case 'SET_ROUTE': {
      const { tab, params } = action
      localStorage.setItem('lbTab', tab)
      const next = { ...state, tab, routeParams: params }
      if (tab === 'Fill gaps') {
        const id = params[0] || null
        if (id !== state.selGap) {
          lsSet('lb.selGap', id)
          next.selGap = id
          // gapDetail is left alone: FillGaps only renders it when its id
          // matches the selection, so a stale one is already inert, and
          // clearing it here would just be another render of churn.
          //
          // Only a *deep link* needs special handling. When the album is
          // already in the list — a rail click, Prev/Next, cursor re-homing
          // after a search — every bit of state we have is still valid, so
          // touching none of it keeps the screen rendered. Blanking `gaps`
          // here is what made every album pick, and every keystroke that
          // re-homed the cursor, flash through "Loading gaps…" and refetch.
          if (id && !(state.gaps?.items || []).some(g => g.id === id)) {
            // The list can't confirm this id: it may predate the scan that
            // created the group, or the default "needs" filter may simply
            // hide it (a downloading album, say). Widen the filter and hold
            // the cursor until a fresh list arrives.
            lsSet('lb.gapFilter', '')
            next.gapFilter = ''
            next.gapsStale = true
          }
        }
      } else if (tab === 'System') {
        const sub = params[0] || state.sysTab
        lsSet('lb.sysTab', sub)
        next.sysTab = sub
      } else if (tab === 'Import') {
        next.pendingImportTarget = params[0]
          ? { folderPath: params[0],
              groupId: params[1] && params[1] !== '-' ? params[1] : '',
              mode: params[2] || '' }
          : null
      }
      return next
    }
    case 'SET_GAP_FILTER':
      lsSet('lb.gapFilter', action.filter)
      return { ...state, gapFilter: action.filter }
    case 'SET_GAP_SEARCH':
      return { ...state, gapSearch: action.q }
    case 'SET_LIB_FILTER':
      lsSet('lb.libFilter', action.filter); lsSet('lb.libPage', 0)
      return { ...state, libFilter: action.filter, libPage: 0 }
    case 'SET_LIB_SEARCH':
      lsSet('lb.libSearch', action.q); lsSet('lb.libPage', 0)
      return { ...state, libSearch: action.q, libPage: 0 }
    case 'SET_LIB_PAGE':
      lsSet('lb.libPage', action.page)
      return { ...state, libPage: action.page }
    case 'SET_SYS_TAB':
      lsSet('lb.sysTab', action.sysTab)
      return { ...state, sysTab: action.sysTab }
    case 'SET_LOG_FILTER':
      return { ...state, logFilter: action.filter }
    case 'SET_SYNC':
      return { ...state, pollError: null, lastSyncAt: Date.now() }
    case 'SET_POLL_ERROR':
      return { ...state, pollError: action.error }
    case 'PUSH_TOAST':
      return { ...state, toasts: [...state.toasts, action.toast] }
    case 'DISMISS_TOAST':
      return { ...state, toasts: state.toasts.filter(t => t.id !== action.id) }
    case 'SET_IMPORT_TARGET':
      return { ...state, pendingImportTarget: action.folderPath ? { folderPath: action.folderPath, groupId: action.groupId || '', mode: action.mode || '' } : null }
    default:
      return state
  }
}

// ── App ───────────────────────────────────────────────────────────────────────
export default function App() {
  const [state, dispatch] = useReducer(reducer, initialState)
  const refreshingRef = useRef(false)
  const refreshTimerRef = useRef(null)
  const refreshIntervalRef = useRef(5000)
  const abortControllerRef = useRef(null)
  // Monotonic request id: a slower, older refresh can resolve after a newer one
  // and must not overwrite fresher screen data or reschedule the poll loop.
  const reqIdRef = useRef(0)
  // refresh() reads UI params (tab, filters, selection) from here so its
  // identity stays stable while still fetching the right screen's data.
  const uiRef = useRef(state)
  uiRef.current = state

  const scheduleRefresh = useCallback((delayMs = 5000) => {
    if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current)
    refreshTimerRef.current = setTimeout(() => refresh({ silent: true }), delayMs)
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  const pushToast = useCallback((msg, level = 'info') => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`
    dispatch({ type: 'PUSH_TOAST', toast: { id, msg, level } })
    if (level !== 'error') {
      setTimeout(() => dispatch({ type: 'DISMISS_TOAST', id }), 4000)
    }
  }, [])

  const dismissToast = useCallback((id) => dispatch({ type: 'DISMISS_TOAST', id }), [])

  // `only` restricts the batch to named jobs. Selecting an album needs the new
  // album's detail and nothing else — refetching the summary and the whole gaps
  // list too made a rail click cost three round trips and replace every object
  // the screen renders from.
  const refresh = useCallback(async ({ silent = false, force = false, only = null } = {}) => {
    if (refreshTimerRef.current) { clearTimeout(refreshTimerRef.current); refreshTimerRef.current = null }
    if (refreshingRef.current && !force) return
    if (abortControllerRef.current) {
      abortControllerRef.current.abort()
    }
    abortControllerRef.current = new AbortController()
    const signal = abortControllerRef.current.signal
    const myReq = ++reqIdRef.current
    const stale = () => myReq !== reqIdRef.current
    refreshingRef.current = true
    if (!silent) dispatch({ type: 'SET_SYNC', msg: 'Refreshing…' })
    const ui = uiRef.current
    try {
      // Guard at creation, not after: api() fires on construction, so filtering
      // a built map would still cost the round trips it was meant to avoid.
      const want = k => !only || only.includes(k)
      const jobs = {}
      if (want('summary')) jobs.summary = api('/api/summary', { signal })
      if (ui.tab === 'Fill gaps') {
        if (want('gaps')) jobs.gaps = api('/api/gaps', { signal })
        // A selected id can stop existing — a deep link into a group a later
        // scan replaced, or a stale localStorage value. Its 404 must not reject
        // the batch below and take the gaps list down with it: the list is what
        // FillGaps needs to re-home the cursor onto a real album, so losing it
        // strands the screen on "Loading gaps…" through every later poll.
        if (ui.selGap && want('gapDetail')) {
          jobs.gapDetail = api(`/api/gaps/${ui.selGap}`, { signal })
            .catch(e => { if (e.name === 'AbortError') throw e; return null })
        }
        // Progress bars on the focused album come from the transfers snapshot.
        if (want('transfers') && (!ui.summary || (ui.summary?.transfers?.active || 0) + (ui.summary?.transfers?.queued || 0) > 0 || ui.gaps?.items?.some(g => g.status === 'downloading'))) {
          jobs.transfers = api('/api/transfers', { signal })
        }
      } else if (ui.tab === 'Downloads') {
        if (want('transfers')) jobs.transfers = api('/api/transfers', { signal })
      } else if (ui.tab === 'Library') {
        if (want('library')) jobs.library = api(`/api/library?filter=${encodeURIComponent(ui.libFilter)}&q=${encodeURIComponent(ui.libSearch)}&page=${ui.libPage}`, { signal })
      } else if (ui.tab === 'System') {
        if (ui.sysTab === 'health' && want('health')) jobs.health = api('/api/system/health', { signal })
        if (ui.sysTab === 'prefs' && !ui.prefs && want('prefs')) jobs.prefs = api('/api/prefs', { signal })
        if (ui.sysTab === 'logs' && want('logEntries')) jobs.logEntries = api(`/api/logs?tag=${encodeURIComponent(ui.logFilter === 'errors' ? '' : ui.logFilter)}&severity=${ui.logFilter === 'errors' ? 'error' : ''}`, { signal })
      } else if (LEGACY_TABS.has(ui.tab)) {
        // Only the two panels actually read: Import/Playlist render from review
        // + tasks. The old bundle also pulled operations, downloads, searches
        // and action-center on every poll, and nothing consumed them.
        jobs.legacy = Promise.all([
          api('/api/review', { signal }), api('/api/tasks', { signal }),
        ])
      }
      const keys = Object.keys(jobs)
      const results = await Promise.all(Object.values(jobs))
      // A newer refresh superseded us while awaiting — drop this result rather
      // than clobber the fresher screen data or restart the poll timer.
      if (stale()) return
      const data = {}
      keys.forEach((k, i) => {
        if (k === 'legacy') {
          const [review, tasks] = results[i]
          dispatch({ type: 'LOAD_DATA', review, tasks })
        } else if (k === 'logEntries') {
          data.logEntries = results[i]?.entries || []
        } else {
          data[k] = results[i]
        }
      })
      dispatch({ type: 'LOAD_SCREEN', data })
      // Poll fast while anything is moving; settle down when idle. A partial
      // fetch has no summary to judge by, so it leaves the cadence as-is rather
      // than reading "nothing active" from data it never asked for.
      if (!only) {
        const active = (data.summary?.transfers?.active || 0) + (data.summary?.transfers?.queued || 0)
        // A running source search has a live line to show, so poll it at the
        // fast cadence the same way a scan or a transfer does.
        const scanning = !!data.gaps?.scanTask
          || !!data.gaps?.items?.some(g => g.searching)
          || data.gapDetail?.sourceTask?.status === 'running'
        refreshIntervalRef.current = (active > 0 || scanning) ? 2000 : 5000
      }
      // Silent polls still bump lastSyncAt so the Live dot reflects reality.
      dispatch({ type: 'SET_SYNC', msg: silent ? undefined : `Updated ${new Date().toLocaleTimeString()}` })
    } catch (e) {
      if (e.name === 'AbortError') return
      // Silent polls report too — quietly dropping them is what left a tab on
      // "Loading…" indefinitely when the backend went away. The banner is the
      // visible channel; only an explicit refresh also raises a toast.
      dispatch({ type: 'SET_POLL_ERROR', error: e.message })
      if (!silent) {
        dispatch({ type: 'SET_SYNC', msg: `Error: ${e.message}` })
        pushToast(`Refresh failed: ${e.message}`, 'error')
      }
    } finally {
      refreshingRef.current = false
    }
    scheduleRefresh(refreshIntervalRef.current)
  }, [scheduleRefresh, pushToast])

  // Initial load + polling; also refetch immediately when the screen or its
  // parameters change so navigation feels instant instead of poll-paced.
  useEffect(() => {
    refresh()
    return () => {
      if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current)
      if (abortControllerRef.current) abortControllerRef.current.abort()
    }
  }, [refresh])

  // Refetch when the screen or its params change so navigation feels instant.
  // Skip the very first run — the mount effect above already did the initial
  // load, and firing both would double-fetch on startup.
  const paramsMountedRef = useRef(false)
  const { tab, selGap, libFilter, libSearch, libPage, sysTab, logFilter } = state
  const prevDepsRef = useRef(null)
  useEffect(() => {
    const deps = { tab, selGap, libFilter, libSearch, libPage, sysTab, logFilter }
    const prev = prevDepsRef.current
    prevDepsRef.current = deps
    if (!paramsMountedRef.current) { paramsMountedRef.current = true; return }
    // A bare tab switch back to a screen whose data is already in hand and only
    // seconds old doesn't need a round trip — the running poll brings it
    // forward. Any *parameter* change (selection, filter, page) always
    // refetches: that data isn't loaded at all.
    const ui = uiRef.current
    const onlyTabChanged = prev && prev.tab !== deps.tab &&
      Object.keys(deps).every(k => k === 'tab' || prev[k] === deps[k])
    const haveTabData = { 'Fill gaps': ui.gaps, 'Downloads': ui.transfers,
                          'Library': ui.library }[deps.tab]
    if (onlyTabChanged && haveTabData && Date.now() - ui.lastSyncAt < 3000) {
      scheduleRefresh(500)
      return
    }
    // Moving the cursor between albums only invalidates the focused album's
    // detail. The rail, the counts and the summary are all still correct, so
    // fetching them again just replaced every object the screen renders from.
    const onlySelGapChanged = prev && prev.tab === deps.tab && prev.selGap !== deps.selGap &&
      Object.keys(deps).every(k => k === 'selGap' || prev[k] === deps[k])
    if (onlySelGapChanged && deps.tab === 'Fill gaps' && ui.gaps) {
      refresh({ silent: true, force: true, only: ['gapDetail'] })
      return
    }
    refresh({ silent: true, force: true })
  }, [tab, selGap, libFilter, libSearch, libPage, sysTab, logFilter, refresh, scheduleRefresh])

  // The router. One hashchange listener, one SET_ROUTE dispatch — every screen
  // and drill-down derives from it, so Back/Forward always move between views.
  //
  // Routes: #/gaps[/<groupId>], #/downloads, #/library, #/artist[/<artistId>
  // [/<rgid>]], #/fresh, #/system[/<subtab>], #/playlist, and
  // #/import/<folderPath>[/<groupId>[/<mode>]] — where a groupId of "-" means
  // "no group" so <mode> stays reachable positionally, and mode "pick" forces
  // the MusicBrainz release picker over the review-group matcher.
  useEffect(() => {
    function syncHash() {
      const { tab, params } = parseHash()
      if (tab) {
        dispatch({ type: 'SET_ROUTE', tab, params })
      } else {
        // Bare or foreign hash (first visit, a stale link): adopt the current
        // tab's route without adding an entry, so the hash is never empty and
        // Back can't land on a URL that means nothing.
        replaceRoute(uiRef.current.tab)
        dispatch({ type: 'SET_ROUTE', tab: uiRef.current.tab, params: [] })
      }
    }
    syncHash()
    window.addEventListener('hashchange', syncHash)
    return () => window.removeEventListener('hashchange', syncHash)
  }, [])

  const action = useCallback(async (path, body = {}) => {
    const r = await post(path, body)
    if (r.operation || r.operation_id) {
      const op = r.operation
      const msg = op ? `${op.status}: ${op.message || op.kind}` : `Operation ${r.operation_id} queued`
      pushToast(msg, op?.status === 'error' ? 'error' : 'info')
    }
    scheduleRefresh(800)
    return r
  }, [scheduleRefresh, pushToast])

  const [pendingConfirm, setPendingConfirm] = useState(null)

  const requestConfirm = useCallback((message) => (
    new Promise(resolve => setPendingConfirm({ message, resolve }))
  ), [])

  const resolveConfirm = useCallback((ok) => {
    setPendingConfirm(prev => { prev?.resolve(ok); return null })
  }, [])

  const confirmAction = useCallback(async (message, path, body = {}) => {
    if (!(await requestConfirm(message))) return null
    return action(path, body)
  }, [action, requestConfirm])

  const ctx = {
    state,
    dispatch,
    refresh,
    scheduleRefresh,
    action,
    confirmAction,
    requestConfirm,
    pushToast,
  }

  const panels = {
    'Fill gaps': FillGaps,
    'Downloads': Transfers,
    'Library': Library,
    'Artist': Artist,
    'Fresh': Fresh,
    'System': System,
    'Import': Import,
    'Playlist': Playlist,
  }

  // Legacy panels need the old data bundle before first render; new screens
  // gate on the summary instead.
  const isLegacy = LEGACY_TABS.has(tab)
  const ready = isLegacy
    ? (state.review !== null || tab === 'Import')
    : state.summary !== null

  const Panel = panels[tab] || null

  return (
    <AppContext.Provider value={ctx}>
      <Layout>
        <main className="app-main">
          {state.pollError && (
            <div role="status" aria-live="polite"
              className="mb-4 flex flex-wrap items-center gap-3 rounded-[12px] border p-3.5 text-[13px]"
              style={{ background: 'var(--warn-tint)', borderColor: 'var(--danger-bd)',
                       color: 'var(--danger-text)' }}>
              <span className="font-semibold" style={{ color: 'var(--danger)' }}>
                Can’t reach lb-bot
              </span>
              <span className="muted">{state.pollError}</span>
              <span className="spacer" />
              <button onClick={() => refresh({ force: true })}>Retry now</button>
            </div>
          )}
          {!ready
            ? <p className="muted">{state.pollError ? 'Waiting for the backend…' : 'Loading…'}</p>
            : Panel ? <Panel /> : null}
        </main>
      </Layout>
      <ConfirmBar pending={pendingConfirm} onResolve={resolveConfirm} />
      <Toasts toasts={state.toasts} onDismiss={dismissToast} />
    </AppContext.Provider>
  )
}
