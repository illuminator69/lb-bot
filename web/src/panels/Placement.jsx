import { useEffect, useMemo, useRef, useState } from 'react'
import { useApp, navigate, replaceRoute } from '../App.jsx'
import { api, post } from '../lib/api.js'
import Cover from '../components/Cover.jsx'
import Page, { Grp } from '../components/Page.jsx'
import { EmptyState, Notice, ProgressBar } from '../components/ui.jsx'

// Filing one downloaded folder into the library, by hand: either into an album
// the library has gaps in, or as a release picked on MusicBrainz. The folders
// themselves are listed under Needs you › To fix, and these pickers open there
// beside the list. <file> (relative to the folder) files that one file only: a
// loose track's download folder can hold other peers' unrelated tracks.

// Per-file result, shown once a placement task finishes.
function PerFileResult({ perFile }) {
  if (!perFile?.length) return null
  // 'bonus' is a success: a file with no tracklist slot, placed on its own tags.
  const color = status => (status === 'matched' || status === 'bonus') ? 'var(--ok)'
    : status === 'ambiguous' ? 'var(--wait)' : 'var(--bad)'
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
export function PlacementResult({ taskId, onDone, doneLabel = 'Done' }) {
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
  if (!task) return <p className="muted mt-3">Placing…</p>
  const finished = task.status === 'complete' || task.status === 'error'
  return (
    <div className="mt-4">
      <Notice tone={task.status === 'error' ? 'danger' : finished ? 'quiet' : 'warn'}
        title={task.status === 'error' ? 'Placement failed' : finished ? 'Placed' : 'Placing…'}
        actions={finished && onDone && <button className="sm" onClick={onDone}>{doneLabel}</button>}>
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
  if (folder === null) return <div className="mb-3"><Notice tone="danger">This folder is no longer in the downloads directory.</Notice></div>
  if (folder === false) return <div className="mb-3"><Notice>Couldn’t read the downloads folder list, so the file count below is unknown.</Notice></div>
  return null
}

/**
 * Match the folder to an album the library already has gaps in.
 * Rendered inside To fix beside the folder list, and on the #/place page.
 *   onPickRelease  switch to "it is a new album: pick the release"
 *   onDone         the placement finished and the user is moving on
 */
export function MatchToGap({ path, initialGroupId, onPickRelease, onDone, doneLabel = 'Done' }) {
  const { pushToast, scheduleRefresh } = useApp()
  const folder = useFolder(path)
  const [groups, setGroups] = useState(null)
  const [query, setQuery] = useState('')
  const [selectedId, setSelectedId] = useState(initialGroupId || '')
  const [taskId, setTaskId] = useState(null)
  const [placing, setPlacing] = useState(false)

  useEffect(() => {
    let dead = false
    // The gap list, once — it is what "an album with missing tracks" means.
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
      scheduleRefresh(1500)
    } catch (e) {
      pushToast(`Placement failed: ${e.message}`, 'error')
    } finally {
      setPlacing(false)
    }
  }

  return (
    <>
      <FolderMissing folder={folder} />
      {selected && (
        <>
          <Grp>File into</Grp>
          <div className="rows">
            <div className="row">
              <Cover albumId={selected.albumId} url={selected.coverUrl} name={selected.album} size={40} />
              <div className="grow">
                <div className="t clip">{selected.album}</div>
                <div className="s clip">{selected.artist} · {selected.missingCount} of {selected.total} tracks missing</div>
              </div>
              {!taskId && <button className="sm quiet" onClick={() => setSelectedId('')}>Choose another</button>}
            </div>
          </div>
          {folder && selected.missingCount && folder.file_count > selected.missingCount * 3 && (
            <p className="why">
              This folder holds {folder.file_count} files for {selected.missingCount} missing track(s). Placement only
              files tracks that match the album’s tracklist, but check it is the right album.
            </p>
          )}
          {!taskId && (
            <div className="acts mt-3">
              <button className="primary" onClick={place} disabled={placing || !folder} aria-busy={placing}>
                {placing ? 'Starting…' : `File ${folder?.file_count ?? ''} file(s) into this album`}
              </button>
            </div>
          )}
          {taskId && <PlacementResult taskId={taskId} onDone={onDone} doneLabel={doneLabel} />}
        </>
      )}

      {!taskId && !selected && (
        <>
          <Grp right={onPickRelease && <button className="link" onClick={onPickRelease}>Not in the list? Pick a release</button>}>
            The album it fills
            <span className="note">
              {groups === null ? 'loading albums…' : `${groups.length.toLocaleString()} albums with missing tracks`}
            </span>
          </Grp>
          <input type="search" className="mb-2.5 w-full max-w-[420px]" placeholder="Search artist or album"
            aria-label="Search albums with missing tracks" value={query} onChange={e => setQuery(e.target.value)} />
          {groups !== null && !filtered.length
            ? <p className="muted">No album with missing tracks matches.</p>
            : (
              <div className="rows">
                {filtered.map(g => (
                  <button key={g.id} className="row tap" onClick={() => setSelectedId(g.id)}>
                    <Cover albumId={g.albumId} url={g.coverUrl} name={g.album} size={40} />
                    <span className="grow">
                      <span className="t clip block">{g.album}</span>
                      <span className="s clip block">{g.artist}</span>
                    </span>
                    <span className="st wait">{g.missingCount} missing</span>
                  </button>
                ))}
              </div>
            )}
        </>
      )}
    </>
  )
}

/**
 * Pick a MusicBrainz release for a folder that is not a gap in an album the
 * library has — a brand-new album. `file` (relative to the folder) files that
 * one file only.
 */
export function PickRelease({ path, file, onMatchGap, onDone, doneLabel = 'Done' }) {
  const { pushToast, scheduleRefresh } = useApp()
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
      scheduleRefresh(1500)
    } catch (e) {
      pushToast(`Placement failed: ${e.message}`, 'error')
    } finally {
      setPlacing('')
    }
  }

  return (
    <>
      {file && <p className="why">Only <span className="num">{file}</span> will be filed. The rest of this folder stays where it is.</p>}
      <FolderMissing folder={folder} />
      {taskId ? <PlacementResult taskId={taskId} onDone={onDone} doneLabel={doneLabel} /> : (
        <>
          <Grp right={onMatchGap && <button className="link" onClick={onMatchGap}>It fills gaps in an album I have</button>}>
            Pick the release
          </Grp>
          <form className="mb-2.5 flex flex-wrap gap-2" onSubmit={e => { e.preventDefault(); search() }}>
            <input type="search" className="min-w-0 max-w-[420px] flex-1" placeholder="Artist – Album"
              aria-label="Search MusicBrainz releases" value={query} onChange={e => setQuery(e.target.value)} />
            <button type="submit" disabled={searching} aria-busy={searching}>
              {searching ? 'Searching…' : 'Search MusicBrainz'}
            </button>
          </form>
          {candidates !== null && (
            candidates.length === 0
              ? <p className="muted">No releases found. Try the artist and album name the way MusicBrainz spells them.</p>
              : (
                <div className="rows">
                  {candidates.map(c => (
                    <div key={c.release_mbid} className="row wrap">
                      <Cover url={c.cover_url} name={c.title || c.label} size={48} />
                      <div className="grow" style={{ minWidth: 160 }}>
                        <div className="t">{c.title || c.label}</div>
                        <div className="s">{[c.artist, c.date, c.country, c.format, c.type, c.packaging].filter(Boolean).join(' · ')}</div>
                        <div className="s num">
                          {c.track_count || 0} track(s)
                          {folder && c.track_count && folder.file_count !== c.track_count && !file
                            ? ` · folder has ${folder.file_count} file(s)` : ''}
                        </div>
                      </div>
                      <button className="sm whitespace-nowrap" onClick={() => place(c)}
                        disabled={!!placing} aria-busy={placing === c.release_mbid}>
                        {placing === c.release_mbid ? 'Starting…' : 'File as this release'}
                      </button>
                    </div>
                  ))}
                </div>
              )
          )}
        </>
      )}
    </>
  )
}

