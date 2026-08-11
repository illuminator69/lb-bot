import { useCallback, useEffect, useRef, useState } from 'react'
import { useApp, navigate } from '../App.jsx'
import { api, get, post } from '../lib/api.js'
import Cover from '../components/Cover.jsx'
import { Badge, Chip, EmptyState, EMPTY_SOURCE_FILTERS, filterSources, PageTitle, SourceFilters, SourceRow, StatusChip } from '../components/ui.jsx'

const STATUS_WORD = {
  ready: 'Has gaps',
  picking: 'Needs decision',
  downloading: 'Working',
  failed: 'Failed',
  complete: 'Complete',
}

// Context action per row status, mirroring the mock's action column.
const ROW_ACTION = {
  ready: 'Fill gaps',
  picking: 'Choose',
  downloading: 'View',
  failed: 'Recover',
  complete: 'Rescan',
}

const FILTERS = [
  ['all', 'All'],
  ['gaps', 'Has gaps'],
  ['working', 'Working'],
  ['decision', 'Needs decision'],
  ['failed', 'Failed'],
  ['complete', 'Complete'],
]

// In-place search results: MusicBrainz release picker + slskd source list.
function SearchResults({ query, onBack }) {
  const { action, pushToast } = useApp()
  const [candidates, setCandidates] = useState(null) // release-group candidates
  const [rg, setRg] = useState(null)                 // picked release group
  const [releases, setReleases] = useState(null)     // editions of picked rg
  const [selRelease, setSelRelease] = useState(0)
  const [search, setSearch] = useState(null)         // slskd search snapshot
  const [requested, setRequested] = useState({})     // folder index -> true
  const [downloading, setDownloading] = useState(false)
  const [srcFilters, setSrcFilters] = useState(EMPTY_SOURCE_FILTERS)
  const searchIdRef = useRef(null)
  const pollRef = useRef(null)

  // Kick off both halves on mount: MB lookup and an slskd search.
  useEffect(() => {
    let dead = false
    api('/api/album/lookup?q=' + encodeURIComponent(query))
      .then(r => {
        if (dead) return
        const cands = (r.candidates || []).slice(0, 4)
        setCandidates(cands)
        if (cands[0]) pickRg(cands[0])
      })
      .catch(e => !dead && pushToast(`Album lookup failed: ${e.message}`, 'error'))
    action('/api/search', { query }).catch(e => pushToast(`slskd search failed: ${e.message}`, 'error'))
    return () => { dead = true; if (pollRef.current) clearInterval(pollRef.current) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query])

  // Poll /api/searches until our query's folders land, then a few more times
  // for stragglers.
  useEffect(() => {
    let ticks = 0
    pollRef.current = setInterval(async () => {
      ticks += 1
      try {
        const all = await api('/api/searches')
        const entries = Object.values(all || {})
          .filter(s => (s.query || '').toLowerCase() === query.toLowerCase())
        const latest = entries[entries.length - 1]
        if (latest) {
          searchIdRef.current = latest.id
          setSearch(latest)
          if ((latest.folders || []).length && ticks > 4) clearInterval(pollRef.current)
        }
      } catch { /* poll again */ }
      if (ticks > 20) clearInterval(pollRef.current)
    }, 3000)
    return () => clearInterval(pollRef.current)
  }, [query])

  function pickRg(candidate) {
    setRg(candidate)
    setReleases(null)
    setSelRelease(0)
    api('/api/album/releases?rgid=' + encodeURIComponent(candidate.rgid))
      .then(r => setReleases(r.releases || []))
      .catch(() => setReleases([]))
  }

  const release = releases?.[selRelease]
  const trackCount = release?.trackCount || 0

  const sources = (search?.folders || []).slice(0, 12).map((f, i) => {
    const files = (f.files || []).length
    const ext = ((f.files || [])[0]?.filename || '').split('.').pop()
    return {
      id: i,
      format: /flac|opus|mp3|ogg|m4a/i.test(ext) ? ext : '?',
      peer: f.username,
      speedMbps: f.upload_speed ? (f.upload_speed / 1024 / 1024).toFixed(1) : null,
      queueLength: f.queue_length ?? null,
      coverage: trackCount ? `${Math.min(files, trackCount)}/${trackCount} tracks` : `${files} files`,
      coverageFull: trackCount ? files >= trackCount : false,
      flags: [],
    }
  })

  async function request(idx) {
    const sid = searchIdRef.current
    if (sid == null) return
    setRequested(r => ({ ...r, [idx]: true })) // optimistic
    try {
      await action(`/api/searches/${sid}/download`, { index: idx, mode: 'folder' })
    } catch (e) {
      setRequested(r => ({ ...r, [idx]: false }))
      pushToast(`Request failed: ${e.message}`, 'error')
    }
  }

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="text-[15px] font-semibold">Results for “{query}”</div>
        <span className="text-[12.5px] text-faint">
          {sources.length} sources{releases?.length ? ` · ${releases.length} releases` : ''}
        </span>
        <span className="spacer" />
        <button onClick={onBack}>← Back to library</button>
      </div>

      {candidates === null ? (
        <p className="muted">Looking up “{query}” on MusicBrainz…</p>
      ) : !candidates.length ? (
        <p className="muted">No MusicBrainz match for “{query}” — slskd results below still work.</p>
      ) : (
        <div className="hero mb-4 flex items-start gap-[22px] rounded-[14px] border border-line bg-panel p-5">
          <div className="shrink-0" style={{ boxShadow: '0 10px 26px -8px rgba(0,0,0,.6)', borderRadius: 12 }}>
            <Cover url={rg ? `https://coverartarchive.org/release-group/${rg.rgid}/front-250` : null}
              name={rg?.title || query} size={112} />
          </div>
          <div className="min-w-0 flex-1">
            {candidates.length > 1 && (
              <div className="mb-2 flex flex-wrap gap-1.5">
                {candidates.map(c => (
                  <button key={c.rgid} className={rg?.rgid === c.rgid ? 'active' : ''}
                    onClick={() => pickRg(c)}>
                    {c.title}{c.year ? ` (${c.year})` : ''}
                  </button>
                ))}
              </div>
            )}
            {rg && (
              <>
                <div className="text-[12px] font-semibold uppercase tracking-[.12em]" style={{ color: 'var(--accent)' }}>
                  {rg.artist}
                </div>
                <div className="mb-1 mt-0.5 text-[24px] font-bold leading-[1.1]">{rg.title}</div>
                <div className="muted mb-3.5 text-[12.5px]">
                  Matched on MusicBrainz — pick which release to target, then request a source.
                </div>
                <div className="mb-2 text-[11px] font-semibold uppercase tracking-[.06em] text-faint">Release</div>
                <div className="flex flex-wrap gap-2">
                  {releases === null && <span className="muted">Loading editions…</span>}
                  {(releases || []).map((r, i) => {
                    const active = i === selRelease
                    return (
                      <button key={r.releaseMbid}
                        className="!rounded-[10px] px-3.5 text-left"
                        style={{
                          borderColor: active ? 'var(--accent)' : 'var(--border)',
                          background: active ? 'var(--accent-tint)' : 'var(--inset-warm)',
                          color: active ? 'var(--accent)' : 'var(--text2)',
                        }}
                        onClick={() => setSelRelease(i)}>
                        <div className="font-semibold">
                          {r.disambiguation || (i === 0 ? 'Original' : r.title !== rg.title ? r.title : r.year || 'Edition')}
                        </div>
                        <div className="text-[11.5px] opacity-80">
                          {r.trackCount} tracks{r.year ? ` · ${r.year}` : ''}
                        </div>
                      </button>
                    )
                  })}
                </div>
                {release && (
                  <div className="mt-3.5">
                    <button className="primary" disabled={downloading} onClick={async () => {
                      setDownloading(true)
                      try {
                        await action('/api/album/download', {
                          release_mbid: release.releaseMbid,
                          artist: rg.artist, title: rg.title,
                          total_tracks: release.trackCount,
                        })
                        pushToast('Album download started — see Downloads')
                      } catch (e) {
                        pushToast(`Download failed: ${e.message}`, 'error')
                        setDownloading(false)
                      }
                    }}>
                      {downloading ? '✓ Requested — see Downloads' : 'Get this album →'}
                    </button>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}

      <div className="mb-[11px] flex flex-wrap items-center gap-2.5">
        <div className="text-[13px] font-semibold tracking-[.02em]" style={{ color: 'var(--text2)' }}>AVAILABLE ON SLSKD</div>
        <span className="h-px flex-1" style={{ background: 'var(--border)' }} />
        {sources.length > 0 && <SourceFilters value={srcFilters} onChange={setSrcFilters} />}
        <span className="text-[11.5px] text-faint">ranked by your source preferences</span>
      </div>
      {!search && <p className="muted">Searching slskd…</p>}
      {search && !sources.length && <p className="muted">No slskd results yet for “{query}” — peers can take ~10s to answer.</p>}
      {(() => {
        const visible = filterSources(sources, srcFilters)
        if (sources.length > 0 && !visible.length) {
          return <p className="muted">No sources match these filters — loosen them to see all {sources.length}.</p>
        }
        return visible.map(s => (
          <SourceRow key={s.id} src={s}
            actionLabel="Request →"
            done={!!requested[s.id]}
            doneLabel="✓ Requested — see Downloads"
            onUse={() => request(s.id)} />
        ))
      })()}
    </>
  )
}

// ── Duplicates view ───────────────────────────────────────────────────────────
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
      pushToast(`Could not save canonical pick: ${e.message}`, 'error')
    }
  }

  // Retag runs as a background task, and its outcome used to go nowhere: no
  // catch here, and TaskCards (the only thing that renders task.error) isn't
  // mounted on this screen. So a merge blocked by a missing canonical MBID, an
  // unresolvable path or an unwritable file looked exactly like a merge that
  // worked. Wait for the task and say what happened.
  async function merge() {
    setMerging(true)
    try {
      const r = await confirmAction(
        `Merge the duplicate copies of “${g.album}” into the version you kept?`,
        `/api/groups/${g.id}/retag`)
      if (!r) return
      setMergeNote('')
      if (r.task_id) {
        for (let i = 0; i < 60; i++) {
          await new Promise(res => setTimeout(res, 1000))
          let t
          try { t = await api(`/api/tasks/${r.task_id}`) } catch { continue }
          if (t.status === 'complete') {
            pushToast(`Merged “${g.album}”`)
            setMergeNote(t.summary || '')
            break
          }
          if (t.status === 'error') {
            pushToast(`Merge failed: ${t.error || 'unknown error'}`, 'error')
            setMergeNote(t.error || '')
            break
          }
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
      <div className="text-[12px] font-semibold uppercase tracking-[.12em]" style={{ color: 'var(--accent)' }}>{g.artist}</div>
      <div className="mb-2.5 mt-[3px] text-[19px] font-bold leading-[1.15]">{g.album}</div>
      <div className="muted mb-2.5 text-[12px]">
        {(g.albums || []).length} copies in the library — pick the one to keep, the rest merge into it.
      </div>
      {(g.albums || []).map(a => {
        const keep = a.id === canonical
        return (
          <div key={a.id}
            className="mb-2 flex cursor-pointer items-center gap-3 rounded-[11px] border p-3"
            style={{
              background: keep ? 'var(--sel-row)' : 'var(--inset-warm)',
              borderColor: keep ? 'var(--accent-bd-sel)' : 'var(--border)',
            }}
            onClick={() => pick(a.id)}>
            <Cover albumId={a.id} name={a.name} size={44} />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <b className="text-[13.5px]">{a.name}</b>
                {keep && <span className="chip good !my-0">keep</span>}
              </div>
              <div className="muted mt-0.5 text-[12px]">
                {(a.tracks || []).length} track(s){a.year ? ` · ${a.year}` : ''}
              </div>
              {a.musicBrainzId && <div className="mt-0.5 font-mono text-[11px] text-faint">{a.musicBrainzId}</div>}
            </div>
          </div>
        )
      })}
      <div className="mt-3 flex items-center gap-2.5">
        <button className="primary" disabled={merging} onClick={merge}>
          {merging ? 'Merging…' : 'Merge duplicates'}
        </button>
        <button onClick={() => action(`/api/groups/${g.id}/hide`, { hidden: true }).then(onChanged)}>
          Ignore this set
        </button>
      </div>
      {mergeNote && (
        <pre className="mt-2.5 whitespace-pre-wrap rounded-[9px] border border-line p-2.5 font-mono text-[11.5px]"
          style={{ background: 'var(--inset-deep)', color: 'var(--text2)' }}>
          {mergeNote}
        </pre>
      )}
    </div>
  )
}

function fmtSize(n) {
  if (!n) return null
  return n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`
}

// How confident the backend is that these really are the same song. The evidence
// is named rather than implied: identical audio is proof, matching tags are a
// strong signal. There used to be a third, "same stream shape" — duration and
// sample rate for lossy files — which grouped two different songs of equal
// length; it is gone. See _duplicate_file_sets.
const MATCH_BASIS = {
  audio: { label: 'identical audio', chip: 'good',
           hint: 'These files decode to byte-identical audio — the same recording, filed twice.' },
  tags: { label: 'same tags', chip: 'warn',
          hint: 'Same title or recording MBID. Check they are not two different takes that share a name.' },
}

// One song that exists twice inside a single album. The user picks which copy
// survives — lb-bot only ever proposes, because "same song" is a judgement it
// can get wrong (alternate takes, live versions sharing a title).
export function DuplicateFileSet({ set: dup, onDeleted }) {
  const { confirmAction, pushToast } = useApp()
  const [gone, setGone] = useState({})
  const [busy, setBusy] = useState(null)
  // Which copy the set proposes keeping. The backend ranks it, but the user can
  // re-point it — and only the *other* rows can be deleted, so emptying a set
  // takes a deliberate change of mind rather than clicking Delete down the list.
  const [keepId, setKeepId] = useState(null)
  // Identity (st_dev:st_ino), not the path string: one file can be named two
  // ways, which is exactly the bug that made the best copy look deletable.
  const idOf = f => f.identity || f.path
  const files = (dup.files || []).filter(f => !gone[idOf(f)])
  if (files.length < 2) return null

  const basis = MATCH_BASIS[dup.matchBasis] || MATCH_BASIS.tags
  const keep = files.find(f => idOf(f) === keepId)
    || files.find(f => f.recommendedKeep)
    || files[0]

  async function remove(file) {
    setBusy(idOf(file))
    try {
      const r = await confirmAction(
        `Delete this file from the library?\n\nIt moves to the trash and can be restored.\n\n${file.path}\n\n${basis.hint}`,
        '/api/library/delete-file', { path: file.path, confirm: true })
      // null = the user cancelled at the confirm; leave the row alone.
      if (!r) return
      // Hide only on success. The optimistic hide used to fire even when the
      // delete failed, so a refused file looked gone until the next poll.
      setGone(g => ({ ...g, [idOf(file)]: true }))
      pushToast('Moved to trash — Navidrome is rescanning')
      onDeleted?.()
    } catch (e) {
      pushToast(e.payload?.code === 'last_copy'
        ? `Refused: ${e.message}`
        : `Delete failed: ${e.message}`, 'error')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="mb-2.5 rounded-[11px] border border-line bg-panel p-3.5">
      <div className="flex flex-wrap items-baseline gap-2">
        <b className="text-[13.5px]">{dup.track ? `${dup.track}. ` : ''}{dup.title}</b>
        <span className="muted text-[12px]">{dup.artist} — {dup.album}</span>
        <span className="spacer" />
        <span className={`chip ${basis.chip} !my-0`} title={basis.hint}>{basis.label}</span>
        <span className="chip warn !my-0">{files.length} copies</span>
      </div>
      {files.map(f => {
        const isKeep = f === keep
        return (
          <div key={idOf(f)}
            className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-[9px] border p-2.5"
            style={{
              background: isKeep ? 'var(--sel-row)' : 'var(--inset-warm)',
              borderColor: isKeep ? 'var(--accent-bd-sel)' : 'var(--border)',
            }}>
            <Badge format={f.format} />
            <span className="font-mono text-[12px]" style={{ color: 'var(--text2)' }}>
              {[f.bitRate ? `${f.bitRate} kbps` : null, fmtSize(f.size)].filter(Boolean).join(' · ')}
            </span>
            {isKeep && <span className="chip good !my-0">keeping this one</span>}
            {f.onlyOnDisk && (
              <span className="chip warn !my-0" title="On disk but not indexed by Navidrome yet — usually a file placed in the last few minutes.">
                not in Navidrome
              </span>
            )}
            <span className="spacer" />
            <span className="muted min-w-0 flex-1 truncate font-mono text-[11px]" title={f.path}>
              {f.path}
            </span>
            {isKeep
              // No Delete on the copy the set is keeping. Deleting every copy of
              // a song has to start with saying which one you'd rather keep.
              ? <span className="text-[11.5px] text-faint">kept</span>
              : (
                <>
                  <button className="!py-1 !text-[12px]"
                    title="Make this the copy that survives; the current one becomes deletable instead."
                    onClick={() => setKeepId(idOf(f))}>keep this one instead</button>
                  <button className="!py-1 !text-[12px]" disabled={busy === idOf(f)}
                    aria-busy={busy === idOf(f)}
                    style={{ color: 'var(--danger)' }}
                    onClick={() => remove(f)}>Delete</button>
                </>
              )}
          </div>
        )
      })}
    </div>
  )
}

function fmtWhen(ts) {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleString()
}

// Deleting a duplicate is a move into LB_BOT_TRASH_DIR, not an unlink. This is
// the way back — without it a wrong call about "same song" is unrecoverable, and
// that is exactly how good files were lost.
export function TrashPanel({ refreshKey }) {
  const { pushToast } = useApp()
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(null)

  const load = useCallback(() => {
    get('/api/library/trash').then(setData).catch(() => setData(null))
  }, [])
  useEffect(() => { load() }, [load, refreshKey])

  const files = data?.files || []
  if (!files.length) return null

  async function restore(f) {
    setBusy(f.trashPath)
    try {
      await post('/api/library/trash/restore', { path: f.trashPath })
      pushToast('Restored — Navidrome is rescanning')
      load()
    } catch (e) {
      pushToast(`Restore failed: ${e.message}`, 'error')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="mt-4 rounded-[11px] border border-line bg-panel p-3.5">
      <div className="flex flex-wrap items-baseline gap-2">
        <b className="text-[13.5px]">Trash</b>
        <span className="muted text-[12px]">
          {files.length} deleted file(s) in {data?.trashDir} — still recoverable
        </span>
      </div>
      {files.slice(0, 25).map(f => (
        <div key={f.trashPath}
          className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-[9px] border p-2.5"
          style={{ background: 'var(--inset-warm)', borderColor: 'var(--border)' }}>
          <span className="muted min-w-0 flex-1 truncate font-mono text-[11px]" title={f.originalPath}>
            {f.originalPath}
          </span>
          <span className="text-[11.5px] text-faint">{fmtWhen(f.deletedAt)}</span>
          <span className="font-mono text-[11px] text-faint">{fmtSize(f.size)}</span>
          {f.restorable
            ? <button className="!py-1 !text-[12px]" disabled={busy === f.trashPath}
                aria-busy={busy === f.trashPath}
                onClick={() => restore(f)}>Restore</button>
            : <span className="text-[11.5px] text-faint" title="Something already occupies the original path.">
                original path occupied
              </span>}
        </div>
      ))}
    </div>
  )
}

// "No duplicate files" is the right answer for a clean library, but it is also
// what a scan that never ran, a Navidrome that timed out, and a music path that
// doesn't line up all look like. The backend now counts what it actually did;
// say which one this is rather than always implying the happy case.
function duplicateFilesEmptyHint(data) {
  const s = data?.stats || {}
  const base = 'Files inside one album that are the same song show up here — usually the leftovers of a gap fill that matched the wrong track.'
  if (!data?.scanned_at && !s.albums_scanned) {
    return `${base} No duplicate scan has run yet — start one from Duplicate albums above.`
  }
  if (s.files_missing_on_disk) {
    return `Found ${s.sets_stored} set(s) in the last scan, but ${s.files_missing_on_disk} of their files are not visible on disk — so nothing can be shown or deleted. Navidrome's paths and ${s.music_dir || 'the music directory'} are not lining up; e.g. ${s.sample_missing_path}. Check the "Library path ↔ Navidrome" diagnostic in System.`
  }
  if (s.listing_partial) {
    return `${base} The last scan only listed part of the library before Navidrome errored (${s.listing_error || 'unknown error'}), so this answer is incomplete.`
  }
  if (s.albums_short || s.albums_empty) {
    return `${base} The last scan checked ${s.albums_scanned} of ${s.albums_total} album(s), but ${s.albums_short + s.albums_empty} returned no or partial track lists from Navidrome — those albums were not really examined.`
  }
  if (s.tracks_missing_path) {
    return `${base} The last scan skipped ${s.tracks_missing_path} track(s) that Navidrome reported without a file path.`
  }
  if (s.albums_scanned) {
    return `${base} The last scan examined ${s.albums_scanned} album(s) and found none.`
  }
  return `${base} Run a duplicate scan to refresh.`
}

function DuplicateFilesView() {
  const [data, setData] = useState(null)
  const [deletes, setDeletes] = useState(0)
  const load = useCallback(async () => {
    try { setData(await api('/api/duplicate-files')) } catch { /* keep last */ }
    setDeletes(n => n + 1)
  }, [])
  useEffect(() => { load() }, [load])

  const sets = data?.sets || []
  const emptyHint = duplicateFilesEmptyHint(data)
  return (
    <div className="mt-6">
      <div className="mb-2.5 flex items-center gap-2.5">
        <div className="text-[13px] font-semibold tracking-[.02em]" style={{ color: 'var(--text2)' }}>
          DUPLICATE FILES
        </div>
        <span className="h-px flex-1" style={{ background: 'var(--border)' }} />
        <span className="muted text-[12px]">{sets.length} set(s)</span>
      </div>
      {data === null ? <p className="muted">Loading…</p> : !sets.length ? (
        <EmptyState title="No duplicate files" hint={emptyHint} />
      ) : sets.map((s, i) => (
        <DuplicateFileSet key={`${s.albumId}-${s.title}-${i}`} set={s} onDeleted={load} />
      ))}
      <TrashPanel refreshKey={deletes} />
    </div>
  )
}

function DuplicatesView() {
  const { action, pushToast } = useApp()
  const [data, setData] = useState(null)   // { groups, status, message, fuzzy, deep }
  const [fuzzy, setFuzzy] = useState(null) // null until first load seeds it
  const [deep, setDeep] = useState(null)   // likewise — the backend persists it
  const pollRef = useRef(null)

  const load = useCallback(async () => {
    try {
      const r = await api('/api/duplicates')
      setData(r)
      setFuzzy(f => (f === null ? !!r.fuzzy : f))
      setDeep(d => (d === null ? !!r.deep : d))
      return r
    } catch {
      return null
    }
  }, [])

  useEffect(() => { load() }, [load])
  useEffect(() => () => clearInterval(pollRef.current), [])

  function pollWhileScanning() {
    clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      const r = await load()
      if (r && r.status !== 'running') clearInterval(pollRef.current)
    }, 3000)
  }

  async function scan() {
    try {
      await action('/api/scan', { fuzzy: !!fuzzy, deep: !!deep })
      pushToast('Duplicate scan started')
      pollWhileScanning()
    } catch (e) {
      pushToast(`Scan failed: ${e.message}`, 'error')
    }
  }

  const groups = (data?.groups || []).filter(g => !g.hidden)
  const scanning = data?.status === 'running' && /duplicate/i.test(data?.message || '')

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-2.5">
        <button className="primary" disabled={scanning} onClick={scan}>
          {scanning ? 'Scanning…' : 'Scan for duplicates'}
        </button>
        <label className="text-[12.5px]" title="Also group albums whose titles are nearly identical (deluxe/remaster editions)">
          <input type="checkbox" checked={!!fuzzy} onChange={e => setFuzzy(e.target.checked)} />
          {' '}fuzzy matching
        </label>
        <label className="text-[12.5px]"
          title="Finds copies whose titles are in different languages, by confirming same-runtime albums against their MusicBrainz release-group or their per-track runtimes. Slower — MusicBrainz is rate-limited and untagged albums need one extra Navidrome read each.">
          <input type="checkbox" checked={!!deep} onChange={e => setDeep(e.target.checked)} />
          {' '}match across languages
        </label>
        {scanning && <span className="muted">{data?.message}</span>}
        <span className="spacer" />
        {data && <span className="muted">{groups.length} duplicate set(s)</span>}
      </div>
      {data === null && <p className="muted">Loading duplicate scan results…</p>}
      {data !== null && !groups.length && !scanning && (
        <EmptyState title="No duplicate albums found"
          hint="Run a scan to compare every album in the library. Fuzzy matching also catches near-identical titles." />
      )}
      {groups.map(g => <DuplicateGroupCard key={g.id} g={g} onChanged={load} />)}
      <DuplicateFilesView />
    </>
  )
}

export default function Library() {
  const { state, dispatch, action, pushToast } = useApp()
  const { library, libFilter, libSearch, libPage, routeParams } = state
  const [addQuery, setAddQuery] = useState('')
  const [activeSearch, setActiveSearch] = useState(null)
  // 'albums' | 'duplicates' — from the route (#/library vs #/library/duplicates)
  // so the toggle is a history entry and survives a refresh.
  const view = routeParams[0] === 'duplicates' ? 'duplicates' : 'albums'

  // The filter box is local and debounced: committing every keystroke to
  // libSearch fired one /api/library round trip per character. 300ms was still
  // inside a normal typing cadence, so it fired mid-word anyway — 450ms waits
  // for an actual pause.
  const [draftSearch, setDraftSearch] = useState(libSearch)
  useEffect(() => {
    if (draftSearch === libSearch) return
    const t = setTimeout(() => dispatch({ type: 'SET_LIB_SEARCH', q: draftSearch }), 450)
    return () => clearTimeout(t)
  }, [draftSearch, libSearch, dispatch])

  const totals = library?.libraryTotals

  if (activeSearch) {
    return <SearchResults query={activeSearch} onBack={() => setActiveSearch(null)} />
  }

  function rowAction(row) {
    if (!row.groupId) return null
    const label = ROW_ACTION[row.status] || 'View'
    if (row.status === 'complete') {
      return (
        <button onClick={() => action(`/api/groups/${row.groupId}/missing`)
          .then(() => pushToast(`Rescanning ${row.album}…`))
          .catch(e => pushToast(`Rescan failed: ${e.message}`, 'error'))}>
          {label}
        </button>
      )
    }
    return (
      <button className={row.status === 'ready' ? 'primary' : ''}
        onClick={() => navigate('Fill gaps', row.groupId)}>
        {label}
      </button>
    )
  }

  return (
    <>
      <div className="mb-[18px] flex items-end gap-4">
        <PageTitle eyebrow="Library"
          title={totals
            ? `${totals.albums.toLocaleString()} albums · ${totals.withGaps} with gaps`
            : 'Library'} />
        <span className="spacer" />
        <div className="flex gap-1.5 pb-0.5">
          <Chip active={view === 'albums'} onClick={() => navigate('Library')}>Albums</Chip>
          <Chip active={view === 'duplicates'} onClick={() => navigate('Library', 'duplicates')}>Duplicates</Chip>
        </div>
      </div>

      {view === 'duplicates' && <DuplicatesView />}

      {view === 'albums' && <>
      <div className="mb-[18px] rounded-[12px] border border-line bg-panel px-4 py-3.5">
        <div className="flex flex-wrap items-center gap-2.5">
          <span className="whitespace-nowrap text-[12px] font-semibold uppercase tracking-[.08em] text-muted">Add music</span>
          <input className="min-w-0 flex-1" placeholder="Artist – Album, or paste a playlist link…"
            value={addQuery} onChange={e => setAddQuery(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && addQuery.trim() && setActiveSearch(addQuery.trim())} />
          <button className="primary" disabled={!addQuery.trim()}
            onClick={() => setActiveSearch(addQuery.trim())}
            title="Match on MusicBrainz and find sources on slskd">Search</button>
          <button onClick={() => action('/api/playlists/scan')}>Scan playlists</button>
        </div>
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input placeholder="Filter this list…" className="w-60" value={draftSearch}
          onChange={e => setDraftSearch(e.target.value)} />
        {FILTERS.map(([k, label]) => (
          <Chip key={k} active={libFilter === k}
            onClick={() => dispatch({ type: 'SET_LIB_FILTER', filter: k })}>{label}</Chip>
        ))}
        <span className="spacer" />
        <button onClick={async () => {
          await action('/api/scan-all')
          pushToast('Library scan started — albums with gaps will appear in Fill gaps')
        }}>Scan all for missing</button>
      </div>

      {!library ? <p className="muted">Loading library…</p> : (
        <>
          <div className="library-table">
            <div className="library-head library-row">
              <span>Album</span><span>Status</span><span>Tracks</span><span>Action</span>
            </div>
            {!library.items.length && (
              <div className="library-row">
                <span className="muted">No albums match this filter.</span>
              </div>
            )}
            {library.items.map(row => (
              <div className="library-row" key={row.id}>
                <div className="flex min-w-0 items-center gap-[11px]">
                  <Cover albumId={row.albumId || row.id} url={row.coverUrl} name={row.album} size={34} />
                  <div className="min-w-0">
                    <div className="truncate text-[13.5px] font-semibold">{row.album}</div>
                    <div className="muted truncate text-[11.5px]">{row.artist}{row.year ? ` · ${row.year}` : ''}</div>
                  </div>
                </div>
                <span><StatusChip status={row.status} word={STATUS_WORD[row.status]} /></span>
                <span className="font-mono text-[12.5px]" style={{ color: 'var(--text2)' }}>{row.present}/{row.total}</span>
                <span className="text-right">{rowAction(row)}</span>
              </div>
            ))}
          </div>
          <div className="toolbar mt-3">
            <button disabled={libPage <= 0}
              onClick={() => dispatch({ type: 'SET_LIB_PAGE', page: libPage - 1 })}>← Prev</button>
            <span className="muted">Page {library.page + 1} of {library.pages} · {library.total} album(s)</span>
            <button disabled={library.page + 1 >= library.pages}
              onClick={() => dispatch({ type: 'SET_LIB_PAGE', page: libPage + 1 })}>Next →</button>
          </div>
        </>
      )}
      </>}
    </>
  )
}
