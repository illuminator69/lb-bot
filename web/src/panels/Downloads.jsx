import { useState } from 'react'
import { useApp, navigate, goAlbum } from '../App.jsx'
import { api, post } from '../lib/api.js'
import Cover from '../components/Cover.jsx'
import Placement from './Placement.jsx'
import {
  ago, failWords, fmtBytes, fmtDuration, PageTitle, ProgressBar,
  SectionHeader, StatusChip,
} from '../components/ui.jsx'

// How a folder's release match was made. Absent for folders the bot queued.
const MATCH_SOURCE_LABEL = {
  tag_mbid: 'tags',
  tag_rgid: 'tags',
  tag_text: 'tags',
  folder_name: 'folder name',
}

const caaCover = rgid => rgid ? `https://coverartarchive.org/release-group/${rgid}/front-250` : null

// ── Album requests ───────────────────────────────────────────────────────────
// Whole-album downloads (from an album page, Discover, the wishlist). Their
// progress used to be fire-and-forget: a toast said "see Downloads", and
// Downloads showed per-file transfers with no word on whether the album as a
// whole had landed, failed, or been refused for want of a source.
const FILL_LIVE = new Set(['searching', 'queued', 'downloading', 'placing'])

function AlbumRequest({ f }) {
  const { action, pushToast, scheduleRefresh } = useApp()
  const [busy, setBusy] = useState(false)
  const live = FILL_LIVE.has(f.state)
  const failed = f.state === 'failed'
  const pct = f.percent || (f.total ? Math.round((f.done / f.total) * 100) : 0)
  const detail = [
    f.total ? `${f.done}/${f.total} files` : null,
    f.failed ? `${f.failed} failed` : null,
    f.bytesTotal ? `${fmtBytes(f.bytesDone)} / ${fmtBytes(f.bytesTotal)}` : null,
    f.speedBps ? `${(f.speedBps / 1024 / 1024).toFixed(1)} MB/s` : null,
    f.source ? `@${f.source}` : null,
  ].filter(Boolean).join(' · ')

  async function run(fn, ok, bad) {
    setBusy(true)
    try {
      const r = await fn()
      const msg = typeof ok === 'function' ? ok(r) : ok
      if (msg) pushToast(msg)
    } catch (e) { pushToast(`${bad}: ${e.message}`, 'error') }
    finally { setBusy(false); scheduleRefresh(600) }
  }
  const retry = (extra = {}) => run(
    () => action('/api/album/download', {
      rgid: f.rgid, release_mbid: f.releaseMbid, artist: f.artist, album: f.album, total: f.total, ...extra,
    }), `Retrying ${f.album}`, 'Retry failed')
  // A 200 is not a cancel: once placement has claimed the row the server
  // answers cancelled:false with the status, and the files land anyway.
  const cancel = () => run(() => action('/api/album/cancel', { release_mbid: f.releaseMbid }),
    r => (r?.cancelled === false
      ? `Too late to cancel ${f.album} — it is already ${r.status || 'being placed'}`
      : `Cancelled ${f.album}`), 'Cancel failed')
  const wishlist = () => run(() => post('/api/wishlist', { rgid: f.rgid, artist: f.artist, title: f.album }),
    `${f.album} is on the wishlist — lb-bot will keep looking`, 'Could not add to the wishlist')

  return (
    <div className="mb-2 flex flex-wrap items-center gap-x-3.5 gap-y-2 rounded-card border bg-panel px-4 py-3"
      style={{ borderColor: failed ? 'var(--danger-bd)' : 'var(--border)' }}>
      <Cover url={caaCover(f.rgid)} name={f.album} size={44} />
      <div className="min-w-[180px] flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate text-body font-semibold">{f.album || f.releaseMbid}</span>
          <StatusChip status={f.state} />
        </div>
        <div className="mt-0.5 text-caption text-muted">
          {f.artist}{detail ? ` · ${detail}` : ''}{f.updatedAt ? ` · ${ago(f.updatedAt)} ago` : ''}
        </div>
        {failed && (
          <div className="mt-0.5 text-caption" style={{ color: 'var(--danger)' }}>
            {failWords(f.failureKind)}{f.reason ? ` — ${f.reason}` : ''}
            {f.retryAt ? ` · retrying automatically in ${fmtDuration(f.retryAt - f.serverTime)}` : ''}
          </div>
        )}
        {f.verifyGaveUp && (
          <div className="mt-0.5 text-caption text-muted">On disk, but Navidrome has not listed it yet.</div>
        )}
        {live && <div className="mt-2 max-w-[420px]"><ProgressBar value={pct} height={5} /></div>}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {f.cancellable && <button className="sm" disabled={busy} onClick={cancel}>Cancel</button>}
        {failed && f.mp3WouldHelp && !f.allowMp3 && (
          <button className="sm" disabled={busy} onClick={() => retry({ allowMp3: true })}
            title="Only MP3 copies were found. Accept MP3 for this album only.">Retry with MP3</button>
        )}
        {failed && f.retryable && !f.retryAt && (
          <button className="sm primary" disabled={busy} onClick={() => retry(f.source ? { excludeUsers: [f.source] } : {})}
            title={f.source ? `Try again, skipping @${f.source}` : 'Try again'}>Retry</button>
        )}
        {failed && f.failureKind === 'no_source' && f.rgid && (
          <button className="sm tint" disabled={busy} onClick={wishlist}
            title="Re-search slowly, every few hours, until a peer shares it">Add to wishlist</button>
        )}
        {f.rgid && <button className="sm quiet" onClick={() => goAlbum(f.rgid)}>Open album</button>}
      </div>
    </div>
  )
}

