import { useCallback, useEffect, useRef, useState } from 'react'
import { useApp, navigate, goGap } from '../App.jsx'
import { api, post } from '../lib/api.js'
import Cover from '../components/Cover.jsx'
import { DuplicateFileSet, TrashPanel } from '../components/Duplicates.jsx'
import Page, { Grp, Pick, Seg } from '../components/Page.jsx'
import { ArtistIndex } from './Artist.jsx'
import { Pager, StatusChip } from '../components/ui.jsx'

// Library is what you have: your artists (each opens their whole discography,
// owned or not), your albums as a list, and cleanup.
const TABS = ['artists', 'albums', 'cleanup']

function LibraryFrame({ tab, bar, action, children }) {
  const { state } = useApp()
  // The summary: it refreshes on every poll of every screen, while the album
  // list's own totals move only on the Albums tab.
  const totals = state.summary?.library || state.library?.libraryTotals
  const artists = state.summary?.index?.total
  const sub = [
    artists ? `${artists.toLocaleString()} artists` : null,
    totals?.albums ? `${totals.albums.toLocaleString()} albums` : null,
    totals?.albums ? `${(totals.withGaps || 0).toLocaleString()} with gaps` : null,
  ].filter(Boolean).join(' · ')
  return (
    <Page title="Library" sub={sub} action={action}
      tabs={[['artists', 'Artists', artists ?? null], ['albums', 'Albums', totals?.albums ?? null], ['cleanup', 'Cleanup']]}
      tab={tab} onTab={k => navigate('library', k)} bar={bar}>
      {children}
    </Page>
  )
}

// ── Albums ───────────────────────────────────────────────────────────────────
const SPLIT = [['all', 'All'], ['gaps', 'With gaps'], ['complete', 'Complete']]
const MORE = [['', 'Any status'], ['working', 'Working'], ['decision', 'Needs a decision'], ['failed', 'Failed']]

