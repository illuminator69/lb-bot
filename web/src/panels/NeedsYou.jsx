import { useEffect, useMemo, useRef, useState } from 'react'
import { useApp, navigate, replaceRoute, goArtist, goAlbum, lsGet, lsSet } from '../App.jsx'
import { api } from '../lib/api.js'
import { entryBefore } from '../lib/router.js'
import { isListEntry, offeredOrAny, showsSelected } from '../lib/view.js'
import { hasKeptPlace, letGoOfPlace, useSessionState, useViewState } from '../lib/useView.js'
import { fixRowView, keepListed } from '../lib/artistMatch.js'
import { artistsTouched, keepSets, setRowView, toFixNote, toFixWaiting } from '../lib/merges.js'
import { failedGaps, failedRequests, foldersToFile, toFixCount } from '../lib/needs.js'
import { useNarrow } from '../lib/useNarrow.js'
import Cover from '../components/Cover.jsx'
import Page, { Dot, Grp, Meter, Pick, Seg } from '../components/Page.jsx'
import { ago, Chip, failWords, gapLine, Menu, MenuItem, Skeleton, STATUS } from '../components/ui.jsx'
import { forgetDiscography, loadArtists } from './Artist.jsx'
import ArtistMatch, { useOverridesRead } from './ArtistMatch.jsx'
import ArtistMerge from './ArtistMerge.jsx'
import GapDetail, { missingOf, ORIGIN_LABELS } from './GapDetail.jsx'
import { MatchToGap, PickRelease, PlacementResult } from './Placement.jsx'
import { RequestFailure, RequestProgress } from './requests.jsx'

// Needs you: the gaps queue first — it is what this page is used for most — and
// beside it To fix, where every failed fill, folder waiting to be filed and
// unmatched artist is gathered. Both are a list with the selected row's detail
// and its actions beside it.

// Which scan put an album in the review, widest first.
const ORIGIN_ORDER = ['library', 'playlist', 'spotify', 'repair']

// 166 albums on the live library miss exactly one track; "fewest missing"
// is how you find them. The default keeps the server's order (artist A to Z).
const SORTS = [
  ['', 'Artist A to Z'],
  ['album', 'Album A to Z'],
  ['most', 'Most missing'],
  ['fewest', 'Fewest missing'],
  ['recent', 'Recently changed'],
]
const byText = f => (a, b) => f(a).localeCompare(f(b), undefined, { sensitivity: 'base' })
const SORTERS = {
  album: byText(g => g.album.replace(/^[^\p{L}\p{N}]+/u, '')),
  fewest: (a, b) => missingOf(a) - missingOf(b) || a.artist.localeCompare(b.artist),
  most: (a, b) => missingOf(b) - missingOf(a) || a.artist.localeCompare(b.artist),
  recent: (a, b) => (b.updatedAt || 0) - (a.updatedAt || 0),
}

// The list renders this many rows, growing as you scroll. It used to render
// all ~3100 at once on every poll.
const LIST_CHUNK = 150

