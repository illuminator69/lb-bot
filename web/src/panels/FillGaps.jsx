import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useApp, navigate, replaceRoute } from '../App.jsx'
import { api } from '../lib/api.js'
import Cover from '../components/Cover.jsx'
import { DuplicateFileSet } from './Library.jsx'
import { Badge, Chip, EmptyState, EMPTY_SOURCE_FILTERS, filterSources, Pager, ProgressBar, SourceFileList, SourceFilters, SourceFilesNote, SourceRow, StatusChip, useSourceFiles } from '../components/ui.jsx'

const SOURCES_PER_PAGE = 4

// "45s" / "3m" / "2h" for a unix timestamp, or '' if there isn't one.
function relativeAge(ts) {
  if (!ts) return ''
  const secs = Math.max(0, Math.round(Date.now() / 1000 - ts))
  if (secs < 60) return `${secs}s`
  if (secs < 3600) return `${Math.round(secs / 60)}m`
  return `${Math.round(secs / 3600)}h`
}

// Colored state line for queue items, per the mock's stateWord().
function queueState(g) {
  const miss = g.missingCount ?? (g.total - g.present)
  // A search runs in the background, so the rail has to say which album is
  // mid-search — otherwise moving to another album looks like the first one
  // silently stopped.
  if (g.searching) return { word: 'searching for sources…', color: 'var(--accent)' }
  switch (g.status) {
    case 'downloading': return { word: `downloading ${miss} of ${miss}`, color: 'var(--green)' }
    case 'failed': return { word: 'failed — needs recovery', color: 'var(--danger)' }
    case 'picking': return { word: 'choosing a source', color: 'var(--decide-fg)' }
    case 'complete': return { word: 'complete', color: 'var(--muted)' }
    default: return { word: `${miss} missing · source ready`, color: 'var(--accent)' }
  }
}

function fmtMB(n) { return n ? `${(n / 1024 / 1024).toFixed(1)} MB` : null }

// Per-album MP3 opt-in. Deliberately not a global setting: lb-bot's format
// policy is flac/opus, and this only relaxes it for one album — with mp3 ranked
// last, so it is chosen only where nothing lossless covers the track. Surfaced
// with the observed count of tracks the source had no accepted file for, so the
// choice is informed rather than a shot in the dark.
function Mp3Fallback({ detail, busy, onToggle }) {
  const allow = !!detail.allowMp3
  const stranded = detail.noFileInSourceCount || 0
  // mp3WouldHelp is set by a search that found peers and files but rejected all
  // of them on format. Without it the opt-in only appeared after a download had
  // already failed — which never happens when there was nothing to download.
  const wouldHelp = !!detail.mp3WouldHelp
  if (!allow && !stranded && !wouldHelp) return null
  return (
    <div className="mt-2 flex flex-wrap items-center gap-2 text-[11.5px]">
      {stranded > 0 && !allow && (
        <span style={{ color: 'var(--danger)' }}>
          {stranded} track(s) had no FLAC/Opus file in this source
        </span>
      )}
      {wouldHelp && !allow && !stranded && (
        <span style={{ color: 'var(--danger)' }}>
          The peers that answered had this album in MP3 only
        </span>
      )}
      {allow && <span className="chip !my-0 !py-0">MP3 allowed</span>}
      <button className="!py-0.5 !text-[11.5px]" disabled={busy}
        onClick={() => onToggle(!allow)}
        title="Accept MP3 for this album only, ranked below FLAC and Opus">
        {allow ? 'Require FLAC/Opus again' : 'Allow MP3 as a last resort'}
      </button>
    </div>
  )
}

// Duplicate files in *this* album, on demand. A fill that matched the wrong
// file leaves a second copy of a song we already had, and this is where the
// user is standing when that happens — so it is checkable here rather than
// only after a full library duplicate scan.
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

  // Reset when the focused album changes so one album's result never shows
  // under another's title.
  useEffect(() => { setSets(null) }, [groupId])

  return (
    <div className="mt-6">
      <div className="mb-2.5 flex items-center gap-2.5">
        <div className="text-[13px] font-semibold tracking-[.02em]" style={{ color: 'var(--text2)' }}>
          DUPLICATE FILES
        </div>
        <span className="h-px flex-1" style={{ background: 'var(--border)' }} />
        <button type="button" className="link-inline !text-[12px] !text-muted"
          disabled={checking} aria-busy={checking} onClick={load}>
          {sets === null ? 'Check this album' : 'Check again'}
        </button>
      </div>
      {sets === null ? (
        <p className="muted text-[12.5px]">
          Checks whether any song in this album exists twice on disk.
        </p>
      ) : !sets.length ? (
        <p className="muted text-[12.5px]">No duplicate files in this album.</p>
      ) : sets.map((s, i) => (
        <DuplicateFileSet key={`${s.albumId}-${s.title}-${i}`} set={s} onDeleted={load} />
      ))}
    </div>
  )
}

