import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useApp, navigate, replaceRoute, goArtist, lsGet, lsSet } from '../App.jsx'
import { api } from '../lib/api.js'
import Cover from '../components/Cover.jsx'
import { DuplicateFileSet } from '../components/Duplicates.jsx'
import {
  ago, Badge, Chip, EmptyState, EMPTY_SOURCE_FILTERS, failWords, filterSources, gapLine,
  Menu, MenuItem, Notice, Pager, ProgressBar, SectionHeader, SelectField, SourceFileList,
  SourceFilters, SourceFilesNote, SourceRow, StatusChip, useSourceFiles,
} from '../components/ui.jsx'

// The server pages sources ten at a time; the picker shows its pages as-is
// rather than paging locally over the first ten while claiming "N found".
const SOURCES_PER_PAGE = 10

// Which scan put an album in the review, widest first.
const ORIGIN_ORDER = ['library', 'playlist', 'spotify', 'repair']
const ORIGIN_LABELS = { library: 'Library scan', playlist: 'Playlists', spotify: 'Spotify', repair: 'Repairs' }

// 166 albums on the live library miss exactly one track; "fewest missing"
// is how you find them. The default keeps the server's order.
const SORTS = [
  ['', 'Default order'],
  ['fewest', 'Fewest missing first'],
  ['most', 'Most missing first'],
  ['artist', 'Artist A–Z'],
  ['recent', 'Recently changed'],
]
const missingOf = g => g.missingCount ?? (g.total - g.present)
const SORTERS = {
  fewest: (a, b) => missingOf(a) - missingOf(b) || a.artist.localeCompare(b.artist),
  most: (a, b) => missingOf(b) - missingOf(a) || a.artist.localeCompare(b.artist),
  artist: (a, b) => a.artist.localeCompare(b.artist) || a.album.localeCompare(b.album),
  recent: (a, b) => (b.updatedAt || 0) - (a.updatedAt || 0),
}

// The rail renders this many rows, growing as you scroll. It used to render
// all ~3100 at once on every poll.
const RAIL_CHUNK = 150

function fmtMB(n) { return n ? `${(n / 1024 / 1024).toFixed(1)} MB` : null }

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

// Per-album MP3 opt-in. Not a global setting: lb-bot's policy is flac/opus, and
// this relaxes it for one album with mp3 ranked last.
function Mp3Fallback({ detail, busy, onToggle }) {
  const allow = !!detail.allowMp3
  const stranded = detail.noFileInSourceCount || 0
  const wouldHelp = !!detail.mp3WouldHelp
  if (!allow && !stranded && !wouldHelp) return null
  return (
    <div className="mt-2 flex flex-wrap items-center gap-2 text-micro">
      {stranded > 0 && !allow && (
        <span style={{ color: 'var(--danger)' }}>{stranded} track(s) had no FLAC/Opus file in this source</span>
      )}
      {wouldHelp && !allow && !stranded && (
        <span style={{ color: 'var(--danger)' }}>The peers that answered had this album in MP3 only</span>
      )}
      {allow && <span className="chip !my-0 !py-0">MP3 allowed</span>}
      <button className="sm !py-0.5" disabled={busy} onClick={() => onToggle(!allow)}
        title="Accept MP3 for this album only, ranked below FLAC and Opus">
        {allow ? 'Require FLAC/Opus again' : 'Allow MP3 as a last resort'}
      </button>
    </div>
  )
}

// Duplicate files in *this* album, on demand.
function AlbumDuplicateFiles({ groupId }) {
  const { pushToast } = useApp()
  const [sets, setSets] = useState(null)
  const [checking, setChecking] = useState(false)

  const load = useCallback(async () => {
    setChecking(true)
    try {
      const r = await api(`/api/gaps/${groupId}/duplicate-files`)
      setSets(r.sets || [])
    } catch (e) {
      pushToast(`Duplicate check failed: ${e.message}`, 'error')
    } finally {
      setChecking(false)
    }
  }, [groupId, pushToast])

  useEffect(() => { setSets(null) }, [groupId])

  return (
    <div className="mt-6">
      <SectionHeader label="Duplicate files" action={
        <button type="button" className="link-inline !text-caption !text-muted"
          disabled={checking} aria-busy={checking} onClick={load}>
          {sets === null ? 'Check this album' : 'Check again'}
        </button>} />
      {sets === null ? (
        <p className="text-caption text-muted">Checks whether any song in this album exists twice on disk.</p>
      ) : !sets.length ? (
        <p className="text-caption text-muted">No duplicate files in this album.</p>
      ) : sets.map((s, i) => (
        <DuplicateFileSet key={`${s.albumId}-${s.title}-${i}`} set={s} onDeleted={load} />
      ))}
    </div>
  )
}

