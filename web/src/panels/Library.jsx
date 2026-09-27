import { useCallback, useEffect, useRef, useState } from 'react'
import { useApp, navigate } from '../App.jsx'
import { api, post } from '../lib/api.js'
import Cover from '../components/Cover.jsx'
import { DuplicateFileSet, TrashPanel } from '../components/Duplicates.jsx'
import Artist from './Artist.jsx'
import {
  Chip, EmptyState, Pager, PageTitle, SectionHeader, SelectField, StatusChip, SubTabs,
} from '../components/ui.jsx'

// Library is what you have: your artists (with every album of theirs, owned or
// not), your albums as a table, and cleanup. Before 2026-09-26 the Library tab
// also carried an acquire-new search (now the header box) and two scan buttons
// (now Fill gaps → Scan), and Artist was its own tab.
const SUBS = [['artists', 'Artists'], ['albums', 'Albums'], ['cleanup', 'Cleanup']]

// ── Albums ───────────────────────────────────────────────────────────────────
// What each row's button does, by status — one verb per state.
const ROW_ACTION = {
  ready: 'Fill gaps',
  picking: 'Decide',
  downloading: 'View',
  failed: 'Recover',
  complete: 'Re-check',
}

const FILTERS = [
  ['all', 'All'],
  ['gaps', 'Has gaps'],
  ['working', 'Working'],
  ['decision', 'Needs a decision'],
  ['failed', 'Failed'],
  ['complete', 'Complete'],
]

const LIB_SORTS = [
  ['', 'Navidrome order'],
  ['artist', 'Artist A–Z'],
  ['album', 'Album A–Z'],
  ['year', 'Newest first'],
  ['missing', 'Most missing first'],
]

function AlbumsView() {
  const { state, dispatch, action, pushToast } = useApp()
  const { library, libFilter, libSearch, libSort } = state

  // Debounced: committing every keystroke fired one /api/library per character.
  const [draftSearch, setDraftSearch] = useState(libSearch)
  useEffect(() => {
    if (draftSearch === libSearch) return
    const t = setTimeout(() => dispatch({ type: 'SET_LIB_SEARCH', q: draftSearch }), 450)
    return () => clearTimeout(t)
  }, [draftSearch, libSearch, dispatch])

  function rowAction(row) {
    if (!row.groupId) return null
    const label = ROW_ACTION[row.status] || 'View'
    if (row.status === 'complete') {
      return (
        <button className="sm" title="Recompute which tracks are missing"
          onClick={() => action(`/api/groups/${row.groupId}/missing`)
            .then(() => pushToast(`Re-checking ${row.album}…`))
            .catch(e => pushToast(`Re-check failed: ${e.message}`, 'error'))}>
          {label}
        </button>
      )
    }
    return (
      <button className={`sm ${row.status === 'ready' ? 'primary' : ''}`} onClick={() => navigate('Fill gaps', row.groupId)}>
        {label}
      </button>
    )
  }

  return (
    <>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input type="search" placeholder="Filter by artist or album…" aria-label="Filter albums" className="w-60"
          value={draftSearch} onChange={e => setDraftSearch(e.target.value)} />
        {FILTERS.map(([k, label]) => (
          <Chip key={k} active={libFilter === k} onClick={() => dispatch({ type: 'SET_LIB_FILTER', filter: k })}>{label}</Chip>
        ))}
        <span className="spacer" />
        <SelectField label="Sort" value={libSort} options={LIB_SORTS} onChange={v => dispatch({ type: 'SET_LIB_SORT', sort: v })} />
      </div>

      {!library ? <p className="text-caption text-muted">Loading albums…</p> : (
        <>
          <div className="library-table">
            <div className="library-head library-row">
              <span>Album</span><span>Status</span><span>Tracks</span><span className="text-right">Action</span>
            </div>
            {!library.items.length && (
              <div className="library-row"><span className="text-caption text-muted">No albums match this filter.</span></div>
            )}
            {library.items.map(row => (
              <div className="library-row" key={row.id}>
                <div className="flex min-w-0 items-center gap-[11px]">
                  <Cover albumId={row.albumId || row.id} url={row.coverUrl} name={row.album} size={34} />
                  <div className="min-w-0">
                    <div className="truncate text-small font-semibold">{row.album}</div>
                    <div className="truncate text-micro text-muted">{row.artist}{row.year ? ` · ${row.year}` : ''}</div>
                  </div>
                </div>
                <span><StatusChip status={row.status} /></span>
                <span className="font-mono text-caption" style={{ color: 'var(--text2)' }}>{row.present}/{row.total}</span>
                <span className="text-right">{rowAction(row)}</span>
              </div>
            ))}
          </div>
          <div className="mt-3">
            <Pager page={library.page} pages={library.pages} total={library.total} pageSize={library.per || 50}
              onPage={p => dispatch({ type: 'SET_LIB_PAGE', page: p })} />
          </div>
        </>
      )}
    </>
  )
}

