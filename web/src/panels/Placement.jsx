import { useEffect, useMemo, useRef, useState } from 'react'
import { useApp, navigate } from '../App.jsx'
import { api, post } from '../lib/api.js'
import Cover from '../components/Cover.jsx'
import { EmptyState, Notice, PageTitle, ProgressBar } from '../components/ui.jsx'

// Filing one downloaded folder into the library, by hand. This used to be the
// Advanced → Import tab: a second list of the download folders with its own
// one-tap suggestions. The list lives in Downloads now; this is only the
// drill-down it opens — route #/downloads/place/<folder path>[/<group id>|-[/pick[/<file>]]].
// <file> (relative to the folder) files that one file only: a loose track's
// download folder can hold other peers' unrelated tracks.

// Per-file result, shown once a placement task finishes.
function PerFileResult({ perFile }) {
  if (!perFile?.length) return null
  // 'bonus' is a success: a file with no tracklist slot, placed on its own tags.
  const color = status => (status === 'matched' || status === 'bonus') ? 'var(--green)'
    : status === 'ambiguous' ? 'var(--accent-2)' : 'var(--danger)'
  const mark = status => status === 'matched' ? '✓' : status === 'bonus' ? '+' : status === 'ambiguous' ? '~' : '!'
  return (
    <div className="mt-2 font-mono text-micro">
      {perFile.map((f, i) => (
        <div key={i} style={{ color: color(f.status) }}>
          {mark(f.status)} {f.file} — {f.status}
          {f.reason ? ` (${f.reason})` : ''}
        </div>
      ))}
    </div>
  )
}

// Watches the placement task and says how it ended — the old Import tab only
// learned about it on the next full poll of every task.
function PlacementResult({ taskId, onDone }) {
  const [task, setTask] = useState(null)
  useEffect(() => {
    let dead = false
    let timer
    const tick = async () => {
      try {
        const t = await api(`/api/tasks/${taskId}`)
        if (dead) return
        setTask(t)
        if (t.status === 'complete' || t.status === 'error') return
      } catch { /* poll again */ }
      if (!dead) timer = setTimeout(tick, 1500)
    }
    tick()
    return () => { dead = true; clearTimeout(timer) }
  }, [taskId])
  if (!task) return <p className="mt-3 text-caption text-muted">Placing…</p>
  const finished = task.status === 'complete' || task.status === 'error'
  return (
    <div className="mt-4">
      <Notice tone={task.status === 'error' ? 'danger' : finished ? 'quiet' : 'warn'}
        title={task.status === 'error' ? 'Placement failed' : finished ? 'Placed' : 'Placing…'}
        actions={finished && <button className="primary sm" onClick={onDone}>Back to Downloads</button>}>
        {!finished && <div className="mt-1"><ProgressBar value={task.percent || 10} height={5} /></div>}
        <div className="mt-1">{task.error || task.summary || task.current}</div>
        <PerFileResult perFile={task.per_file} />
      </Notice>
    </div>
  )
}

// undefined while loading, null when the folder list loaded without it, and
// false when the list itself couldn't be read — which says nothing about the
// folder and must not be reported as "gone".
function useFolder(path) {
  const [folder, setFolder] = useState(undefined)
  useEffect(() => {
    let dead = false
    api(`/api/download-folders?path=${encodeURIComponent(path)}`)
      .then(r => { if (!dead) setFolder((r.folders || []).find(f => f.path === path) || null) })
      .catch(() => { if (!dead) setFolder(false) })
    return () => { dead = true }
  }, [path])
  return folder
}

function FolderMissing({ folder }) {
  if (folder === null) return <div className="mb-4"><Notice tone="danger">This folder is no longer in the downloads directory.</Notice></div>
  if (folder === false) return <div className="mb-4"><Notice>Couldn't read the downloads folder list, so the file count below is unknown.</Notice></div>
  return null
}

function FolderHeader({ folder, path, title }) {
  return (
    <div className="mb-4 flex flex-wrap items-end gap-3">
      <button onClick={() => navigate('Downloads')}>← Downloads</button>
      <PageTitle eyebrow={title} title={folder?.name || path.split('/').pop()}>
        <p className="mt-1 text-caption text-muted">
          {folder ? `${folder.file_count} ${folder.formats || ''} file(s) · ` : ''}
          <span className="font-mono">{path}</span>
        </p>
      </PageTitle>
    </div>
  )
}

