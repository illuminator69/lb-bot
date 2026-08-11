import { useState, useEffect, useMemo, useRef } from 'react'
import { useApp } from '../App.jsx'
import { api } from '../lib/api.js'
import { FolderCard, NavidromeMatchPanel, ReleasePicker } from '../components/FolderCard.jsx'
import { EmptyState, PageTitle } from '../components/ui.jsx'

// ── Main Import panel ─────────────────────────────────────────────────────────
export default function Import() {
  const { action, state, dispatch } = useApp()
  const [folders, setFolders] = useState(null)
  const [loading, setLoading] = useState(true)
  const [mode, setMode] = useState(null) // { type: 'match'|'pick', folder, initialSelectedGroupId }
  const [confirmingPath, setConfirmingPath] = useState('')
  const [taskByPath, setTaskByPath] = useState({}) // folder.path -> task_id
  const consumedTargetRef = useRef(false)

  async function load() {
    setLoading(true)
    try {
      const r = await api('/api/beets/folders')
      setFolders(r.folders || [])
    } catch {
      setFolders([])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const reviewGroups = useMemo(() => {
    const groups = state.review?.groups || []
    return groups
      .filter(g => (g.missing_tracks?.length > 0) && (g.canonical_mbid || g.release_mbid))
      .sort((a, b) => (a.artist + a.album).localeCompare(b.artist + b.album))
  }, [state.review])

  function done() { setMode(null); load() }

  // Deep-link route: #/import/<folderPath>/<groupId>
  useEffect(() => {
    if (consumedTargetRef.current) return
    if (!state.pendingImportTarget || !folders) return
    const target = state.pendingImportTarget
    const folder = folders.find(f => f.path === target.folderPath)
    if (folder) {
      const group = target.groupId ? reviewGroups.find(g => g.id === target.groupId) : null
      // mode "pick" comes from Downloads → "Not this — pick release": the folder
      // is not a review group, so the group matcher has nothing to offer it.
      setMode(target.mode === 'pick'
        ? { type: 'pick', folder }
        : { type: 'match', folder, initialSelectedGroupId: group ? group.id : '' })
    }
    consumedTargetRef.current = true
    dispatch({ type: 'SET_IMPORT_TARGET', folderPath: '' })
  }, [state.pendingImportTarget, folders, reviewGroups, dispatch])

  async function confirmSuggestion(folder) {
    const suggestion = folder.suggested_match
    if (!suggestion || confirmingPath) return
    const group = reviewGroups.find(g => g.id === suggestion.group_id)
    setConfirmingPath(folder.path)
    try {
      const r = await action('/api/beets/import', {
        path: folder.path,
        release_mbid: (group && (group.canonical_mbid || group.release_mbid)) || '',
        artist: suggestion.artist,
        album: suggestion.album,
        group_id: suggestion.group_id || '',
      })
      if (r.task_id) setTaskByPath(prev => ({ ...prev, [folder.path]: r.task_id }))
    } finally {
      setConfirmingPath('')
    }
  }

  if (mode?.type === 'match') {
    return (
      <NavidromeMatchPanel
        folder={mode.folder}
        reviewGroups={reviewGroups}
        onDone={done}
        action={action}
        initialSelectedGroupId={mode.initialSelectedGroupId}
      />
    )
  }

  if (mode?.type === 'pick') {
    return (
      <ReleasePicker
        folder={mode.folder}
        onDone={done}
        action={action}
      />
    )
  }

  return (
    <>
      <div className="mb-[18px]">
        <PageTitle eyebrow="Advanced" title="Import & placement" />
      </div>
      <p className="muted" style={{ marginBottom: 14 }}>
        Place downloaded folders into your music library.
        {reviewGroups.length > 0 && ` ${reviewGroups.length} album(s) with missing tracks available to match.`}
      </p>
      {loading && <p className="muted">Loading download folders…</p>}
      <div className="placement-grid">
        {(folders || []).length === 0 && !loading
          ? <EmptyState title="No folders waiting for import"
              hint="Folders appear here once slskd downloads complete." />
          : (folders || []).map(f => {
            const taskId = taskByPath[f.path]
            const taskResult = taskId ? state.tasks?.[taskId] : null
            const finished = taskResult && (taskResult.status === 'complete' || taskResult.status === 'error')
            return (
              <FolderCard
                key={f.path}
                folder={f}
                confirming={confirmingPath === f.path}
                taskResult={finished ? taskResult : null}
                onMatch={() => setMode({ type: 'match', folder: f })}
                onNewAlbum={() => setMode({ type: 'pick', folder: f })}
                onConfirmSuggestion={() => confirmSuggestion(f)}
              />
            )
          })
        }
      </div>
    </>
  )
}