// One missing track, with the two escape hatches for when the matcher got it
// wrong: pick a file out of a specific source, and place-anyway.
function MissingTrackRow({ track: t, detail, sources, expandSource }) {
  const { action, pushToast, requestConfirm } = useApp()
  const [picking, setPicking] = useState(false)
  const [srcIdx, setSrcIdx] = useState(sources[0]?.id ?? 0)
  const [listing, setListing] = useState(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const canPick = t.index != null && sources.length > 0

  async function openPicker() {
    const next = !picking
    setPicking(next)
    if (next) await loadSource(srcIdx)
  }

  async function loadSource(id) {
    setSrcIdx(id)
    setListing(null)
    setLoading(true)
    try {
      setListing(await expandSource(id))
    } catch (e) {
      pushToast(`Could not read that source: ${e.message}`, 'error')
    } finally {
      setLoading(false)
    }
  }

  async function pick(file) {
    setBusy(true)
    try {
      await action(`/api/groups/${detail.id}/tracks/${t.index}/pick-file`,
        { sourceIndex: srcIdx, filename: file.peerFilename || file.filename })
      pushToast(`Queued ${file.filename} for “${t.title}”`)
      setPicking(false)
    } catch (e) {
      pushToast(`Could not queue that file: ${e.message}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  async function placeAnyway() {
    const ok = await requestConfirm(
      `Place this file anyway?\n\nThe album already contains the same audio as ` +
      `${t.forcePlaceConflict || 'another file'}, so this will add a second copy.`,
      { confirmLabel: 'Place anyway' })
    if (!ok) return
    setBusy(true)
    try {
      await action(`/api/groups/${detail.id}/tracks/${t.index}/place-anyway`)
      pushToast('Placed')
    } catch (e) {
      pushToast(`Place anyway failed: ${e.message}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mb-[7px] rounded-card border px-3.5 py-[11px]"
      style={{ background: 'var(--inset-warm)', borderColor: 'var(--hairline)' }}>
      <div className="flex items-center gap-3.5">
        <span className="w-6 font-mono text-caption text-faint">{t.position}</span>
        <div className="min-w-0 flex-1">
          <div className="text-body">{t.title}</div>
          {t.artist ? <div className="text-micro text-faint">{t.artist}</div> : null}
          {t.downloadError ? <div className="text-micro text-bad">{t.downloadError}</div> : null}
          {t.manualPick ? (
            <div className="text-micro text-faint" title={t.manualPick.filename}>hand-picked from @{t.manualPick.peer}</div>
          ) : null}
        </div>
        {canPick && (
          <button className="sm" aria-expanded={picking} onClick={openPicker}>{picking ? 'Close' : 'Pick a file…'}</button>
        )}
        {t.canForcePlace && (
          <button className="sm" disabled={busy} aria-busy={busy} onClick={placeAnyway}>Place anyway</button>
        )}
        <StatusChip status={t.state} />
      </div>
      {picking && (
        <div className="mt-2 rounded-ctl border border-line p-2.5" style={{ background: 'var(--inset-deep)' }}>
          <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
            <span className="text-micro text-faint">Source:</span>
            {sources.map(s => (
              <button key={s.id} className="sm !py-0.5"
                style={s.id === srcIdx ? { borderColor: 'var(--accent-bd-sel)' } : undefined}
                onClick={() => loadSource(s.id)}>@{s.peer}</button>
            ))}
          </div>
          {loading ? <div className="text-micro text-faint">Reading the peer’s folder…</div>
            : listing ? <SourceFileList src={listing} onPick={busy ? undefined : pick} />
            : <div className="text-micro text-faint">No listing yet.</div>}
        </div>
      )}
    </div>
  )
}

// Live view of the background source search for this album.
function SourceSearchCard({ task }) {
  const [, setTick] = useState(0)
  useEffect(() => {
    const t = setInterval(() => setTick(n => n + 1), 1000)
    return () => clearInterval(t)
  }, [])
  const elapsed = Math.max(0, Math.round(Date.now() / 1000 - (task.startedAt || 0)))
  return (
    <div className="workspace-card mt-6">
      <div className="mb-0.5 text-caption font-semibold uppercase tracking-[.1em]" style={{ color: 'var(--accent)' }}>Searching</div>
      <div className="text-title font-semibold">Looking for sources on Soulseek</div>
      <div className="mt-0.5 truncate text-small text-muted">{task.current || 'Waiting for peers to answer…'}</div>
      <div className="mt-4">
        {/* Elapsed against a nominal 60s: two 30s slskd passes is the worst
            case, so this reads as "still going" rather than promising a finish. */}
        <ProgressBar value={Math.min(95, (elapsed / 60) * 100)} />
      </div>
      <div className="mt-2 font-mono text-caption text-muted">{elapsed}s elapsed · peers answer on their own schedule</div>
    </div>
  )
}

// The auto-picked source, with the disclosure that answers "is this actually
// the right album?" before anything is downloaded.
function ChosenSourceCard({ src, busy, expandSource, onChangeSource }) {
  const { open, toggle, expanding, expandError, view, isFullFolder } = useSourceFiles(src, expandSource)
  return (
    <>
      <div className="flex flex-wrap items-center gap-x-3.5 gap-y-2.5 rounded-card border p-3.5"
        style={{ background: 'var(--inset-warm)', borderColor: 'var(--border-warm)' }}>
        <Badge format={src.format} size="lg" />
        <div className="min-w-[140px] flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            {[src.bitrate, src.size].filter(Boolean).map((tok, i) => (
              <span key={i} className="whitespace-nowrap font-mono text-body font-semibold">{tok}</span>
            ))}
            <span className="whitespace-nowrap text-small text-muted">@{src.peer}</span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-caption">
            <span className="whitespace-nowrap" style={{ color: 'var(--green)' }}>
              {src.coverage}{typeof src.coverage === 'number' ? ' tracks' : ''}
            </span>
            {src.speedMbps != null && <span className="whitespace-nowrap text-muted">{src.speedMbps} MB/s</span>}
            {src.recommended && <span className="chip good !my-0">✓ recommended</span>}
          </div>
        </div>
        <button className="ml-auto whitespace-nowrap" aria-expanded={open} aria-busy={expanding} onClick={toggle}>
          {open ? '▾ Hide files' : '▸ Show files'}
        </button>
        <button className="tint whitespace-nowrap font-semibold" disabled={busy} onClick={onChangeSource}>
          Change source
        </button>
      </div>
      {open && (
        <div>
          <SourceFilesNote expanding={expanding} expandError={expandError} isFullFolder={isFullFolder} />
          <SourceFileList src={view} />
        </div>
      )}
    </>
  )
}