// ── Transfers ────────────────────────────────────────────────────────────────
function TransferRow({ t, onDismiss, nested }) {
  const { action, pushToast } = useApp()
  const stat = t.state === 'failed'
    ? (t.error || 'failed')
    : [
        t.bytesTotal ? `${fmtBytes(t.bytesDone)} / ${fmtBytes(t.bytesTotal)}` : '',
        t.rate ? `${(t.rate / 1024 / 1024).toFixed(1)} MB/s` : (t.state === 'queued' ? 'waiting' : ''),
      ].filter(Boolean).join(' · ')

  const fileTitle = (() => {
    const base = (t.filename || '').replace(/\\/g, '/').split('/').pop() || ''
    return base.replace(/\.[^.]+$/, '')
  })()
  const title = nested
    ? (t.trackTitle || t.displayTitle || t.title || fileTitle || '(unknown track)')
    : (t.displayTitle || t.title || t.trackTitle || fileTitle || '(unknown track)')
  const sub = [t.sub, t.stateDetail && t.stateDetail !== t.state ? t.stateDetail : ''].filter(Boolean).join(' · ')

  return (
    <div className={nested ? 'rounded-ctl px-2.5 py-2' : 'mb-2 rounded-card border border-line bg-panel px-4 py-3'}
      style={!nested && t.state === 'failed' ? { borderColor: 'var(--danger-bd)' } : undefined}>
      <div className="flex items-center gap-3.5">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className={`truncate font-semibold ${nested ? 'text-small' : 'text-body'}`}>{title}</span>
            <StatusChip status={t.state} />
          </div>
          {sub && <div className="mt-0.5 text-caption text-muted">{sub}</div>}
        </div>
        <div className="whitespace-nowrap text-right font-mono text-caption text-muted">{stat}</div>
        {t.state === 'failed' && t.groupId && (
          <button className="sm" onClick={() => navigate('Fill gaps', t.groupId)}
            title="Open this album in Fill gaps to pick another source">Open album</button>
        )}
        {t.kind === 'track' && t.state !== 'done' && t.state !== 'failed' && (
          <button className="sm" onClick={() => action('/api/downloads/cancel', { username: t.username, filename: t.filename })}>
            Cancel
          </button>
        )}
        <button className="sm quiet !px-2" title="Remove from this list (slskd is left untouched)" aria-label="Remove from list"
          onClick={async () => {
            onDismiss(t.id)
            try {
              await action(`/api/transfers/${t.id}/dismiss`)
            } catch (e) {
              onDismiss(t.id, false)
              pushToast(`Dismiss failed: ${e.message}`, 'error')
            }
          }}>✕</button>
      </div>
      {!nested && t.state === 'active' && <div className="mt-[11px]"><ProgressBar value={t.pct || 0} /></div>}
    </div>
  )
}