// ── Cleanup ──────────────────────────────────────────────────────────────────
// Duplicate albums live in their own scan results on the backend, so scanning
// here never resets the missing-tracks scan (and vice versa).
function DuplicateGroupCard({ g, onChanged }) {
  const { action, confirmAction, pushToast } = useApp()
  const [canonical, setCanonical] = useState(g.canonical_album_id || (g.albums?.[0]?.id ?? ''))
  const [merging, setMerging] = useState(false)
  const [mergeNote, setMergeNote] = useState('')

  async function pick(albumId) {
    setCanonical(albumId)
    try {
      await post(`/api/groups/${g.id}/canonical`, { album_id: albumId })
    } catch (e) {
      pushToast(`Could not save the pick: ${e.message}`, 'error')
    }
  }

  // Retag runs as a background task; wait for it and say what happened.
  async function merge() {
    setMerging(true)
    try {
      const r = await confirmAction(`Merge the duplicate copies of “${g.album}” into the version you kept?`,
        `/api/groups/${g.id}/retag`, {}, { confirmLabel: 'Merge' })
      if (!r) return
      setMergeNote('')
      if (r.task_id) {
        for (let i = 0; i < 60; i++) {
          await new Promise(res => setTimeout(res, 1000))
          let t
          try { t = await api(`/api/tasks/${r.task_id}`) } catch { continue }
          if (t.status === 'complete') { pushToast(`Merged “${g.album}”`); setMergeNote(t.summary || ''); break }
          if (t.status === 'error') { pushToast(`Merge failed: ${t.error || 'unknown error'}`, 'error'); setMergeNote(t.error || ''); break }
        }
      }
      onChanged()
    } catch (e) {
      pushToast(`Merge failed: ${e.message}`, 'error')
      setMergeNote(e.message)
    } finally {
      setMerging(false)
    }
  }

  return (
    <div className="workspace-card mb-3">
      <div className="text-caption font-semibold uppercase tracking-[.12em]" style={{ color: 'var(--accent)' }}>{g.artist}</div>
      <div className="mb-2.5 mt-[3px] text-title font-bold">{g.album}</div>
      <div className="mb-2.5 text-caption text-muted">
        {(g.albums || []).length} copies in the library — pick the one to keep, the rest merge into it.
      </div>
      {(g.albums || []).map(a => {
        const keep = a.id === canonical
        return (
          <button key={a.id} onClick={() => pick(a.id)} aria-pressed={keep}
            className="mb-2 !flex w-full items-center gap-3 !rounded-card !p-3 text-left"
            style={{ background: keep ? 'var(--sel-row)' : 'var(--inset-warm)', borderColor: keep ? 'var(--accent-bd-sel)' : 'var(--border)' }}>
            <Cover albumId={a.id} name={a.name} size={44} />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <b className="text-small">{a.name}</b>
                {keep && <span className="chip good !my-0">keep</span>}
              </div>
              <div className="mt-0.5 text-caption text-muted">{(a.tracks || []).length} track(s){a.year ? ` · ${a.year}` : ''}</div>
              {a.musicBrainzId && <div className="mt-0.5 font-mono text-micro text-faint">{a.musicBrainzId}</div>}
            </div>
          </button>
        )
      })}
      <div className="mt-3 flex items-center gap-2.5">
        <button className="primary" disabled={merging} onClick={merge}>{merging ? 'Merging…' : 'Merge duplicates'}</button>
        <button className="quiet" onClick={() => action(`/api/groups/${g.id}/hide`, { hidden: true }).then(onChanged)}>Ignore this set</button>
      </div>
      {mergeNote && (
        <pre className="mt-2.5 whitespace-pre-wrap rounded-ctl border border-line p-2.5 font-mono text-micro"
          style={{ background: 'var(--inset-deep)', color: 'var(--text2)' }}>{mergeNote}</pre>
      )}
    </div>
  )
}

// "No duplicate files" is also what a scan that never ran, a Navidrome that
// timed out, and a music path that doesn't line up look like. Say which.
function duplicateFilesEmptyHint(data) {
  const s = data?.stats || {}
  const base = 'Files inside one album that are the same song show up here — usually the leftovers of a gap fill that matched the wrong track.'
  if (!data?.scanned_at && !s.albums_scanned) return `${base} No duplicate scan has run yet — start one above.`
  if (s.files_missing_on_disk) {
    return `Found ${s.sets_stored} set(s) in the last scan, but ${s.files_missing_on_disk} of their files are not visible on disk — so nothing can be shown or deleted. Navidrome's paths and ${s.music_dir || 'the music directory'} are not lining up; e.g. ${s.sample_missing_path}. Check "Library path ↔ Navidrome" in Settings → Status.`
  }
  if (s.listing_partial) return `${base} The last scan only listed part of the library before Navidrome errored (${s.listing_error || 'unknown error'}), so this answer is incomplete.`
  if (s.albums_short || s.albums_empty) {
    return `${base} The last scan checked ${s.albums_scanned} of ${s.albums_total} album(s), but ${s.albums_short + s.albums_empty} returned no or partial track lists from Navidrome — those albums were not really examined.`
  }
  if (s.tracks_missing_path) return `${base} The last scan skipped ${s.tracks_missing_path} track(s) that Navidrome reported without a file path.`
  if (s.albums_scanned) return `${base} The last scan examined ${s.albums_scanned} album(s) and found none.`
  return `${base} Run a duplicate scan to refresh.`
}