// One missing track, with the two escape hatches for when the automatic matcher
// got it wrong: pick a specific file out of a specific source, and — if
// placement then refuses because that audio is already in the album — place it
// anyway, having been told by name what it duplicates.
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
    if (!next) return
    await loadSource(srcIdx)
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
      `${t.forcePlaceConflict || 'another file'}, so this will add a second copy.`)
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
    <div className="mb-[7px] rounded-[10px] border px-3.5 py-[11px]"
      style={{ background: 'var(--inset-warm)', borderColor: 'var(--hairline)' }}>
      <div className="flex items-center gap-3.5">
        <span className="w-6 font-mono text-[12px] text-faint">{t.position}</span>
        <div className="min-w-0 flex-1">
          <div className="text-[14px]">{t.title}</div>
          {t.artist ? <div className="text-[11.5px] text-faint">{t.artist}</div> : null}
          {t.downloadError ? <div className="muted bad text-[11.5px]">{t.downloadError}</div> : null}
          {t.manualPick ? (
            <div className="text-[11.5px] text-faint" title={t.manualPick.filename}>
              hand-picked from @{t.manualPick.peer}
            </div>
          ) : null}
        </div>
        {canPick && (
          <button className="!py-1 !text-[12px]" aria-expanded={picking}
            onClick={openPicker}>{picking ? 'Close' : 'Pick a file…'}</button>
        )}
        {t.canForcePlace && (
          <button className="!py-1 !text-[12px]" disabled={busy} aria-busy={busy}
            onClick={placeAnyway}>Place anyway</button>
        )}
        <StatusChip status={t.state} />
      </div>
      {picking && (
        <div className="mt-2 rounded-[9px] border border-line p-2.5" style={{ background: 'var(--inset-deep)' }}>
          <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
            <span className="text-[11.5px] text-faint">Source:</span>
            {sources.map(s => (
              <button key={s.id} className="!py-0.5 !text-[11.5px]"
                style={s.id === srcIdx ? { borderColor: 'var(--accent-bd-sel)' } : undefined}
                onClick={() => loadSource(s.id)}>@{s.peer}</button>
            ))}
          </div>
          {loading ? <div className="text-[11.5px] text-faint">Reading the peer’s folder…</div>
            : listing ? <SourceFileList src={listing} onPick={busy ? undefined : pick} />
            : <div className="text-[11.5px] text-faint">No listing yet.</div>}
        </div>
      )}
    </div>
  )
}

// Live view of the background source search for this album. The POST that
// starts it returns immediately, so without this the screen showed nothing at
// all for the 30-90s slskd takes — and a search that ended in an error said so
// only in a task nobody was looking at.
function SourceSearchCard({ task }) {
  // Tick so the elapsed counter moves; a slskd search has no total to count
  // towards, so elapsed and the peer count are the only real progress there is.
  const [, setTick] = useState(0)
  useEffect(() => {
    const t = setInterval(() => setTick(n => n + 1), 1000)
    return () => clearInterval(t)
  }, [])
  const elapsed = Math.max(0, Math.round(Date.now() / 1000 - (task.startedAt || 0)))
  return (
    <div className="workspace-card mt-6">
      <div className="mb-0.5 text-[12px] font-semibold uppercase tracking-[.1em]" style={{ color: 'var(--accent)' }}>
        Searching
      </div>
      <div className="text-[18px] font-semibold">Looking for sources on slskd</div>
      <div className="muted mt-0.5 truncate text-[13px]">
        {task.current || 'Waiting for peers to answer…'}
      </div>
      <div className="mt-4">
        {/* Elapsed against a nominal 60s: two 30s slskd passes is the worst
            case, so this reads as "still going" rather than promising a finish. */}
        <ProgressBar value={Math.min(95, (elapsed / 60) * 100)} />
      </div>
      <div className="muted mt-2 font-mono text-[12px]">
        {elapsed}s elapsed · peers answer on their own schedule
      </div>
    </div>
  )
}