// Watches one background task (a playlist scan) until it ends.
function useTaskWatch(taskId, onDone) {
  const [task, setTask] = useState(null)
  useEffect(() => {
    if (!taskId) { setTask(null); return }
    let dead = false
    let timer
    const tick = async () => {
      try {
        const t = await api(`/api/tasks/${taskId}`)
        if (dead) return
        setTask(t)
        if (t.status === 'complete' || t.status === 'error') { onDone?.(t); return }
      } catch { /* poll again */ }
      if (!dead) timer = setTimeout(tick, 3000)
    }
    tick()
    return () => { dead = true; clearTimeout(timer) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId])
  return task
}

// Start a scan from here — where its results land.
function ScanMenu({ scanTask, onStarted }) {
  const { action, pushToast } = useApp()
  const [status, setStatus] = useState(null)
  const [spotifyOpen, setSpotifyOpen] = useState(false)
  const [spotifyUrl, setSpotifyUrl] = useState('')
  useEffect(() => { api('/api/system/status').then(setStatus).catch(() => {}) }, [])
  const spotifyReady = !!status?.spotify?.configured
  const playlists = status?.listenbrainz?.playlists || []

  async function start(path, body, label) {
    try {
      const r = await action(path, body)
      pushToast(`${label} started`)
      onStarted?.(r.task_id, label)
    } catch (e) {
      if (e.status === 409) pushToast('That scan is already running', 'info')
      else pushToast(`${label} failed to start: ${e.message}`, 'error')
    }
  }

  if (spotifyOpen) {
    return (
      <form className="flex min-w-0 gap-1.5"
        onSubmit={e => {
          e.preventDefault()
          if (!spotifyUrl.trim()) return
          start('/api/spotify/scan', { playlist: spotifyUrl.trim() }, 'Spotify playlist scan')
          setSpotifyOpen(false); setSpotifyUrl('')
        }}>
        <input autoFocus className="min-w-0" style={{ width: 240 }} placeholder="Spotify playlist link or ID"
          aria-label="Spotify playlist link" value={spotifyUrl} onChange={e => setSpotifyUrl(e.target.value)} />
        <button className="primary" type="submit">Scan</button>
        <button type="button" aria-label="Cancel" onClick={() => setSpotifyOpen(false)}>✕</button>
      </form>
    )
  }
  return (
    <Menu label="Scan ▾" align="right" width={290} title="Look for missing tracks">
      {close => (
        <>
          <MenuItem disabled={!!scanTask} onClick={() => { close(); start('/api/scan-all', {}, 'Library scan') }}
            hint="Every album in Navidrome against its MusicBrainz tracklist">
            {scanTask ? 'Library scan (running)' : 'Library scan'}
          </MenuItem>
          <MenuItem onClick={() => { close(); start('/api/playlists/scan', {}, 'ListenBrainz playlist scan') }}
            hint={playlists.length ? playlists.join(', ') : 'Your ListenBrainz playlists'}>
            ListenBrainz playlists
          </MenuItem>
          <MenuItem disabled={!spotifyReady} onClick={() => { close(); setSpotifyOpen(true) }}
            hint={spotifyReady ? 'Paste a playlist link' : 'Needs SPOTIFY_CLIENT_ID and _SECRET on the container'}>
            Spotify playlist…
          </MenuItem>
        </>
      )}
    </Menu>
  )
}

// A running scan, at the top of the list it is filling.
function ScanStrip({ label, done, total, current, percent, onCancel }) {
  return (
    <div className="box mb-3 !p-2.5">
      <div className="mb-1.5 flex items-center gap-2">
        <span className="st run grow clip">
          {total ? `${label} ${done.toLocaleString()} of ${total.toLocaleString()}` : `${label}…`}
          {current ? ` · ${current}` : ''}
        </span>
        {onCancel && <button className="sm" onClick={onCancel}>Cancel</button>}
      </div>
      <Meter value={percent} />
    </div>
  )
}

// One row of either list.
function ListRow({ selected, onSelect, onMove, rowRef, cover, title, sub, status }) {
  return (
    <button className="row tap" role="option" aria-selected={selected} ref={rowRef}
      onClick={onSelect}
      onKeyDown={onMove && (e => {
        if (e.key === 'ArrowDown') { e.preventDefault(); onMove(1) }
        if (e.key === 'ArrowUp') { e.preventDefault(); onMove(-1) }
      })}>
      {cover}
      <span className="grow">
        <span className="t clip block">{title}</span>
        <span className="s clip block">{sub}</span>
      </span>
      {status}
    </button>
  )
}

// lb-bot's list of the artists to match and of the sets is on its way (Z5): a
// heading and three rows stand where the two groups will be. They come after
// the other lists, and arriving with nothing in their place they pushed "Left
// on disk" down by their whole height in one frame.
const ListsSkeleton = () => (
  <>
    <div className="grp fadein" style={{ minHeight: '1lh' }} aria-hidden="true">
      <Skeleton className="h-2.5 w-28 self-center" />
    </div>
    <div className="rows fadein" aria-busy="true" aria-label="Reading which artists need a look">
      {[0, 1, 2].map(i => (
        <div key={i} className="row" aria-hidden="true">
          <Skeleton className="!rounded-pill" style={{ width: 40, height: 40, flex: 'none' }} />
          <div className="grow"><Skeleton className="h-3" style={{ width: `${46 - i * 9}%` }} /><Skeleton className="mt-2 h-2.5 w-1/5" /></div>
        </div>
      ))}
    </div>
  </>
)

// ── Gaps to fill ─────────────────────────────────────────────────────────────
function useGapsQueue() {
  const { state, dispatch } = useApp()
  const { gaps, hiddenGaps, gapFilter, gapHidden, gapSearch } = state
  const [size, setSizeState] = useState(() => lsGet('lb.gapSize', 'all'))
  // ("Found by" is the queue's for the session, like its search — X2. Not the history entry's: a tab pressed and
  // pressed back is a fresh page, and the filter is not the page's.)
  // A kept one that the queue no longer offers is "any" (Y2): its last album of that origin is gone, or one
  // origin is left and the menu is not shown — and the queue would be filtered to nothing with nothing to clear it.
  const [keptOrigin, setOrigin] = useSessionState('gaps.origin', '')
  const origin = offeredOrAny(keptOrigin, gaps?.origins)
  const [sort, setSortState] = useState(() => lsGet('lb.gapSort', ''))
  const setSize = v => { setSizeState(v); lsSet('lb.gapSize', v === 'all' ? '' : v) }
  const setSort = v => { setSortState(v); lsSet('lb.gapSort', v) }

  const [draftSearch, setDraftSearch] = useState(gapSearch)
  useEffect(() => {
    if (draftSearch === gapSearch) return
    const t = setTimeout(() => dispatch({ type: 'SET_GAP_SEARCH', q: draftSearch }), 250)
    return () => clearTimeout(t)
  }, [draftSearch, gapSearch, dispatch])

  const list = gapHidden ? hiddenGaps : gaps
  const items = useMemo(() => {
    let rows = list?.items || []
    if (!gapHidden) {
      if (gapFilter === 'needs') rows = rows.filter(g => ['ready', 'picking', 'failed'].includes(g.status))
      if (gapFilter === 'working') rows = rows.filter(g => g.status === 'downloading')
      if (gapFilter === 'done') rows = rows.filter(g => g.status === 'complete')
      if (origin) rows = rows.filter(g => (g.origin || 'library') === origin)
    }
    if (size === 'small') rows = rows.filter(g => missingOf(g) <= 2)
    if (size === 'big') rows = rows.filter(g => missingOf(g) >= 3)
    const q = gapSearch.toLowerCase().trim()
    if (q) rows = rows.filter(g => g.artist.toLowerCase().includes(q) || g.album.toLowerCase().includes(q))
    if (SORTERS[sort]) rows = [...rows].sort(SORTERS[sort])
    return rows
  }, [list, gapFilter, gapHidden, origin, size, gapSearch, sort])

  return { list, items, size, setSize, origin, setOrigin, sort, setSort, draftSearch, setDraftSearch }
}

const noop = () => {}

function GapsTab({ q, routeId, onOpened, statusChips }) {
  const { state, dispatch, action, pushToast, refresh } = useApp()
  const { gaps, gapsStale, gapDetail, selGap, gapFilter, gapHidden, gapSearch, transfers } = state
  const { list, size, origin, sort } = q
  const narrow = useNarrow()

  // The album you are on stays in the list while you are on it. Picking a
  // source turns it from "needs you" into "working", which the filter would
  // drop: the row vanished and the page moved to the next album, taking the
  // download you had just started out of sight. It leaves when you do, or
  // when you change what the list shows.
  const filtersKey = `${gapFilter}|${origin}|${size}|${gapSearch}|${sort}|${gapHidden}`
  const stickyRef = useRef({ id: null, at: 0, filtersKey: '' })
  const filtered = q.items
  const selInFiltered = !!selGap && filtered.some(g => g.id === selGap)
  let items = filtered
  if (selGap && !selInFiltered && stickyRef.current.id === selGap && stickyRef.current.filtersKey === filtersKey) {
    const row = (list?.items || []).find(g => g.id === selGap)
    if (row) {
      const at = Math.min(stickyRef.current.at, filtered.length)
      items = [...filtered.slice(0, at), row, ...filtered.slice(at)]
    }
  }
  useEffect(() => {
    if (selInFiltered) stickyRef.current = { id: selGap, at: filtered.findIndex(g => g.id === selGap), filtersKey }
  })

  // A playlist scan started from the Scan menu, until it finishes.
  const { watchTask, watched } = q

  const listIdsRef = useRef([])
  // Re-home the cursor onto a visible album when filters change — but only once
  // a list that can speak to this id is here (gapsStale marks the window).
  const sel = (() => {
    if (selGap && items.some(g => g.id === selGap)) return selGap
    if (!list || gapsStale) return selGap
    // The album left the filtered list (a source was picked, it completed, it
    // was hidden) — land on whatever took its slot, the next in the queue.
    const wasAt = listIdsRef.current.indexOf(selGap)
    if (wasAt >= 0 && items.length) return items[Math.min(wasAt, items.length - 1)].id
    return items[0]?.id ?? null
  })()
  useEffect(() => {
    if (sel === selGap) listIdsRef.current = items.map(g => g.id)
  }, [items, sel, selGap])
  // On a narrow screen the route's album IS "the detail pane is open", so an
  // empty route stays empty: it is the list.
  const open = !!routeId
  useEffect(() => {
    if (sel && sel !== routeId && (!narrow || open)) replaceRoute('needs', 'gaps', sel)
  }, [sel, routeId, narrow, open])
  const idx = items.findIndex(g => g.id === sel)
  const focusRow = items[idx]
  const focus = gapDetail && gapDetail.id === sel ? gapDetail : null

  // Windowed list: enough rows to include the cursor, growing on scroll.
  // (Kept with the history entry — N1: the list comes back with as many rows
  // drawn as it had, so its place is there in the first frame. Back to the
  // first chunk when what is listed changes, not at mount.)
  const [limit, setLimit] = useViewState('gaps.rows', LIST_CHUNK)
  const listedFor = useRef('')
  useEffect(() => {
    const now = [gapFilter, origin, size, gapSearch, sort, gapHidden].join('|')
    const was = listedFor.current
    listedFor.current = now
    if (was && was !== now) setLimit(LIST_CHUNK)
  }, [gapFilter, origin, size, gapSearch, sort, gapHidden])
  const shownCount = Math.min(items.length, Math.max(limit, idx + 40))
  const sentinelRef = useRef(null)
  const listRef = useRef(null)
  useEffect(() => {
    const root = listRef.current
    const node = sentinelRef.current
    if (!root || !node) return
    // Grow from what is shown, not from `limit`: once the cursor sits past
    // ~260 rows, idx + 40 decides shownCount, a bump changed nothing, this
    // effect never re-ran, and the list stopped loading.
    const obs = new IntersectionObserver(([e]) => {
      if (e.isIntersecting) setLimit(n => Math.max(n, shownCount) + LIST_CHUNK)
    }, { root, rootMargin: '400px' })
    obs.observe(node)
    return () => obs.disconnect()
  }, [shownCount, items.length])

  const selRowRef = useRef(null)
  // The selected row is brought into view — but a list that has a saved place
  // on this arrival is not moved off it (N1g): it came back where it was left.
  const arrived = useRef(true)
  const selAtArrival = useRef(sel)
  if (!selAtArrival.current) selAtArrival.current = sel
  const selNow = useRef(sel)
  selNow.current = sel
  useEffect(() => {
    const first = arrived.current
    arrived.current = false
    if (first && hasKeptPlace(listRef.current)) return
    selRowRef.current?.scrollIntoView({ block: 'nearest' })
  }, [sel])
  // On a narrow screen the list is not laid out while a detail is open, and
  // nothing can be scrolled in a box that has no size: a list with no saved
  // place brings its selected row into view when it is shown (N1f).
  useEffect(() => {
    const root = listRef.current
    if (!root || typeof ResizeObserver !== 'function') return undefined
    let shown = root.clientHeight > 0
    const watch = new ResizeObserver(() => {
      const now = root.clientHeight > 0
      // A kept place is for the arrival it was kept for (X5): once another
      // album is selected — Next pressed in the pane — the list shows that one.
      if (now && !shown && showsSelected({ kept: hasKeptPlace(root), selAtArrival: selAtArrival.current, sel: selNow.current })) {
        letGoOfPlace(root)
        selRowRef.current?.scrollIntoView({ block: 'nearest' })
      }
      shown = now
    })
    watch.observe(root)
    return () => watch.disconnect()
  }, [])

  // "Jump to the current album" when the cursor has scrolled out of the list.
  const [cursorOffScreen, setCursorOffScreen] = useState(false)
  useEffect(() => {
    const root = listRef.current
    const row = selRowRef.current
    if (!root || !row) { setCursorOffScreen(false); return }
    const obs = new IntersectionObserver(([e]) => setCursorOffScreen(!e.isIntersecting), { root, threshold: 0.35 })
    obs.observe(row)
    return () => obs.disconnect()
  }, [sel, items])
  const jumpToCursor = () => selRowRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })

  function select(id) {
    if (narrow && !open) onOpened()
    navigate('needs', 'gaps', id)
  }
  function move(delta) {
    const next = items[(idx + delta + items.length) % items.length]
    if (next) navigate('needs', 'gaps', next.id)
  }

  // Hide = take the album out of the queue. Reversible under Show: Hidden.
  async function setHidden(groupId, hidden) {
    if (!groupId) return
    if (hidden && items.length > 1) move(1)
    try {
      await action(`/api/groups/${groupId}/hide`, { hidden })
      pushToast(hidden ? 'Hidden. Find it again under Show: Hidden' : 'Back in the queue')
      if (gapHidden) refresh({ silent: true, force: true })
    } catch (e) {
      pushToast(`${hidden ? 'Hide' : 'Unhide'} failed: ${e.message}`, 'error')
    }
  }

  const scanTask = gaps?.scanTask || null
  async function cancelScan() {
    if (!scanTask) return
    try {
      await action(`/api/tasks/${scanTask.id}/cancel`)
      pushToast('Cancelling the scan…')
    } catch (e) {
      pushToast(`Cancel failed: ${e.message}`, 'error')
    }
  }
  async function startLibraryScan() {
    try {
      await action('/api/scan-all')
      pushToast('Library scan started')
    } catch (e) {
      if (e.status === 409) pushToast('A scan is already running')
      else pushToast(`Scan failed to start: ${e.message}`, 'error')
    }
  }

  if (!list) return <div className="body"><p className="muted">{state.pollError ? 'Waiting for lb-bot…' : 'Loading the queue…'}</p></div>

  const total = list.counts?.[gapHidden ? 'hidden' : 'all'] ?? items.length
  return (
    <div className={`two${open ? ' open' : ''}`}>
      <div className="list" ref={listRef}>
        <div className="list-head">{statusChips}</div>
        {scanTask && (
          <ScanStrip label="Scanning" done={scanTask.done || 0} total={scanTask.total || 0} current={scanTask.current}
            percent={scanTask.total ? (scanTask.done / scanTask.total) * 100 : 4} onCancel={cancelScan} />
        )}
        {watchTask && (
          <ScanStrip label={watchTask.label} done={0} total={0} current={watched?.current} percent={watched?.percent || 4} />
        )}
        {items.length > 0 ? (
          <div className="rows arrive" key={filtersKey} role="listbox" aria-label="Albums with missing tracks">
            {items.slice(0, shownCount).map(g => {
              const st = gapLine(g)
              return (
                <ListRow key={g.id} selected={g.id === sel} rowRef={g.id === sel ? selRowRef : null}
                  onSelect={() => select(g.id)} onMove={move}
                  cover={<Cover albumId={g.albumId} url={g.coverUrl} name={g.album} size={40} />}
                  title={g.album} sub={`${g.artist} · ${st.note}`}
                  status={<span className={`st ${st.tone}`}>{st.word}</span>} />
              )
            })}
            {shownCount < items.length && <div ref={sentinelRef} className="row muted">Loading more…</div>}
          </div>
        ) : (
          <div className="empty">
            <p>{gapHidden ? 'No hidden albums.' : 'No album matches this filter.'}</p>
            {!gapHidden && !scanTask && gapFilter === 'needs' && !gapSearch && size === 'all' && !origin && (
              <button className="primary mt-3" onClick={startLibraryScan}>Scan the library for missing tracks</button>
            )}
          </div>
        )}
        {items.length > 0 && (
          <p className="muted mt-2.5 text-caption">Showing {items.length.toLocaleString()} of {total.toLocaleString()}</p>
        )}
        {cursorOffScreen && (
          <button className="sm sticky bottom-0 float-right" style={{ boxShadow: '0 6px 18px -8px rgba(0,0,0,.5)' }}
            onClick={jumpToCursor}>Jump to current</button>
        )}
      </div>
      <div className="detail swap" key={sel || 'none'}>
        {focusRow ? (
          <GapDetail key={focusRow.id} row={focusRow} detail={focus} transfers={transfers}
            pager={{ index: idx, total: items.length, onPrev: () => move(-1), onNext: () => move(1) }}
            onDetail={gap => dispatch({ type: 'GAP_UPDATED', gap })}
            onHide={hidden => setHidden(focusRow.id, hidden)} />
        ) : (
          <div className="empty">
            <p>{gapHidden ? 'Albums you hide land here.' : 'Nothing in the queue. A library scan finds albums with missing tracks.'}</p>
            {!gapHidden && !scanTask && (
              <button className="primary mt-3" onClick={startLibraryScan}>Scan the library for missing tracks</button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// ── To fix ───────────────────────────────────────────────────────────────────
const caaCover = rgid => rgid ? `https://coverartarchive.org/release-group/${rgid}/front-250` : null

// How a folder's release match was made. Absent for folders the bot queued.
const MATCH_SOURCE_LABEL = { tag_mbid: 'tags', tag_rgid: 'tags', tag_text: 'tags', folder_name: 'folder name' }

function folderLine(p) {
  if (p.filed) return p.filed === 'imported' ? 'Already filed, files still on disk' : 'Dismissed, files still on disk'
  const basis = p.matchBasis || MATCH_SOURCE_LABEL[p.matchSource]
  if (!p.matchLabel) return p.identifyError ? 'Could not be identified' : 'Not identified'
  if (p.canConfirm) return `Matches ${p.matchLabel}`
  return `Possible match${basis ? ` by ${basis} only` : ''}: ${p.matchLabel}`
}

// A whole-album request (an album the library does not have) that failed.
function RequestDetail({ f }) {
  return (
    <>
      <div className="hero">
        <Cover url={caaCover(f.rgid)} name={f.album || '?'} size={96} />
        <div className="grow">
          <h2>{f.album || f.releaseMbid}</h2>
          <p className="muted mt-1">
            {f.artist}{f.artist ? ' · ' : ''}whole album, not in the library
            {f.updatedAt ? ` · ${ago(f.updatedAt)} ago` : ''}
          </p>
        </div>
      </div>
      {f.state === 'failed' ? (
        <RequestFailure f={f} extra={f.rgid && (
          <>
            <button onClick={() => goAlbum(f.rgid, true)}>Pick a source</button>
            <button className="quiet" onClick={() => goAlbum(f.rgid)}>Open album</button>
          </>
        )} />
      ) : (
        // Tried again: it stays here, and this is where it has got to.
        <div className="fadein">
          <Grp>Source<span className="note">{f.state}</span></Grp>
          <RequestProgress f={f} />
          {f.rgid && <div className="acts mt-3"><button onClick={() => goAlbum(f.rgid)}>Open album</button></div>}
        </div>
      )}
    </>
  )
}

// A downloaded folder waiting to be filed into the library.
function FolderDetail({ p, onGone }) {
  const { action, confirmAction, pushToast } = useApp()
  const [busy, setBusy] = useState(false)
  const [filing, setFiling] = useState(false)
  const [taskId, setTaskId] = useState('')
  // null = the summary and its one-tap actions; 'gap' / 'pick' = a picker open below.
  const [mode, setMode] = useState(null)
  const diff = p.diff || {}
  const leftover = !!p.filed
  const possible = p.matchLabel && !p.canConfirm
  const basis = p.matchBasis || MATCH_SOURCE_LABEL[p.matchSource]

  async function dismiss() {
    setBusy(true)
    try {
      await action(`/api/placements/${p.id}/dismiss`)
      pushToast('Dismissed. The files stay on disk')
      onGone?.()
    } catch (e) {
      pushToast(`Dismiss failed: ${e.message}`, 'error')
    } finally { setBusy(false) }
  }

  async function deleteFiles() {
    try {
      const r = await confirmAction(
        `Delete “${p.name || p.path}” and all its files from the downloads folder? This cannot be undone.`,
        `/api/placements/${p.id}/delete`, { confirm: true },
        { confirmLabel: 'Delete folder', danger: true })
      if (r) { pushToast('Folder deleted from downloads'); onGone?.() }
    } catch (e) {
      pushToast(`Delete failed: ${e.message}`, 'error')
    }
  }

  async function confirm() {
    setBusy(true)
    try {
      // Say which match was confirmed: with no body the server re-derived one
      // at click time, which need not be the one shown here.
      const r = await action(`/api/placements/${p.id}/confirm`,
        p.groupId ? { groupId: p.groupId } : p.releaseMbid ? { releaseMbid: p.releaseMbid } : {})
      setFiling(true)
      setTaskId(r.task_id || '')
      if (r.alreadyActive) pushToast('Already being filed')
    } catch (e) {
      pushToast(`Filing failed: ${e.message}`, 'error')
    } finally { setBusy(false) }
  }

  const why = leftover
    ? `${p.filed === 'imported' ? 'This folder was already filed into the library' : 'You dismissed this folder'}, and ${p.fileCount} file(s) are still on disk. Placement moves the tracks it matched and leaves the rest. Delete the folder to reclaim the space, or file it again if something was missed.`
    : !p.matchLabel
      ? (p.identifyError ? `Identifying it failed: ${p.identifyError}. Pick the release yourself.`
        : p.identified ? 'Nothing in the tags or the folder name identifies a release. Pick one.'
        : 'Not looked up yet. Identify it, or pick the release yourself.')
      : p.canConfirm
        ? `Every sign points at ${p.matchLabel}${basis ? `, matched on its ${basis}` : ''}. ${diff.filesFound} file(s) in the folder${diff.trackCount ? `, the release has ${diff.trackCount} track(s)` : ''}${p.match ? `, filling up to ${diff.willFill} missing` : ''}.`
        : `Matched to ${p.matchLabel}${basis ? ` by ${basis} only` : ''}. That is not enough to file on one tap, so look before filing.`

  return (
    <>
      <div className="hero">
        <Cover url={p.coverUrl || null} name={p.matchLabel || p.name} size={96} />
        <div className="grow">
          <h2>{p.name}</h2>
          <p className="muted mt-1">{p.fileCount} {p.formats || ''} file(s)</p>
          <p className="num muted mt-0.5" style={{ overflowWrap: 'anywhere' }}>{p.path}</p>
        </div>
      </div>
      <p className={`why${leftover ? ' quiet' : ''}`}>{why}</p>
      {filing ? (
        <div className="fadein">
          {taskId
            ? <PlacementResult taskId={taskId} onDone={onGone} />
            : <p><span className="st run">Filing into the library…</span></p>}
        </div>
      ) : (
        <div className="acts">
          {leftover ? (
            <button onClick={() => setMode('gap')}>File again</button>
          ) : p.canConfirm ? (
            <>
              <button className="primary" disabled={busy} onClick={confirm}>File into library</button>
              {/* Not the suggested album: the picker with nothing chosen.
                  Opening it on the rejected group left one click from filing
                  into the album just turned down. */}
              <button disabled={busy} onClick={() => setMode(p.groupId ? 'gap-clear' : 'pick')}>Pick another release</button>
            </>
          ) : possible ? (
            <>
              <button className="primary" onClick={() => setMode(p.groupId ? 'gap' : 'pick')}>Review the match</button>
              <button onClick={() => setMode('pick')}>Pick another release</button>
            </>
          ) : (
            <>
              <button className="primary" onClick={() => setMode('pick')}>Pick the release</button>
              <button onClick={() => setMode('gap-clear')}>It fills gaps in an album I have</button>
            </>
          )}
          {!leftover && <button className="quiet" disabled={busy} title="Take it off this list. The files stay on disk" onClick={dismiss}>Dismiss</button>}
          <button className="danger" disabled={busy} title="Delete this folder from the downloads directory" onClick={deleteFiles}>
            Delete files
          </button>
        </div>
      )}
      {mode && !filing && (
        <div className="fadein" key={mode}>
          {mode === 'pick'
            ? <PickRelease path={p.path} file="" onMatchGap={() => setMode('gap-clear')} onDone={onGone} />
            : <MatchToGap path={p.path} initialGroupId={mode === 'gap' ? (p.groupId || '') : ''}
                onPickRelease={() => setMode('pick')} onDone={onGone} />}
        </div>
      )}
    </>
  )
}

// An artist whose MusicBrainz match is worth a look: lb-bot's own guess holds
// none of its albums, or lb-bot found nobody. The pane is the match picker
// (ArtistMatch): what lb-bot uses now, another artist to choose, and the scan
// that follows — it says why the artist is here, so the pane does not.
function ArtistDetail({ a, overrides, onRead }) {
  return (
    <>
      <div className="hero">
        <Cover name={a.name} size={96} round />
        <div className="grow">
          <h2>{a.name}</h2>
          <p className="muted mt-1">
            <Dot after={`${a.albums} album${a.albums === 1 ? '' : 's'} in the library`}>
              <button className="link" onClick={() => goArtist(a.ndArtistId)}>Open artist page</button>
            </Dot>
          </p>
        </div>
      </div>
      <ArtistMatch artist={{ id: a.ndArtistId, name: a.name }} indexMbid={null} overrides={overrides} onRead={onRead}
        // lb-bot stored the change, and again when its scan is over (or none was
        // started): the artist's page reads its discography afresh (F1).
        onChanged={() => forgetDiscography([a.ndArtistId])}
        onOpenArtist={() => goArtist(a.ndArtistId)} />
    </>
  )
}

// One artist the library has under two names (or more): a merge set, as lb-bot
// lists them. The pane is the merge itself (ArtistMerge) — the names, which is
// kept, what would change, the job. A set merged from here stays, and says so.
function TwoNamesDetail({ item, merges, art, onRead }) {
  const v = setRowView(item.set, item.moved)
  const first = item.set.artists[0]
  const n = item.set.artists.length
  return (
    <>
      <div className="hero">
        <Cover url={art?.get(first.ndArtistId) || null} name={first.name} size={96} round />
        <div className="grow">
          <h2>{first.name}</h2>
          <p className="muted mt-1">
            {item.moved ? v.word : `In your library under ${NAME_COUNT[n] || n} names, for one MusicBrainz artist`}
          </p>
        </div>
      </div>
      <ArtistMerge set={item.set} merged={!!item.moved} merges={merges} art={art} onRead={onRead}
        // A merge or its undo ended: both names' pages read afresh (F1).
        onChanged={job => forgetDiscography(artistsTouched('merge', job?.row || job?.startRow))} />
    </>
  )
}
const NAME_COUNT = ['no', 'one', 'two', 'three', 'four', 'five', 'six']

function IdentifyAction({ unidentified, identify }) {
  const { action, pushToast } = useApp()
  const [busy, setBusy] = useState(false)
  if (identify?.running) {
    const { done, total, current } = identify
    return <span className="st run">Identifying{total ? ` ${done} of ${total}` : ''}{current ? ` · ${current}` : ''}</span>
  }
  if (!unidentified) return null
  return (
    <button className="link" disabled={busy} title="Look each folder up on MusicBrainz, one request per second"
      onClick={async () => {
        setBusy(true)
        try {
          const r = await action('/api/placements/identify')
          pushToast(r.alreadyActive ? 'Already identifying' : 'Identifying the download folders…')
        } catch (e) {
          pushToast(`Identify failed: ${e.message}`, 'error')
        } finally { setBusy(false) }
      }}>
      Identify {unidentified}
    </button>
  )
}

const ARTISTS_SHOWN = 6

function FixTab({ routeId, onOpened }) {
  const { state, dispatch, action, pushToast } = useApp()
  const { gaps, fills, transfers, gapDetail } = state
  const narrow = useNarrow()

  // Artists to match ride on the overrides answer: read once per visit, and
  // again — behind what is on the page — when the picker has changed a match.
  const overrides = useOverridesRead()
  // An artist that was listed on this visit stays listed, in its place, when
  // it leaves lb-bot's list — it has been matched from here — and says so
  // (`moved`), until the tab is left. One that comes back (the match was
  // removed again) is simply listed as before. `keepListed` (lib) decides,
  // with its matrix; the ref is the list as the page last had it.
  const artsRef = useRef([])
  const arts = useMemo(() => {
    artsRef.current = keepListed(artsRef.current, overrides.state.data)
    return artsRef.current
  }, [overrides.state.data])
  const [allArtists, setAllArtists] = useViewState('fix.allArtists', false)
  // One artist, two names: the merge sets ride on the same answer and are kept
  // the same way — a set merged from here stays, in its place, saying so
  // (`keepSets`, lib/merges.js). Listed, and counted nowhere but beside their
  // own heading: like the artists, they are not something lb-bot is stuck on.
  const setsRef = useRef([])
  const sets = useMemo(() => {
    setsRef.current = keepSets(setsRef.current, overrides.state.data)
    return setsRef.current
  }, [overrides.state.data])
  const [allSets, setAllSets] = useViewState('fix.allSets', false)
  // Their portraits: the artist list's, read only when there is a set to show.
  const [art, setArt] = useState(null)
  const anySets = sets.length > 0
  useEffect(() => {
    if (!anySets) return undefined
    let dead = false
    loadArtists().then(list => { if (!dead) setArt(new Map(list.map(a => [a.id, a.coverUrl]))) }).catch(() => {})
    return () => { dead = true }
  }, [anySets])

  // Folders already filed or dismissed that are still on disk, on demand.
  const [leftovers, setLeftovers] = useState(null)
  const [leftError, setLeftError] = useState('')
  const [leftLoading, setLeftLoading] = useState(false)
  async function loadLeftovers() {
    setLeftLoading(true)
    setLeftError('')
    try {
      const r = await api('/api/placements?all=1')
      setLeftovers((r.items || []).filter(i => i.filed))
    } catch (e) { setLeftError(e.message || 'failed') }
    finally { setLeftLoading(false) }
  }

  const groups = useMemo(() => {
    const failed = [
      ...failedGaps(gaps).map(g => ({ key: `gap:${g.id}`, kind: 'gap', g })),
      ...failedRequests(fills).map(f => ({ key: `req:${f.releaseMbid}`, kind: 'req', f })),
    ]
    const folders = foldersToFile(transfers).map(p => ({ key: `dir:${p.id}`, kind: 'dir', p }))
    const left = (leftovers || []).map(p => ({ key: `left:${p.id}`, kind: 'dir', p }))
    return { failed, folders, arts, twos: sets, left }
  }, [gaps, fills, transfers, arts, sets, leftovers])

  const shownArts = allArtists ? groups.arts : groups.arts.slice(0, ARTISTS_SHOWN)
  const shownSets = allSets ? groups.twos : groups.twos.slice(0, ARTISTS_SHOWN)
  const ready = !!gaps && !!transfers
  const lists = { failed: groups.failed, folders: groups.folders, arts: shownArts, twos: shownSets, left: groups.left }

  // The item you are on stays while you are on it. Trying the next source
  // makes a failed fill a running one, and filing a folder takes it off the
  // to-file list: both left this list at once and the page moved on, so what
  // you had just started was nowhere to be seen. It is kept where it was, with
  // what is now true of it, until you pick something else.
  const stuckRef = useRef(null)     // { key, group, at, item }
  // Items the user sent away themselves (dismissed, deleted, or closed once
  // filed): those do leave.
  const releasedRef = useRef(new Set())
  let found = null
  for (const [group, rows] of Object.entries(lists)) {
    const at = rows.findIndex(i => i.key === routeId)
    if (at >= 0) { found = { key: routeId, group, at, item: rows[at] }; break }
  }
  let kept = null
  if (!found && ready && routeId && stuckRef.current?.key === routeId && !releasedRef.current.has(routeId)) {
    const was = stuckRef.current
    const now = was.item.kind === 'gap' ? (gaps.items || []).find(g => g.id === was.item.g.id)
      : was.item.kind === 'req' ? fills?.albums?.[was.item.f.releaseMbid]
      : null
    const item = was.item.kind === 'gap' ? (now ? { ...was.item, g: now, moved: true } : null)
      : was.item.kind === 'req' ? { ...was.item, f: now && now.state !== 'unknown' ? now : was.item.f, moved: true }
      : was.item.kind === 'dir' ? { ...was.item, moved: true }
      // An artist or a set is still listed, only not among the few shown: it
      // is what its own list says of it now, not "matched" for being folded away.
      : groups[was.group]?.find(i => i.key === was.key) || was.item
    if (item) {
      kept = { ...was, item }
      const rows = lists[was.group].slice()
      rows.splice(Math.min(was.at, rows.length), 0, item)
      lists[was.group] = rows
    }
  }
  useEffect(() => { if (found) stuckRef.current = found })
  const all = [...lists.failed, ...lists.folders, ...lists.arts, ...lists.twos, ...lists.left]
  // An artist or a set the address names is not "gone" while lb-bot's list of
  // them is still on its way: arriving at one (a reload, a link) used to land
  // on the first thing in the list instead, because that list comes after the
  // others. Until it is read — or could not be — nothing is selected.
  const listsRead = overrides.state.status === 'ok' || overrides.state.status === 'error'
  const awaited = !listsRead && /^(art|two):/.test(routeId)
  // Something that is simply gone (deleted, dismissed): land on what took its slot.
  const orderRef = useRef([])
  const sel = found?.item || kept?.item
    || (!ready || awaited ? null : (() => {
        const at = orderRef.current.indexOf(routeId)
        return all[at >= 0 ? Math.min(at, all.length - 1) : 0] || null
      })())
  useEffect(() => { if (found || kept) orderRef.current = all.map(i => i.key) })
  const open = !!routeId
  useEffect(() => {
    if (!ready) return
    if (sel && sel.key !== routeId && (!narrow || open)) replaceRoute('needs', 'fix', sel.key)
    if (!sel && routeId && !awaited) replaceRoute('needs', 'fix')
  }, [sel, routeId, ready, narrow, open, awaited])

  function select(key) {
    if (narrow && !open) onOpened()
    navigate('needs', 'fix', key)
  }
  function move(delta) {
    const at = all.findIndex(i => i.key === sel?.key)
    const next = all[(at + delta + all.length) % all.length]
    if (next) navigate('needs', 'fix', next.key)
  }

  async function hideGap(g, hidden) {
    try {
      await action(`/api/groups/${g.id}/hide`, { hidden })
      pushToast(hidden ? 'Hidden. Find it again in the queue under Show: Hidden' : 'Back in the queue')
    } catch (e) {
      pushToast(`${hidden ? 'Hide' : 'Unhide'} failed: ${e.message}`, 'error')
    }
  }

  if (!ready) return <div className="body"><p className="muted">{state.pollError ? 'Waiting for lb-bot…' : 'Loading…'}</p></div>

  const row = i => {
    const selected = i.key === sel?.key
    const common = { selected, onSelect: () => select(i.key), onMove: move }
    if (i.kind === 'gap') {
      const g = i.g
      const st = gapLine(g)
      return <ListRow key={i.key} {...common}
        cover={<Cover albumId={g.albumId} url={g.coverUrl} name={g.album} size={40} />}
        title={g.album} sub={`${g.artist} · ${g.status === 'failed' ? `${missingOf(g)} missing` : st.note}`}
        status={<span className={`st ${st.tone}`} aria-label={st.word} title={st.word} />} />
    }
    if (i.kind === 'req') {
      const f = i.f
      const failed = f.state === 'failed'
      const meta = STATUS[f.state] || { tone: '', word: f.state }
      return <ListRow key={i.key} {...common}
        cover={<Cover url={caaCover(f.rgid)} name={f.album || '?'} size={40} />}
        title={f.album || f.releaseMbid}
        sub={`${f.artist ? `${f.artist} · ` : ''}${failed ? failWords(f.failureKind) : meta.word.toLowerCase()}`}
        status={<span className={`st ${failed ? 'bad' : meta.tone}`} aria-label={meta.word} title={meta.word} />} />
    }
    if (i.kind === 'dir') {
      const p = i.p
      return <ListRow key={i.key} {...common}
        cover={<Cover url={p.coverUrl || null} name={p.matchLabel || p.name} size={40} />}
        title={p.name} sub={folderLine(p)}
        status={<span className={`st ${i.moved ? 'run' : p.filed ? '' : 'wait'}`}
          aria-label={i.moved ? 'being filed' : p.filed ? 'left on disk' : 'to file'} />} />
    }
    if (i.kind === 'two') {
      const v = setRowView(i.set, !!i.moved)
      const first = i.set.artists[0]
      return <ListRow key={i.key} {...common}
        cover={<Cover url={art?.get(first.ndArtistId) || null} name={first.name} size={40} round />}
        title={v.title} sub={v.sub}
        status={<span className={`st ${v.tone}`} aria-label={v.word} title={v.word} />} />
    }
    const a = i.a
    const v = fixRowView(a, !!i.moved)
    return <ListRow key={i.key} {...common}
      cover={<Cover name={a.name} size={40} round />}
      title={a.name} sub={v.sub}
      status={<span className={`st ${v.tone}`} aria-label={v.word} title={v.word} />} />
  }

  const nothing = !lists.failed.length && !lists.folders.length && !lists.arts.length && !lists.twos.length
  // lb-bot did not give its list of artists to match, or of sets — or gave no
  // answer: said as that (lib `toFixNote`), never as "nothing needs a decision".
  // While a read is out, the lists are not held and the page draws neither of
  // the two groups — the first read, a Try again from a page that has neither
  // — a skeleton stands in their place, and neither the note nor "nothing" is
  // said (`toFixWaiting`). With a group on the page a read moves nothing (F3):
  // the note stays as it is until the read's answer replaces it.
  const drawn = lists.arts.length > 0 || lists.twos.length > 0
  const waiting = toFixWaiting(overrides.state, drawn)
  const unlisted = toFixNote(overrides.state, drawn)
  // The note's Try again shows that it was PRESSED, for as long as the read
  // the press asked for is out (`asked`, the read's own). A read nobody asked
  // for leaves the note exactly as it is, its button too (a spinner in a
  // sentence is eighteen pixels that can wrap the line under it).
  const pressed = !!overrides.state.asked
  return (
    <div className={`two${open ? ' open' : ''}`}>
      <div className="list" role="listbox" aria-label="Things to fix">
        {nothing && !unlisted && !waiting && (
          <p className="empty">Nothing needs a decision. Failed fills, folders to file, unmatched artists and artists under two names show up here.</p>
        )}
        {lists.failed.length > 0 && (
          <>
            <Grp n={groups.failed.length}>Failed or stuck</Grp>
            <div className="rows">{lists.failed.map(row)}</div>
          </>
        )}
        {(lists.folders.length > 0 || transfers.identify?.running) && (
          <>
            <Grp n={groups.folders.length}
              right={<IdentifyAction unidentified={transfers.counts?.unidentified ?? 0} identify={transfers.identify} />}>
              Folders to file
            </Grp>
            <div className="rows">{lists.folders.map(row)}</div>
          </>
        )}
        {/* The count is lb-bot's — the artists still to match; the rows are the
            page's, which keeps the ones matched on this visit. */}
        {lists.arts.length > 0 && (
          <>
            <Grp n={groups.arts.filter(i => !i.moved).length || ''}
              right={groups.arts.length > ARTISTS_SHOWN && (
                <button className="link" onClick={() => setAllArtists(v => !v)}>{allArtists ? 'Show fewer' : 'Show all'}</button>
              )}>
              Artists to match
            </Grp>
            <div className="rows">{lists.arts.map(row)}</div>
          </>
        )}
        {lists.twos.length > 0 && (
          <>
            <Grp n={groups.twos.filter(i => !i.moved).length || ''}
              right={groups.twos.length > ARTISTS_SHOWN && (
                <button className="link" onClick={() => setAllSets(v => !v)}>{allSets ? 'Show fewer' : 'Show all'}</button>
              )}>
              One artist, two names
            </Grp>
            <div className="rows">{lists.twos.map(row)}</div>
          </>
        )}
        {waiting && <ListsSkeleton />}
        {unlisted && (
          <p className="muted text-caption mt-3 fadein">
            {unlisted}{' '}
            {/* It shows that it was pressed while that read is out, and keeps the
                keyboard's place (`aria-disabled`: a disabled button drops it);
                a second press sends nothing. */}
            <button className="link" aria-busy={pressed} aria-disabled={pressed || undefined}
              onClick={pressed ? undefined : () => overrides.read({ asked: true })}>Try again</button>
          </p>
        )}
        <Grp n={leftovers ? leftovers.length : ''}
          right={<button className="link" disabled={leftLoading} aria-busy={leftLoading} onClick={loadLeftovers}>
            {leftovers ? 'Check again' : 'Check'}
          </button>}>
          Left on disk
        </Grp>
        {leftError ? <p className="bad text-caption">Couldn’t list them: {leftError}.</p>
          : leftovers === null ? <p className="muted text-caption">Folders already filed or dismissed whose files are still in the downloads folder.</p>
          : !leftovers.length ? <p className="muted text-caption">Nothing left behind in the downloads folder.</p>
          : <div className="rows">{lists.left.map(row)}</div>}
      </div>
      <div className="detail swap" key={sel?.key || 'none'}>
        {!sel ? (
          <p className="empty">Nothing selected.</p>
        ) : sel.kind === 'gap' ? (
          <GapDetail key={sel.g.id} row={sel.g} detail={gapDetail && gapDetail.id === sel.g.id ? gapDetail : null}
            transfers={transfers} onDetail={gap => dispatch({ type: 'GAP_UPDATED', gap })}
            onHide={hidden => hideGap(sel.g, hidden)} />
        ) : sel.kind === 'req' ? (
          <RequestDetail key={sel.key} f={sel.f} />
        ) : sel.kind === 'dir' ? (
          <FolderDetail key={sel.key} p={sel.p}
            onGone={() => { releasedRef.current.add(sel.key); if (sel.p.filed) loadLeftovers() }} />
        ) : sel.kind === 'two' ? (
          <TwoNamesDetail key={sel.key} item={sel} merges={overrides.state.data?.merges} art={art} onRead={overrides.read} />
        ) : (
          <ArtistDetail key={sel.key} a={sel.a} overrides={overrides.state} onRead={overrides.read} />
        )}
      </div>
    </div>
  )
}

// ── The page ─────────────────────────────────────────────────────────────────
export default function NeedsYou() {
  const { state, dispatch, pushToast, refresh } = useApp()
  const { gaps, gapFilter, gapHidden, routeParams } = state
  const tab = routeParams[0] === 'fix' ? 'fix' : 'gaps'
  const routeId = routeParams[1] || ''
  const narrow = useNarrow()
  const q = useGapsQueue()

  // A playlist scan started from the Scan menu, until it finishes.
  const [watchTask, setWatchTask] = useState(null)   // { id, label }
  const watched = useTaskWatch(watchTask?.id, t => {
    pushToast(t.status === 'error' ? `${watchTask?.label} failed: ${t.error}` : `${watchTask?.label}: ${t.summary || 'done'}`,
      t.status === 'error' ? 'error' : 'info')
    setWatchTask(null)
    refresh({ silent: true, force: true })
  })

  // On a narrow screen a row tapped in the list can be left with Back.
  // Back to the list's own entry when that is the entry before this one —
  // asked of the router (N1f): a ref set by the tap was false again once the
  // page had been left and come back, and the pane's Back then replaced the
  // detail's entry with a second list entry, at the list's top.
  const paneBack = narrow && routeId ? {
    label: 'Needs you',
    onBack: () => {
      if (isListEntry(entryBefore(), 'needs', tab)) history.back()
      else replaceRoute('needs', tab)
    },
  } : null

  const counts = gaps?.counts || {}
  const fixN = toFixCount(state)
  const failedN = state.summary?.gaps?.failed ?? failedGaps(gaps).length
  const foldersN = state.summary?.transfers?.needsPlacement ?? 0
  const sub = tab === 'gaps'
    ? (gaps ? `${(counts.needs ?? 0).toLocaleString()} albums with missing tracks` : '')
    : (fixN
        ? [failedN ? `${failedN} failed or stuck` : null, foldersN ? `${foldersN} folder${foldersN === 1 ? '' : 's'} to file` : null]
            .filter(Boolean).join(' · ')
        : 'Nothing to fix')

  const origins = gaps?.origins || {}
  const originOptions = Object.keys(origins).length > 1
    ? [['', 'Found by any scan'],
       ...ORIGIN_ORDER.filter(k => origins[k]).map(k => [k, `${ORIGIN_LABELS[k] || k} (${origins[k].toLocaleString()})`])]
    : null
  // What the queue shows, as buttons above the list. Each says how many.
  const show = gapHidden ? 'hidden' : gapFilter
  const setShow = v => (v === 'hidden'
    ? dispatch({ type: 'SET_GAP_HIDDEN', hidden: true })
    : dispatch({ type: 'SET_GAP_FILTER', filter: v }))
  const statusChips = (
    <div className="chips" role="group" aria-label="Show">
      {[['needs', 'Needs you'], ['working', 'Working'], ['done', 'Done'], ['', 'All'],
        ...(counts.hidden > 0 || gapHidden ? [['hidden', 'Hidden']] : [])].map(([k, label]) => (
        <Chip key={k || 'all'} active={show === k} count={counts[k || 'all'] ?? 0}
          title={k === 'hidden' ? 'Albums you hid from the queue' : undefined}
          onClick={() => show !== k && setShow(k)}>{label}</Chip>
      ))}
    </div>
  )

  const bar = tab === 'gaps' ? (
    <>
      <Seg label="How much is missing" value={q.size} onChange={q.setSize}
        options={[['all', 'All'], ['small', '1 or 2 missing'], ['big', '3 or more']]} />
      <input className="find" type="search" aria-label="Filter albums"
        placeholder={`Filter ${(counts.all ?? 0).toLocaleString()} albums`}
        value={q.draftSearch} onChange={e => q.setDraftSearch(e.target.value)} />
      {originOptions && !gapHidden && <Pick label="Found by" value={q.origin} onChange={q.setOrigin} options={originOptions} />}
      <Pick label="Sort" value={q.sort} onChange={q.setSort} options={SORTS} />
    </>
  ) : null

  return (
    <Page title="Needs you" sub={sub} paneBack={paneBack}
      action={tab === 'gaps' && (
        <ScanMenu scanTask={gaps?.scanTask || null}
          onStarted={(id, label) => id && label !== 'Library scan' && setWatchTask({ id, label })} />
      )}
      tabs={[['gaps', 'Gaps to fill', gaps ? (counts.needs ?? 0) : null], ['fix', 'To fix', fixN || null]]}
      tab={tab} onTab={k => navigate('needs', k)}
      bar={bar} flush>
      {tab === 'gaps'
        ? <GapsTab q={{ ...q, watchTask, watched }} routeId={routeId} statusChips={statusChips}
            onOpened={noop} />
        : <FixTab routeId={routeId} onOpened={noop} />}
    </Page>
  )
}
