import { createContext, useContext, useReducer, useEffect, useCallback, useRef, useState } from 'react'
import { api, post } from './lib/api.js'
import Layout from './components/Layout.jsx'
import ConfirmBar from './components/ConfirmBar.jsx'
import FillGaps from './panels/FillGaps.jsx'
import Downloads from './panels/Downloads.jsx'
import Library from './panels/Library.jsx'
import Discover from './panels/Discover.jsx'
import Settings from './panels/Settings.jsx'
import Search from './panels/Search.jsx'
import Toasts from './components/Toasts.jsx'

// ── Context ──────────────────────────────────────────────────────────────────
export const AppContext = createContext(null)
export const useApp = () => useContext(AppContext)

// ── Nav ──────────────────────────────────────────────────────────────────────
// Five sections. Search is a screen but not a tab — the header box opens it.
//
// Before 2026-09-26 there were seven tabs plus an "Advanced" dropdown: Library
// held four unrelated features, Artist and Fresh were top-level, and Advanced
// hid Import (a second, older copy of Downloads' placement list) and Playlist
// (a scan button over a generic task log). See the SESSION note of that date.
export const SECTIONS = ['Fill gaps', 'Downloads', 'Library', 'Discover', 'Settings']
export const TAB_ROUTES = {
  'Fill gaps': 'gaps',
  'Downloads': 'downloads',
  'Library': 'library',
  'Discover': 'discover',
  'Settings': 'settings',
  'Search': 'search',
}
export const TABS = Object.keys(TAB_ROUTES)
const ROUTE_TABS = Object.fromEntries(
  Object.entries(TAB_ROUTES).map(([tab, route]) => [route, tab]))

// Old hashes → new ones, so bookmarks and history entries from before the
// restructure still land somewhere sensible.
function aliasOf(segs) {
  const [head, ...rest] = segs
  switch (head) {
    case 'artist': return ['library', 'artists', ...rest]
    case 'fresh': return ['discover', 'new']
    case 'playlist': return ['gaps']
    case 'import': return rest.length ? ['downloads', 'place', ...rest] : ['downloads']
    case 'system': {
      const sub = { health: 'status', prefs: 'sources', logs: 'logs' }[rest[0]] || 'status'
      return ['settings', sub]
    }
    case 'library':
      if (rest[0] === 'duplicates') return ['library', 'cleanup']
      return null
    default: return null
  }
}

const enc = s => encodeURIComponent(s)

/** Build a hash for a tab plus positional params. Empty params stop the list. */
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