// The auto-picked source, with the disclosure that answers "is this actually
// the right album?" before anything is downloaded. The file list is the peer's
// real folder listing (the same expansion the picker rows use), not filenames
// synthesized from the tracklist — a guess here would only ever re-state what
// the row above already says.
function ChosenSourceCard({ src, busy, expandSource, onChangeSource }) {
  const { open, toggle, expanding, expandError, view, isFullFolder } = useSourceFiles(src, expandSource)
  return (
    <>
      <div className="flex flex-wrap items-center gap-x-3.5 gap-y-2.5 rounded-[11px] border p-3.5"
        style={{ background: 'var(--inset-warm)', borderColor: 'var(--border-warm)' }}>
        <Badge format={src.format} size="lg" />
        <div className="min-w-[140px] flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            {[src.bitrate, src.size].filter(Boolean).map((tok, i) => (
              <span key={i} className="whitespace-nowrap font-mono text-[14px] font-semibold">{tok}</span>
            ))}
            <span className="muted whitespace-nowrap text-[13px]">@{src.peer}</span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px]">
            <span className="whitespace-nowrap" style={{ color: 'var(--green)' }}>
              {src.coverage}{typeof src.coverage === 'number' ? ' tracks' : ''}
            </span>
            {src.speedMbps != null && <span className="muted whitespace-nowrap">{src.speedMbps} MB/s</span>}
            {src.recommended && <span className="chip good !my-0">✓ recommended</span>}
          </div>
        </div>
        <button className="ml-auto whitespace-nowrap" aria-expanded={open} aria-busy={expanding}
          onClick={toggle}>
          {open ? '▾ Hide files' : '▸ Show files'}
        </button>
        <button className="tint whitespace-nowrap font-semibold" disabled={busy}
          onClick={onChangeSource}>
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