function AlbumGroup({ group, onDismiss, defaultOpen }) {
  const [open, setOpen] = useState(!!defaultOpen)
  const rows = group.rows
  const total = rows.length
  const done = rows.filter(r => r.state === 'done').length
  const failed = rows.filter(r => r.state === 'failed').length
  const active = rows.filter(r => r.state === 'active').length
  const pct = Math.round(rows.reduce((s, r) => s + (r.state === 'done' ? 100 : r.pct || 0), 0) / total)
  const state = active ? 'active'
    : rows.some(r => r.state === 'queued') ? 'queued'
    : done === total ? 'done'
    : failed ? 'failed' : 'queued'

  return (
    <div className="mb-2 rounded-card border border-line bg-panel"
      style={state === 'failed' ? { borderColor: 'var(--danger-bd)' } : undefined}>
      <button className="!block !w-full !rounded-card !border-0 !bg-transparent px-4 py-3 text-left"
        aria-expanded={open} onClick={() => setOpen(o => !o)}>
        <div className="flex items-center gap-3">
          <span className="w-3 shrink-0 text-micro text-faint">{open ? '▾' : '▸'}</span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="truncate text-body font-semibold">{group.album || '(album)'}</span>
              <StatusChip status={state} />
            </div>
            <div className="mt-0.5 text-caption text-muted">
              {group.artist ? `${group.artist} · ` : ''}{done}/{total} tracks{failed ? ` · ${failed} failed` : ''}
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

function groupByAlbum(rows) {
  const groups = new Map()
  const loose = []
  for (const t of rows) {
    if (t.kind !== 'track' || !(t.groupId || t.album)) { loose.push(t); continue }
    const key = t.groupId || `album:${t.album}`
    if (!groups.has(key)) groups.set(key, { key, album: t.album, artist: t.artist, rows: [] })
    groups.get(key).rows.push(t)
  }
  const grouped = []
  for (const g of groups.values()) {
    if (g.rows.length === 1 && !g.album) loose.push(g.rows[0])
    else grouped.push(g)
  }
  return { grouped, loose }
}

// ── Needs placement ──────────────────────────────────────────────────────────
function PlacementCard({ p }) {
  const { action, confirmAction, pushToast } = useApp()
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
        `Delete “${p.name || p.path}” and all its files from the downloads folder? This cannot be undone.`,
        `/api/placements/${p.id}/delete`, { confirm: true },
        { confirmLabel: 'Delete folder', danger: true })
      if (r) { setHidden(true); pushToast('Folder deleted from downloads') }
    } catch (e) {
      pushToast(`Delete failed: ${e.message}`, 'error')
    }
  }

  async function confirm() {
    setBusy(true)
    try {
      // Say which match was confirmed: with no body the server re-derived one
      // at click time, which need not be the one this card showed.
      const r = await action(`/api/placements/${p.id}/confirm`,
        p.groupId ? { groupId: p.groupId } : p.releaseMbid ? { releaseMbid: p.releaseMbid } : {})
      setFiled(true)
      if (r.alreadyActive) pushToast('Placement already running')
    } catch (e) {
      pushToast(`Placement failed: ${e.message}`, 'error')
    } finally { setBusy(false) }
  }

  if (hidden) return null
  // What the match was made on: the files' tags or only the folder's name.
  const sourceLabel = p.matchBasis || MATCH_SOURCE_LABEL[p.matchSource]
  const review = () => navigate('Downloads', 'place', p.path, p.groupId || '-')
  const chooseOther = () => navigate('Downloads', 'place', p.path, '-')
  const pickRelease = () => navigate('Downloads', 'place', p.path, '-', 'pick')
  const possible = p.matchLabel && !p.canConfirm
  const leftover = !!p.filed

  return (
    <div className="flex flex-col rounded-card border p-[15px]"
      style={{ background: 'var(--surface)', borderColor: possible ? 'var(--border)' : 'var(--accent-bd-soft)' }}>
      <div className="truncate font-mono text-micro text-faint" title={p.path}>{p.path || p.name}</div>
      {leftover ? (
        <div className="mt-2 text-small text-muted">
          {p.filed === 'imported' ? 'Already filed into the library' : 'Dismissed'} — {p.fileCount} file(s) still on disk.
        </div>
      ) : (
        <>
          <div className="mb-1 mt-2 flex flex-wrap items-center gap-1.5 text-caption text-muted">
            {p.matchLabel ? (possible ? 'Might be' : 'Matches') : 'Not identified'}
            {p.matchLabel && sourceLabel && <span className="chip !my-0">on {sourceLabel}</span>}
            {possible && <span className="chip warn !my-0">check first</span>}
          </div>
          <div className="flex items-start gap-3">
            {p.coverUrl && <Cover url={p.coverUrl} name={p.matchLabel || p.name} size={48} />}
            <div className="min-w-0 flex-1">
              <div className="text-lead font-semibold">{p.matchLabel || p.name}</div>
              {p.matchLabel ? (
                <div className="mt-0.5 text-caption" style={{ color: p.canConfirm ? 'var(--green)' : 'var(--muted)' }}>
                  {diff.filesFound} file(s) in the folder
                  {diff.trackCount ? ` · release has ${diff.trackCount} track(s)` : ''}
                  {p.match ? ` · fills up to ${diff.willFill} missing track(s)` : ''}
                </div>
              ) : p.identifyError ? (
                <div className="mt-0.5 text-caption text-muted">Identify failed: {p.identifyError}</div>
              ) : p.identified ? (
                <div className="mt-0.5 text-caption text-muted">No release matched — pick one.</div>
              ) : (
                <div className="mt-0.5 text-caption text-muted">Run Identify above, or pick the release yourself.</div>
              )}
            </div>
          </div>
        </>
      )}
      <span className="spacer min-h-3" />
      <div className="flex flex-wrap gap-2">
        {filed ? (
          <span className="chip good !my-0 self-center">✓ Filing…</span>
        ) : leftover ? (
          <button onClick={review}>File again…</button>
        ) : p.canConfirm ? (
          <>
            <button className="primary" disabled={busy} onClick={confirm}>Confirm &amp; file</button>
            {/* Not the suggested album: the match page with nothing chosen.
                Opening it on the rejected group left one click from filing
                into the album just turned down. */}
            <button disabled={busy} onClick={p.groupId ? chooseOther : pickRelease}>Not this…</button>
          </>
        ) : possible ? (
          <>
            {/* Not one-tap: a name-only or oversized match has to be looked at. */}
            <button className="primary" onClick={p.groupId ? review : pickRelease}>Review match</button>
            <button onClick={pickRelease}>Pick release</button>
          </>
        ) : (
          <button className="primary" onClick={pickRelease}>Pick release</button>
        )}
        <span className="spacer" />
        {!leftover && (
          <button className="quiet" disabled={busy} title="Hide from this list — files stay on disk" onClick={dismiss}>Dismiss</button>
        )}
        <button className="danger" disabled={busy} title="Delete this folder from the downloads directory" onClick={deleteFiles}>
          Delete files
        </button>
      </div>
    </div>
  )
}