function CleanupView() {
  const { action, pushToast } = useApp()
  const [data, setData] = useState(null)
  const [files, setFiles] = useState(null)
  const [fuzzy, setFuzzy] = useState(null)
  const [deep, setDeep] = useState(null)
  const [trashKey, setTrashKey] = useState(0)
  const pollRef = useRef(null)

  const load = useCallback(async () => {
    try {
      const [r, f] = await Promise.all([api('/api/duplicates'), api('/api/duplicate-files')])
      setData(r)
      setFiles(f)
      setFuzzy(v => (v === null ? !!r.fuzzy : v))
      setDeep(v => (v === null ? !!r.deep : v))
      return r
    } catch {
      return null
    }
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => () => clearInterval(pollRef.current), [])

  async function scan() {
    try {
      await action('/api/scan', { fuzzy: !!fuzzy, deep: !!deep })
      pushToast('Duplicate scan started')
      clearInterval(pollRef.current)
      pollRef.current = setInterval(async () => {
        const r = await load()
        if (r && r.status !== 'running') clearInterval(pollRef.current)
      }, 3000)
    } catch (e) {
      pushToast(`Scan failed: ${e.message}`, 'error')
    }
  }

  const groups = (data?.groups || []).filter(g => !g.hidden)
  const scanning = data?.status === 'running' && /duplicate/i.test(data?.message || '')
  const sets = files?.sets || []

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-2.5">
        <button className="primary" disabled={scanning} aria-busy={scanning} onClick={scan}>
          {scanning ? 'Scanning…' : 'Scan for duplicates'}
        </button>
        <label className="flex items-center gap-1.5 text-caption" title="Also group albums whose titles are nearly identical (deluxe/remaster editions)">
          <input type="checkbox" checked={!!fuzzy} onChange={e => setFuzzy(e.target.checked)} /> fuzzy titles
        </label>
        <label className="flex items-center gap-1.5 text-caption"
          title="Finds copies whose titles are in different languages, by confirming same-runtime albums against MusicBrainz. Slower — MusicBrainz is rate-limited.">
          <input type="checkbox" checked={!!deep} onChange={e => setDeep(e.target.checked)} /> match across languages
        </label>
        {scanning && <span className="text-caption text-muted">{data?.message}</span>}
      </div>

      <SectionHeader label="Duplicate albums" sub={data ? `${groups.length} set(s)` : ''} />
      {data === null ? <p className="text-caption text-muted">Loading…</p>
        : !groups.length && !scanning ? (
          <p className="mb-2 text-caption text-muted">No duplicate albums found. Fuzzy matching also catches near-identical titles.</p>
        ) : groups.map(g => <DuplicateGroupCard key={g.id} g={g} onChanged={load} />)}

      <SectionHeader className="mt-6" label="Duplicate files" sub={files ? `${sets.length} set(s)` : ''} />
      {files === null ? <p className="text-caption text-muted">Loading…</p>
        : !sets.length ? <EmptyState title="No duplicate files" hint={duplicateFilesEmptyHint(files)} />
        : sets.map((s, i) => (
          <DuplicateFileSet key={`${s.albumId}-${s.title}-${i}`} set={s} onDeleted={() => { load(); setTrashKey(k => k + 1) }} />
        ))}

      <SectionHeader className="mt-6" label="Trash" />
      <TrashPanel refreshKey={trashKey} />
      <p className="mt-2 text-caption text-muted">Deleted duplicates move here first, so a wrong call can be undone.</p>
    </>
  )
}

export default function Library() {
  const { state } = useApp()
  const params = state.routeParams
  const sub = SUBS.some(([k]) => k === params[0]) ? params[0] : 'artists'
  // The summary first: it refreshes on every poll of every screen, while the
  // table's own totals move only on the Albums sub-tab — on Artists or Cleanup
  // the title and the header stat disagreed after any change.
  const totals = state.summary?.library || state.library?.libraryTotals

  // The Artists sub-tab owns its own headline (artist name, album title).
  const inArtist = sub === 'artists' && params[1]

  return (
    <>
      {!inArtist && (
        <div className="mb-4 flex flex-wrap items-end gap-4">
          <PageTitle eyebrow="Library"
            title={totals?.albums
              ? `${totals.albums.toLocaleString()} albums · ${(totals.withGaps || 0).toLocaleString()} with gaps`
              : 'Library'} />
        </div>
      )}
      {!inArtist && <SubTabs items={SUBS} value={sub} onChange={k => navigate('Library', k)} label="Library sections" />}
      {sub === 'artists' && <Artist params={params.slice(1)} />}
      {sub === 'albums' && <AlbumsView />}
      {sub === 'cleanup' && <CleanupView />}
    </>
  )
}