function ActionCard({ detail, transfers }) {
  const { action, pushToast, dispatch } = useApp()
  const [busy, setBusy] = useState(false)
  const [picking, setPicking] = useState(false)
  const [srcPage, setSrcPage] = useState(0)
  const [srcFilters, setSrcFilters] = useState(EMPTY_SOURCE_FILTERS)
  const [lastError, setLastError] = useState(null)
  // Per-album chosen source: picking selects; downloading starts only on
  // "Get N tracks →" (mock interaction model).
  const [chosenMap, setChosenMap] = useState({})
  const lastTriedRef = useRef(null)

  const status = detail.status
  const missing = detail.missingCount
  const sources = detail.sources || []
  const chosenId = chosenMap[detail.id] ?? sources[0]?.id
  const chosenSrc = sources.find(s => s.id === chosenId) || sources[0]
  // Peer availability goes stale fast, so say how old this list is.
  const foundAgo = relativeAge(detail.sourcesFoundAt)
  const srcTask = detail.sourceTask
  const searching = srcTask?.status === 'running'
  // A failed search that left no sources behind is the case that used to be
  // invisible: the album sat on "choosing a source" with nothing to choose.
  // detail.noSourceReason outlives the task, so the explanation is still there
  // after a restart or once the task list has rolled over.
  const searchError = !sources.length && !searching
    ? (detail.noSourceReason
        ? `No usable source: ${detail.noSourceReason}`
        : (srcTask?.status === 'error' ? (srcTask.error || 'Source search failed') : null))
    : null

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
  // Without `force`, the backend reuses results younger than its TTL instead of
  // re-running a slskd search that takes tens of seconds — so leaving an album
  // and coming back is instant. "Search again" always forces a fresh one.
  const findSources = (force = false) =>
    run(() => action(`/api/groups/${detail.id}/sources`, { force }), 'Source search failed')
  const cancel = () =>
    run(() => action(`/api/gaps/${detail.id}/cancel`), 'Cancel failed')
  // Headless: let the backend fetch sources, apply the ranking, and start the
  // download in one shot — no manual source pick.
  const autoSelect = () =>
    run(() => action(`/api/gaps/${detail.id}/auto`), 'Auto-select failed')
  const setAllowMp3 = (allow) =>
    run(() => action(`/api/gaps/${detail.id}/allow-mp3`, { allow }),
        'Could not change the format policy')

  function choose(id) {
    setChosenMap(m => ({ ...m, [detail.id]: id }))
    setPicking(false)
  }

  // The peer's real folder listing, fetched when a file disclosure is opened.
  // `src.id` is the source index the backend keyed the folder on.
  const expandSource = useCallback(
    (sourceId) => api(`/api/groups/${detail.id}/sources/${sourceId}/files`),
    [detail.id])

  // Progress for this album from the live transfers snapshot
  const mine = (transfers?.transfers || []).filter(t => t.groupId === detail.id)
  // The peer actually serving the download can differ from the auto-picked
  // source once the server fails over — resolve it from the live transfer so
  // the format/bitrate/size shown match what's really coming down.
  const activeSrc = sources.find(s => s.peer === mine[0]?.username) || chosenSrc
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

  const err = lastError || (status === 'failed'
    ? { reason: detail.failReason || 'Download failed', detail: detail.failDetail, logTail: detail.logTail }
    : null)

  // Next-best fallback when the server didn't hand us one: the source ranked
  // after the last one we tried.
  const lastIdx = sources.findIndex(s => s.id === lastTriedRef.current)
  const nextSource = err?.nextSource
    || (sources.length > 1 ? sources[(lastIdx >= 0 ? lastIdx : 0) + 1] : null)
  const retryId = lastTriedRef.current ?? chosenId

  const filteredSources = filterSources(sources, srcFilters)
  const pages = Math.ceil(filteredSources.length / SOURCES_PER_PAGE)
  const pageSources = filteredSources.slice(srcPage * SOURCES_PER_PAGE, (srcPage + 1) * SOURCES_PER_PAGE)

  const showPicker = picking && status !== 'downloading' && status !== 'complete'

  return (
    <>
      {status === 'complete' && (
        <div className="mt-6">
          <EmptyState title="All tracks present" hint="Nothing to do here 🎉" />
        </div>
      )}

      {status === 'downloading' && (
        <div className="workspace-card mt-6">
          <div className="flex items-center gap-4">
            <div className="min-w-0 flex-1">
              <div className="mb-0.5 text-[12px] font-semibold uppercase tracking-[.1em]" style={{ color: 'var(--green)' }}>Working</div>
              <div className="text-[18px] font-semibold">
                Getting {missing} track(s){mine[0]?.username ? ` from @${mine[0].username}` : activeSrc?.peer ? ` from @${activeSrc.peer}` : ''}
              </div>
              <div className="muted mt-0.5 text-[13px]">
                {[activeSrc?.format, activeSrc?.bitrate, activeSrc?.size].filter(Boolean).join(' · ')}
                {activeSrc ? ' — ' : ''}files into the album folder automatically when done.
              </div>
            </div>
            <button className="self-center" disabled={busy} onClick={cancel}>Cancel</button>
          </div>
          <div className="mt-4">
            <ProgressBar value={activePct} />
          </div>
          <div className="muted mt-2 font-mono text-[12px]">{dlStat}</div>
        </div>
      )}

      {searching && status !== 'downloading' && status !== 'complete' && (
        <SourceSearchCard task={srcTask} />
      )}

      {status !== 'downloading' && status !== 'complete' && !showPicker && (
        <>
          {err ? (
            <div className="mt-6 rounded-[14px] border p-5"
              style={{ background: 'var(--warn-tint)', borderColor: 'var(--danger-bd)' }}>
              <div className="flex items-start gap-3.5">
                <span className="flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-pill border text-[18px]"
                  style={{ background: 'var(--danger-tint-2)', borderColor: 'var(--danger-bd)', color: 'var(--danger)' }}>!</span>
                <div className="min-w-0 flex-1">
                  <div className="mb-0.5 text-[12px] font-semibold uppercase tracking-[.1em]" style={{ color: 'var(--danger)' }}>
                    Download failed
                  </div>
                  <div className="text-[18px] font-semibold">{err.reason}</div>
                  {err.detail ? (
                    <div className="mt-1 text-[13px]" style={{ color: 'var(--danger-text)' }}>{err.detail}</div>
                  ) : null}
                </div>
              </div>
              <div className="mt-4 flex flex-wrap gap-2.5">
                {nextSource && (
                  <button
                    disabled={busy}
                    className="go !rounded-[11px] !px-5 font-semibold"
                    onClick={() => { choose(nextSource.id); fetchTracks(nextSource.id) }}
                  >
                    Try next best source →
                  </button>
                )}
                <button className="primary !rounded-[11px]" disabled={busy}
                  onClick={() => { setSrcPage(0); setPicking(true) }}>
                  Pick a source manually
                </button>
                {retryId != null && (
                  <button className="!rounded-[11px]" disabled={busy} onClick={() => fetchTracks(retryId)}>
                    Retry same peer
                  </button>
                )}
              </div>
              {err.logTail?.length ? (
                <div className="mt-3.5 rounded-[10px] border p-3 font-mono text-[12px] leading-[1.7] text-muted"
                  style={{ background: 'var(--inset-warm)', borderColor: 'var(--border-warm)' }}>
                  {err.logTail.map((l, i) => <div key={i}>{l}</div>)}
                </div>
              ) : null}
            </div>
          ) : null}

          <div className="workspace-card mt-6">
            <div className="flex flex-wrap items-center gap-[18px]">
              <div className="min-w-0 flex-1">
                <div className="mb-2.5 text-[12px] font-semibold uppercase tracking-[.1em] text-muted">
                  Next step · fill {missing} track(s) · chosen source
                </div>
                {chosenSrc ? (
                  <>
                    <ChosenSourceCard key={chosenSrc.id} src={chosenSrc} busy={busy}
                      expandSource={expandSource}
                      onChangeSource={() => { setSrcPage(0); setPicking(true) }} />
                    <div className="mt-2 text-[11.5px] text-faint">
                      Auto-picked by your source ranking ·{' '}
                      <button type="button" className="link-inline !text-muted"
                        onClick={() => navigate('System', 'prefs')}>edit ranking</button>
                      {foundAgo && <> · found {foundAgo} ago</>}
                      {' · '}
                      <button type="button" className="link-inline !text-muted"
                        disabled={busy || searching}
                        onClick={() => findSources(true)}>
                        {searching ? 'searching…' : 'search again'}
                      </button>
                    </div>
                    <Mp3Fallback detail={detail} busy={busy} onToggle={setAllowMp3} />
                  </>
                ) : (
                  <>
                    {searchError && (
                      <div className="mb-3 rounded-[11px] border p-3 text-[13px]"
                        style={{ background: 'var(--warn-tint)', borderColor: 'var(--danger-bd)',
                                 color: 'var(--danger-text)' }}>
                        Last search: {searchError}
                      </div>
                    )}
                    <div className="flex flex-wrap gap-2.5">
                      <button className="primary" disabled={busy || searching}
                        aria-busy={searching} onClick={() => findSources()}>
                        {searching ? 'Searching…' : 'Find sources on slskd'}
                      </button>
                      <button disabled={busy || searching} onClick={autoSelect}
                        title="Search, rank, and download the best source automatically">
                        Auto-select best source
                      </button>
                    </div>
                    <Mp3Fallback detail={detail} busy={busy} onToggle={setAllowMp3} />
                  </>
                )}
              </div>
              {chosenSrc && (
                <div className="flex shrink-0 flex-col items-stretch gap-2 self-center">
                  <button
                    className="btn-accent whitespace-nowrap"
                    disabled={busy}
                    aria-busy={busy}
                    onClick={() => fetchTracks(chosenSrc.id)}
                  >
                    Get {missing} track(s) →
                  </button>
                  <button className="!py-1 !text-[12px]" disabled={busy} onClick={autoSelect}
                    title="Let lb-bot pick and download the best source automatically">
                    Auto-select best
                  </button>
                </div>
              )}
            </div>
          </div>
        </>
      )}

      {showPicker && (
        <div className="workspace-card mt-6">
          <div className="mb-1 flex items-center gap-3">
            <div className="text-[18px] font-semibold">Choose a source</div>
            <span className="spacer" />
            <span className="text-[12.5px] text-faint">
              {srcFilters === EMPTY_SOURCE_FILTERS || filteredSources.length === sources.length
                ? `${detail.sourcesTotal ?? sources.length} found`
                : `${filteredSources.length} of ${sources.length}`} · ranked by your preferences
            </span>
            <button onClick={() => setPicking(false)}>Cancel</button>
          </div>
          <div className="muted mb-3 text-[12.5px]">
            Top match auto-selected from your ranking — pick any other if you prefer.
          </div>
          <div className="mb-3.5">
            <SourceFilters value={srcFilters} onChange={f => { setSrcFilters(f); setSrcPage(0) }} />
          </div>
          {pageSources.map(s => (
            <SourceRow key={s.id} src={s} busy={busy}
              selected={s.id === chosenId}
              done={s.id === chosenId}
              doneLabel="Selected"
              onExpand={expandSource}
              onUse={choose} />
          ))}
          {!sources.length && <p className="muted">No sources yet — run a search.</p>}
          {sources.length > 0 && !filteredSources.length && (
            <p className="muted">No sources match these filters — loosen them to see all {sources.length}.</p>
          )}
          {pages > 1 && (
            <div className="mt-3 border-t pt-3.5" style={{ borderColor: 'var(--hairline)' }}>
              <Pager page={srcPage} pages={pages} onPage={setSrcPage}
                total={filteredSources.length} pageSize={SOURCES_PER_PAGE} />
            </div>
          )}
        </div>
      )}

      <div className="mt-6">
        <div className="mb-2.5 flex items-center gap-2.5">
          <div className="text-[13px] font-semibold tracking-[.02em]" style={{ color: 'var(--text2)' }}>MISSING TRACKS</div>
          <span className="h-px flex-1" style={{ background: 'var(--border)' }} />
          <button type="button" className="link-inline !text-[12px] !text-muted" onClick={() =>
            action(`/api/groups/${detail.id}/reconcile-downloads`)
              .catch(e => pushToast(`Reconcile failed: ${e.message}`, 'error'))
          }>Reconcile downloaded files</button>
        </div>
        {(detail.tracks || []).filter(t => t.state !== 'present').map((t, i) => (
          <MissingTrackRow key={t.index ?? i} track={t} detail={detail}
            sources={sources} expandSource={expandSource} />
        ))}
      </div>

      <AlbumDuplicateFiles groupId={detail.id} />
    </>
  )
}