// The page: one downloaded folder, opened from a loose track's "Pick the
// release" or from a link. Route #/place/<folder path>[/<group id>|-[/pick[/<file>]]].
// The same two pickers sit inside Needs you › To fix for the folders listed there.
export default function Placement() {
  const { state } = useApp()
  const [path, groupId, mode, file] = state.routeParams
  const folder = useFolder(path || '')
  const toFix = () => navigate('needs', 'fix')
  if (!path) {
    return (
      <Page title="File a folder" fallbackBack={{ label: 'Needs you', onBack: toFix }}>
        <EmptyState title="No folder chosen" hint="Folders waiting to be filed are listed under Needs you, To fix." />
      </Page>
    )
  }
  const pick = mode === 'pick'
  return (
    <Page title={folder?.name || path.split('/').pop()}
      sub={[folder ? `${folder.file_count} ${folder.formats || ''} file(s)` : null, path].filter(Boolean).join(' · ')}
      fallbackBack={{ label: 'Needs you', onBack: toFix }}>
      <div style={{ maxWidth: 860 }}>
        {pick
          ? <PickRelease key={`${path}|${file || ''}`} path={path} file={file || ''}
              onMatchGap={file ? undefined : () => replaceRoute('place', path)}
              onDone={() => history.length > 1 ? history.back() : toFix()} doneLabel="Back" />
          : <MatchToGap key={path} path={path} initialGroupId={groupId && groupId !== '-' ? groupId : ''}
              onPickRelease={() => replaceRoute('place', path, '-', 'pick')}
              onDone={() => history.length > 1 ? history.back() : toFix()} doneLabel="Back" />}
      </div>
    </Page>
  )
}