// Match the folder to an album the library already has gaps in.
function MatchToGap({ path, initialGroupId }) {
  const { pushToast } = useApp()
  const folder = useFolder(path)
  const [groups, setGroups] = useState(null)
  const [query, setQuery] = useState('')
  const [selectedId, setSelectedId] = useState(initialGroupId || '')
  const [taskId, setTaskId] = useState(null)
  const [placing, setPlacing] = useState(false)

  useEffect(() => {
    let dead = false
    // The gap list, once — it is what "an album with missing tracks" means.
    // The old Import tab polled the whole 14 MB review every 5 s for this.
    // Only groups that name a release: a playlist's "Loose tracks" group has
    // none, and /api/place-folder answers 400 for it.
    api('/api/gaps')
      .then(r => { if (!dead) setGroups((r.items || []).filter(g => g.missingCount > 0 && g.releaseMbid)) })
      .catch(e => { if (!dead) { setGroups([]); pushToast(`Could not load albums: ${e.message}`, 'error') } })
    return () => { dead = true }
  }, [pushToast])

  // Seed the search with the folder name once it's known.
  const seeded = useRef(false)
  useEffect(() => {
    if (seeded.current || !folder) return
    seeded.current = true
    if (!initialGroupId) setQuery(folder.name || '')
  }, [folder, initialGroupId])

  const selected = (groups || []).find(g => g.id === selectedId) || null
  const filtered = useMemo(() => {
    // Letters in any script: "[^a-z0-9]" turned "Кино" into no words at all
    // (so no filter) and "Björk" into "bj" + "rk".
    const words = query.toLowerCase().split(/[^\p{L}\p{N}]+/u)
      .filter(w => w.length > 1 || /[^\x00-\x7f]/.test(w))
    let rows = groups || []
    if (words.length) {
      rows = rows
        .map(g => {
          const hay = `${g.artist} ${g.album}`.toLowerCase()
          return [g, words.filter(w => hay.includes(w)).length]
        })
        .filter(([, n]) => n > 0)
        .sort((a, b) => b[1] - a[1])
        .map(([g]) => g)
    }
    return rows.slice(0, 60)
  }, [groups, query])

  async function place() {
    if (!selected || placing) return
    setPlacing(true)
    try {
      const r = await post('/api/place-folder', { path, group_id: selected.id })
      setTaskId(r.task_id)
    } catch (e) {
      pushToast(`Placement failed: ${e.message}`, 'error')
    } finally {
      setPlacing(false)
    }
  }

  return (
    <>
      <FolderHeader folder={folder} path={path} title="Fill missing tracks" />
      <FolderMissing folder={folder} />
      {selected && (
        <div className="workspace-card mb-4">
          <div className="text-caption font-semibold uppercase tracking-[.1em] text-muted">File into</div>
          <div className="mt-1 flex items-center gap-3">
            <Cover albumId={selected.albumId} url={selected.coverUrl} name={selected.album} size={56} />
            <div className="min-w-0 flex-1">
              <div className="text-lead font-semibold">{selected.album}</div>
              <div className="text-small text-muted">{selected.artist} · {selected.missingCount} of {selected.total} tracks missing</div>
            </div>
          </div>
          {folder && selected.missingCount && folder.file_count > selected.missingCount * 3 && (
            <div className="mt-3"><Notice>
              This folder holds {folder.file_count} files for {selected.missingCount} missing track(s). Placement only
              files tracks that match the album's tracklist, but check it is the right album.
            </Notice></div>
          )}
          {!taskId && (
            <div className="mt-3.5 flex flex-wrap gap-2">
              <button className="primary" onClick={place} disabled={placing || !folder} aria-busy={placing}>
                {placing ? 'Starting…' : `File ${folder?.file_count ?? ''} file(s) into this album`}
              </button>
              <button onClick={() => setSelectedId('')}>Choose another</button>
            </div>
          )}
          {taskId && <PlacementResult taskId={taskId} onDone={() => navigate('Downloads')} />}
        </div>
      )}

      {!taskId && (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-2.5">
            <input type="search" autoFocus className="w-full max-w-[420px]" placeholder="Search artist or album…"
              aria-label="Search albums with missing tracks" value={query} onChange={e => setQuery(e.target.value)} />
            <span className="text-caption text-muted">
              {groups === null ? 'Loading albums…' : `${groups.length.toLocaleString()} albums with missing tracks`}
            </span>
            <span className="spacer" />
            <button onClick={() => navigate('Downloads', 'place', path, '-', 'pick')}>Not in the list — pick a release</button>
          </div>
          <div className="settings-grid">
            {groups !== null && !filtered.length && <p className="text-caption text-muted">No albums with missing tracks match.</p>}
            {filtered.map(g => (
              <button key={g.id} onClick={() => setSelectedId(g.id)}
                className="!flex items-center gap-3 !rounded-card !p-3 text-left"
                style={{
                  background: g.id === selectedId ? 'var(--sel-row)' : 'var(--surface)',
                  borderColor: g.id === selectedId ? 'var(--accent-bd-sel)' : 'var(--border)',
                }}>
                <Cover albumId={g.albumId} url={g.coverUrl} name={g.album} size={40} />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-small font-semibold">{g.album}</div>
                  <div className="truncate text-caption text-muted">{g.artist} · {g.missingCount}/{g.total} missing</div>
                </div>
              </button>
            ))}
          </div>
        </>
      )}
    </>
  )
}