const LIB_SORTS = [
  ['', 'Navidrome order'],
  ['artist', 'Artist A to Z'],
  ['album', 'Album A to Z'],
  ['year', 'Newest first'],
  ['missing', 'Most missing'],
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

  const setFilter = f => dispatch({ type: 'SET_LIB_FILTER', filter: f || 'all' })
  const inSplit = SPLIT.some(([k]) => k === libFilter)

  // One verb per state.
  function rowAction(row) {
    if (!row.groupId) return null
    if (row.status === 'complete') {
      return (
        <button className="sm quiet" title="Recompute which tracks are missing"
          onClick={() => action(`/api/groups/${row.groupId}/missing`)
            .then(() => pushToast(`Re-checking ${row.album}`))
            .catch(e => pushToast(`Re-check failed: ${e.message}`, 'error'))}>
          Re-check
        </button>
      )
    }
    if (row.status === 'failed') {
      return <button className="sm" onClick={() => navigate('needs', 'fix', `gap:${row.groupId}`)}>Fix</button>
    }
    return (
      <button className="sm" onClick={() => goGap(row.groupId)}>
        {row.status === 'downloading' ? 'View' : 'Find sources'}
      </button>
    )
  }

  const bar = (
    <>
      <Seg label="Which albums" value={inSplit ? libFilter : ''} onChange={setFilter} options={SPLIT} />
      <input className="find" type="search" placeholder="Filter by artist or album" aria-label="Filter albums"
        value={draftSearch} onChange={e => setDraftSearch(e.target.value)} />
      <Pick label="Status" value={inSplit ? '' : libFilter} onChange={setFilter} options={MORE} />
      <Pick label="Sort" value={libSort} onChange={v => dispatch({ type: 'SET_LIB_SORT', sort: v })} options={LIB_SORTS} />
    </>
  )

  return (
    <LibraryFrame tab="albums" bar={bar}>
      {!library ? <p className="muted">{state.pollError ? 'Waiting for lb-bot…' : 'Loading albums…'}</p> : (
        <>
          {!library.items.length ? <p className="empty">No album matches this filter.</p> : (
            <div className="rows">
              {library.items.map(row => (
                <div className="row stack" key={row.id}>
                  <Cover albumId={row.albumId || row.id} url={row.coverUrl} name={row.album} size={40} />
                  <div className="grow">
                    {row.groupId && row.status !== 'complete'
                      ? <button className="link plain t clip block max-w-full" onClick={() => goGap(row.groupId)}>{row.album}</button>
                      : <div className="t clip">{row.album}</div>}
                    <div className="s clip">{row.artist}{row.year ? ` · ${row.year}` : ''}</div>
                  </div>
                  <span className="num muted hide-sm">{row.present}/{row.total}</span>
                  <StatusChip status={row.status}
                    word={row.status === 'ready' ? `${Math.max(0, row.total - row.present)} missing`
                      : row.status === 'complete' ? 'In library' : undefined} />
                  {rowAction(row)}
                </div>
              ))}
            </div>
          )}
          <div className="mt-3">
            <Pager page={library.page} pages={library.pages} total={library.total} pageSize={library.per || 50}
              onPage={p => dispatch({ type: 'SET_LIB_PAGE', page: p })} />
          </div>
        </>
      )}
    </LibraryFrame>
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

  const copies = g.albums || []
  return (
    <div className="rows mb-2.5">
      <div className="row wrap">
        <div className="grow" style={{ minWidth: 160 }}>
          <div className="t">{g.album}</div>
          <div className="s">{g.artist} · {copies.length} copies. Pick the one to keep; the rest merge into it.</div>
        </div>
        <button className="sm" disabled={merging} aria-busy={merging} onClick={merge}>
          {merging ? 'Merging…' : 'Merge duplicates'}
        </button>
        <button className="sm quiet" title="Not duplicates. Take this set off the list"
          onClick={() => action(`/api/groups/${g.id}/hide`, { hidden: true }).then(onChanged)
            .catch(e => pushToast(`Could not ignore the set: ${e.message}`, 'error'))}>
          Ignore this set
        </button>
      </div>
      {copies.map(a => {
        const keep = a.id === canonical
        return (
          <button key={a.id} className="row tap" onClick={() => pick(a.id)} aria-pressed={keep} aria-selected={keep}>
            <Cover albumId={a.id} name={a.name} size={40} />
            <span className="grow">
              <span className="t clip block">{a.name}</span>
              <span className="s clip block">
                {(a.tracks || []).length} track(s){a.year ? ` · ${a.year}` : ''}
                {a.musicBrainzId ? <span className="num"> · {a.musicBrainzId}</span> : null}
              </span>
            </span>
            {keep ? <span className="st ok">Keep</span> : <span className="st">Merge into the kept copy</span>}
          </button>
        )
      })}
      {mergeNote && <div className="row"><pre className="num m-0 whitespace-pre-wrap" style={{ color: 'var(--fg2)' }}>{mergeNote}</pre></div>}
    </div>
  )
}

// "No duplicate files" is also what a scan that never ran, a Navidrome that
// timed out, and a music path that doesn't line up look like. Say which.
function duplicateFilesEmptyHint(data) {
  const s = data?.stats || {}
  const base = 'Files inside one album that are the same song show up here. They are usually the leftovers of a gap fill that matched the wrong track.'
  if (!data?.scanned_at && !s.albums_scanned) return `${base} No duplicate scan has run yet. Start one above.`
  if (s.files_missing_on_disk) {
    return `Found ${s.sets_stored} set(s) in the last scan, but ${s.files_missing_on_disk} of their files are not visible on disk, so nothing can be shown or deleted. Navidrome's paths and ${s.music_dir || 'the music directory'} are not lining up, for example ${s.sample_missing_path}. Check "Library path ↔ Navidrome" under System, Status.`
  }
  if (s.listing_partial) return `${base} The last scan only listed part of the library before Navidrome errored (${s.listing_error || 'unknown error'}), so this answer is incomplete.`
  if (s.albums_short || s.albums_empty) {
    return `${base} The last scan checked ${s.albums_scanned} of ${s.albums_total} album(s), but ${s.albums_short + s.albums_empty} returned no or partial track lists from Navidrome, so those albums were not really examined.`
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

  const bar = (
    <>
      <button disabled={scanning} aria-busy={scanning} onClick={scan}>
        {scanning ? 'Scanning…' : 'Scan for duplicates'}
      </button>
      <label className="flex items-center gap-1.5" title="Also group albums whose titles are nearly identical (deluxe and remaster editions)">
        <input type="checkbox" checked={!!fuzzy} onChange={e => setFuzzy(e.target.checked)} /> Fuzzy titles
      </label>
      <label className="flex items-center gap-1.5"
        title="Finds copies whose titles are in different languages, by confirming same-runtime albums against MusicBrainz. Slower: MusicBrainz is rate-limited.">
        <input type="checkbox" checked={!!deep} onChange={e => setDeep(e.target.checked)} /> Match across languages
      </label>
      {scanning && <span className="st run clip">{data?.message}</span>}
    </>
  )

  return (
    <LibraryFrame tab="cleanup" bar={bar}>
      <Grp n={data ? groups.length : ''}>Duplicate albums</Grp>
      {data === null ? <p className="muted">Loading…</p>
        : !groups.length && !scanning ? (
          <p className="muted">No duplicate albums found. Fuzzy titles also catches near-identical names.</p>
        ) : groups.map(g => <DuplicateGroupCard key={g.id} g={g} onChanged={load} />)}

      <Grp n={files ? sets.length : ''}>Duplicate files</Grp>
      {files === null ? <p className="muted">Loading…</p>
        : !sets.length ? <p className="muted" style={{ maxWidth: '80ch' }}>{duplicateFilesEmptyHint(files)}</p>
        : sets.map((s, i) => (
          <DuplicateFileSet key={`${s.albumId}-${s.title}-${i}`} set={s} onDeleted={() => { load(); setTrashKey(k => k + 1) }} />
        ))}

      <Grp>Trash</Grp>
      <TrashPanel refreshKey={trashKey} />
      <p className="muted mt-2">Deleted duplicates move here first, so a wrong call can be undone.</p>
    </LibraryFrame>
  )
}

export default function Library() {
  const { state } = useApp()
  const tab = TABS.includes(state.routeParams[0]) ? state.routeParams[0] : 'artists'
  if (tab === 'albums') return <AlbumsView />
  if (tab === 'cleanup') return <CleanupView />
  return <ArtistIndex frame={({ bar, children }) => <LibraryFrame tab="artists" bar={bar}>{children}</LibraryFrame>} />
}
