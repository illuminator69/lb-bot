import { useState, useMemo, useEffect, useRef } from 'react'
import { post } from '../lib/api.js'
import Cover from './Cover.jsx'

// Shared "a downloaded folder needs placement" UI, used by the Import panel.

// Per-file result list, shown inline after a placement task finishes.
export function PerFileResult({ perFile }) {
  if (!perFile || perFile.length === 0) return null
  const color = status => {
    if (status === 'matched') return 'var(--green)'
    if (status === 'matched (fallback)') return 'var(--accent-2)'
    if (status === 'ambiguous') return 'var(--accent-2)'
    return 'var(--danger)'
  }
  return (
    <div style={{ marginTop: 8, fontSize: '0.85em' }}>
      {perFile.map((f, i) => (
        <div key={i} style={{ color: color(f.status) }}>
          {f.status === 'matched' ? '✓' : f.status === 'matched (fallback)' ? '~' : '!'} {f.file} — {f.status}
          {f.reason ? ` (${f.reason})` : ''}
        </div>
      ))}
    </div>
  )
}

// Match to existing Navidrome album (a group with missing tracks).
export function NavidromeMatchPanel({ folder, reviewGroups, onDone, action, initialSelectedGroupId }) {
  const [query, setQuery] = useState(folder.name || '')
  const [selected, setSelected] = useState(() =>
    initialSelectedGroupId ? reviewGroups.find(g => g.id === initialSelectedGroupId) || null : null)
  const [placing, setPlacing] = useState(false)

  const filtered = useMemo(() => {
    const q = query.toLowerCase()
    if (!q) return reviewGroups.slice(0, 60)
    return reviewGroups.filter(g =>
      (g.artist + ' ' + g.album).toLowerCase().includes(q)
    ).slice(0, 60)
  }, [reviewGroups, query])

  async function place() {
    if (!selected || placing) return
    setPlacing(true)
    try {
      await action('/api/beets/import', {
        path: folder.path,
        release_mbid: selected.canonical_mbid || selected.release_mbid || '',
        artist: selected.artist,
        album: selected.album,
        group_id: selected.id || '',
      })
      onDone()
    } finally {
      setPlacing(false)
    }
  }

  return (
    <>
      <div className="mb-3.5 flex items-center gap-2.5">
        <button onClick={onDone}>← Back</button>
        <div className="text-[20px] font-bold">Match to existing album: {folder.name}</div>
      </div>
      <p className="muted" style={{ marginBottom: 12 }}>
        {folder.file_count} {folder.formats} file(s) · {folder.path}
      </p>

      {selected && (
        <div className="card" style={{ marginBottom: 14, outline: '2px solid var(--accent)' }}>
          <b>{selected.artist} — {selected.album}</b>
          <div className="muted">
            {selected.missing_tracks?.length ?? '?'} missing track(s) ·{' '}
            {selected.canonical_mbid || selected.release_mbid || 'no MBID'}
          </div>
          <div className="toolbar" style={{ marginTop: 8 }}>
            <button className="primary" onClick={place} disabled={placing}>
              {placing ? 'Placing…' : `Copy ${folder.file_count} file(s) into library`}
            </button>
            <button onClick={() => setSelected(null)}>Clear</button>
          </div>
        </div>
      )}

      <input
        type="text"
        placeholder="Search artist or album…"
        value={query}
        onChange={e => setQuery(e.target.value)}
        style={{ width: '100%', maxWidth: 420, marginBottom: 12 }}
        autoFocus
      />

      <div className="settings-grid">
        {filtered.length === 0
          ? <p className="muted">No matching albums in the missing list.</p>
          : filtered.map(g => {
            const gid = g.id || (g.artist + '|' + g.album)
            const isSelected = selected && (selected.id === g.id || (!g.id && selected.artist === g.artist && selected.album === g.album))
            return (
              <div
                key={gid}
                className="cursor-pointer rounded-[11px] border p-3"
                style={{
                  background: isSelected ? 'var(--sel-row)' : 'var(--surface)',
                  borderColor: isSelected ? 'var(--accent-bd-sel)' : 'var(--border)',
                }}
                onClick={() => setSelected(g)}
              >
                <b className="text-[14px]">{g.artist}</b>
                <div className="text-[13.5px]">{g.album}</div>
                <div className="muted mt-0.5">
                  {g.missing_tracks?.length ?? '?'} missing / {g.total ?? '?'} total
                </div>
              </div>
            )
          })
        }
      </div>
    </>
  )
}