// Folders already filed or dismissed that are still on disk — what the old
// Import tab listed next to the real queue without telling them apart.
function Leftovers() {
  const [rows, setRows] = useState(null)
  const [error, setError] = useState('')
  const [open, setOpen] = useState(false)
  async function load() {
    setError('')
    try {
      const r = await api('/api/placements?all=1')
      setRows((r.items || []).filter(i => i.filed))
    } catch (e) { setError(e.message || 'failed') }
  }
  function toggle(e) {
    const next = e.currentTarget.open
    setOpen(next)
    // Every open refetches: the list changes as folders are filed or deleted,
    // and a failed load must not read "Nothing left behind" for good.
    if (next) load()
  }
  return (
    <details className="mt-3" onToggle={toggle}>
      <summary className="text-small">Already filed or dismissed, still on disk{rows ? ` (${rows.length})` : ''}</summary>
      {open && (
        <div className="mt-3">
          {error ? (
            <p className="text-caption" style={{ color: 'var(--danger)' }}>
              Couldn't list them: {error}. <button className="link-inline" onClick={load}>Try again</button>
            </p>
          ) : rows === null ? <p className="text-caption text-muted">Loading…</p>
            : !rows.length ? <p className="text-caption text-muted">Nothing left behind in the downloads folder.</p>
            : (
              <>
                <p className="mb-3 text-caption text-muted">
                  Placement moves the tracks it matched and leaves the rest. These folders are done as far as lb-bot is
                  concerned; delete them to reclaim the space, or file them again if something was missed.
                </p>
                <div className="placement-grid">{rows.map(p => <PlacementCard key={p.id} p={p} />)}</div>
              </>
            )}
        </div>
      )}
    </details>
  )
}

