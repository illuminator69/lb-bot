import { useCallback, useEffect, useRef, useState } from 'react'
import { useApp, navigate, goArtist, goPlace } from '../App.jsx'
import { heroTextBasis } from '../lib/frame.js'
import { api } from '../lib/api.js'
import { placeAnywayBody, placeAnywayConfirm, placeAnywayFailure } from '../lib/placeAnyway.js'
import Cover from '../components/Cover.jsx'
import { DuplicateFileSet } from '../components/Duplicates.jsx'
import { Dot, Grp, Meter, Roll, St } from '../components/Page.jsx'
import {
  ago, EMPTY_SOURCE_FILTERS, failWords, filterSources, fmtClock, Pager, SourceFileList,
  SourceFilters, SourceRow, STATUS,
} from '../components/ui.jsx'

// The server pages sources ten at a time; the list shows its pages as-is
// rather than paging locally over the first ten while claiming "N found".
// The header's cover, said once: its size is also what the text's floor is worked out from.
const HERO_COVER = 96
const SOURCES_PER_PAGE = 10

export const ORIGIN_LABELS = { library: 'Library scan', playlist: 'Playlists', spotify: 'Spotify', repair: 'Repairs' }
export const missingOf = g => g.missingCount ?? (g.total - g.present)
const tracksWord = n => `${n} track${n === 1 ? '' : 's'}`

// A track that is no longer waiting on a download (PROTOCOL §15.2).
const FINISHED = new Set(['downloaded', 'done', 'skipped', 'cancelled'])

// Seconds since a unix timestamp, ticking, for "searching… 12 s".
function useElapsed(since, running) {
  const [, setTick] = useState(0)
  useEffect(() => {
    if (!running) return
    const t = setInterval(() => setTick(n => n + 1), 1000)
    return () => clearInterval(t)
  }, [running])
  return running && since ? Math.max(0, Math.round(Date.now() / 1000 - since)) : 0
}

// Per-album MP3 opt-in. Not a global setting: lb-bot's policy is flac/opus, and
// this relaxes it for one album with mp3 ranked last. Offered only when it
// would change something: tracks a source had no FLAC/Opus file for, peers that
// answered in MP3 only, or to turn it back off.
function Mp3Fallback({ detail, busy, onToggle }) {
  const allow = !!detail.allowMp3
  const stranded = detail.noFileInSourceCount || 0
  const wouldHelp = !!detail.mp3WouldHelp
  if (!allow && !stranded && !wouldHelp) return null
  return (
    <div className="s mt-2 flex flex-wrap items-center gap-2">
      {stranded > 0 && !allow && <span className="bad">{tracksWord(stranded)} had no FLAC or Opus file in this source.</span>}
      {wouldHelp && !allow && !stranded && <span className="bad">The peers that answered had this album in MP3 only.</span>}
      {allow && <span className="chip warn !my-0">MP3 allowed</span>}
      <button className="sm" disabled={busy} onClick={() => onToggle(!allow)}
        title="Accept MP3 for this album only, ranked below FLAC and Opus">
        {allow ? 'Require FLAC or Opus again' : 'Allow MP3 for this album'}
      </button>
    </div>
  )
}

// Duplicate files in *this* album, on demand.
function AlbumDuplicateFiles({ groupId, sets, checking, onCheck }) {
  if (sets === null) return null
  return (
    <>
      <Grp n={sets.length || ''} right={
        <button className="link" disabled={checking} aria-busy={checking} onClick={onCheck}>Check again</button>
      }>Duplicate files</Grp>
      {!sets.length
        ? <p className="muted">No song in this album exists twice on disk.</p>
        : sets.map((s, i) => <DuplicateFileSet key={`${groupId}-${s.title}-${i}`} set={s} onDeleted={onCheck} />)}
    </>
  )
}

const TRACK_STATE = {
  queued: { tone: 'run', word: 'Queued' },
  downloading: { tone: 'run', word: 'Downloading' },
  failed: { tone: 'bad', word: 'Failed' },
}

