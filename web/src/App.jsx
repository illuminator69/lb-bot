import { createContext, useContext, useReducer, useEffect, useCallback, useRef, useState } from 'react'
import { api, post } from './lib/api.js'
import { jobs } from './lib/jobs.js'
import {
  isSection, navigate, onRoute, parseHash, replaceRoute, routeHash, currentKind,
} from './lib/router.js'
import Shell from './components/Shell.jsx'
import ConfirmBar from './components/ConfirmBar.jsx'
import Toasts from './components/Toasts.jsx'
import NeedsYou from './panels/NeedsYou.jsx'
import Activity from './panels/Activity.jsx'
import Library from './panels/Library.jsx'
import Discover from './panels/Discover.jsx'
import System from './panels/System.jsx'
import Search from './panels/Search.jsx'
import { ArtistPage, AlbumPage, forgetDiscography } from './panels/Artist.jsx'
import Placement from './panels/Placement.jsx'

// ── Context ──────────────────────────────────────────────────────────────────
export const AppContext = createContext(null)
export const useApp = () => useContext(AppContext)

// ── Navigation ───────────────────────────────────────────────────────────────
// Five sections in the sidebar. Artist, album, search and "file a folder"
// pages belong to none of them: they open on top of the page you were on, and
// Back names that page (lib/router.js).
//
// Before 2026-10 the sections were Fill gaps / Downloads / Library / Discover /
// Settings in a header, and artist and album pages lived under Library with a
// Back button hardcoded to "All artists". Old hashes are aliased in the router.
export { navigate, replaceRoute, routeHash }

/** What the app does with a job that ended while no page was showing it (the
 *  watch's notifier, lib/jobs.js): one toast says how it ended — and what the
 *  session kept of the artists' discographies is let go of first (F1):
 *  whatever the job changed, no page was there to read it again, and the
 *  artist's page would open on what it read before. */
export const onJobEnded = pushToast => toast => {
  forgetDiscography()
  pushToast(toast.text, toast.level)
}

// Where an artist, or one of their albums, lives. `artistId` is a Navidrome id
// for an owned artist or `mb:<mbid>` for anyone else; `pick` opens the album
// page with its source list showing.
export function goArtist(artistId, rgid, pick) {
  if (rgid) navigate('album', artistId, rgid, pick ? 'sources' : undefined)
  else navigate('artist', artistId)
}

// An album known only by its release-group id (search results, charts, the
// wishlist). The album page needs an artist, so this lands on a small resolver
// that asks MusicBrainz who made it and then replaces itself with the real page.
export function goAlbum(rgid, pick) {
  navigate('album', '-', rgid, pick ? 'sources' : undefined)
}

/** One album in the gaps queue. */
export const goGap = id => navigate('needs', 'gaps', id)

/** The "file this downloaded folder" page. `groupId` preselects the album it
 *  fills; mode 'pick' opens the release search; `file` files one file only. */
export function goPlace(path, groupId, mode, file) {
  navigate('place', path, groupId || (mode ? '-' : undefined), mode, file)
}

