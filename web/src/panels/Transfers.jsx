import { useState } from 'react'
import { useApp, navigate } from '../App.jsx'
import { EmptyState, PageTitle, ProgressBar, StatusChip } from '../components/ui.jsx'
import Cover from '../components/Cover.jsx'

// How a folder's release match was arrived at. Absent for folders the bot
// queued itself (those match a review group and never need identifying).
const MATCH_SOURCE_LABEL = {
  group: 'album review',
  tag_mbid: 'from tags',
  tag_rgid: 'from tags',
  tag_text: 'from tags',
  folder_name: 'from folder name',
}

function fmtBytes(n) {
  if (!n) return ''
  const mb = n / 1024 / 1024
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb.toFixed(1)} MB`
}

// Full-width row card per the mock: title+chip / sub left, mono stat right,
// action button, progress bar below (active rows only). `nested` renders it as
// a lighter sub-row inside an expanded album group and shows the track title
// rather than the "artist — title" line the standalone card uses.
function TransferRow({ t, onDismiss, nested }) {
  const { action, dispatch, pushToast } = useApp()
  const stat = t.state === 'failed'
    ? (t.error || 'failed')
    : [
        t.bytesTotal ? `${fmtBytes(t.bytesDone)} / ${fmtBytes(t.bytesTotal)}` : '',
        t.rate ? `${(t.rate / 1024 / 1024).toFixed(1)} MB/s` : (t.state === 'queued' ? 'waiting' : ''),
      ].filter(Boolean).join(' · ')

  function jumpToAlbum() {
    navigate('Fill gaps', t.groupId)
  }

  // Last-resort title from the download's filename, so a row never renders as
  // "(unnamed)" just because the backend row had no title.
  const fileTitle = (() => {
    const base = (t.filename || '').replace(/\\/g, '/').split('/').pop() || ''
    return base.replace(/\.[^.]+$/, '')
  })()
  const title = nested
    ? (t.trackTitle || t.displayTitle || t.title || fileTitle || '(unknown track)')
    : (t.displayTitle || t.title || t.trackTitle || fileTitle || '(unknown track)')
  const sub = nested
    ? [t.sub, t.stateDetail && t.stateDetail !== t.state ? t.stateDetail : '']
        .filter(Boolean).join(' · ')
    : `${t.sub} ${t.stateDetail && t.stateDetail !== t.state ? `· ${t.stateDetail}` : ''}`

  return (
    <div className={nested
      ? 'rounded-[9px] px-2.5 py-2'
      : 'mb-[9px] rounded-[12px] border border-line bg-panel px-4 py-3.5'}
      style={!nested && t.state === 'failed' ? { borderColor: 'var(--danger-bd)' } : undefined}>
      <div className="flex items-center gap-3.5">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className={`truncate font-semibold ${nested ? 'text-[13px]' : 'text-[14.5px]'}`}>{title}</span>
            <StatusChip status={t.state} />
          </div>
          <div className="muted mt-0.5 text-[12px]">{sub}</div>
        </div>
        <div className="whitespace-nowrap text-right font-mono text-[12px] text-muted">{stat}</div>
        {t.state === 'failed' && t.groupId && (
          <button className="whitespace-nowrap font-semibold"
            style={{ background: 'var(--green-tint)', borderColor: 'var(--green-bd)', color: 'var(--green)' }}
            onClick={jumpToAlbum}>
            Try next
          </button>
        )}
        {t.kind === 'track' && t.state !== 'done' && t.state !== 'failed' && (
          <button onClick={() =>
            action('/api/downloads/cancel', { username: t.username, filename: t.filename })}>
            Cancel
          </button>
        )}
        <button className="!px-2.5" title="Remove from this list (slskd is left untouched)"
          onClick={async () => {
            onDismiss(t.id)
            try {
              await action(`/api/transfers/${t.id}/dismiss`)
            } catch (e) {
              onDismiss(t.id, false)
              pushToast(`Dismiss failed: ${e.message}`, 'error')
            }
          }}>
          ✕
        </button>
      </div>
      {!nested && t.state === 'active' && (
        <div className="mt-[11px]">
          <ProgressBar value={t.pct || 0} />
        </div>
      )}
    </div>
  )
}

// Per-track album fills, collapsed under one album header (album name + rolled-up
// progress). Click to expand the individual tracks. A lone ungrouped track (no
// album context) renders as a plain row instead — see Transfers().
function AlbumGroup({ group, onDismiss, defaultOpen }) {
  const [open, setOpen] = useState(!!defaultOpen)
  const rows = group.rows
  const total = rows.length
  const done = rows.filter(r => r.state === 'done').length
  const failed = rows.filter(r => r.state === 'failed').length
  const active = rows.filter(r => r.state === 'active').length
  const pct = Math.round(
    rows.reduce((s, r) => s + (r.state === 'done' ? 100 : r.pct || 0), 0) / total)
  const state = active ? 'active'
    : rows.some(r => r.state === 'queued') ? 'queued'
    : done === total ? 'done'
    : failed ? 'failed' : 'queued'

  return (
    <div className="mb-[9px] rounded-[12px] border border-line bg-panel"
      style={state === 'failed' ? { borderColor: 'var(--danger-bd)' } : undefined}>
      <button className="!block !w-full !rounded-[12px] !border-0 !bg-transparent px-4 py-3.5 text-left"
        onClick={() => setOpen(o => !o)}>
        <div className="flex items-center gap-3">
          <span className="w-3 shrink-0 text-[11px] text-faint">{open ? '▾' : '▸'}</span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="truncate text-[14.5px] font-semibold">{group.album || '(album)'}</span>
              <StatusChip status={state} />
            </div>
            <div className="muted mt-0.5 text-[12px]">
              {group.artist ? `${group.artist} · ` : ''}{done}/{total} tracks
              {failed ? ` · ${failed} failed` : ''}
            </div>
          </div>
          <div className="w-[130px] shrink-0"><ProgressBar value={pct} /></div>
        </div>
      </button>
      {open && (
        <div className="border-t border-line px-2 py-1">
          {rows.map(t => <TransferRow key={t.id} t={t} onDismiss={onDismiss} nested />)}
        </div>
      )}
    </div>
  )
}

// Group per-track (kind:"track") rows by the album they're filling. Rows without
// album context stay ungrouped so they still render as normal rows.
function groupByAlbum(rows) {
  const groups = new Map()
  const loose = []
  for (const t of rows) {
    if (t.kind !== 'track' || !(t.groupId || t.album)) { loose.push(t); continue }
    const key = t.groupId || `album:${t.album}`
    if (!groups.has(key)) groups.set(key, { key, album: t.album, artist: t.artist, rows: [] })
    groups.get(key).rows.push(t)
  }
  // A single-track "album" is just a track — don't hide it behind a caret.
  const grouped = []
  for (const g of groups.values()) {
    if (g.rows.length === 1 && !g.album) loose.push(g.rows[0])
    else grouped.push(g)
  }
  return { grouped, loose }
}

function PlacementCard({ p }) {
  const { action, confirmAction, pushToast, dispatch } = useApp()
  const [busy, setBusy] = useState(false)
  const [filed, setFiled] = useState(false)
  const [hidden, setHidden] = useState(false)
  const diff = p.diff || {}

  async function dismiss() {
    setHidden(true)
    try {
      await action(`/api/placements/${p.id}/dismiss`)
    } catch (e) {
      setHidden(false)
      pushToast(`Dismiss failed: ${e.message}`, 'error')
    }
  }

  async function deleteFiles() {
    try {
      const r = await confirmAction(
        `Delete “${p.name || p.path}” and all its files from the downloads folder?`,
        `/api/placements/${p.id}/delete`, { confirm: true })
      if (r) {
        setHidden(true)
        pushToast('Folder deleted from downloads')
      }
    } catch (e) {
      pushToast(`Delete failed: ${e.message}`, 'error')
    }
  }

  if (hidden) return null
  const sourceLabel = MATCH_SOURCE_LABEL[p.matchSource]
  const pickRelease = () => {
    navigate('Import', p.path, '-', 'pick')
  }
  return (
    <div className="rounded-[12px] border p-[15px]"
      style={{ background: 'var(--surface)', borderColor: 'var(--accent-bd-soft)' }}>
      <div className="truncate font-mono text-[11px] text-faint" title={p.path}>{p.path || p.name}</div>
      <div className="muted mb-1 mt-2 flex items-center gap-1.5 text-[12px]">
        Matches
        {sourceLabel && <span className="chip !my-0">{sourceLabel}</span>}
      </div>
      <div className="flex items-start gap-3">
        {p.coverUrl && <Cover url={p.coverUrl} name={p.matchLabel || p.name} size={48} />}
        <div className="min-w-0 flex-1">
          <div className="text-[15px] font-semibold">{p.matchLabel || p.name}</div>
          {p.matchLabel ? (
            <div className="mt-0.5 text-[12.5px]"
              style={{ color: p.canConfirm ? 'var(--green)' : 'var(--muted)' }}>
              {diff.filesFound} file(s) found
              {diff.trackCount ? ` · release has ${diff.trackCount} track(s)` : ''}
              {p.match ? ` · will fill ${diff.willFill} missing track(s)` : ''}
              {p.confidence && p.confidence !== 'likely' ? ` · ${p.confidence} match` : ''}
            </div>
          ) : p.identifyError ? (
            <div className="muted mt-0.5 text-[12.5px]">Identify failed: {p.identifyError}</div>
          ) : p.identified ? (
            <div className="muted mt-0.5 text-[12.5px]">No release matched — pick one manually.</div>
          ) : (
            <div className="muted mt-0.5 text-[12.5px]">Not identified yet — run Identify, or pick the release manually.</div>
          )}
        </div>
      </div>
      <div className="mt-3.5 flex gap-2">
        {filed ? (
          <span className="chip good !my-0 self-center">✓ Filed</span>
        ) : p.canConfirm ? (
          <button className="primary" disabled={busy} onClick={async () => {
            setBusy(true)
            try {
              const r = await action(`/api/placements/${p.id}/confirm`)
              setFiled(true)
              if (r.alreadyActive) pushToast('Placement already running')
            } catch (e) {
              pushToast(`Placement failed: ${e.message}`, 'error')
            } finally { setBusy(false) }
          }}>
            Confirm &amp; file
          </button>
        ) : (
          <button className="primary" disabled={busy} onClick={pickRelease}>
            Pick release
          </button>
        )}
        {p.canConfirm && !filed && (
          <button disabled={busy} onClick={pickRelease}>
            Not this — pick release
          </button>
        )}
        <span className="spacer" />
        <button disabled={busy} title="Hide from this list — files stay on disk"
          onClick={dismiss}>
          Dismiss
        </button>
        <button disabled={busy} title="Delete this folder from the downloads directory"
          style={{ borderColor: 'var(--danger-bd)', color: 'var(--danger)' }}
          onClick={deleteFiles}>
          Delete files
        </button>
      </div>
    </div>
  )
}

// Identification is a MusicBrainz sweep at 1 req/sec, so it never runs on its
// own — it's offered here when folders are sitting unidentified, and reports
// progress from the transfers payload the tab already polls.
function IdentifyButton({ unidentified, identify }) {
  const { action, pushToast } = useApp()
  const [busy, setBusy] = useState(false)
  const running = identify?.running
  if (!running && !unidentified) return null
  if (running) {
    const { done, total, current } = identify
    return (
      <span className="muted !my-0 text-[12px] font-normal">
        Identifying{total ? ` ${done}/${total}` : ''}
        {current ? ` · ${current}` : ''}…
      </span>
    )
  }
  return (
    <button className="!py-[5px] text-[12px]" disabled={busy} onClick={async () => {
      setBusy(true)
      try {
        const r = await action('/api/placements/identify')
        pushToast(r.alreadyActive ? 'Identify already running' : 'Identifying download folders…')
      } catch (e) {
        pushToast(`Identify failed: ${e.message}`, 'error')
      } finally { setBusy(false) }
    }}>
      Identify {unidentified} folder(s)
    </button>
  )
}

export default function Transfers() {
  const { state, action } = useApp()
  const { transfers } = state
  // Optimistically hide dismissed rows until the next poll confirms removal.
  const [dismissed, setDismissed] = useState(() => new Set())
  if (!transfers) return <p className="muted">Loading transfers…</p>
  const { counts } = transfers
  const rows = (transfers.transfers || []).filter(t => !dismissed.has(t.id))
  const markDismissed = (id, hide = true) => setDismissed(prev => {
    const next = new Set(prev)
    if (hide) next.add(id); else next.delete(id)
    return next
  })
  // Group every fill by its album first — a finished track stays inside its
  // album card so a partly-done album reads as one unit. Only albums where
  // every track is done (and loose done tracks) drop into Finished.
  const { grouped: allGroups, loose: allLoose } = groupByAlbum(rows)
  const groupDone = g => g.rows.every(r => r.state === 'done')
  const liveGroups = allGroups.filter(g => !groupDone(g))
  const doneGroups = allGroups.filter(groupDone)
  const liveLoose = allLoose.filter(t => t.state !== 'done')
  const doneLoose = allLoose.filter(t => t.state === 'done')
  const hasLive = liveGroups.length > 0 || liveLoose.length > 0
  const finishedCount = doneGroups.reduce((s, g) => s + g.rows.length, 0) + doneLoose.length
  const placements = transfers.needsPlacement || []

  const stats = [
    [counts.active ?? 0, 'downloading', 'var(--green)'],
    [counts.queued ?? 0, 'queued', 'var(--accent-2)'],
    [counts.needsPlacement ?? 0, 'need placement', 'var(--accent)'],
  ]

  return (
    <>
      <div className="mb-[22px] flex flex-wrap items-end gap-4">
        <PageTitle eyebrow="Downloads" title="Transfer queue" />
        <span className="spacer" />
        {stats.map(([n, label, color]) => (
          <div key={label} className="px-1 text-right">
            <div className="text-[22px] font-bold" style={{ color }}>{n}</div>
            <div className="muted text-[11.5px]">{label}</div>
          </div>
        ))}
        <button className="self-center" onClick={() => action('/api/downloads/clear')}>Clear finished</button>
      </div>

      <div className="mb-2.5 text-[13px] font-semibold" style={{ color: 'var(--text2)' }}>ACTIVE &amp; QUEUED</div>
      {liveGroups.map(g => (
        <AlbumGroup key={g.key} group={g} onDismiss={markDismissed}
          defaultOpen={g.rows.some(r => r.state === 'failed')} />
      ))}
      {liveLoose.map(t => <TransferRow key={t.id} t={t} onDismiss={markDismissed} />)}
      {!hasLive && (
        <EmptyState title="Nothing downloading right now"
          hint="Queue tracks from Fill gaps or Library and they'll show up here." />
      )}

      <div className="mb-2.5 mt-[22px] flex items-center gap-2.5 text-[13px] font-semibold" style={{ color: 'var(--text2)' }}>
        NEEDS PLACEMENT
        {placements.length ? (
          <span className="rounded-pill border px-2 py-px text-[11px] font-semibold"
            style={{ background: 'var(--accent-tint)', borderColor: 'var(--accent-bd)', color: 'var(--accent)' }}>
            {placements.length}
          </span>
        ) : null}
        <span className="spacer" />
        <IdentifyButton unidentified={counts.unidentified ?? 0} identify={transfers.identify} />
      </div>
      {placements.length > 0 && (
        <p className="muted !mt-0 mb-3 text-[12.5px]">
          Downloaded folders waiting to be filed into the library. lb-bot has a match ready — one tap confirms it.
        </p>
      )}
      <div className="placement-grid">
        {placements.map(p => <PlacementCard key={p.id} p={p} />)}
        {!placements.length &&
          <p className="muted">No downloaded folders waiting to be filed.</p>}
      </div>

      {finishedCount > 0 && (
        <details>
          <summary>Finished ({finishedCount})</summary>
          <div className="mt-2">
            {doneGroups.map(g => (
              <AlbumGroup key={g.key} group={g} onDismiss={markDismissed} />
            ))}
            {doneLoose.map(t => <TransferRow key={t.id} t={t} onDismiss={markDismissed} />)}
          </div>
        </details>
      )}
    </>
  )
}