function IdentifyButton({ unidentified, identify }) {
  const { action, pushToast } = useApp()
  const [busy, setBusy] = useState(false)
  const running = identify?.running
  if (!running && !unidentified) return null
  if (running) {
    const { done, total, current } = identify
    return (
      <span className="text-caption font-normal text-muted">
        Identifying{total ? ` ${done}/${total}` : ''}{current ? ` · ${current}` : ''}…
      </span>
    )
  }
  return (
    <button className="sm" disabled={busy} onClick={async () => {
      setBusy(true)
      try {
        const r = await action('/api/placements/identify')
        pushToast(r.alreadyActive ? 'Identify already running' : 'Identifying download folders…')
      } catch (e) {
        pushToast(`Identify failed: ${e.message}`, 'error')
      } finally { setBusy(false) }
    }} title="Look each folder up on MusicBrainz (one request per second)">
      Identify {unidentified} folder(s)
    </button>
  )
}

// ── Wishlist ─────────────────────────────────────────────────────────────────
function Wishlist({ data }) {
  const { pushToast, scheduleRefresh } = useApp()
  const [busy, setBusy] = useState('')
  const rows = data?.wishlist || []
  async function remove(r) {
    setBusy(r.rgid)
    try {
      await post('/api/wishlist/remove', { rgid: r.rgid })
      pushToast(`Removed ${r.title} from the wishlist`)
      scheduleRefresh(300)
    } catch (e) {
      pushToast(`Could not remove: ${e.message}`, 'error')
    } finally { setBusy('') }
  }
  const every = data?.intervalSeconds ? fmtDuration(data.intervalSeconds) : '6 h'
  return (
    <>
      <SectionHeader className="mt-[26px]" label="Wishlist" sub={rows.length || ''} />
      <p className="mb-3 text-caption text-muted">
        Albums no peer was sharing. lb-bot re-searches one every {every}, and each at most every{' '}
        {data?.cooldownSeconds ? fmtDuration(data.cooldownSeconds) : '12 h'}; when one lands it leaves the list on its own.
        Add albums from an album page, from New releases, or from a failed request above.
      </p>
      {!rows.length ? (
        <p className="text-caption text-muted">Nothing on the wishlist.</p>
      ) : rows.map(r => (
        <div key={r.rgid} className="mb-2 flex flex-wrap items-center gap-3 rounded-card border border-line bg-panel px-4 py-2.5">
          <Cover url={caaCover(r.rgid)} name={r.title} size={36} />
          <div className="min-w-[160px] flex-1">
            <div className="truncate text-small font-semibold">{r.title || r.rgid}</div>
            <div className="text-caption text-muted">
              {r.artist}{' · added '}{ago(r.addedAt)} ago
              {r.lastTriedAt ? ` · last searched ${ago(r.lastTriedAt)} ago` : ' · not searched yet'}
              {r.attempts ? ` · ${r.attempts} search(es)` : ''}
            </div>
            {r.lastReason && <div className="text-micro text-faint">{r.lastReason}</div>}
          </div>
          <button className="sm quiet" onClick={() => goAlbum(r.rgid)}>Open album</button>
          <button className="sm" disabled={busy === r.rgid} onClick={() => remove(r)}>Remove</button>
        </div>
      ))}
    </>
  )
}