function ActionCard({ detail, transfers, rescanAlbum, rescanning }) {
  const { action, pushToast } = useApp()
  const [busy, setBusy] = useState(false)
  const [picking, setPicking] = useState(false)
  const [srcPage, setSrcPage] = useState(0)
  // Sources beyond the first ten come from the server, a page at a time.
  const [pageData, setPageData] = useState(null)   // { page, sources } | null
  const [pageLoading, setPageLoading] = useState(false)
  const [srcFilters, setSrcFilters] = useState(EMPTY_SOURCE_FILTERS)
  const [lastError, setLastError] = useState(null)
  const [chosenId, setChosenId] = useState(null)
  const lastTriedRef = useRef(null)

  // Every source seen so far, by id, so a pick on page 3 still resolves.
  const seenRef = useRef(new Map())
  // A new search replaces the list the ids index into: pages fetched, sources
  // seen and the pick all belong to the old one. Kept across a re-search, the
  // picker showed the old page and "Use this" sent its index — which the
  // server resolved against the NEW results, a different peer and folder.
  const [resultsKey, setResultsKey] = useState(detail.sourcesFoundAt)
  if (resultsKey !== detail.sourcesFoundAt) {
    setResultsKey(detail.sourcesFoundAt)
    setPageData(null)
    setChosenId(null)
    setSrcPage(0)
    seenRef.current = new Map()
  }

  const status = detail.status
  const missing = detail.missingCount
  const firstPage = detail.sources || []
  const totalSources = detail.sourcesTotal ?? firstPage.length
  const pages = Math.max(1, Math.ceil(totalSources / SOURCES_PER_PAGE))
  const pageSources = srcPage === 0 ? firstPage : (pageData?.page === srcPage ? pageData.sources : [])
  for (const s of [...firstPage, ...(pageData?.sources || [])]) seenRef.current.set(s.id, s)
  const chosenSrc = (chosenId != null && seenRef.current.get(chosenId)) || firstPage[0]
  const foundAgo = ago(detail.sourcesFoundAt)
  const srcTask = detail.sourceTask
  const searching = srcTask?.status === 'running'
  const searchError = !firstPage.length && !searching
    ? (detail.noSourceReason
        ? `No usable source: ${detail.noSourceReason}`
        : (srcTask?.status === 'error' ? (srcTask.error || 'Source search failed') : null))
    : null

  useEffect(() => {
    // Nothing to fetch — and a fetch this page left in flight will never clear
    // the flag itself (its cleanup marked it dead), so clear it here.
    if (srcPage === 0 || pageData?.page === srcPage) { setPageLoading(false); return }
    let dead = false
    setPageLoading(true)
    api(`/api/gaps/${detail.id}?sourcePage=${srcPage}`)
      .then(r => { if (!dead) setPageData({ page: srcPage, sources: r.sources || [] }) })
      .catch(e => { if (!dead) pushToast(`Could not load more sources: ${e.message}`, 'error') })
      .finally(() => { if (!dead) setPageLoading(false) })
    return () => { dead = true }
  }, [srcPage, detail.id, pageData, pushToast])

  async function run(fn, failMsg) {
    setBusy(true)
    try {
      const r = await fn()
      setLastError(null)
      return r
    } catch (e) {
      setLastError(e.payload || { reason: e.message })
      pushToast(`${failMsg}: ${e.message}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  const fetchTracks = (sourceId = 0) => {
    lastTriedRef.current = sourceId
    return run(() => action(`/api/gaps/${detail.id}/fetch`, { sourceId }), 'Could not queue tracks')
  }
  const findSources = (force = false) =>
    run(() => action(`/api/groups/${detail.id}/sources`, { force }), 'Source search failed')
  const cancel = () => run(() => action(`/api/gaps/${detail.id}/cancel`), 'Cancel failed')
  const autoSelect = () => run(() => action(`/api/gaps/${detail.id}/auto`), 'Auto-select failed')
  const setAllowMp3 = (allow) =>
    run(() => action(`/api/gaps/${detail.id}/allow-mp3`, { allow }), 'Could not change the format policy')
  const reconcile = () =>
    run(async () => {
      const r = await action(`/api/groups/${detail.id}/reconcile-downloads`)
      pushToast('Reconciling downloaded files with the missing tracks…')
      return r
    }, 'Reconcile failed')

  function choose(id) {
    setChosenId(id)
    setPicking(false)
  }

  const expandSource = useCallback(
    (sourceId) => api(`/api/groups/${detail.id}/sources/${sourceId}/files`),
    [detail.id])

  // Progress for this album from the live transfers snapshot.
  const mine = (transfers?.transfers || []).filter(t => t.groupId === detail.id)
  // The peer serving the download can differ from the auto-pick after failover.
  const activeSrc = [...seenRef.current.values()].find(s => s.peer === mine[0]?.username) || chosenSrc
  const bytesDone = mine.reduce((s, t) => s + (t.bytesDone || 0), 0)
  const bytesTotal = mine.reduce((s, t) => s + (t.bytesTotal || 0), 0)
  const rate = mine.reduce((s, t) => s + (t.rate || 0), 0)
  const eta = Math.max(...mine.map(t => t.etaSeconds || 0), 0)
  const activePct = bytesTotal
    ? Math.round((bytesDone / bytesTotal) * 100)
    : (mine.length ? Math.round(mine.reduce((s, t) => s + (t.pct || 0), 0) / mine.length) : 0)
  const dlStat = [
    bytesTotal ? `${fmtMB(bytesDone)} / ${fmtMB(bytesTotal)}` : null,
    `${activePct}%`,
    rate ? `${(rate / 1024 / 1024).toFixed(1)} MB/s` : null,
    eta ? `~${eta >= 90 ? `${Math.ceil(eta / 60)} min` : `${Math.ceil(eta)}s`} left` : null,
  ].filter(Boolean).join(' · ')

  const stalled = !!detail.stalledPlacement
  // Tracks the server has queued or is downloading. The transfers snapshot can
  // be a poll behind the album (it is only fetched while something is known to
  // be moving), and without this the card said "filing into the album" with no
  // Cancel right after "Get N tracks".
  const inFlight = (detail.tracks || []).filter(t => t.state === 'queued' || t.state === 'downloading').length
  const err = lastError || (status === 'failed'
    ? { reason: failWords(detail.failReason), detail: detail.failDetail, logTail: detail.logTail }
    : null)

  // Next-best fallback when the server didn't hand one: the source ranked after
  // the last one tried.
  const ranked = [...seenRef.current.values()].sort((a, b) => (a.rank || a.id) - (b.rank || b.id))
  const lastIdx = ranked.findIndex(s => s.id === lastTriedRef.current)
  const nextSource = stalled ? null : (err?.nextSource
    || (ranked.length > 1 ? ranked[(lastIdx >= 0 ? lastIdx : 0) + 1] : null))
  const retryId = lastTriedRef.current ?? chosenSrc?.id

  const filteredSources = filterSources(pageSources, srcFilters)
  const showPicker = picking && status !== 'downloading' && status !== 'complete'

  return (
    <>
      {status === 'complete' && (
        <div className="mt-6"><EmptyState title="All tracks present" hint="Nothing to do here." /></div>
      )}

      {status === 'downloading' && (
        <div className="workspace-card mt-6">
          <div className="flex items-center gap-4">
            <div className="min-w-0 flex-1">
              <div className="mb-0.5 text-caption font-semibold uppercase tracking-[.1em]" style={{ color: 'var(--green)' }}>Working</div>
              <div className="text-title font-semibold">
                {mine.length
                  ? `Getting ${missing} track(s)${mine[0]?.username ? ` from @${mine[0].username}` : ''}`
                  : inFlight ? `Getting ${inFlight} track(s)`
                  : 'Downloaded — filing into the album'}
              </div>
              <div className="mt-0.5 text-small text-muted">
                {mine.length
                  ? <>{[activeSrc?.format, activeSrc?.bitrate, activeSrc?.size].filter(Boolean).join(' · ')}
                      {activeSrc ? ' — ' : ''}files into the album folder automatically when done.</>
                  : inFlight ? 'Queued with the peer — progress shows once the transfer starts.'
                  : 'Nothing is transferring; the downloaded files are being placed and checked in Navidrome.'}
              </div>
            </div>
            {(mine.length > 0 || inFlight > 0) && <button className="self-center" disabled={busy} onClick={cancel}>Cancel</button>}
          </div>
          {mine.length > 0 && (
            <>
              <div className="mt-4"><ProgressBar value={activePct} /></div>
              <div className="mt-2 font-mono text-caption text-muted">{dlStat}</div>
            </>
          )}
        </div>
      )}

      {searching && status !== 'downloading' && status !== 'complete' && <SourceSearchCard task={srcTask} />}

      {status !== 'downloading' && status !== 'complete' && !showPicker && (
        <>
          {err ? (
            <div className="mt-6 rounded-panel border p-5"
              style={{ background: 'var(--warn-tint)', borderColor: 'var(--danger-bd)' }}>
              <div className="flex items-start gap-3.5">
                <span className="flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-pill border text-title"
                  style={{ background: 'var(--danger-tint-2)', borderColor: 'var(--danger-bd)', color: 'var(--danger)' }}>!</span>
                <div className="min-w-0 flex-1">
                  <div className="mb-0.5 text-caption font-semibold uppercase tracking-[.1em]" style={{ color: 'var(--danger)' }}>
                    {stalled ? 'Stuck' : 'Download failed'}
                  </div>
                  <div className="text-title font-semibold">{err.reason}</div>
                  {err.detail ? <div className="mt-1 text-small" style={{ color: 'var(--danger-text)' }}>{err.detail}</div> : null}
                  {/* The normal card that shows this is hidden while stuck, so
                      a failed "Search for sources again" said nothing at all. */}
                  {stalled && searchError ? <div className="mt-1 text-small" style={{ color: 'var(--danger-text)' }}>Last search: {searchError}</div> : null}
                </div>
              </div>
              <div className="mt-4 flex flex-wrap gap-2.5">
                {stalled ? (
                  <>
                    {/* The files are already here; another source would fetch
                        them again. Matching them is the fix. */}
                    <button className="go !rounded-card !px-5 font-semibold" disabled={busy} onClick={reconcile}>
                      Reconcile downloaded files
                    </button>
                    {/* Only an album with a library copy has anything to rescan;
                        a playlist or repair group carries no album record. */}
                    {detail.albumId && (
                      <button className="!rounded-card" disabled={rescanning} aria-busy={rescanning} onClick={rescanAlbum}>
                        Rescan album
                      </button>
                    )}
                    {totalSources > 0
                      ? <button className="!rounded-card" disabled={busy} onClick={() => { setSrcPage(0); setPicking(true) }}>
                          Download again from another source
                        </button>
                      : <button className="!rounded-card" disabled={busy || searching} onClick={() => findSources(true)}>
                          Search for sources again
                        </button>}
                  </>
                ) : (
                  <>
                    {nextSource && (
                      <button disabled={busy} className="go !rounded-card !px-5 font-semibold"
                        onClick={() => { choose(nextSource.id); fetchTracks(nextSource.id) }}>
                        Try next best source →
                      </button>
                    )}
                    <button className="primary !rounded-card" disabled={busy}
                      onClick={() => { setSrcPage(0); setPicking(true) }}>
                      Pick a source manually
                    </button>
                    {retryId != null && (
                      <button className="!rounded-card" disabled={busy} onClick={() => fetchTracks(retryId)}>Retry same peer</button>
                    )}
                  </>
                )}
              </div>
              {err.logTail?.length ? (
                <div className="mt-3.5 rounded-card border p-3 font-mono text-caption leading-[1.7] text-muted"
                  style={{ background: 'var(--inset-warm)', borderColor: 'var(--border-warm)' }}>
                  {err.logTail.map((l, i) => <div key={i}>{l}</div>)}
                </div>
              ) : null}
            </div>
          ) : null}

          {!stalled && (
            <div className="workspace-card mt-6">
              <div className="flex flex-wrap items-center gap-[18px]">
                <div className="min-w-0 flex-1">
                  <div className="mb-2.5 text-caption font-semibold uppercase tracking-[.1em] text-muted">
                    Next step · fill {missing} track(s){chosenSrc ? ' · chosen source' : ''}
                  </div>
                  {chosenSrc ? (
                    <>
                      <ChosenSourceCard key={chosenSrc.id} src={chosenSrc} busy={busy}
                        expandSource={expandSource}
                        onChangeSource={() => { setSrcPage(0); setPicking(true) }} />
                      <div className="mt-2 text-micro text-faint">
                        {chosenId == null ? 'Auto-picked by your source ranking' : 'Picked by you'} ·{' '}
                        <button type="button" className="link-inline !text-muted"
                          onClick={() => navigate('Settings', 'sources')}>edit ranking</button>
                        {foundAgo && <> · found {foundAgo} ago</>}
                        {' · '}
                        <button type="button" className="link-inline !text-muted"
                          disabled={busy || searching} onClick={() => findSources(true)}>
                          {searching ? 'searching…' : 'search again'}
                        </button>
                      </div>
                      <Mp3Fallback detail={detail} busy={busy} onToggle={setAllowMp3} />
                    </>
                  ) : (
                    <>
                      {searchError && (
                        <div className="mb-3"><Notice tone="danger">Last search: {searchError}</Notice></div>
                      )}
                      <div className="flex flex-wrap gap-2.5">
                        <button className="primary" disabled={busy || searching} aria-busy={searching} onClick={() => findSources()}>
                          {searching ? 'Searching…' : 'Find sources on Soulseek'}
                        </button>
                        <button disabled={busy || searching} onClick={autoSelect}
                          title="Search, rank, and download the best source automatically">
                          Search and download the best
                        </button>
                      </div>
                      <Mp3Fallback detail={detail} busy={busy} onToggle={setAllowMp3} />
                    </>
                  )}
                </div>
                {chosenSrc && (
                  <div className="flex shrink-0 flex-col items-stretch gap-2 self-center">
                    <button className="btn-accent whitespace-nowrap" disabled={busy} aria-busy={busy}
                      onClick={() => fetchTracks(chosenSrc.id)}>
                      Get {missing} track(s) →
                    </button>
                    <button className="sm" disabled={busy} onClick={autoSelect}
                      title="Search again, rank, and download the best source automatically">
                      Search again and download the best
                    </button>
                  </div>
                )}
              </div>
            </div>
          )}
        </>
      )}

      {showPicker && (
        <div className="workspace-card mt-6">
          <div className="mb-1 flex flex-wrap items-center gap-3">
            <div className="text-title font-semibold">Choose a source</div>
            <span className="spacer" />
            <span className="text-caption text-faint">{totalSources} found · ranked by your preferences</span>
            <button onClick={() => setPicking(false)}>Cancel</button>
          </div>
          <div className="mb-3 text-caption text-muted">The top-ranked source is picked for you — choose any other if you prefer.</div>
          <div className="mb-3.5"><SourceFilters value={srcFilters} onChange={setSrcFilters} /></div>
          {pageLoading && <p className="text-caption text-muted">Loading sources {srcPage * SOURCES_PER_PAGE + 1}–{Math.min((srcPage + 1) * SOURCES_PER_PAGE, totalSources)}…</p>}
          {filteredSources.map(s => (
            <SourceRow key={s.id} src={s} busy={busy}
              selected={s.id === chosenSrc?.id} done={s.id === chosenSrc?.id} doneLabel="Selected"
              onExpand={expandSource}
              // While stuck the "Get N tracks" button is hidden, so a pick has
              // to download there and then or it goes nowhere.
              onUse={stalled ? id => { choose(id); fetchTracks(id) } : choose} />
          ))}
          {!totalSources && <p className="text-caption text-muted">No sources yet — run a search.</p>}
          {pageSources.length > 0 && !filteredSources.length && (
            <p className="text-caption text-muted">No sources on this page match these filters.</p>
          )}
          {pages > 1 && (
            <div className="mt-3 border-t pt-3.5" style={{ borderColor: 'var(--hairline)' }}>
              <Pager page={srcPage} pages={pages} onPage={setSrcPage} total={totalSources} pageSize={SOURCES_PER_PAGE} />
            </div>
          )}
        </div>
      )}

      <div className="mt-6">
        <SectionHeader label="Missing tracks" action={
          <button type="button" className="link-inline !text-caption !text-muted" onClick={reconcile}
            title="Match files already in the downloads folder against these tracks">
            Reconcile downloaded files
          </button>} />
        {(detail.tracks || []).filter(t => t.state !== 'present').map((t, i) => (
          <MissingTrackRow key={t.index ?? i} track={t} detail={detail} sources={firstPage} expandSource={expandSource} />
        ))}
      </div>

      <AlbumDuplicateFiles groupId={detail.id} />
    </>
  )
}

// Start a scan from here — where its results land. The ListenBrainz and Spotify
// scans used to live on Library and Advanced → Playlist.
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

  return (
    <>
      <Menu label="Scan ▾" buttonClass="sm" width={280} title="Start a scan for missing tracks">
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
      {spotifyOpen && (
        <form className="mt-2 flex w-full gap-1.5"
          onSubmit={e => {
            e.preventDefault()
            if (!spotifyUrl.trim()) return
            start('/api/spotify/scan', { playlist: spotifyUrl.trim() }, 'Spotify playlist scan')
            setSpotifyOpen(false); setSpotifyUrl('')
          }}>
          <input autoFocus className="min-w-0 flex-1 !py-1.5 text-caption" placeholder="Spotify playlist URL or ID"
            value={spotifyUrl} onChange={e => setSpotifyUrl(e.target.value)} />
          <button className="sm primary" type="submit">Scan</button>
          <button className="sm" type="button" onClick={() => setSpotifyOpen(false)}>✕</button>
        </form>
      )}
    </>
  )
}

export default function FillGaps() {
  const { state, dispatch, action, pushToast, refresh } = useApp()
  const { gaps, gapsStale, gapDetail, selGap, gapFilter, gapHidden, gapSearch, transfers } = state

  const [draftSearch, setDraftSearch] = useState(gapSearch)
  useEffect(() => {
    if (draftSearch === gapSearch) return
    const t = setTimeout(() => dispatch({ type: 'SET_GAP_SEARCH', q: draftSearch }), 250)
    return () => clearTimeout(t)
  }, [draftSearch, gapSearch, dispatch])

  const [gapOrigin, setGapOrigin] = useState('')
  const [sort, setSortState] = useState(() => lsGet('lb.gapSort', ''))
  const setSort = v => { setSortState(v); lsSet('lb.gapSort', v) }

  // A playlist scan started from the Scan menu, until it finishes.
  const [watchTask, setWatchTask] = useState(null)   // { id, label }
  const watched = useTaskWatch(watchTask?.id, t => {
    pushToast(t.status === 'error' ? `${watchTask?.label} failed: ${t.error}` : `${watchTask?.label}: ${t.summary || 'done'}`,
      t.status === 'error' ? 'error' : 'info')
    setWatchTask(null)
    refresh({ silent: true, force: true })
  })

  const items = useMemo(() => {
    let rows = gaps?.items || []
    if (!gapHidden) {
      if (gapFilter === 'needs') rows = rows.filter(g => ['ready', 'picking', 'failed'].includes(g.status))
      if (gapFilter === 'working') rows = rows.filter(g => g.status === 'downloading')
      if (gapFilter === 'done') rows = rows.filter(g => g.status === 'complete')
      if (gapOrigin) rows = rows.filter(g => (g.origin || 'library') === gapOrigin)
    }
    const q = gapSearch.toLowerCase().trim()
    if (q) rows = rows.filter(g => g.artist.toLowerCase().includes(q) || g.album.toLowerCase().includes(q))
    if (SORTERS[sort]) rows = [...rows].sort(SORTERS[sort])
    return rows
  }, [gaps, gapFilter, gapHidden, gapOrigin, gapSearch, sort])

  // Whole-corpus counts, except with an origin selected — then within it.
  const counts = useMemo(() => {
    if (!gapOrigin || gapHidden) return gaps?.counts || {}
    const rows = (gaps?.items || []).filter(g => (g.origin || 'library') === gapOrigin)
    return {
      ...gaps?.counts,
      all: rows.length,
      needs: rows.filter(g => ['ready', 'picking', 'failed'].includes(g.status)).length,
      working: rows.filter(g => g.status === 'downloading').length,
      done: rows.filter(g => g.status === 'complete').length,
    }
  }, [gaps, gapOrigin, gapHidden])
  const origins = gaps?.origins || {}
  const originChips = !gapHidden && Object.keys(origins).length > 1
    ? [['', 'All sources', gaps?.counts?.all ?? 0],
       ...ORIGIN_ORDER.filter(k => origins[k]).map(k => [k, ORIGIN_LABELS[k] || k, origins[k]])]
    : []

  const railIdsRef = useRef([])
  // Re-home the cursor onto a visible album when filters change — but only once
  // a list that can speak to this id is here (gapsStale marks the window).
  const sel = (() => {
    if (selGap && items.some(g => g.id === selGap)) return selGap
    if (!gaps || gapsStale) return selGap
    // The album left the filtered list (you picked a source, it completed, you
    // skipped it) — land on whatever took its slot, the next in the queue.
    const wasAt = railIdsRef.current.indexOf(selGap)
    if (wasAt >= 0 && items.length) return items[Math.min(wasAt, items.length - 1)].id
    return items[0]?.id ?? null
  })()
  useEffect(() => {
    if (sel === selGap) railIdsRef.current = items.map(g => g.id)
  }, [items, sel, selGap])
  useEffect(() => {
    if (sel !== selGap) replaceRoute('Fill gaps', sel)
  }, [sel, selGap])
  const idx = items.findIndex(g => g.id === sel)
  const focus = gapDetail && gapDetail.id === sel ? gapDetail : null
  const focusRow = items[idx]

  // Windowed rail: enough rows to include the cursor, growing on scroll.
  const [railLimit, setRailLimit] = useState(RAIL_CHUNK)
  useEffect(() => { setRailLimit(RAIL_CHUNK) }, [gapFilter, gapOrigin, gapSearch, sort, gapHidden])
  const shownCount = Math.min(items.length, Math.max(railLimit, idx + 40))
  const sentinelRef = useRef(null)
  const railRef = useRef(null)
  useEffect(() => {
    const root = railRef.current
    const node = sentinelRef.current
    if (!root || !node) return
    // Grow from what is shown, not from railLimit: once the cursor sits past
    // ~260 rows, idx + 40 decides shownCount, a railLimit bump changed nothing,
    // this effect never re-ran, and the rail stopped loading.
    const obs = new IntersectionObserver(([e]) => {
      if (e.isIntersecting) setRailLimit(n => Math.max(n, shownCount) + RAIL_CHUNK)
    }, { root, rootMargin: '400px' })
    obs.observe(node)
    return () => obs.disconnect()
  }, [shownCount, items.length])

  const selRowRef = useRef(null)
  useEffect(() => {
    selRowRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [sel])

  // "Jump to current album" when the cursor has scrolled out of the rail.
  const [cursorOffScreen, setCursorOffScreen] = useState(false)
  useEffect(() => {
    const root = railRef.current
    const row = selRowRef.current
    if (!root || !row) { setCursorOffScreen(false); return }
    const obs = new IntersectionObserver(([e]) => setCursorOffScreen(!e.isIntersecting), { root, threshold: 0.35 })
    obs.observe(row)
    return () => obs.disconnect()
  }, [sel, items])
  const jumpToCursor = () => selRowRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' })

  function move(delta) {
    const next = items[(idx + delta + items.length) % items.length]
    if (next) navigate('Fill gaps', next.id)
  }

  const [rescanning, setRescanning] = useState(false)
  async function rescanAlbum() {
    if (!sel || rescanning) return
    setRescanning(true)
    try {
      const r = await action(`/api/gaps/${sel}/rescan`)
      pushToast(r.foundOnDisk
        ? `Found ${r.foundOnDisk} file(s) on disk Navidrome hadn’t indexed — ${r.present}/${r.total} present`
        : `Re-checked — ${r.present}/${r.total} present`)
    } catch (e) {
      pushToast(`Rescan failed: ${e.message}`, 'error')
    } finally {
      setRescanning(false)
    }
  }

  // Skip = hide the group. Reversible from the Hidden chip.
  async function setHidden(groupId, hidden) {
    if (!groupId) return
    if (hidden) move(1)
    try {
      await action(`/api/groups/${groupId}/hide`, { hidden })
      pushToast(hidden ? 'Hidden — find it again under “Hidden”' : 'Back in the list')
    } catch (e) {
      pushToast(`${hidden ? 'Skip' : 'Unhide'} failed: ${e.message}`, 'error')
    }
  }

  const scanTask = gaps?.scanTask || null
  async function cancelScan() {
    if (!scanTask) return
    try {
      await action(`/api/tasks/${scanTask.id}/cancel`)
      pushToast('Cancelling scan…')
    } catch (e) {
      pushToast(`Cancel failed: ${e.message}`, 'error')
    }
  }
  async function startLibraryScan() {
    try {
      await action('/api/scan-all')
      pushToast('Library scan started')
    } catch (e) {
      if (e.status === 409) pushToast('Scan already running')
      else pushToast(`Scan failed to start: ${e.message}`, 'error')
    }
  }

  const scanBar = scanTask ? (
    <div className="rounded-card border border-line bg-panel p-3.5">
      <div className="mb-2 flex items-center gap-2.5">
        <span className="inline-block h-1.5 w-1.5 shrink-0 rounded-pill" style={{ background: 'var(--green)' }} />
        <span className="min-w-0 flex-1 truncate text-caption" style={{ color: 'var(--text2)' }}>
          {scanTask.total
            ? `Scanning ${scanTask.done}/${scanTask.total}${scanTask.current ? ` · ${scanTask.current}` : ''}`
            : 'Scanning library for missing tracks…'}
        </span>
        <button className="sm" onClick={cancelScan}>Cancel</button>
      </div>
      <ProgressBar value={scanTask.total ? (scanTask.done / scanTask.total) * 100 : 4} />
    </div>
  ) : null

  const watchBar = watchTask ? (
    <div className="rounded-card border border-line bg-panel p-3">
      <div className="truncate text-caption" style={{ color: 'var(--text2)' }}>
        {watchTask.label}{watched?.current ? ` · ${watched.current}` : '…'}
      </div>
      <div className="mt-1.5"><ProgressBar value={watched?.percent || 4} height={5} /></div>
    </div>
  ) : null

  if (!gaps) return <p className="text-caption text-muted">Loading gaps…</p>

  return (
    <div className="review-layout fill-gaps">
      <aside className="review-sidebar card !p-0" style={{ background: 'var(--rail)' }} aria-label="Albums with gaps">
        <div className="border-b border-line p-3.5 pb-2.5">
          <div className="flex items-center gap-2">
            <input className="min-w-0 flex-1" type="search"
              aria-label="Filter albums"
              placeholder={`Filter ${(gaps.counts?.all ?? 0).toLocaleString()} albums…`}
              value={draftSearch} onChange={e => setDraftSearch(e.target.value)} />
            <ScanMenu scanTask={scanTask} onStarted={(id, label) => id && label !== 'Library scan' && setWatchTask({ id, label })} />
          </div>
          <div className="mt-3 flex flex-wrap gap-1.5">
            {[['needs', 'Needs you', counts.needs ?? 0],
              ['working', 'Working', counts.working ?? 0],
              ['done', 'Done', counts.done ?? 0],
              ['', 'All', counts.all ?? 0]].map(([k, label, count]) => (
              <Chip key={k || 'all'} variant="solid" active={!gapHidden && gapFilter === k} count={count}
                onClick={() => dispatch({ type: 'SET_GAP_FILTER', filter: k })}>{label}</Chip>
            ))}
            {(counts.hidden > 0 || gapHidden) && (
              <Chip variant="solid" active={gapHidden} count={counts.hidden ?? 0}
                title="Albums you skipped"
                onClick={() => dispatch({ type: 'SET_GAP_HIDDEN', hidden: !gapHidden })}>Hidden</Chip>
            )}
          </div>
          {originChips.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {originChips.map(([k, label, count]) => (
                <Chip key={k || 'all'} active={gapOrigin === k} count={count} onClick={() => setGapOrigin(k)}>{label}</Chip>
              ))}
            </div>
          )}
          <div className="mt-2.5"><SelectField label="Sort" value={sort} options={SORTS} onChange={setSort} /></div>
          {scanBar && <div className="mt-3">{scanBar}</div>}
          {watchBar && <div className="mt-2">{watchBar}</div>}
        </div>
        <div ref={railRef} className="review-list queue-list px-2.5 py-1" role="listbox" aria-label="Albums">
          {items.slice(0, shownCount).map(g => {
            const st = gapLine(g)
            return (
              <div key={g.id}
                ref={g.id === sel ? selRowRef : null}
                role="option" tabIndex={0} aria-selected={g.id === sel}
                className="queue-item mb-1.5 cursor-pointer rounded-card border p-[11px]"
                style={g.id === sel
                  ? { background: 'var(--active-item)', borderColor: 'var(--accent-bd)' }
                  : { borderColor: 'transparent' }}
                onClick={() => navigate('Fill gaps', g.id)}
                onKeyDown={e => {
                  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); navigate('Fill gaps', g.id) }
                  if (e.key === 'ArrowDown') { e.preventDefault(); move(1) }
                  if (e.key === 'ArrowUp') { e.preventDefault(); move(-1) }
                }}>
                <div className="flex items-center gap-[11px]">
                  <Cover albumId={g.albumId} url={g.coverUrl} name={g.album} size={44} />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-small font-semibold">{g.album}</div>
                    {g.artistId
                      ? <button
                          className="block max-w-full truncate !border-0 !bg-transparent !p-0 text-left text-caption text-muted hover:!text-[var(--accent)]"
                          title={`Open ${g.artist}`}
                          onClick={e => { e.stopPropagation(); goArtist(g.artistId) }}>{g.artist}</button>
                      : <div className="truncate text-caption text-muted">{g.artist}</div>}
                    <div className="mt-[5px] flex items-center gap-1.5">
                      <span className="inline-block h-1.5 w-1.5 shrink-0 rounded-pill" style={{ background: st.color }} />
                      <span className="truncate text-micro" style={{ color: st.color }}>{st.word}</span>
                    </div>
                  </div>
                </div>
              </div>
            )
          })}
          {shownCount < items.length && <div ref={sentinelRef} className="p-3 text-center text-caption text-faint">Loading more…</div>}
          {!items.length && (
            <div className="p-3">
              <p className="text-caption text-muted">
                {gapHidden ? 'No hidden albums.' : 'No albums in this filter.'}
              </p>
              {!gapHidden && !scanTask && gapFilter === 'needs' && !gapSearch && (
                <button className="primary mt-2" onClick={startLibraryScan}>Scan library for missing tracks</button>
              )}
            </div>
          )}
        </div>
        {cursorOffScreen && (
          <button title="Jump to current album" aria-label="Jump to current album" onClick={jumpToCursor}
            className="absolute bottom-3.5 right-3.5 flex h-[38px] w-[38px] items-center justify-center !rounded-pill !border-[color:var(--accent)] !bg-accent !p-0 text-lead !text-accent-fg"
            style={{ boxShadow: '0 8px 20px -6px rgba(0,0,0,.6)' }}>
            ◎
          </button>
        )}
      </aside>

      <section className="workspace min-w-0"
        style={{ background: 'radial-gradient(130% 90% at 100% 0%, var(--accent-tint) 0%, var(--bg) 55%)' }}>
        {focusRow ? (
          <>
            <div className="mb-5 flex flex-wrap items-center gap-2.5">
              <button onClick={() => move(-1)}>← Prev</button>
              <span className="text-caption text-faint">Album {(idx + 1).toLocaleString()} of {items.length.toLocaleString()}</span>
              <button onClick={() => move(1)}>Next →</button>
              <span className="spacer" />
              <button disabled={rescanning} aria-busy={rescanning} onClick={rescanAlbum}
                title="Re-check just this album — re-reads it from Navidrome and looks for files on disk it hasn't indexed">
                {rescanning ? 'Rescanning…' : 'Rescan album'}
              </button>
              {focusRow.hidden
                ? <button className="primary" onClick={() => setHidden(focusRow.id, false)}>Unhide</button>
                : <button className="quiet" onClick={() => setHidden(focusRow.id, true)}
                    title="Hide this album from the queue. It stays under “Hidden”, where you can bring it back.">
                    Skip this album
                  </button>}
            </div>
            <div className="hero flex items-start gap-[26px]">
              <div className="shrink-0" style={{ boxShadow: '0 14px 34px -10px rgba(0,0,0,.65)', borderRadius: 13 }}>
                <Cover albumId={focusRow.albumId} url={focusRow.coverUrl} name={focusRow.album} size={154} />
              </div>
              <div className="min-w-0 flex-1">
                {focusRow.artistId
                  ? <button
                      className="!border-0 !bg-transparent !p-0 text-caption font-semibold uppercase tracking-[.12em] hover:underline"
                      style={{ color: 'var(--accent)' }} title={`Open ${focusRow.artist}`}
                      onClick={() => goArtist(focusRow.artistId)}>{focusRow.artist}</button>
                  : <div className="text-caption font-semibold uppercase tracking-[.12em]" style={{ color: 'var(--accent)' }}>
                      {focusRow.artist}
                    </div>}
                <h1 className="mb-3 mt-[3px] text-display font-bold">{focusRow.album}</h1>
                <div className="flex flex-wrap gap-2">
                  <span className="rounded-pill border border-line bg-panel px-[11px] py-1 text-caption" style={{ color: 'var(--text2)' }}>
                    {focusRow.present} of {focusRow.total} present
                  </span>
                  <span className="rounded-pill border px-[11px] py-1 text-caption"
                    style={{ background: 'var(--accent-tint)', borderColor: 'var(--accent-bd)', color: 'var(--accent)' }}>
                    {missingOf(focusRow)} tracks missing
                  </span>
                  <span className="rounded-pill border border-line bg-panel px-[11px] py-1 text-caption"
                    style={{ color: 'var(--text2)', borderBottom: '1px dashed var(--faint)', cursor: 'help' }}
                    title="Measured against the MusicBrainz tracklist of the release your files are tagged with — what a complete copy should contain.">
                    vs. MusicBrainz tracklist
                  </span>
                  {focusRow.extra > 0 && (
                    <span className="rounded-pill border px-[11px] py-1 text-caption"
                      style={{ background: 'var(--accent-tint)', borderColor: 'var(--warn)', color: 'var(--warn)', borderBottom: '1px dashed var(--warn)', cursor: 'help' }}
                      title="Files the tracklist can't account for — duplicate copies, bonus tracks, or the leftovers of a fill that matched the wrong file. Use Duplicate files below to check.">
                      +{focusRow.extra} unaccounted
                    </span>
                  )}
                  {focusRow.origin && focusRow.origin !== 'library' && (
                    <span className="rounded-pill border border-line bg-panel px-[11px] py-1 text-caption text-muted">
                      from {ORIGIN_LABELS[focusRow.origin] || focusRow.origin}
                    </span>
                  )}
                </div>
              </div>
            </div>
            {focusRow.hidden && (
              <div className="mt-5"><Notice tone="quiet" title="Hidden">You skipped this album, so it is out of the queue. Unhide it to work on it again.</Notice></div>
            )}
            {focus
              ? <ActionCard key={focus.id} detail={focus} transfers={transfers} rescanAlbum={rescanAlbum} rescanning={rescanning} />
              : <p className="mt-6 text-caption text-muted">Loading album detail…</p>}
          </>
        ) : (
          <EmptyState title={gapHidden ? 'No hidden albums' : 'No gaps to review'}
            hint={gapHidden ? 'Albums you skip land here.' : 'Run a library scan to find albums with missing tracks.'}>
            {!gapHidden && (scanTask
              ? <div className="mx-auto w-full max-w-[420px]">{scanBar}</div>
              : <button className="primary" onClick={startLibraryScan}>Scan library for missing tracks</button>)}
          </EmptyState>
        )}
      </section>
    </div>
  )
}