function splitHash(hash) {
  const raw = (hash || '').replace(/^#\/?/, '')
  return raw.split('/').filter(s => s !== '').map(s => { try { return decodeURIComponent(s) } catch { return s } })
}

/** Parse location.hash into { tab, params } — params are already decoded.
 *  An old-style hash is rewritten in place (no history entry). */
export function parseHash(hash = location.hash) {
  let segs = splitHash(hash)
  if (!segs.length) return { tab: null, params: [] }
  const aliased = aliasOf(segs)
  if (aliased) {
    segs = aliased
    const next = routeHash(ROUTE_TABS[segs[0]], ...segs.slice(1))
    if (next !== location.hash) history.replaceState(null, '', location.pathname + location.search + next)
  }
  const tab = ROUTE_TABS[segs[0]]
  if (!tab) return { tab: null, params: [] }
  return { tab, params: segs.slice(1) }
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
  window.dispatchEvent(new HashChangeEvent('hashchange'))
}

// Where an artist, or one of their albums, lives. `artistId` is a Navidrome id
// for an owned artist or `mb:<mbid>` for anyone else; `pick` opens the album
// page with its source picker showing.
export function goArtist(artistId, rgid, pick) {
  navigate('Library', 'artists', artistId, rgid, pick ? 'sources' : undefined)
}

// An album known only by its release-group id (search results, charts, the
// wishlist). The album page needs an artist, so this lands on a small resolver
// that asks MusicBrainz who made it and then replaces itself with the real page.
export function goAlbum(rgid, pick) {
  navigate('Library', 'artists', '-', rgid, pick ? 'sources' : undefined)
}

// ── State ─────────────────────────────────────────────────────────────────────
function initialTab() {
  const fromHash = parseHash().tab
  if (fromHash) return fromHash
  const stored = lsGet('lbTab', null)
  return stored && SECTIONS.includes(stored) ? stored : 'Fill gaps'
}

// Sticky UI bits persist across tab switches and reloads. Storage can throw
// (private windows, blocked site data), so every access is guarded.
export function lsGet(key, fallback) {
  try {
    const v = localStorage.getItem(key)
    return v == null ? fallback : v
  } catch { return fallback }
}
export function lsSet(key, value) {
  try {
    if (value == null || value === '') localStorage.removeItem(key)
    else localStorage.setItem(key, String(value))
  } catch { /* per-viewer convenience only */ }
}

const initialState = {
  summary: null,
  gaps: null,          // { items, counts, origins, scanTask }
  // The list in hand can't confirm the selected album (deep link, or a group a
  // later scan created) — FillGaps holds the cursor instead of re-homing.
  gapsStale: false,
  gapDetail: null,
  transfers: null,     // { transfers, needsPlacement, counts, identify }
  wishlist: null,      // { wishlist, total, intervalSeconds, cooldownSeconds }
  fills: null,         // { albums: {mbid: view}, gaps, serverTime }
  library: null,       // { items, total, page, pages, libraryTotals }
  health: null,
  systemStatus: null,
  prefs: null,
  logEntries: null,
  tasks: null,
  // ui
  tab: initialTab(),
  routeParams: parseHash().params,
  selGap: parseHash().tab === 'Fill gaps'
    ? (parseHash().params[0] || null)
    : lsGet('lb.selGap', null),
  gapFilter: lsGet('lb.gapFilter', 'needs'),
  gapHidden: false,
  gapSearch: '',
  libFilter: lsGet('lb.libFilter', 'all'),
  libSearch: lsGet('lb.libSearch', ''),
  libPage: Number(lsGet('lb.libPage', 0)) || 0,
  libSort: lsGet('lb.libSort', ''),
  logFilter: '',
  pollError: null,
  lastSyncAt: 0,
  toasts: [],
}

function reducer(state, action) {
  switch (action.type) {
    case 'LOAD_SCREEN': {
      const next = { ...state, ...action.data }
      if (action.data.gaps) next.gapsStale = false
      return next
    }
    // The one place the hash turns into UI state.
    case 'SET_ROUTE': {
      const { tab, params } = action
      if (SECTIONS.includes(tab)) lsSet('lbTab', tab)
      const next = { ...state, tab, routeParams: params }
      if (tab === 'Fill gaps') {
        const id = params[0] || null
        if (id !== state.selGap) {
          lsSet('lb.selGap', id)
          next.selGap = id
          // Only a *deep link* needs special handling: when the album isn't in
          // the list in hand, widen the filter and hold the cursor until a
          // fresh list arrives, rather than blanking the screen.
          if (id && !(state.gaps?.items || []).some(g => g.id === id)) {
            lsSet('lb.gapFilter', '')
            next.gapFilter = ''
            next.gapsStale = true
            // The Hidden view persists across tabs; a link to a visible album
            // fetched the hidden list, missed the album and re-homed onto a
            // hidden one.
            if (state.gapHidden) { next.gapHidden = false; next.gaps = null }
          }
        }
      }
      return next
    }
    case 'SET_GAP_FILTER':
      lsSet('lb.gapFilter', action.filter)
      // Leaving the Hidden view drops its list, or the rail showed hidden
      // albums under "Needs you" until the next fetch.
      return {
        ...state, gapFilter: action.filter, gapHidden: false,
        ...(state.gapHidden ? { gaps: null, gapsStale: false } : {}),
      }
    case 'SET_GAP_HIDDEN':
      return { ...state, gapHidden: action.hidden, gaps: null, gapsStale: false }
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
    case 'SET_LIB_SORT':
      lsSet('lb.libSort', action.sort); lsSet('lb.libPage', 0)
      return { ...state, libSort: action.sort, libPage: 0 }
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
    default:
      return state
  }
}

// Which screen-level data each route needs. A key here is refetched on every
// poll of that screen; anything a panel loads for itself is not listed.
function jobsFor(ui, want, signal) {
  const jobs = {}
  if (want('summary')) jobs.summary = api('/api/summary', { signal })
  const sub = ui.routeParams[0] || ''
  switch (ui.tab) {
    case 'Fill gaps':
      if (want('gaps')) jobs.gaps = api(ui.gapHidden ? '/api/gaps?hidden=1' : '/api/gaps', { signal })
      // A selected id can stop existing — its 404 must not reject the batch
      // and take the gaps list down with it.
      if (ui.selGap && want('gapDetail')) {
        jobs.gapDetail = api(`/api/gaps/${ui.selGap}`, { signal })
          .catch(e => { if (e.name === 'AbortError') throw e; return null })
      }
      // The selected album's own status counts too: right after "Get N
      // tracks" the list is a poll behind, and without its transfers the card
      // read "filing into the album" with no Cancel.
      if (want('transfers') && (!ui.summary
          || (ui.summary?.transfers?.active || 0) + (ui.summary?.transfers?.queued || 0) > 0
          || ui.gapDetail?.status === 'downloading'
          || ui.gaps?.items?.some(g => g.status === 'downloading'))) {
        jobs.transfers = api('/api/transfers', { signal })
      }
      break
    case 'Downloads':
      // The placement drill-down loads its own folder and never reads these.
      if (sub === 'place') break
      if (want('transfers')) jobs.transfers = api('/api/transfers', { signal })
      if (want('wishlist')) jobs.wishlist = api('/api/wishlist', { signal }).catch(() => null)
      if (want('fills')) jobs.fills = api('/api/fills?recent=1', { signal }).catch(() => null)
      break
    case 'Library':
      if (sub === 'albums' && want('library')) {
        const q = new URLSearchParams({ filter: ui.libFilter, q: ui.libSearch, page: ui.libPage, sort: ui.libSort })
        jobs.library = api(`/api/library?${q}`, { signal })
      }
      break
    case 'Settings':
      // Status loads its own health checks: the first run takes ~6 s and would
      // hold every other poll (and the Live dot) hostage.
      if (sub === 'sources' && !ui.prefs && want('prefs')) jobs.prefs = api('/api/prefs', { signal })
      if (sub === 'logs' && want('logEntries')) {
        const f = ui.logFilter
        jobs.logEntries = api(`/api/logs?tag=${encodeURIComponent(f === 'errors' ? '' : f)}&severity=${f === 'errors' ? 'error' : ''}`, { signal })
      }
      if (sub === 'activity' && want('tasks')) jobs.tasks = api('/api/tasks', { signal })
      break
    default:
      break
  }
  return jobs
}

// ── App ───────────────────────────────────────────────────────────────────────
export default function App() {
  const [state, dispatch] = useReducer(reducer, initialState)
  const refreshingRef = useRef(false)
  const refreshTimerRef = useRef(null)
  const refreshIntervalRef = useRef(5000)
  const abortControllerRef = useRef(null)
  // Monotonic request id: an older refresh resolving late must not overwrite
  // fresher data or reschedule the poll loop.
  const reqIdRef = useRef(0)
  const uiRef = useRef(state)
  uiRef.current = state

  const scheduleRefresh = useCallback((delayMs = 5000) => {
    if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current)
    refreshTimerRef.current = setTimeout(() => refresh({ silent: true }), delayMs)
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  const pushToast = useCallback((msg, level = 'info') => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`
    dispatch({ type: 'PUSH_TOAST', toast: { id, msg, level } })
    if (level !== 'error') setTimeout(() => dispatch({ type: 'DISMISS_TOAST', id }), 4000)
  }, [])

  const dismissToast = useCallback((id) => dispatch({ type: 'DISMISS_TOAST', id }), [])

  // `only` restricts the batch to named jobs (selecting an album needs its
  // detail and nothing else).
  const refresh = useCallback(async ({ silent = false, force = false, only = null } = {}) => {
    if (refreshTimerRef.current) { clearTimeout(refreshTimerRef.current); refreshTimerRef.current = null }
    if (refreshingRef.current && !force) return
    if (abortControllerRef.current) abortControllerRef.current.abort()
    abortControllerRef.current = new AbortController()
    const signal = abortControllerRef.current.signal
    const myReq = ++reqIdRef.current
    const stale = () => myReq !== reqIdRef.current
    refreshingRef.current = true
    const ui = uiRef.current
    try {
      const want = k => !only || only.includes(k)
      const jobs = jobsFor(ui, want, signal)
      const keys = Object.keys(jobs)
      const results = await Promise.all(Object.values(jobs))
      if (stale()) return
      const data = {}
      keys.forEach((k, i) => {
        if (k === 'logEntries') data.logEntries = results[i]?.entries || []
        else data[k] = results[i]
      })
      dispatch({ type: 'LOAD_SCREEN', data })
      if (!only) {
        const active = (data.summary?.transfers?.active || 0) + (data.summary?.transfers?.queued || 0)
        const scanning = !!data.gaps?.scanTask
          || !!data.gaps?.items?.some(g => g.searching)
          || data.gapDetail?.sourceTask?.status === 'running'
        refreshIntervalRef.current = (active > 0 || scanning) ? 2000 : 5000
      }
      dispatch({ type: 'SET_SYNC' })
    } catch (e) {
      if (e.name === 'AbortError') return
      dispatch({ type: 'SET_POLL_ERROR', error: e.message })
      if (!silent) pushToast(`Refresh failed: ${e.message}`, 'error')
    } finally {
      if (myReq === reqIdRef.current) refreshingRef.current = false
    }
    if (!stale()) scheduleRefresh(refreshIntervalRef.current)
  }, [scheduleRefresh, pushToast])

  useEffect(() => {
    refresh()
    return () => {
      if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current)
      if (abortControllerRef.current) abortControllerRef.current.abort()
    }
  }, [refresh])

  // Refetch when the screen or its parameters change so navigation feels
  // instant instead of poll-paced. Skips the first run (the mount did it).
  const paramsMountedRef = useRef(false)
  const { tab, selGap, gapHidden, libFilter, libSearch, libPage, libSort, logFilter } = state
  // On Fill gaps the first route param IS the selected album, already tracked
  // as selGap; counting it again made every album switch a full forced refresh
  // instead of the one-request detail fetch below.
  const sub = tab === 'Fill gaps' ? '' : (state.routeParams[0] || '')
  const prevDepsRef = useRef(null)
  useEffect(() => {
    const deps = { tab, sub, selGap, gapHidden, libFilter, libSearch, libPage, libSort, logFilter }
    const prev = prevDepsRef.current
    prevDepsRef.current = deps
    if (!paramsMountedRef.current) { paramsMountedRef.current = true; return }
    const ui = uiRef.current
    const onlyTabChanged = prev && prev.tab !== deps.tab &&
      Object.keys(deps).every(k => k === 'tab' || prev[k] === deps[k])
    const haveTabData = { 'Fill gaps': ui.gaps, 'Downloads': ui.transfers }[deps.tab]
    if (onlyTabChanged && haveTabData && Date.now() - ui.lastSyncAt < 3000) {
      scheduleRefresh(500)
      return
    }
    const onlySelGapChanged = prev && prev.tab === deps.tab && prev.selGap !== deps.selGap &&
      Object.keys(deps).every(k => k === 'selGap' || prev[k] === deps[k])
    if (onlySelGapChanged && deps.tab === 'Fill gaps' && ui.gaps) {
      refresh({ silent: true, force: true, only: ['gapDetail'] })
      return
    }
    refresh({ silent: true, force: true })
  }, [tab, sub, selGap, gapHidden, libFilter, libSearch, libPage, libSort, logFilter, refresh, scheduleRefresh])

  // The router: one hashchange listener, one SET_ROUTE dispatch.
  useEffect(() => {
    function syncHash() {
      const { tab, params } = parseHash()
      if (tab) {
        dispatch({ type: 'SET_ROUTE', tab, params })
      } else {
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
    if (r.operation?.status === 'error') pushToast(`${r.operation.message || r.operation.kind}`, 'error')
    scheduleRefresh(800)
    return r
  }, [scheduleRefresh, pushToast])

  const [pendingConfirm, setPendingConfirm] = useState(null)
  // A second request declines the first rather than stranding its promise:
  // the bar is non-modal, and a caller awaiting it stayed busy forever.
  const requestConfirm = useCallback((message, opts = {}) => (
    new Promise(resolve => setPendingConfirm(prev => {
      prev?.resolve(false)
      return { message, resolve, ...opts }
    }))
  ), [])
  const resolveConfirm = useCallback((ok) => {
    setPendingConfirm(prev => { prev?.resolve(ok); return null })
  }, [])
  const confirmAction = useCallback(async (message, path, body = {}, opts = {}) => {
    if (!(await requestConfirm(message, opts))) return null
    return action(path, body)
  }, [action, requestConfirm])

  const ctx = { state, dispatch, refresh, scheduleRefresh, action, confirmAction, requestConfirm, pushToast }

  const panels = {
    'Fill gaps': FillGaps,
    'Downloads': Downloads,
    'Library': Library,
    'Discover': Discover,
    'Settings': Settings,
    'Search': Search,
  }
  const Panel = panels[tab] || null
  // Screens gate on the summary; the ones that load their own data don't wait.
  const ready = state.summary !== null || ['Library', 'Discover', 'Search', 'Settings'].includes(tab)

  return (
    <AppContext.Provider value={ctx}>
      <Layout>
        <main className="app-main">
          {state.pollError && (
            <div role="status" aria-live="polite"
              className="mb-4 flex flex-wrap items-center gap-3 rounded-card border p-3.5 text-small"
              style={{ background: 'var(--warn-tint)', borderColor: 'var(--danger-bd)', color: 'var(--danger-text)' }}>
              <span className="font-semibold" style={{ color: 'var(--danger)' }}>Can’t reach lb-bot</span>
              <span className="text-muted">{state.pollError}</span>
              <span className="spacer" />
              <button onClick={() => refresh({ force: true })}>Retry now</button>
            </div>
          )}
          {!ready
            ? <p className="text-caption text-muted">{state.pollError ? 'Waiting for the backend…' : 'Loading…'}</p>
            : Panel ? <Panel /> : null}
        </main>
      </Layout>
      <ConfirmBar pending={pendingConfirm} onResolve={resolveConfirm} />
      <Toasts toasts={state.toasts} onDismiss={dismissToast} />
    </AppContext.Provider>
  )
}