// One row of the tracklist. A missing track carries its own state and error,
// and the two escape hatches for when the matcher got it wrong: pick a file out
// of a specific source, and place-anyway.
function TrackRow({ track: t, detail, sources, expandSource }) {
  const { action, pushToast, requestConfirm } = useApp()
  const [picking, setPicking] = useState(false)
  const [srcIdx, setSrcIdx] = useState(sources[0]?.id ?? 0)
  const [listing, setListing] = useState(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const present = t.state === 'present'
  const canPick = !present && t.index != null && sources.length > 0

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

  async function openPicker() {
    const next = !picking
    setPicking(next)
    if (next) await loadSource(srcIdx)
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
    // What this one does, by the kind of refusal it overrules (placeAnyway.js):
    // a second copy of audio the album has, or a file lb-bot's check found to
    // be another recording.
    const ok = await requestConfirm(placeAnywayConfirm(t), { confirmLabel: 'Place anyway' })
    if (!ok) return
    setBusy(true)
    try {
      // With the row this page drew (J19): `t.index` is a place in a list a
      // rescan can reorder, and lb-bot turns the press away (409, in its own
      // words) when another row is there now.
      await action(`/api/groups/${detail.id}/tracks/${t.index}/place-anyway`, placeAnywayBody(t))
      pushToast('Placed')
    } catch (e) {
      pushToast(placeAnywayFailure(e), 'error')
    } finally {
      setBusy(false)
    }
  }

  // A track's own words: "downloading" is "Working" for an album, not for a file.
  const meta = TRACK_STATE[t.state] || STATUS[t.state] || { tone: '', word: t.state }
  return (
    <div className="row col">
      <div className="flex min-w-0 items-center gap-3">
        <span className="num muted" style={{ width: 22 }}>{t.position || ''}</span>
        <div className="grow">
          <div className="clip">{t.title}</div>
          {t.artist && t.artist !== detail.artist ? <div className="s clip">{t.artist}</div> : null}
          {t.downloadError ? <div className="s bad" style={{ whiteSpace: 'normal' }}>{t.downloadError}</div> : null}
          {/* Placed as another recording of this track: lb-bot's own sentence. */}
          {t.placementNote ? <div className="s" style={{ whiteSpace: 'normal' }}>{t.placementNote}</div> : null}
          {t.manualPick ? <div className="s clip" title={t.manualPick.filename}>hand-picked from @{t.manualPick.peer}</div> : null}
        </div>
        {/* A loose track's download has no release on record to be filed
            under; the server names its folder so the user can pick one. */}
        {t.placeFolder && (
          <button className="sm" onClick={() => goPlace(t.placeFolder, '', 'pick', t.placeFile)}>Pick the release</button>
        )}
        {canPick && (
          <button className="sm quiet" aria-expanded={picking} onClick={openPicker}
            title="Choose the file for this track out of one source’s folder">
            {picking ? 'Close' : 'Pick a file'}
          </button>
        )}
        {t.canForcePlace && (
          <button className="sm" disabled={busy} aria-busy={busy} onClick={placeAnyway}>Place anyway</button>
        )}
        {t.duration ? <span className="num muted hide-sm">{fmtClock(t.duration)}</span> : null}
        <span className={`st ${meta.tone}`}>{meta.word}</span>
      </div>
      {picking && (
        <div className="fadein mt-2">
          <div className="chips items-center">
            <span className="s">From</span>
            {sources.map(s => (
              <button key={s.id} className="chip num" aria-pressed={s.id === srcIdx} onClick={() => loadSource(s.id)}>@{s.peer}</button>
            ))}
          </div>
          {loading ? <div className="s mt-2">Reading the peer’s folder…</div>
            : listing ? <SourceFileList src={listing} onPick={busy ? undefined : pick} />
            : <div className="s mt-2">No listing yet.</div>}
        </div>
      )}
    </div>
  )
}

const titleKey = s => (s || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim()

/**
 * The tracklist of an album with gaps. ONE list.
 *
 * Without `reference` it is the tracks that need something, each with its own
 * state, error and the two ways to repair it by hand. With `reference` (the
 * MusicBrainz tracklist of a release, each row marked present or not) it is
 * that whole tracklist, and a row that is missing IS the gap's row, state and
 * repair included. The album page used to show both lists, one under the
 * other, saying the same thing twice.
 *
 *   reference  { rows, presenceKnown } from /api/album/tracklist, or null
 */
export function GapTracks({ detail, reference }) {
  const sources = detail.sources || []
  const expandSource = useCallback(
    sourceId => api(`/api/groups/${detail.id}/sources/${sourceId}/files`),
    [detail.id])
  const open = (detail.tracks || []).filter(t => t.state !== 'present')
  const gapRow = (t, key) => (
    <TrackRow key={key} track={t} detail={detail} sources={sources} expandSource={expandSource} />
  )

  if (!reference?.rows?.length) {
    if (!open.length) {
      return <p className="muted">{detail.status === 'complete' ? 'Every track is in the library.' : 'lb-bot lists no missing track for this album.'}</p>
    }
    return <div className="rows">{open.map((t, i) => gapRow(t, `${t.index ?? 'p'}-${t.position}-${i}`))}</div>
  }

  // Pair each row of the tracklist with the gap's row for it: by recording
  // first, then by title. A gap row is used once.
  const byMbid = new Map()
  const byTitle = new Map()
  for (const t of open) {
    if (t.recordingMbid && !byMbid.has(t.recordingMbid)) byMbid.set(t.recordingMbid, t)
    const k = titleKey(t.title)
    if (k && !byTitle.has(k)) byTitle.set(k, t)
  }
  const used = new Set()
  const take = r => {
    let t = r.mbid ? byMbid.get(r.mbid) : null
    if (!t || used.has(t)) t = byTitle.get(titleKey(r.title))
    if (!t || used.has(t)) return null
    used.add(t)
    return t
  }
  const rows = reference.rows.map(r => ({ r, t: take(r) }))
  // On another release of the album a missing track may not be listed at all.
  // It still needs something, so it is not dropped.
  const extra = open.filter(t => !used.has(t))
  const known = !!reference.presenceKnown

  return (
    <div className="rows">
      {rows.map(({ r, t }, i) => {
        const key = `${r.mbid || r.title}-${r.position}-${i}`
        // The library has it and the gap has nothing left to do for it.
        if (t && !(r.present === true && t.state === 'done')) {
          return gapRow({ ...t, position: r.position ?? t.position, duration: r.duration }, key)
        }
        const absent = known && r.present === false
        return (
          <div key={key} className="row">
            <span className="num muted" style={{ width: 22 }}>{r.position || ''}</span>
            {/* A track the fill placed and the library now lists is this plain
                row; what was said of its placement stays under it. */}
            {t?.placementNote ? (
              <div className="grow">
                <div className="clip">{r.title}</div>
                <div className="s" style={{ whiteSpace: 'normal' }}>{t.placementNote}</div>
              </div>
            ) : <span className="grow clip">{r.title}</span>}
            <span className="num muted hide-sm">{fmtClock(r.duration)}</span>
            {known && <span className={`st ${absent ? 'wait' : 'ok'}`}>{absent ? 'Missing' : 'In library'}</span>}
          </div>
        )
      })}
      {extra.length > 0 && (
        <div className="row s" style={{ background: 'var(--sunk)' }}>
          Missing, and not on this release’s tracklist
        </div>
      )}
      {extra.map((t, i) => gapRow(t, `x-${t.index ?? 'p'}-${i}`))}
    </div>
  )
}

/**
 * What can be done to an album with gaps as a whole: match files already
 * downloaded, re-read it from Navidrome, look for duplicate files, hide it.
 *
 *   bare    on the album's own page: no "Open artist"
 *   hidden  the queue's list says this album is hidden
 */
export function GapFoot({ detail, bare = false, hidden = false, onHide }) {
  const { action, pushToast } = useApp()
  const id = detail.id
  const [busy, setBusy] = useState(false)
  const [rescanning, setRescanning] = useState(false)
  const [dupSets, setDupSets] = useState(null)
  const [dupChecking, setDupChecking] = useState(false)
  // Stuck: the failure box above already offers these two, as the way out.
  const stalled = !!detail.stalledPlacement

  async function reconcile() {
    setBusy(true)
    try {
      await action(`/api/groups/${id}/reconcile-downloads`)
      pushToast('Checking the downloads folder for files of this album')
    } catch (e) {
      pushToast(`Reconcile failed: ${e.message}`, 'error')
    } finally { setBusy(false) }
  }
  async function rescan() {
    setRescanning(true)
    try {
      pushToast(rescanSaid(await action(`/api/gaps/${id}/rescan`)))
    } catch (e) {
      pushToast(`Rescan failed: ${e.message}`, 'error')
    } finally { setRescanning(false) }
  }
  const checkDuplicates = useCallback(async () => {
    setDupChecking(true)
    try {
      const r = await api(`/api/gaps/${id}/duplicate-files`)
      setDupSets(r.sets || [])
    } catch (e) {
      pushToast(`Duplicate check failed: ${e.message}`, 'error')
    } finally {
      setDupChecking(false)
    }
  }, [id, pushToast])

  return (
    <>
      <div className="acts mt-4">
        {!bare && detail.artistId && (
          <button onClick={() => goArtist(detail.artistId)}>Open artist</button>
        )}
        {/* A loose group has no release to reconcile against (Q-020). */}
        {!detail.loose && !stalled && (
          <button disabled={busy} onClick={reconcile}
            title="Match files already in the downloads folder against these tracks">
            Reconcile downloaded files
          </button>
        )}
        {detail.albumId && !stalled && (
          <button disabled={rescanning} aria-busy={rescanning} onClick={rescan}
            title="Re-check just this album: re-reads it from Navidrome and looks for files on disk it hasn’t indexed">
            Rescan album
          </button>
        )}
        {dupSets === null && (
          <button disabled={dupChecking} aria-busy={dupChecking} onClick={checkDuplicates}
            title="Checks whether any song in this album exists twice on disk">
            Check for duplicate files
          </button>
        )}
        {onHide && (hidden
          ? <button onClick={() => onHide(false)}>Unhide</button>
          : <button className="quiet" onClick={() => onHide(true)}
              title="Take this album out of the queue. It stays under Hidden, where you can bring it back.">
              Hide
            </button>)}
      </div>
      <AlbumDuplicateFiles groupId={id} sets={dupSets} checking={dupChecking} onCheck={checkDuplicates} />
    </>
  )
}

const rescanSaid = r => (r.foundOnDisk
  ? `Found ${r.foundOnDisk} file(s) on disk Navidrome hadn’t indexed. ${r.present} of ${r.total} present`
  : `Re-checked. ${r.present} of ${r.total} present`)

/**
 * Everything about one album with missing tracks: what is wrong, the next
 * step, its sources, its tracks. The gaps queue and To fix both show it.
 *
 *   row     the list row (cover, counts) — shown at once, before `detail` loads
 *   detail  /api/gaps/<id>, or null while it loads
 *   pager   { index, total, onPrev, onNext } in the queue
 *   onDetail  called with the album as a write route answered it, so the
 *           page shows the result at once instead of at the next poll
 *   bare    no hero: the album page shows its own above this
 *   flowOnly  stop after the next step and its sources: the album page puts
 *           the tracklist (GapTracks) and the album's actions (GapFoot) where
 *           its own layout wants them
 *   autoFind  start the source search on arrival if none has run
 */
export default function GapDetail({ row, detail, transfers, pager, onHide, onDetail, bare = false, flowOnly = false, autoFind = false }) {
  const { action, pushToast } = useApp()
  const g = detail || row
  const [busy, setBusy] = useState(false)
  const [finding, setFinding] = useState(false)
  const [srcPage, setSrcPage] = useState(0)
  // Sources beyond the first ten come from the server, a page at a time.
  const [pageData, setPageData] = useState(null)   // { page, sources } | null
  const [pageLoading, setPageLoading] = useState(false)
  const [srcFilters, setSrcFilters] = useState(EMPTY_SOURCE_FILTERS)
  const [lastError, setLastError] = useState(null)
  const [rescanning, setRescanning] = useState(false)
  const [whole, setWhole] = useState(null)          // { rows, presenceKnown } | null
  const [wholeLoading, setWholeLoading] = useState(false)
  const lastTriedRef = useRef(null)
  const sourcesRef = useRef(null)

  // Every source seen so far, by id, so a pick on page 3 still resolves.
  const seenRef = useRef(new Map())
  // A new search replaces the list the ids index into: pages fetched and
  // sources seen all belong to the old one. Kept across a re-search, the list
  // showed the old page and "Use this source" sent its index — which the server
  // resolved against the NEW results, a different peer and folder.
  const foundAt = detail?.sourcesFoundAt
  const [resultsKey, setResultsKey] = useState(foundAt)
  if (detail && resultsKey !== foundAt) {
    setResultsKey(foundAt)
    setPageData(null)
    setSrcPage(0)
    seenRef.current = new Map()
  }

  const id = g.id
  // Only the list says an album is hidden; its detail does not carry the flag.
  const hidden = !!(row?.hidden ?? g.hidden)
  const status = g.status
  const missing = missingOf(g)
  const firstPage = detail?.sources || []
  const totalSources = detail ? (detail.sourcesTotal ?? firstPage.length) : (row?.sourceCount || 0)
  const pages = Math.max(1, Math.ceil(totalSources / SOURCES_PER_PAGE))
  const pageSources = srcPage === 0 ? firstPage : (pageData?.page === srcPage ? pageData.sources : [])
  for (const s of [...firstPage, ...(pageData?.sources || [])]) seenRef.current.set(s.id, s)
  const srcTask = detail?.sourceTask

  // What was just asked for, shown at once. Between a click and the next poll
  // the server's answer is not on screen yet, and without this the page fell
  // back to its previous state for a moment: the button came back, then
  // "searching" appeared; the source list stayed, then the download replaced it.
  //   search  a source search was started
  //   auto    "download the best match": a search, then a download
  //   fetch   a source was picked; the route is walking the peers
  const [phase, setPhase] = useState(null)      // { kind, at, taskId?, sourceId? }
  const begin = (kind, extra = {}) => setPhase({ kind, at: Date.now(), ...extra })
  useEffect(() => {
    if (!phase || !detail) return
    const age = Date.now() - phase.at
    const moved = detail.status === 'downloading' || detail.status === 'complete'
    const taskOver = !!phase.taskId && srcTask?.id === phase.taskId && srcTask.status !== 'running'
    const over = phase.kind === 'search' ? taskOver
      : phase.kind === 'auto' ? (moved || taskOver)
      : (moved || (phase.answered && age > 6000))
    if (over || age > 150000) setPhase(null)
  }, [phase, detail, srcTask])

  const taskRunning = srcTask?.status === 'running'
  const searching = taskRunning || phase?.kind === 'search' || phase?.kind === 'auto' || (!detail && !!row?.searching)
  const elapsed = useElapsed(taskRunning ? srcTask.startedAt : phase ? phase.at / 1000 : 0, searching && (taskRunning || !!phase))
  const searchError = detail && !firstPage.length && !searching
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
    api(`/api/gaps/${id}?sourcePage=${srcPage}`)
      .then(r => { if (!dead) setPageData({ page: srcPage, sources: r.sources || [] }) })
      .catch(e => { if (!dead) pushToast(`Could not load more sources: ${e.message}`, 'error') })
      .finally(() => { if (!dead) setPageLoading(false) })
    return () => { dead = true }
  }, [srcPage, id, pageData, pushToast])

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

  const fetchTracks = async (sourceId = 0) => {
    lastTriedRef.current = sourceId
    begin('fetch', { sourceId })
    const r = await run(() => action(`/api/gaps/${id}/fetch`, { sourceId }), 'Could not queue tracks')
    if (!r) { setPhase(null); return r }
    // The route answers with the album as it now stands.
    if (r.gap) onDetail?.(r.gap)
    setPhase(p => (p ? { ...p, answered: true } : p))
    return r
  }
  const findSources = async (force = false) => {
    begin('search')
    const r = await run(() => action(`/api/groups/${id}/sources`, { force }), 'Source search failed')
    if (!r) setPhase(null)
    else setPhase(p => (p ? { ...p, taskId: r.task_id || '' } : p))
    return r
  }
  const cancel = async () => {
    const r = await run(() => action(`/api/gaps/${id}/cancel`), 'Cancel failed')
    setPhase(null)
    if (r?.gap) onDetail?.(r.gap)
  }
  const autoSelect = async () => {
    begin('auto')
    const r = await run(() => action(`/api/gaps/${id}/auto`), 'Could not start the download')
    if (!r) { setPhase(null); return }
    if (r.gap) onDetail?.(r.gap)
    setPhase(p => (p ? { ...p, taskId: r.task_id || '' } : p))
  }
  const setAllowMp3 = (allow) =>
    run(() => action(`/api/gaps/${id}/allow-mp3`, { allow }), 'Could not change the format policy')
  const reconcile = () =>
    run(async () => {
      const r = await action(`/api/groups/${id}/reconcile-downloads`)
      pushToast('Checking the downloads folder for files of this album')
      return r
    }, 'Reconcile failed')

  async function rescanAlbum() {
    if (rescanning) return
    setRescanning(true)
    try {
      pushToast(rescanSaid(await action(`/api/gaps/${id}/rescan`)))
    } catch (e) {
      pushToast(`Rescan failed: ${e.message}`, 'error')
    } finally {
      setRescanning(false)
    }
  }

  async function loadWhole() {
    setWholeLoading(true)
    try {
      const q = new URLSearchParams({ release_mbid: g.releaseMbid, group_id: id })
      const r = await api('/api/album/tracklist?' + q, { timeoutMs: 15000 })
      setWhole({ rows: r.tracks || [], presenceKnown: !!r.presenceKnown })
    } catch (e) {
      pushToast(`Could not load the tracklist: ${e.message}`, 'error')
    } finally {
      setWholeLoading(false)
    }
  }

  const expandSource = useCallback(
    (sourceId) => api(`/api/groups/${id}/sources/${sourceId}/files`),
    [id])

  // Progress for this album from the live transfers snapshot.
  const mine = (transfers?.transfers || []).filter(t => t.groupId === id)
  // The peer serving the download can differ from the one picked, after failover.
  const activeSrc = [...seenRef.current.values()].find(s => s.peer === mine[0]?.username)
    || seenRef.current.get(lastTriedRef.current) || null
  const rate = mine.reduce((s, t) => s + (t.rate || 0), 0)
  const eta = Math.max(...mine.map(t => t.etaSeconds || 0), 0)
  // How far the ALBUM has got. A finished file leaves the transfer list at
  // once, so a percentage made from the transfers measures only what is left:
  // the bar ran to the end of one song and started again for the next. The
  // tracks say what has finished (PROTOCOL §15.2's counts, the same rule the
  // other clients use); the transfers add the fraction of what is in flight.
  const fillRows = (detail?.tracks || []).filter(t => t.state !== 'present')
  const fillTotal = fillRows.length || missing || 0
  const fillDone = fillRows.filter(t => FINISHED.has(t.state)).length
  const fillFailed = fillRows.filter(t => t.state === 'failed').length
  const partial = mine.reduce((s, t) => s + (t.state === 'active' ? Math.min(100, t.pct || 0) / 100 : 0), 0)
  const rawPct = fillTotal ? Math.min(100, Math.round(((fillDone + partial) / fillTotal) * 100)) : 0
  // A file can be counted finished one poll before its transfer row goes, or
  // the other way round. Within one download the bar only moves forward.
  const pctRef = useRef(0)
  if (status !== 'downloading') pctRef.current = 0
  else pctRef.current = Math.max(pctRef.current, rawPct)
  const activePct = status === 'downloading' ? pctRef.current : rawPct
  const dlStat = [
    `${fillDone} of ${fillTotal} file${fillTotal === 1 ? '' : 's'}`,
    fillFailed ? `${fillFailed} failed` : null,
    rate ? `${(rate / 1024 / 1024).toFixed(1)} MB/s` : null,
    eta ? `this file about ${eta >= 90 ? `${Math.ceil(eta / 60)} min` : `${Math.ceil(eta)} s`}` : null,
  ].filter(Boolean).join(' · ')

  const stalled = !!detail?.stalledPlacement
  // Downloaded loose tracks with no release to file under. Reconcile cannot
  // help those; picking the release for each file can.
  const pickTracks = (detail?.tracks || []).filter(t => t.placeFolder)
  // Tracks the server has queued or is downloading. The transfers snapshot can
  // be a poll behind the album (it is only fetched while something is known to
  // be moving), and without this the detail said "filing into the album" with
  // no Cancel right after a source was picked.
  const inFlight = (detail?.tracks || []).filter(t => t.state === 'queued' || t.state === 'downloading').length
  const err = lastError || (detail && status === 'failed'
    ? { reason: failWords(detail.failReason), detail: detail.failDetail, logTail: detail.logTail }
    : null)

  // Next-best fallback when the server didn't hand one: the source ranked after
  // the last one tried.
  const ranked = [...seenRef.current.values()].sort((a, b) => (a.rank || a.id) - (b.rank || b.id))
  const lastIdx = ranked.findIndex(s => s.id === lastTriedRef.current)
  const nextSource = stalled ? null : (err?.nextSource
    || (ranked.length > 1 ? ranked[(lastIdx >= 0 ? lastIdx : 0) + 1] : null))
  const retryId = lastTriedRef.current

  // The rows that need something. The server lists the tracks it knows are
  // present too, but not always all of them, so they are not shown as "the"
  // tracklist.
  const missingRows = (detail?.tracks || []).filter(t => t.state !== 'present')
  const filteredSources = filterSources(pageSources, srcFilters)
  // A source was just picked and the route has not answered yet.
  const asking = phase?.kind === 'fetch' && status !== 'downloading'
  const downloading = status === 'downloading' || asking
  const complete = status === 'complete'
  const pickedSrc = asking ? seenRef.current.get(phase.sourceId) : null
  // The list folds away while a new search runs and opens on what it found.
  const showSources = !!detail && !downloading && !complete && totalSources > 0 && !searching
  const toSources = () => {
    if (!totalSources) { findSources(true); return }
    setSrcPage(0)
    sourcesRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  const hint = complete ? 'all tracks present'
    : downloading ? `file ${tracksWord(missing)} into the album`
    : phase?.kind === 'auto' ? 'searching, then downloading the best match'
    : searching ? 'searching Soulseek'
    : stalled ? 'match the downloaded files'
    : totalSources ? 'pick a source below'
    : `fill ${tracksWord(missing)}`
  const showActs = !!detail && !downloading && !complete && !stalled

  // Arriving by "Find sources" on an album page: run the search once, unless
  // one has already left results or something is in flight.
  const autoRef = useRef(false)
  useEffect(() => {
    if (!autoFind || autoRef.current || !detail) return
    autoRef.current = true
    if (!totalSources && !searching && status !== 'downloading' && status !== 'complete' && !detail.stalledPlacement) {
      findSources()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoFind, detail])

  // "Wrong edition?" — the gap is measured against the release the files are
  // tagged with. Which release that is, is said on the album's own page (the
  // edition chips, "My files are this edition"): the flow is not built twice.
  // The gap names its Navidrome album; lb-bot says which MusicBrainz album it is.
  async function wrongEdition() {
    setFinding(true)
    try {
      const a = await api(`/api/album/override?album_id=${encodeURIComponent(g.albumId)}`)
      if (a.rgid) navigate('album', g.artistId || '-', a.rgid, 'edition')
      else pushToast('lb-bot cannot tell which MusicBrainz album this is. Open it from the artist’s page.', 'error')
    } catch (e) {
      pushToast(e.payload?.error || `Could not open the album: ${e.message}`, 'error')
    } finally {
      setFinding(false)
    }
  }

  return (
    <>
      {!bare && <div className="hero">
        <Cover albumId={g.albumId} url={g.coverUrl} name={g.album} size={HERO_COVER} />
        {/* The text has a floor (N4): where the row cannot give it beside the
            pager, the pager takes a row of its own. */}
        <div className="grow" style={{ flexBasis: heroTextBasis(HERO_COVER) }}>
          <h2>{g.album}</h2>
          <p className="muted mt-1">
            {g.artistId
              ? <button className="link" onClick={() => goArtist(g.artistId)}>{g.artist}</button>
              : g.artist}
            {/* A separator starts no line (V9, W4): this dot stays with the artist, and
                "Wrong edition?" wraps together with its dot and the word before it. */}
            {'\u00a0· '}
            <span title="Measured against the MusicBrainz tracklist of the release your files are tagged with">
              {g.albumId && !g.loose ? (
                <Dot after={`${g.present} of ${g.total} tracks in the library`}>
                  <button className="link" disabled={finding} aria-busy={finding} onClick={wrongEdition}
                    title="Opens the album’s page at its editions, where you can say which release your files are">
                    Wrong edition?
                  </button>
                </Dot>
              ) : `${g.present} of ${g.total} tracks in the library`}
            </span>
          </p>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-1 empty:hidden">
            {g.extra > 0 && (
              <span className="chip warn !my-0"
                title="Files the tracklist can’t account for: duplicate copies, bonus tracks, or the leftovers of a fill that matched the wrong file. Check for duplicate files below.">
                +{g.extra} unaccounted
              </span>
            )}
            {g.origin && g.origin !== 'library' && <span className="chip !my-0">from {ORIGIN_LABELS[g.origin] || g.origin}</span>}
            {hidden && <span className="chip !my-0">hidden</span>}
          </div>
        </div>
        {pager && (
          <div className="acts">
            <button className="sm" disabled={pager.total < 2} onClick={pager.onPrev}>← Prev</button>
            <span className="num muted">{(pager.index + 1).toLocaleString()} / {pager.total.toLocaleString()}</span>
            <button className="sm" disabled={pager.total < 2} onClick={pager.onNext}>Next →</button>
          </div>
        )}
      </div>}

      {hidden && (
        <p className="why quiet">
          You hid this album, so it is out of the queue.{' '}
          {onHide && <button className="link" onClick={() => onHide(false)}>Unhide</button>}
        </p>
      )}

      {err && !downloading && !complete && (
        <div className="why bad">
          <div className="font-semibold">{stalled ? `Stuck. ${err.reason}` : err.reason}</div>
          {err.detail ? <div className="mt-0.5">{err.detail}</div> : null}
          {/* The usual place for this is hidden while stuck, so a failed
              "Search for sources again" said nothing at all. */}
          {stalled && searchError ? <div className="mt-0.5">Last search: {searchError}</div> : null}
          <div className="acts mt-2.5 empty:hidden">
            {stalled ? (
              <>
                {/* The files are already here; another source would fetch them
                    again. Matching them is the fix — or, for loose tracks with
                    no release, picking one. Reconcile matches against a release
                    the group's own tracks would be filed under, which a loose
                    group has none of. The server says which kind this is
                    (`loose`); an empty releaseMbid is not it, since an album
                    gap with no MBID on record has one too and still needs
                    Reconcile. */}
                {pickTracks.length ? (
                  <button className="primary"
                    onClick={() => goPlace(pickTracks[0].placeFolder, '', 'pick', pickTracks[0].placeFile)}>
                    {pickTracks.length > 1 ? `Pick the release (1 of ${pickTracks.length})` : 'Pick the release'}
                  </button>
                ) : !detail.loose ? (
                  <button className="primary" disabled={busy} onClick={reconcile}>Reconcile downloaded files</button>
                ) : null}
                {/* Only an album with a library copy has anything to rescan. */}
                {detail.albumId && (
                  <button disabled={rescanning} aria-busy={rescanning} onClick={rescanAlbum}>Rescan album</button>
                )}
                {totalSources > 0
                  ? <button disabled={busy} onClick={toSources}>Download again from another source</button>
                  : <button disabled={busy || searching} aria-busy={searching} onClick={() => findSources(true)}>Search for sources again</button>}
              </>
            ) : (
              <>
                {nextSource && (
                  <button disabled={busy} className="primary" onClick={() => fetchTracks(nextSource.id)}>
                    Try next source
                  </button>
                )}
                {totalSources > 0 && <button disabled={busy} onClick={toSources}>Pick a source</button>}
                {retryId != null && (
                  <button disabled={busy} onClick={() => fetchTracks(retryId)}>Retry same peer</button>
                )}
              </>
            )}
          </div>
          {err.logTail?.length ? (
            <details className="!mt-2.5 !border-0 !bg-transparent !p-0">
              <summary className="s !font-normal">What lb-bot logged</summary>
              <div className="logtail">{err.logTail.map((l, i) => <div key={i}>{l}</div>)}</div>
            </details>
          ) : null}
        </div>
      )}

      <div className="next">
        <Grp>Next step<span className="note">{hint}</span></Grp>
        <Roll open={showActs}>
          <div style={{ paddingTop: 10 }}>
            <div className="acts">
              <button className={!searching && totalSources > 0 ? '' : (err && nextSource ? '' : 'primary')}
                disabled={busy || searching} aria-busy={searching}
                onClick={() => findSources(totalSources > 0 || !!err)}>
                {searching ? `Searching Soulseek… ${elapsed ? `${elapsed} s` : ''}`
                  : totalSources > 0 ? 'Search again'
                  : `Find sources for ${tracksWord(missing)}`}
              </button>
              <button disabled={busy || searching} onClick={autoSelect}
                title="Search, rank, and download the best source automatically">
                Download the best match
              </button>
              {detail?.sourcesFoundAt && totalSources > 0 && !searching
                ? <span className="s">found {ago(detail.sourcesFoundAt)} ago</span> : null}
            </div>
            <Roll open={searching}>
              <div className="s clip" style={{ paddingTop: 8 }}>
                {srcTask?.status === 'running' && srcTask.current
                  ? srcTask.current : 'Waiting for peers to answer. They answer on their own schedule.'}
              </div>
            </Roll>
            {searchError && !err && <div className="s bad mt-2">Last search: {searchError}</div>}
            {detail && <Mp3Fallback detail={detail} busy={busy} onToggle={setAllowMp3} />}
          </div>
        </Roll>
      </div>

      {!detail && <p className="muted mt-4">Loading album detail…</p>}

      <Roll open={!!detail && downloading}>
        <Grp>Source<span className="note">
          {asking ? 'asking the peer' : mine.length ? 'downloading' : inFlight ? 'queued with the peer' : 'filing into the album'}
        </span></Grp>
        <div className="rows">
          <div className="row col">
            <div className="flex min-w-0 items-center gap-3">
              <div className="grow">
                <div className="t num clip">
                  {asking ? (pickedSrc?.peer ? `@${pickedSrc.peer}` : 'The source you picked')
                    : mine[0]?.username ? `@${mine[0].username}` : activeSrc?.peer ? `@${activeSrc.peer}` : 'Downloaded'}
                </div>
                <div className="s">
                  {asking
                    ? ([pickedSrc?.format, pickedSrc?.bitrate, pickedSrc?.size].filter(Boolean).join(' · ') || tracksWord(missing))
                    : mine.length || inFlight
                      ? [activeSrc?.format, activeSrc?.bitrate, activeSrc?.size].filter(Boolean).join(' · ')
                        || `${tracksWord(mine.length || inFlight)}`
                      : 'Nothing is transferring. The files are being placed and checked in Navidrome.'}
                </div>
              </div>
              <St tone="run">
                {asking ? 'Starting' : mine.length ? `Downloading ${activePct}%` : inFlight ? 'Queued' : 'Placing'}
              </St>
            </div>
            {(asking || mine.length > 0 || inFlight > 0) && (
              <div className="flex items-center gap-3 pt-2.5">
                <div className="grow flex flex-col gap-1.5">
                  <Meter lg value={asking ? 0 : activePct} />
                  <div className="s">
                    {asking
                      ? 'Queueing the files with this peer. If it refuses, lb-bot tries the next source on its own.'
                      : `${dlStat} · filed into the album when the download finishes`}
                  </div>
                </div>
                {!asking && <button className="sm" disabled={busy} onClick={cancel}>Cancel</button>}
              </div>
            )}
          </div>
        </div>
      </Roll>

      <div ref={sourcesRef}>
        <Roll open={showSources}>
          <Grp n={totalSources} right={<SourceFilters value={srcFilters} onChange={setSrcFilters} />}>
            Sources<span className="note">ranked by <button className="link plain underline"
              onClick={() => navigate('system', 'sources')}>your preferences</button></span>
          </Grp>
          {pageLoading && <p className="muted mb-2">Loading sources {srcPage * SOURCES_PER_PAGE + 1}–{Math.min((srcPage + 1) * SOURCES_PER_PAGE, totalSources)}…</p>}
          {filteredSources.length > 0 && (
            <div className="rows">
              {filteredSources.map(s => (
                <SourceRow key={s.id} src={s} busy={busy}
                  primary={!err && s.rank === 1}
                  onExpand={expandSource} onUse={fetchTracks} />
              ))}
            </div>
          )}
          {pageSources.length > 0 && !filteredSources.length && (
            <p className="muted">No source on this page matches these filters.</p>
          )}
          {pages > 1 && (
            <div className="mt-2.5"><Pager page={srcPage} pages={pages} onPage={setSrcPage} total={totalSources} pageSize={SOURCES_PER_PAGE} /></div>
          )}
        </Roll>
      </div>

      {detail && !flowOnly && (
        <>
          {/* One list. It is the tracks that need something; asked, it becomes
              the whole tracklist with those same rows in place. The whole one
              is a MusicBrainz lookup, so it is asked for rather than fetched
              for every album the cursor passes. */}
          <Grp n={whole ? `${g.present}/${g.total}` : (missingRows.length || '')}
            right={g.releaseMbid && (whole
              ? <button className="link" onClick={() => setWhole(null)}>Show only what is missing</button>
              : <button className="link" disabled={wholeLoading} aria-busy={wholeLoading} onClick={loadWhole}>
                  {wholeLoading ? 'Loading the whole tracklist…' : 'Show the whole tracklist'}
                </button>)}>
            {whole || complete ? 'Tracks' : 'Missing tracks'}
          </Grp>
          <div key={whole ? 'whole' : 'missing'} className="fadein">
            {whole && !whole.rows.length && <p className="muted mb-2">MusicBrainz has no tracklist for this release.</p>}
            <GapTracks detail={detail} reference={whole} />
          </div>
          <GapFoot detail={detail} bare={bare} hidden={hidden} onHide={onHide} />
        </>
      )}
    </>
  )
}