// Pick a MusicBrainz release for a brand-new album (no existing group).
export function ReleasePicker({ folder, onDone, action }) {
  const [query, setQuery] = useState(folder.suggested_release_label || folder.name || '')
  const [candidates, setCandidates] = useState(null)
  const [searching, setSearching] = useState(false)
  const [placing, setPlacing] = useState(false)
  const autoSearchedRef = useRef(false)

  async function search(q = query) {
    if (!q.trim()) return
    setSearching(true)
    setCandidates(null)
    try {
      const r = await post('/api/beets/release-candidates', { path: folder.path, query: q.trim() })
      setCandidates(r.candidates || [])
    } finally {
      setSearching(false)
    }
  }

  // Arriving here always means "identify this folder", so run the first search
  // rather than presenting an empty box over a query we already filled in.
  useEffect(() => {
    if (autoSearchedRef.current) return
    autoSearchedRef.current = true
    const seed = folder.suggested_release_label || folder.name || ''
    if (seed.trim()) search(seed)
  }, [folder.path])

  async function place(c) {
    if (placing) return
    setPlacing(true)
    try {
      await action('/api/beets/import', {
        path: folder.path,
        release_mbid: c.release_mbid || '',
        artist: c.artist || '',
        album: c.title || '',
      })
      onDone()
    } finally {
      setPlacing(false)
    }
  }

  return (
    <>
      <div className="mb-3.5 flex items-center gap-2.5">
        <button onClick={onDone}>← Back</button>
        <div className="text-[20px] font-bold">Pick release: {folder.name}</div>
      </div>
      <p className="muted" style={{ marginBottom: 12 }}>
        {folder.file_count} {folder.formats} file(s) · {folder.path}
      </p>

      <div className="toolbar" style={{ marginBottom: 12 }}>
        <input
          type="text"
          placeholder="Artist - Album name…"
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && search()}
          style={{ flex: 1, maxWidth: 420 }}
          autoFocus
        />
        <button className="primary" onClick={() => search()} disabled={searching}>
          {searching ? 'Searching…' : 'Search MusicBrainz'}
        </button>
      </div>

      {candidates !== null && (
        candidates.length === 0
          ? <p className="muted">No releases found. Try a different search.</p>
          : <div>
            {candidates.map(c => (
              <div key={c.release_mbid}
                className="mb-2 flex items-start gap-3 rounded-[11px] border p-3"
                style={{ background: 'var(--inset-warm)', borderColor: 'var(--border)' }}>
                <Cover url={c.cover_url} name={c.title || c.label} size={64} />
                <div className="min-w-0 flex-1">
                  <b className="text-[14px]">{c.title || c.label}</b>
                  <div className="muted">{c.artist || ''}</div>
                  <div className="muted">{[c.date, c.country, c.format, c.type, c.packaging].filter(Boolean).join(' · ')}</div>
                  <div className="mt-0.5 font-mono text-[11px] text-faint">{c.release_mbid} · {c.track_count || 0} track(s)</div>
                </div>
                <button
                  className="primary self-center whitespace-nowrap"
                  onClick={() => place(c)}
                  disabled={placing}
                >
                  {placing ? 'Placing…' : 'Place into library'}
                </button>
              </div>
            ))}
          </div>
      )}
    </>
  )
}

// Folder card: shows suggested match (default), or Confirm/result state.
export function FolderCard({ folder, onMatch, onNewAlbum, onConfirmSuggestion, confirming, taskResult }) {
  const suggestion = folder.suggested_match
  return (
    <div className="rounded-[12px] border p-[15px]" key={folder.path}
      style={{ background: 'var(--surface)', borderColor: 'var(--accent-bd-soft)' }}>
      <div className="flex items-center gap-2">
        <b className="text-[14.5px]">{folder.name}</b>
        <span className="chip good !my-0">ready</span>
      </div>
      <div className="muted mt-0.5">{folder.file_count} {folder.formats} file(s)</div>
      <div className="truncate font-mono text-[11px] text-faint" title={folder.path}>{folder.path}</div>

      {taskResult ? (
        <>
          <div className="muted" style={{ marginTop: 8 }}>{taskResult.summary || taskResult.error}</div>
          <PerFileResult perFile={taskResult.per_file} />
        </>
      ) : suggestion ? (
        <>
          <div style={{ marginTop: 8 }}>
            Suggested: <b>{suggestion.artist} — {suggestion.album}</b>
            <div className="muted">{suggestion.missing} missing track(s)</div>
          </div>
          <div className="toolbar" style={{ marginTop: 8 }}>
            <button className="primary" onClick={onConfirmSuggestion} disabled={confirming}>
              {confirming ? 'Placing…' : 'Confirm match'}
            </button>
            <button onClick={onMatch}>Not this one → search</button>
          </div>
        </>
      ) : (
        <div className="toolbar" style={{ marginTop: 8 }}>
          <button className="primary" onClick={onMatch}>Fill missing tracks</button>
          <button onClick={onNewAlbum}>New album (pick release)</button>
        </div>
      )}
    </div>
  )
}