export default function FillGaps() {
  const { state, dispatch, action, pushToast } = useApp()
  const { gaps, gapsStale, gapDetail, selGap, gapFilter, gapSearch, transfers, summary } = state

  // The rail filter is local and debounced. Committing every keystroke to
  // gapSearch re-filtered the list mid-word, which kept knocking the selected
  // album out of view — and each re-home rewrote the route, so a single typed
  // word cost a burst of navigations and refetches.
  const [draftSearch, setDraftSearch] = useState(gapSearch)
  useEffect(() => {
    if (draftSearch === gapSearch) return
    const t = setTimeout(() => dispatch({ type: 'SET_GAP_SEARCH', q: draftSearch }), 250)
    return () => clearTimeout(t)
  }, [draftSearch, gapSearch, dispatch])

  const items = useMemo(() => {
    let rows = gaps?.items || []
    if (gapFilter === 'needs') rows = rows.filter(g => ['ready', 'picking', 'failed'].includes(g.status))
    if (gapFilter === 'working') rows = rows.filter(g => g.status === 'downloading')
    if (gapFilter === 'done') rows = rows.filter(g => g.status === 'complete')
    const q = gapSearch.toLowerCase().trim()
    if (q) rows = rows.filter(g => g.artist.toLowerCase().includes(q) || g.album.toLowerCase().includes(q))
    return rows
  }, [gaps, gapFilter, gapSearch])

  const counts = gaps?.counts || {}
  // The rail as it looked on the previous render. Read below to work out where
  // a departed cursor *was*; written in an effect, so during the render where
  // `items` changed it still holds the old order.
  const railIdsRef = useRef([])
  // Re-home the cursor onto a visible album when filters change — but only once
  // a list that can actually speak to this id is here. To a list that predates
  // the selection every id looks absent, so falling back to items[0] would
  // quietly retarget a deep link (Artist → "Fill gaps →") at the first album in
  // the rail; gapsStale marks exactly that window.
  const sel = (() => {
    if (selGap && items.some(g => g.id === selGap)) return selGap
    if (!gaps || gapsStale) return selGap
    // The album under the cursor has left the filtered list — you picked a
    // source and it went `downloading`, it completed, or you skipped it. Land
    // on whatever took its slot, which *is* the next album in the queue, so
    // working the "Needs you" list is a straight walk down it. Snapping back
    // to items[0] sent you to the top of a 46-album rail after every pick and
    // re-offered albums you had just dealt with.
    //
    // Positional, not a server-supplied `nextId`: the list is already stable
    // and ordered across polls, so the index is enough and it costs no API
    // change. Clamped, because the album may have been the last one.
    const wasAt = railIdsRef.current.indexOf(selGap)
    if (wasAt >= 0 && items.length) {
      return items[Math.min(wasAt, items.length - 1)].id
    }
    return items[0]?.id ?? null
  })()
  useEffect(() => {
    // Only snapshot the order once the cursor has settled onto a row this list
    // actually contains. Updating it mid-re-home would erase the very position
    // the walk above needs, and the walk would fall back to the top.
    if (sel === selGap) railIdsRef.current = items.map(g => g.id)
  }, [items, sel, selGap])
  useEffect(() => {
    // Re-homing is a correction, not navigation — replaceRoute so Back doesn't
    // have to step through cursor positions the user never chose. The route is
    // the source of truth, so syncing it also updates selGap.
    if (sel !== selGap) replaceRoute('Fill gaps', sel)
  }, [sel, selGap])
  const idx = items.findIndex(g => g.id === sel)
  const focus = gapDetail && gapDetail.id === sel ? gapDetail : null
  const focusRow = items[idx]

  // Keep the selected album visible in the left rail — e.g. when the selection
  // arrives from the Library table (deep down the list) rather than a click.
  const selRowRef = useRef(null)
  const railRef = useRef(null)
  useEffect(() => {
    selRowRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [sel])

  // "Jump to current album": the rail is long and scrolling it loses the album
  // the workspace is showing. An IntersectionObserver against the scroll
  // container answers "is the cursor still on screen" directly — scroll-offset
  // math would have to be redone for the ≤860px horizontal rail, and would go
  // wrong the moment the list reordered under it.
  const [cursorOffScreen, setCursorOffScreen] = useState(false)
  useEffect(() => {
    const root = railRef.current
    const row = selRowRef.current
    if (!root || !row) { setCursorOffScreen(false); return }
    const obs = new IntersectionObserver(
      ([e]) => setCursorOffScreen(!e.isIntersecting),
      { root, threshold: 0.35 })
    obs.observe(row)
    return () => obs.disconnect()
  }, [sel, items])

  const jumpToCursor = () =>
    selRowRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' })

  function move(delta) {
    const next = items[(idx + delta + items.length) % items.length]
    if (next) navigate('Fill gaps', next.id)
  }

  // Re-check this one album rather than the whole library: the server re-reads
  // it from Navidrome and walks its folder for files Navidrome hasn't indexed.
  // Reports what it found, because "nothing changed" is a real and useful
  // answer here and a silent button would look broken.
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

  // Skip = persistently hide the group (server filters hidden out of every
  // view), not just advance the cursor. Optimistically move on; the next
  // poll drops the row.
  async function skipAlbum() {
    const skipped = sel
    if (!skipped) return
    move(1)
    try {
      await action(`/api/groups/${skipped}/hide`, { hidden: true })
    } catch (e) {
      pushToast(`Skip failed: ${e.message}`, 'error')
    }
  }

  const scanTask = gaps?.scanTask || null

  // Jump to an album's artist page (the Artist screen is hash-routed by nd
  // artist id). No-op when the album isn't resolvable to a library artist.
  function openArtist(row) {
    if (!row?.artistId) return
    navigate('Artist', row.artistId)
  }

  // Trigger a full-library scan, tolerating the "already running" 409 (the bar
  // + Cancel below take over from there) instead of surfacing it as an error.
  async function startScan() {
    try {
      await action('/api/scan-all')
    } catch (e) {
      if (e.status === 409) pushToast('Scan already running', 'info')
      else pushToast(`Scan failed to start: ${e.message}`, 'error')
    }
  }
  async function cancelScan() {
    if (!scanTask) return
    try {
      await action(`/api/tasks/${scanTask.id}/cancel`)
      pushToast('Cancelling scan…', 'info')
    } catch (e) {
      pushToast(`Cancel failed: ${e.message}`, 'error')
    }
  }

  const scanBar = scanTask ? (
    <div className="rounded-[12px] border border-line bg-panel p-3.5">
      <div className="mb-2 flex items-center gap-2.5">
        <span className="inline-block h-1.5 w-1.5 shrink-0 rounded-pill" style={{ background: 'var(--green)' }} />
        <span className="min-w-0 flex-1 truncate text-[12.5px]" style={{ color: 'var(--text2)' }}>
          {scanTask.total
            ? `Scanning ${scanTask.done}/${scanTask.total}${scanTask.current ? ` · ${scanTask.current}` : ''}`
            : 'Scanning library for missing tracks…'}
        </span>
        <button className="!py-1 !text-[12px]" onClick={cancelScan}>Cancel</button>
      </div>
      <ProgressBar value={scanTask.total ? (scanTask.done / scanTask.total) * 100 : 4} />
    </div>
  ) : null

  if (!gaps) return <p className="muted">Loading gaps…</p>

  return (
    <div className="review-layout fill-gaps">
      <aside className="review-sidebar card !p-0" style={{ background: 'var(--rail)' }}>
        <div className="border-b border-line p-3.5 pb-2.5">
          <input className="w-full" placeholder={`Search ${summary?.library?.albums?.toLocaleString() ?? ''} albums…`}
            value={draftSearch}
            onChange={e => setDraftSearch(e.target.value)} />
          <div className="mt-3 flex flex-wrap gap-1.5">
            {[['needs', 'Needs you', counts.needs ?? 0],
              ['working', 'Working', counts.working ?? 0],
              ['done', 'Done', counts.done ?? 0],
              ['', 'All', counts.all ?? 0]].map(([k, label, count]) => (
              <Chip key={k} variant="solid" active={gapFilter === k} count={count}
                onClick={() => dispatch({ type: 'SET_GAP_FILTER', filter: k })}>{label}</Chip>
            ))}
          </div>
          {scanBar && <div className="mt-3">{scanBar}</div>}
        </div>
        <div ref={railRef} className="review-list queue-list px-2.5 py-1" role="listbox" aria-label="Albums with gaps">
          {items.map(g => {
            const st = queueState(g)
            return (
              <div key={g.id}
                ref={g.id === sel ? selRowRef : null}
                role="option" tabIndex={0} aria-selected={g.id === sel}
                className="queue-item mb-1.5 cursor-pointer rounded-[11px] border p-[11px]"
                style={g.id === sel
                  ? { background: 'var(--active-item)', borderColor: 'var(--accent-bd)' }
                  : { borderColor: 'transparent' }}
                onClick={() => navigate('Fill gaps', g.id)}
                onKeyDown={e => {
                  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); navigate('Fill gaps', g.id) }
                }}>
                <div className="flex items-center gap-[11px]">
                  <Cover albumId={g.albumId} url={g.coverUrl} name={g.album} size={44} />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[13.5px] font-semibold">{g.album}</div>
                    {g.artistId
                      ? <button
                          className="muted block max-w-full truncate !border-0 !bg-transparent !p-0 text-left text-[12px] hover:!text-[var(--accent)]"
                          title={`Open ${g.artist}`}
                          onClick={e => { e.stopPropagation(); openArtist(g) }}>{g.artist}</button>
                      : <div className="muted truncate text-[12px]">{g.artist}</div>}
                    <div className="mt-[5px] flex items-center gap-1.5">
                      <span className="inline-block h-1.5 w-1.5 shrink-0 rounded-pill" style={{ background: st.color }} />
                      <span className="truncate text-[11.5px]" style={{ color: st.color }}>{st.word}</span>
                    </div>
                  </div>
                </div>
              </div>
            )
          })}
          {!items.length && (
            <div className="p-3">
              <p className="muted">No albums in this filter.</p>
              {scanTask
                ? scanBar
                : <button className="primary" onClick={startScan}>
                    Scan library for missing tracks
                  </button>}
            </div>
          )}
        </div>
        {cursorOffScreen && (
          <button
            title="Jump to current album"
            aria-label="Jump to current album"
            onClick={jumpToCursor}
            className="absolute bottom-3.5 right-3.5 flex h-[38px] w-[38px] items-center justify-center !rounded-pill !border-[color:var(--accent)] !bg-accent !p-0 text-[16px] !text-accent-fg"
            style={{ boxShadow: '0 8px 20px -6px rgba(0,0,0,.6)' }}
          >
            ◎
          </button>
        )}
      </aside>

      <section
        className="workspace"
        style={{ background: 'radial-gradient(130% 90% at 100% 0%, var(--accent-tint) 0%, var(--bg) 55%)' }}
      >
        {focusRow ? (
          <>
            <div className="mb-5 flex items-center gap-2.5">
              <button onClick={() => move(-1)}>← Prev</button>
              <span className="text-[12.5px] text-faint">Album {idx + 1} of {items.length}</span>
              <button onClick={() => move(1)}>Next →</button>
              <span className="spacer" />
              <button disabled={rescanning} aria-busy={rescanning} onClick={rescanAlbum}
                title="Re-check just this album — re-reads it from Navidrome and looks for files on disk it hasn't indexed">
                {rescanning ? 'Rescanning…' : 'Rescan album'}
              </button>
              <button className="!text-muted" onClick={skipAlbum}>Skip this album</button>
            </div>
            <div className="hero flex items-start gap-[26px]">
              <div className="shrink-0" style={{ boxShadow: '0 14px 34px -10px rgba(0,0,0,.65)', borderRadius: 13 }}>
                <Cover albumId={focusRow.albumId} url={focusRow.coverUrl} name={focusRow.album} size={154} />
              </div>
              <div className="min-w-0 flex-1">
                {focusRow.artistId
                  ? <button
                      className="!border-0 !bg-transparent !p-0 text-[12px] font-semibold uppercase tracking-[.12em] hover:underline"
                      style={{ color: 'var(--accent)' }}
                      title={`Open ${focusRow.artist}`}
                      onClick={() => openArtist(focusRow)}>{focusRow.artist}</button>
                  : <div className="text-[12px] font-semibold uppercase tracking-[.12em]" style={{ color: 'var(--accent)' }}>
                      {focusRow.artist}
                    </div>}
                <div className="mb-3 mt-[3px] text-[32px] font-bold leading-[1.08]">{focusRow.album}</div>
                <div className="flex flex-wrap gap-2">
                  <span className="rounded-pill border border-line bg-panel px-[11px] py-1 text-[12.5px]" style={{ color: 'var(--text2)' }}>
                    {focusRow.present} of {focusRow.total} present
                  </span>
                  <span
                    className="rounded-pill border border-line bg-panel px-[11px] py-1 text-[12.5px]"
                    style={{ color: 'var(--text2)', borderBottom: '1px dashed var(--faint)', cursor: 'help' }}
                    title="The authoritative MusicBrainz tracklist for this release — what a complete copy of the album should contain. lb-bot compares your files against it to find gaps."
                  >canonical set</span>
                  <span className="rounded-pill border px-[11px] py-1 text-[12.5px]"
                    style={{ background: 'var(--accent-tint)', borderColor: 'var(--accent-bd)', color: 'var(--accent)' }}>
                    {focusRow.missingCount ?? (focusRow.total - focusRow.present)} tracks missing
                  </span>
                  {focusRow.extra > 0 && (
                    <span className="rounded-pill border px-[11px] py-1 text-[12.5px]"
                      style={{ background: 'var(--accent-tint)', borderColor: 'var(--warn)', color: 'var(--warn)',
                               borderBottom: '1px dashed var(--warn)', cursor: 'help' }}
                      title="Files the canonical tracklist can't account for — duplicate copies, bonus tracks, or the leftovers of a fill that matched the wrong file. Use DUPLICATE FILES below to check.">
                      +{focusRow.extra} unaccounted
                    </span>
                  )}
                </div>
              </div>
            </div>
            {focus
              ? <ActionCard key={focus.id} detail={focus} transfers={transfers} />
              : <p className="muted mt-6">Loading album detail…</p>}
          </>
        ) : (
          <EmptyState title="No gaps to review"
            hint="Run a library scan to find albums with missing tracks.">
            {scanTask
              ? <div className="mx-auto w-full max-w-[420px]">{scanBar}</div>
              : <button className="primary" onClick={startScan}>Scan all albums</button>}
          </EmptyState>
        )}
      </section>
    </div>
  )
}
