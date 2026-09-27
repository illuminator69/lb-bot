import { useCallback, useEffect, useState } from 'react'
import { useApp } from '../App.jsx'
import { get, post } from '../lib/api.js'
import { Badge } from './ui.jsx'

// Duplicate-file sets and the trash, shared by Fill gaps (one album) and
// Library → Cleanup (the whole library).

export function fmtSize(n) {
  if (!n) return null
  return n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`
}

// How confident the backend is that these really are the same song. Identical
// audio is proof; matching tags are a strong signal. (A third basis, "same
// stream shape", grouped different songs of equal length and is gone.)
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
        `Move this file to the trash? It can be restored from Library → Cleanup.\n\n${file.path}\n\n${basis.hint}`,
        '/api/library/delete-file', { path: file.path, confirm: true },
        { confirmLabel: 'Move to trash', danger: true })
      if (!r) return
      setGone(g => ({ ...g, [idOf(file)]: true }))
      pushToast('Moved to trash — Navidrome is rescanning')
      onDeleted?.()
    } catch (e) {
      pushToast(e.payload?.code === 'last_copy' ? `Refused: ${e.message}` : `Delete failed: ${e.message}`, 'error')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="mb-2.5 rounded-card border border-line bg-panel p-3.5">
      <div className="flex flex-wrap items-baseline gap-2">
        <b className="text-small">{dup.track ? `${dup.track}. ` : ''}{dup.title}</b>
        <span className="text-caption text-muted">{dup.artist} — {dup.album}</span>
        <span className="spacer" />
        <span className={`chip ${basis.chip} !my-0`} title={basis.hint}>{basis.label}</span>
        <span className="chip warn !my-0">{files.length} copies</span>
      </div>
      {files.map(f => {
        const isKeep = f === keep
        return (
          <div key={idOf(f)}
            className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-ctl border p-2.5"
            style={{
              background: isKeep ? 'var(--sel-row)' : 'var(--inset-warm)',
              borderColor: isKeep ? 'var(--accent-bd-sel)' : 'var(--border)',
            }}>
            <Badge format={f.format} />
            <span className="font-mono text-caption" style={{ color: 'var(--text2)' }}>
              {[f.bitRate ? `${f.bitRate} kbps` : null, fmtSize(f.size)].filter(Boolean).join(' · ')}
            </span>
            {isKeep && <span className="chip good !my-0">keeping this one</span>}
            {f.onlyOnDisk && (
              <span className="chip warn !my-0" title="On disk but not indexed by Navidrome yet — usually a file placed in the last few minutes.">
                not in Navidrome
              </span>
            )}
            <span className="spacer" />
            <span className="min-w-0 flex-1 truncate font-mono text-micro text-muted" title={f.path}>{f.path}</span>
            {isKeep
              ? <span className="text-micro text-faint">kept</span>
              : (
                <>
                  <button className="sm"
                    title="Make this the copy that survives; the current one becomes deletable instead."
                    onClick={() => setKeepId(idOf(f))}>keep this one instead</button>
                  <button className="sm danger" disabled={busy === idOf(f)} aria-busy={busy === idOf(f)}
                    onClick={() => remove(f)}>Move to trash</button>
                </>
              )}
          </div>
        )
      })}
    </div>
  )
}

function fmtWhen(ts) {
  return ts ? new Date(ts * 1000).toLocaleString() : ''
}

// Deleting a duplicate is a move into LB_BOT_TRASH_DIR, not an unlink. This is
// the way back — and, since 2026-09-26, the way to actually free the space.
export function TrashPanel({ refreshKey }) {
  const { pushToast, confirmAction } = useApp()
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(null)

  const load = useCallback(() => {
    get('/api/library/trash').then(setData).catch(() => setData(null))
  }, [])
  useEffect(() => { load() }, [load, refreshKey])

  const files = data?.files || []
  if (!files.length) return null
  const totalSize = files.reduce((s, f) => s + (f.size || 0), 0)

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

  async function empty() {
    try {
      const r = await confirmAction(
        `Permanently delete all ${files.length} file(s) in the trash (${fmtSize(totalSize)})? This cannot be undone.`,
        '/api/library/trash/empty', { confirm: true },
        { confirmLabel: 'Delete permanently', danger: true })
      if (r) { pushToast(`Deleted ${r.removed ?? files.length} file(s) permanently`); load() }
    } catch (e) {
      pushToast(`Emptying the trash failed: ${e.message}`, 'error')
    }
  }

  return (
    <div className="rounded-card border border-line bg-panel p-3.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-caption text-muted">
          {files.length} file(s), {fmtSize(totalSize)}, in {data?.trashDir} — still recoverable
        </span>
        <span className="spacer" />
        <button className="sm danger" onClick={empty}>Empty trash…</button>
      </div>
      {files.slice(0, 50).map(f => (
        <div key={f.trashPath}
          className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-ctl border p-2.5"
          style={{ background: 'var(--inset-warm)', borderColor: 'var(--border)' }}>
          <span className="min-w-0 flex-1 truncate font-mono text-micro text-muted" title={f.originalPath}>{f.originalPath}</span>
          <span className="text-micro text-faint">{fmtWhen(f.deletedAt)}</span>
          <span className="font-mono text-micro text-faint">{fmtSize(f.size)}</span>
          {f.restorable
            ? <button className="sm" disabled={busy === f.trashPath} aria-busy={busy === f.trashPath}
                onClick={() => restore(f)}>Restore</button>
            : <span className="text-micro text-faint" title="Something already occupies the original path.">original path occupied</span>}
        </div>
      ))}
      {files.length > 50 && <p className="mt-2 text-caption text-muted">…and {files.length - 50} more.</p>}
    </div>
  )
}