// Pick a MusicBrainz release for a folder that is not a gap in an album the
// library has — a brand-new album.
function PickRelease({ path, file }) {
  const { pushToast } = useApp()
  const folder = useFolder(path)
  const [query, setQuery] = useState('')
  const [candidates, setCandidates] = useState(null)
  const [searching, setSearching] = useState(false)
  const [taskId, setTaskId] = useState(null)
  const [placing, setPlacing] = useState('')

  async function search(q = query) {
    if (!q.trim()) return
    setSearching(true)
    setCandidates(null)
    try {
      const r = await post('/api/place-folder/candidates', { path, query: q.trim() })
      setCandidates(r.candidates || [])
    } catch (e) {
      setCandidates([])
      pushToast(`Release search failed: ${e.message}`, 'error')
    } finally {
      setSearching(false)
    }
  }

  // Arriving here means "identify this folder", so run the first search.
  const searched = useRef(false)
  useEffect(() => {
    if (searched.current || !folder) return
    searched.current = true
    const seed = (file ? file.split('/').pop().replace(/\.[^.]+$/, '').replace(/^\d+\s*[-.]\s*/, '') : '')
      || folder.suggested_release_label || folder.name || ''
    setQuery(seed)
    if (seed.trim()) search(seed)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [folder])

  async function place(c) {
    if (placing) return
    setPlacing(c.release_mbid)
    try {
      const r = await post('/api/place-folder', {
        path, release_mbid: c.release_mbid || '', artist: c.artist || '', album: c.title || '',
        ...(file ? { only_relpaths: [file] } : {}),
      })
      setTaskId(r.task_id)
    } catch (e) {
      pushToast(`Placement failed: ${e.message}`, 'error')
    } finally {
      setPlacing('')
    }
  }

  return (
    <>
      <FolderHeader folder={folder} path={path} title="Pick the release" />
      {file && <div className="mb-3"><Notice>Only <span className="font-mono">{file}</span> will be filed — the rest of this folder stays where it is.</Notice></div>}
      <FolderMissing folder={folder} />
      {taskId ? <PlacementResult taskId={taskId} onDone={() => navigate('Downloads')} /> : (
        <>
          <form className="mb-3 flex flex-wrap gap-2" onSubmit={e => { e.preventDefault(); search() }}>
            <input type="search" className="min-w-0 max-w-[420px] flex-1" placeholder="Artist – Album…" autoFocus
              aria-label="Search MusicBrainz releases" value={query} onChange={e => setQuery(e.target.value)} />
            <button className="primary" type="submit" disabled={searching} aria-busy={searching}>
              {searching ? 'Searching…' : 'Search MusicBrainz'}
            </button>
            <span className="spacer" />
            <button type="button" onClick={() => navigate('Downloads', 'place', path)}>It fills gaps in an album I have</button>
          </form>
          {candidates !== null && (
            candidates.length === 0
              ? <EmptyState title="No releases found" hint="Try the artist and album name the way MusicBrainz spells them." />
              : candidates.map(c => (
                <div key={c.release_mbid}
                  className="mb-2 flex flex-wrap items-start gap-3 rounded-card border p-3"
                  style={{ background: 'var(--inset-warm)', borderColor: 'var(--border)' }}>
                  <Cover url={c.cover_url} name={c.title || c.label} size={64} />
                  <div className="min-w-[180px] flex-1">
                    <b className="text-body">{c.title || c.label}</b>
                    <div className="text-caption text-muted">{c.artist || ''}</div>
                    <div className="text-caption text-muted">{[c.date, c.country, c.format, c.type, c.packaging].filter(Boolean).join(' · ')}</div>
                    <div className="mt-0.5 font-mono text-micro text-faint">
                      {c.track_count || 0} track(s)
                      {folder && c.track_count && folder.file_count !== c.track_count
                        ? ` · folder has ${folder.file_count} file(s)` : ''}
                    </div>
                  </div>
                  <button className="primary self-center whitespace-nowrap" onClick={() => place(c)}
                    disabled={!!placing} aria-busy={placing === c.release_mbid}>
                    {placing === c.release_mbid ? 'Starting…' : 'File as this release'}
                  </button>
                </div>
              ))
          )}
        </>
      )}
    </>
  )
}

export default function Placement({ params }) {
  // params: [<folder path>, <group id> | '-', <mode>, <file>]
  const [path, groupId, mode, file] = params
  if (!path) return <EmptyState title="No folder chosen" />
  return mode === 'pick'
    ? <PickRelease key={`${path}|${file || ''}`} path={path} file={file || ''} />
    : <MatchToGap key={path} path={path} initialGroupId={groupId && groupId !== '-' ? groupId : ''} />
}