export default function Downloads() {
  const { state, action } = useApp()
  const { transfers, wishlist, fills, routeParams } = state
  const [dismissed, setDismissed] = useState(() => new Set())
  if (routeParams[0] === 'place') return <Placement params={routeParams.slice(1)} />
  if (!transfers) return <p className="text-caption text-muted">Loading transfers…</p>

  const { counts } = transfers
  const rows = (transfers.transfers || []).filter(t => !dismissed.has(t.id))
  const markDismissed = (id, hide = true) => setDismissed(prev => {
    const next = new Set(prev)
    if (hide) next.add(id); else next.delete(id)
    return next
  })
  const { grouped: allGroups, loose: allLoose } = groupByAlbum(rows)
  const groupDone = g => g.rows.every(r => r.state === 'done')
  const liveGroups = allGroups.filter(g => !groupDone(g))
  const doneGroups = allGroups.filter(groupDone)
  const liveLoose = allLoose.filter(t => t.state !== 'done')
  const doneLoose = allLoose.filter(t => t.state === 'done')
  const hasLive = liveGroups.length > 0 || liveLoose.length > 0
  const finishedCount = doneGroups.reduce((s, g) => s + g.rows.length, 0) + doneLoose.length
  const placements = transfers.needsPlacement || []

  // Recent album requests, live ones first. Ledger rows that recorded no album,
  // artist or release-group (18 of them on the live ledger, all "placed" days
  // ago) can't be named or acted on, so they get one summary line rather than a
  // wall of release ids.
  const allRequests = Object.values(fills?.albums || {})
    .filter(f => f.state && f.state !== 'unknown')
    .sort((a, b) => (FILL_LIVE.has(b.state) - FILL_LIVE.has(a.state)) || (b.updatedAt - a.updatedAt))
  const nameless = allRequests.filter(f => !f.album && !f.artist && !f.rgid)
  const requests = allRequests.filter(f => !nameless.includes(f))

  const stats = [
    [counts.active ?? 0, 'downloading', 'var(--green)'],
    [counts.queued ?? 0, 'queued', 'var(--decide-fg)'],
    [counts.needsPlacement ?? 0, 'to file', 'var(--accent)'],
    [wishlist?.total ?? 0, 'wishlisted', 'var(--text2)'],
  ]

  return (
    <>
      <div className="mb-[22px] flex flex-wrap items-end gap-4">
        <PageTitle eyebrow="Downloads" title="What's coming in" />
        <span className="spacer" />
        {stats.map(([n, label, color]) => (
          <div key={label} className="px-1 text-right">
            <div className="text-heading font-bold" style={{ color }}>{n}</div>
            <div className="text-micro text-muted">{label}</div>
          </div>
        ))}
      </div>

      {allRequests.length > 0 && (
        <>
          <SectionHeader label="Album requests" sub={requests.length || ''} />
          {requests.map(f => <AlbumRequest key={f.releaseMbid} f={f} />)}
          {nameless.length > 0 && (
            <p className="text-caption text-muted"
              title={nameless.map(f => `${f.releaseMbid} · ${f.state}`).join('\n')}>
              {nameless.length} older request(s) recorded no album name
              {nameless.every(f => f.verifyGaveUp) ? ' — placed on disk, never seen in Navidrome' : ''}.
            </p>
          )}
        </>
      )}

      <SectionHeader className={allRequests.length ? 'mt-[26px]' : ''} label="Transfers"
        action={finishedCount > 0 && <button className="sm" onClick={() => action('/api/downloads/clear')}>Clear finished</button>} />
      {liveGroups.map(g => (
        <AlbumGroup key={g.key} group={g} onDismiss={markDismissed} defaultOpen={g.rows.some(r => r.state === 'failed')} />
      ))}
      {liveLoose.map(t => <TransferRow key={t.id} t={t} onDismiss={markDismissed} />)}
      {!hasLive && (
        <p className="rounded-card border border-line bg-panel px-4 py-3 text-small text-muted">
          Nothing downloading right now. Start one from Fill gaps, an album page, or Discover.
        </p>
      )}
      {finishedCount > 0 && (
        <details className="mt-2">
          <summary className="text-small">Finished ({finishedCount})</summary>
          <div className="mt-2">
            {doneGroups.map(g => <AlbumGroup key={g.key} group={g} onDismiss={markDismissed} />)}
            {doneLoose.map(t => <TransferRow key={t.id} t={t} onDismiss={markDismissed} />)}
          </div>
        </details>
      )}

      <SectionHeader className="mt-[26px]" label="To file into the library" sub={placements.length || ''}
        action={<IdentifyButton unidentified={counts.unidentified ?? 0} identify={transfers.identify} />} />
      {placements.length > 0 ? (
        <>
          <p className="mb-3 text-caption text-muted">
            Downloaded folders waiting to be filed. A strong match files with one tap; anything matched on its folder
            name alone, or much bigger than the gap it fills, asks you to look first.
          </p>
          <div className="placement-grid">{placements.map(p => <PlacementCard key={p.id} p={p} />)}</div>
        </>
      ) : (
        <p className="text-caption text-muted">No downloaded folders waiting to be filed.</p>
      )}
      <Leftovers />

      <Wishlist data={wishlist} />
    </>
  )
}