// ── State ─────────────────────────────────────────────────────────────────────
function initialTab() {
  const fromHash = parseHash().tab
  if (fromHash) return fromHash
  const stored = lsGet('lbTab', null)
  return stored && isSection(stored) ? stored : 'needs'
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

// The gaps queue's selected album is the route's third segment.
const gapFromRoute = (tab, params) =>
  (tab === 'needs' && (params[0] || 'gaps') === 'gaps' ? (params[1] || null) : undefined)

/** Which album's detail the Needs you page is showing: the queue's selection,
 *  or the failed gap selected under To fix. */
export function gapDetailId(ui) {
  if (ui.tab !== 'needs') return null
  const sub = ui.routeParams[0] || 'gaps'
  if (sub === 'gaps') return ui.selGap
  const sel = ui.routeParams[1] || ''
  return sub === 'fix' && sel.startsWith('gap:') ? sel.slice(4) : null
}

const boot = parseHash()
const initialState = {
  summary: null,
  gaps: null,          // { items, counts, origins, scanTask } — the visible queue
  hiddenGaps: null,    // the same shape, for albums the user hid
  // The list in hand can't confirm the selected album (deep link, or a group a
  // later scan created) — the queue holds the cursor instead of re-homing.
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
  routeParams: boot.params,
  navKind: '',
  selGap: gapFromRoute(boot.tab, boot.params) ?? lsGet('lb.selGap', null),
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
      const { tab, params, kind } = action
      if (isSection(tab)) lsSet('lbTab', tab)
      const next = { ...state, tab, routeParams: params, navKind: kind ?? state.navKind }
      const id = gapFromRoute(tab, params)
      // A route that names no album (#/needs/gaps) keeps the cursor where it was.
      if (id != null && id !== state.selGap) {
        lsSet('lb.selGap', id)
        next.selGap = id
        const has = (list, x) => (list?.items || []).some(g => g.id === x)
        const inVisible = has(state.gaps, id)
        if (state.gapHidden) {
          // The Hidden view has its own list. An album in it is an ordinary
          // selection. (Checked against the visible list alone, every hidden
          // album looked like a link to somewhere else and switched the view
          // off: Hidden showed 35 and listed none.) Only a link to an album
          // that is NOT hidden leaves the view, once that list is in hand.
          if (state.hiddenGaps && !has(state.hiddenGaps, id)) {
            next.gapHidden = false
            if (!inVisible) { lsSet('lb.gapFilter', ''); next.gapFilter = ''; next.gapsStale = true }
          }
        } else if (id && !inVisible) {
          // Only a *deep link* needs special handling: when the album isn't in
          // the list in hand, widen the filter and hold the cursor until a
          // fresh list arrives, rather than blanking the screen.
          lsSet('lb.gapFilter', '')
          next.gapFilter = ''
          next.gapsStale = true
        }
      }
      return next
    }
    // A write route answered with the album as it now stands. Showing that at
    // once, in the detail and on its list row, is what keeps the page from
    // sitting on the old state until the next poll.
    case 'GAP_UPDATED': {
      const d = action.gap
      if (!d?.id) return state
      const patch = list => (list?.items?.some(g => g.id === d.id)
        ? { ...list, items: list.items.map(g => (g.id === d.id
            ? { ...g, status: d.status, present: d.present, total: d.total, missingCount: d.missingCount,
                sourceCount: d.sourcesTotal ?? g.sourceCount, updatedAt: d.updatedAt ?? g.updatedAt }
            : g)) }
        : list)
      return {
        ...state,
        gaps: patch(state.gaps),
        hiddenGaps: patch(state.hiddenGaps),
        gapDetail: gapDetailId(state) === d.id ? d : state.gapDetail,
      }
    }
    case 'SET_GAP_FILTER':
      lsSet('lb.gapFilter', action.filter)
      return { ...state, gapFilter: action.filter, gapHidden: false }
    case 'SET_GAP_HIDDEN':
      return { ...state, gapHidden: action.hidden, hiddenGaps: null }
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

const soft = p => p.catch(e => { if (e.name === 'AbortError') throw e; return null })

// Which screen-level data each route needs. A key here is refetched on every
// poll of that screen; anything a panel loads for itself is not listed.
function jobsFor(ui, want, signal) {
  const jobs = {}
  if (want('summary')) jobs.summary = api('/api/summary', { signal })
  const sub = ui.routeParams[0] || ''
  switch (ui.tab) {
    case 'needs': {
      const fix = sub === 'fix'
      if (want('gaps')) jobs.gaps = api('/api/gaps', { signal })
      if (!fix && ui.gapHidden && want('hiddenGaps')) jobs.hiddenGaps = soft(api('/api/gaps?hidden=1', { signal }))
      // A selected id can stop existing — its 404 must not reject the batch
      // and take the gaps list down with it.
      const id = gapDetailId(ui)
      if (id && want('gapDetail')) jobs.gapDetail = soft(api(`/api/gaps/${id}`, { signal }))
      // To fix lists the folders waiting to be filed, which ride on the
      // transfers answer. In the queue the selected album's own status counts
      // too: right after a source is picked the list is a poll behind, and
      // without its transfers the detail read "filing into the album" with no
      // Cancel.
      if (want('transfers') && (fix || !ui.summary
          || (ui.summary?.transfers?.active || 0) + (ui.summary?.transfers?.queued || 0) > 0
          || ui.gapDetail?.status === 'downloading'
          || ui.gaps?.items?.some(g => g.status === 'downloading'))) {
        jobs.transfers = api('/api/transfers', { signal })
      }
      if (fix && want('fills')) jobs.fills = soft(api('/api/fills?recent=1', { signal }))
      break
    }
    case 'album':
      // An incomplete album shows its gap's download as it runs.
      if (want('transfers') && (ui.summary?.transfers?.active || 0) + (ui.summary?.transfers?.queued || 0) > 0) {
        jobs.transfers = soft(api('/api/transfers', { signal }))
      }
      break
    case 'activity':
      if (want('transfers')) jobs.transfers = api('/api/transfers', { signal })
      if (want('fills')) jobs.fills = soft(api('/api/fills?recent=1', { signal }))
      // Once, not on every poll: Landed names a gap fill by its album in the
      // queue, and the list is 1.4 MB.
      if (!ui.gaps && want('gaps')) jobs.gaps = soft(api('/api/gaps', { signal }))
      break
    case 'discover':
      // A few rows at most, and every tab reads it: the Wishlist tab's count,
      // and which new releases are already wished for.
      if (want('wishlist')) jobs.wishlist = soft(api('/api/wishlist', { signal }))
      break
    case 'library':
      if (sub === 'albums' && want('library')) {
        const q = new URLSearchParams({ filter: ui.libFilter, q: ui.libSearch, page: ui.libPage, sort: ui.libSort })
        jobs.library = api(`/api/library?${q}`, { signal })
      }
      break
    case 'system':
      // Status loads its own health checks: the first run takes ~6 s and would
      // hold every other poll (and the connection light) hostage.
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

const PAGES = {
  needs: NeedsYou,
  activity: Activity,
  discover: Discover,
  library: Library,
  system: System,
  search: Search,
  artist: ArtistPage,
  album: AlbumPage,
  place: Placement,
}

const TOAST_MS = 4000
const TOAST_ERROR_MS = 9000

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
    // Every notification leaves on its own. An error stays longer, since it is
    // the one that has to be read, and can still be dismissed by hand. Errors
    // used to stay until dismissed, and a few of them piled up over the page.
    setTimeout(() => dispatch({ type: 'DISMISS_TOAST', id }), level === 'error' ? TOAST_ERROR_MS : TOAST_MS)
  }, [])

  const dismissToast = useCallback((id) => dispatch({ type: 'DISMISS_TOAST', id }), [])

  // A retag or an undo that ends with no page showing it (lib/jobs.js): one
  // toast names the album and says how it ended.
  useEffect(() => {
    jobs.setNotifier(onJobEnded(pushToast))
    return () => jobs.setNotifier(null)
  }, [pushToast])

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
  // In the gaps queue the selected album is tracked as selGap; counting the
  // route segment again made every album switch a full forced refresh instead
  // of the one-request detail fetch below.
  const sub = state.routeParams[0] || ''
  const fixSel = tab === 'needs' && sub === 'fix' ? (state.routeParams[1] || '') : ''
  const prevDepsRef = useRef(null)
  useEffect(() => {
    const deps = { tab, sub, selGap, fixSel, gapHidden, libFilter, libSearch, libPage, libSort, logFilter }
    const prev = prevDepsRef.current
    prevDepsRef.current = deps
    if (!paramsMountedRef.current) { paramsMountedRef.current = true; return }
    const ui = uiRef.current
    const changed = Object.keys(deps).filter(k => !prev || prev[k] !== deps[k])
    const onlyTabChanged = changed.length === 1 && changed[0] === 'tab'
    const haveTabData = { needs: ui.gaps, activity: ui.transfers }[deps.tab]
    if (onlyTabChanged && haveTabData && Date.now() - ui.lastSyncAt < 3000) {
      scheduleRefresh(500)
      return
    }
    // Moving the cursor needs that album's detail and nothing else.
    const onlySelection = changed.length === 1 && (changed[0] === 'selGap' || changed[0] === 'fixSel')
    if (onlySelection && deps.tab === 'needs' && ui.gaps) {
      refresh({ silent: true, force: true, only: ['gapDetail'] })
      return
    }
    refresh({ silent: true, force: true })
  }, [tab, sub, selGap, fixSel, gapHidden, libFilter, libSearch, libPage, libSort, logFilter, refresh, scheduleRefresh])

  // The router: one subscription, one SET_ROUTE dispatch.
  useEffect(() => {
    function sync(kind) {
      const { tab, params } = parseHash()
      if (tab) {
        dispatch({ type: 'SET_ROUTE', tab, params, kind: kind === 'soft' ? '' : kind })
      } else {
        const here = isSection(uiRef.current.tab) ? uiRef.current.tab : 'needs'
        replaceRoute(here)
      }
    }
    sync(currentKind())
    return onRoute(sync)
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

  const Panel = PAGES[tab] || null

  return (
    <AppContext.Provider value={ctx}>
      <Shell>
        {Panel ? <Panel /> : null}
      </Shell>
      <ConfirmBar pending={pendingConfirm} onResolve={resolveConfirm} />
      <Toasts toasts={state.toasts} onDismiss={dismissToast} />
    </AppContext.Provider>
  )
}
