import { useState } from 'react'
import { useApp, navigate, goAlbum, goGap } from '../App.jsx'
import { post } from '../lib/api.js'
import { fillIsLive, fillProgress, namedFills, trackedProgress } from '../lib/needs.js'
import Cover from '../components/Cover.jsx'
import Page, { Grp, Meter, St } from '../components/Page.jsx'
import { ago, failWords, fmtBytes, StatusChip, STATUS } from '../components/ui.jsx'
import { leftOutText } from '../lib/albumSources.js'

// Activity: what is moving and what has landed. A fill that fails or stalls
// leaves In motion and appears under Needs you › To fix, where its actions are.
//
// A row is an album. Its files are one click deeper: when one of nine fails,
// "Files" says which, and each file keeps its own Cancel and Remove.

const caaCover = rgid => rgid ? `https://coverartarchive.org/release-group/${rgid}/front-250` : null
const mbps = n => `${(n / 1024 / 1024).toFixed(1)} MB/s`

// One file. Nested under its album, or on its own for a transfer that belongs
// to no album.
function FileRow({ t, onDismiss, nested }) {
  const { action, pushToast } = useApp()
  const fileTitle = ((t.filename || '').replace(/\\/g, '/').split('/').pop() || '').replace(/\.[^.]+$/, '')
  const title = nested
    ? (t.trackTitle || t.displayTitle || t.title || fileTitle || '(unknown track)')
    : (t.displayTitle || t.title || t.trackTitle || fileTitle || '(unknown track)')
  const stat = t.state === 'failed'
    ? (t.error || 'failed')
    : [
        t.bytesTotal ? `${fmtBytes(t.bytesDone)} of ${fmtBytes(t.bytesTotal)}` : '',
        t.rate ? mbps(t.rate) : '',
        t.username ? `@${t.username}` : '',
      ].filter(Boolean).join(' · ')
  const sub = [t.sub, t.stateDetail && t.stateDetail !== t.state ? t.stateDetail : '', stat].filter(Boolean).join(' · ')
  const live = t.state !== 'done' && t.state !== 'failed'
  return (
    <div className={nested ? 'flex min-w-0 items-center gap-3 py-1.5' : 'row'}>
      <div className="grow">
        <div className={`clip ${nested ? '' : 't'}`}>{title}</div>
        {sub && <div className={`s clip${t.state === 'failed' ? ' bad' : ''}`}>{sub}</div>}
        {!nested && t.state === 'active' && <div className="mt-1.5"><Meter value={t.pct || 0} /></div>}
      </div>
      <StatusChip status={t.state} word={t.state === 'active' ? `Downloading ${Math.round(t.pct || 0)}%` : undefined} />
      {t.kind === 'track' && live && (
        <button className="sm" onClick={() => action('/api/downloads/cancel', { username: t.username, filename: t.filename })
          .catch(e => pushToast(`Cancel failed: ${e.message}`, 'error'))}>Cancel</button>
      )}
      <button className="sm quiet" title="Remove from this list. slskd is left untouched" aria-label="Remove from list"
        onClick={async () => {
          onDismiss(t.id)
          try {
            await action(`/api/transfers/${t.id}/dismiss`)
          } catch (e) {
            onDismiss(t.id, false)
            pushToast(`Remove failed: ${e.message}`, 'error')
          }
        }}>✕</button>
    </div>
  )
}

// A gap fill: the files being fetched into one album.
function AlbumGroup({ group, onDismiss, defaultOpen }) {
  const { state: app, action, pushToast } = useApp()
  const [open, setOpen] = useState(!!defaultOpen)
  const [busy, setBusy] = useState(false)
  const rows = group.rows
  const active = rows.filter(r => r.state === 'active')
  // The album's own counts where the server sends them: a finished file leaves
  // this list, so counting the rows alone restarts the bar after every file.
  const fill = (group.groupId && fillProgress(app.summary, group.groupId))
    || trackedProgress(`act:${group.key}`, rows.filter(r => r.state !== 'failed'))
  const total = fill.total
  const done = fill.done
  const failed = fill.failed ?? rows.filter(r => r.state === 'failed').length
  const pct = fill.pct
  const state = active.length ? 'active'
    : rows.some(r => r.state === 'queued') ? 'queued'
    : rows.every(r => r.state === 'done') ? 'done'
    : rows.some(r => r.state === 'failed') ? 'failed' : 'queued'
  const rate = active.reduce((s, r) => s + (r.rate || 0), 0)
  const peer = (active[0] || rows[0])?.username
  const line = [
    `${done} of ${total} file${total === 1 ? '' : 's'}`,
    failed ? `${failed} failed` : null,
    rate ? mbps(rate) : null,
    state === 'queued' && peer ? `waiting for a slot at @${peer}` : peer ? `@${peer}` : null,
  ].filter(Boolean).join(' · ')
  const live = state === 'active' || state === 'queued'
  const word = state === 'active' ? `Downloading ${pct}%` : state === 'queued' ? 'Queued remotely' : STATUS[state]?.word

  async function cancel() {
    setBusy(true)
    try {
      await action(`/api/gaps/${group.groupId}/cancel`)
      pushToast(`Cancelled ${group.album || 'the fill'}`)
    } catch (e) {
      pushToast(`Cancel failed: ${e.message}`, 'error')
    } finally { setBusy(false) }
  }

  return (
    <div className="row col">
      <div className="flex min-w-0 items-center gap-3">
        <Cover albumId={group.albumId} name={group.album || '?'} size={40} />
        <div className="grow">
          {group.groupId
            ? <button className="link plain t clip block max-w-full" onClick={() => goGap(group.groupId)}>{group.album || '(album)'}</button>
            : <div className="t clip">{group.album || '(album)'}</div>}
          <div className="s clip">{group.artist ? `${group.artist} · ` : ''}{line}</div>
          {live && <div className="mt-1.5"><Meter value={pct} /></div>}
        </div>
        <span className={`st ${STATUS[state]?.tone || ''} hide-sm`}>{word}</span>
        {state === 'failed' && group.groupId && (
          <button className="sm" onClick={() => navigate('needs', 'fix', `gap:${group.groupId}`)}>Fix</button>
        )}
        {live && group.groupId && <button className="sm" disabled={busy} onClick={cancel}>Cancel</button>}
        <button className="sm quiet" aria-expanded={open} onClick={() => setOpen(o => !o)}>{open ? 'Hide files' : 'Files'}</button>
      </div>
      {open && (
        <div className="fadein mt-1.5 border-t border-line pt-1" style={{ marginLeft: 52 }}>
          {rows.map(t => <FileRow key={t.id} t={t} onDismiss={onDismiss} nested />)}
        </div>
      )}
    </div>
  )
}

// A whole-album request (from an album page, Discover, the wishlist).
function RequestRow({ f }) {
  const { action, pushToast, scheduleRefresh } = useApp()
  const [busy, setBusy] = useState(false)
  const live = fillIsLive(f)
  const pct = f.percent || (f.total ? Math.round((f.done / f.total) * 100) : 0)
  const line = [
    f.total ? `${f.done} of ${f.total} files` : null,
    f.failed ? `${f.failed} failed` : null,
    f.bytesTotal && live ? `${fmtBytes(f.bytesDone)} of ${fmtBytes(f.bytesTotal)}` : null,
    f.speedBps ? mbps(f.speedBps) : null,
    f.source ? `@${f.source}` : null,
    !live && f.updatedAt ? `${ago(f.updatedAt)} ago` : null,
  ].filter(Boolean).join(' · ')
  // The files this fill fetched landed, but the album it was for still has
  // tracks missing: saying "In library" beside it would be about the files and
  // read as being about the album.
  const partial = f.gap && f.stillMissing > 0 && !live && f.state !== 'failed'
  const word = f.state === 'downloading' ? `Downloading ${pct}%`
    : f.state === 'queued' ? 'Queued remotely'
    : f.state === 'failed' ? `Failed · ${failWords(f.failureKind)}`
    : partial ? `${f.stillMissing} still missing` : undefined

  // A 200 is not a cancel: once placement has claimed the row the server
  // answers cancelled:false with the status, and the files land anyway.
  async function cancel() {
    setBusy(true)
    try {
      const r = await action('/api/album/cancel', { release_mbid: f.releaseMbid })
      pushToast(r?.cancelled === false
        ? `Too late to cancel ${f.album}. It is already ${r.status || 'being placed'}`
        : `Cancelled ${f.album}`)
    } catch (e) { pushToast(`Cancel failed: ${e.message}`, 'error') }
    finally { setBusy(false); scheduleRefresh(600) }
  }

  return (
    <div className="row stack">
      <Cover albumId={f.albumId} url={f.albumId ? null : caaCover(f.rgid)} name={f.album || '?'} size={40} />
      <div className="grow">
        {f.rgid
          ? <button className="link plain t clip block max-w-full" onClick={() => goAlbum(f.rgid)}>{f.album || f.releaseMbid}</button>
          : f.gap && f.groupId
            ? <button className="link plain t clip block max-w-full" onClick={() => goGap(f.groupId)}>{f.album}</button>
            : <div className="t clip">{f.album || f.releaseMbid}</div>}
        <div className="s clip">{f.artist ? `${f.artist} · ` : ''}{line}</div>
        {/* What a plain download did not ask the folder for ("Download all" is
            on the album page's source list). A line of its own: the one above
            is clipped. */}
        {leftOutText(f) && <div className="s">{leftOutText(f)}</div>}
        {f.verifyGaveUp && <div className="s">On disk, but Navidrome has not listed it yet.</div>}
        {live && <div className="mt-1.5"><Meter value={pct} /></div>}
      </div>
      {partial ? <span className="st wait">{word}</span> : <StatusChip status={f.state} word={word} />}
      {/* A whole-album request that failed waits under To fix. A gap fill's
          own failure is the album's, in the queue. */}
      {f.state === 'failed' && (f.gap
        ? <button className="sm" onClick={() => goGap(f.groupId)}>Open</button>
        : <button className="sm" onClick={() => navigate('needs', 'fix', `req:${f.releaseMbid}`)}>Fix</button>)}
      {f.state !== 'failed' && f.gap && f.stillMissing > 0 && (
        <button className="sm" onClick={() => goGap(f.groupId)}>Fill the rest</button>
      )}
      {f.cancellable && <button className="sm" disabled={busy} onClick={cancel}>Cancel</button>}
    </div>
  )
}

function groupByAlbum(rows) {
  const groups = new Map()
  const loose = []
  for (const t of rows) {
    if (t.kind !== 'track' || !(t.groupId || t.album)) { loose.push(t); continue }
    const key = t.groupId || `album:${t.album}`
    if (!groups.has(key)) groups.set(key, { key, groupId: t.groupId || '', albumId: t.albumId || '', album: t.album, artist: t.artist, rows: [] })
    groups.get(key).rows.push(t)
  }
  const grouped = []
  for (const g of groups.values()) {
    if (g.rows.length === 1 && !g.album) loose.push(g.rows[0])
    else grouped.push(g)
  }
  return { grouped, loose }
}

export default function Activity() {
  const { state, action, pushToast } = useApp()
  const { transfers, fills, gaps, routeParams, summary } = state
  const tab = routeParams[0] === 'landed' ? 'landed' : 'now'
  const [dismissed, setDismissed] = useState(() => new Set())
  const markDismissed = (id, hide = true) => setDismissed(prev => {
    const next = new Set(prev)
    if (hide) next.add(id); else next.delete(id)
    return next
  })

  const rows = (transfers?.transfers || []).filter(t => !dismissed.has(t.id))
  const { grouped, loose } = groupByAlbum(rows)
  const groupDone = g => g.rows.every(r => r.state === 'done')
  const liveGroups = grouped.filter(g => !groupDone(g))
  const doneGroups = grouped.filter(groupDone)
  const liveLoose = loose.filter(t => t.state !== 'done')
  const doneLoose = loose.filter(t => t.state === 'done')
  const finishedFiles = doneGroups.reduce((s, g) => s + g.rows.length, 0) + doneLoose.length

  // The fill ledger, newest first: whole-album requests and gap fills alike.
  // A gap fill's row carries no name of its own; the queue supplies it. What
  // still has none after that (old rows whose album has left the queue) can't
  // be named or acted on, so it gets one summary line, not a wall of ids.
  const requests = namedFills(fills, gaps).sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))
  const named = requests.filter(f => f.album || f.artist || f.rgid)
  const nameless = requests.filter(f => !named.includes(f))
  const liveRequests = named.filter(fillIsLive)
  // A failed request's actions are under To fix; it is listed here too, apart.
  const failedRequests = named.filter(f => f.state === 'failed')
  const landedRequests = named.filter(f => !fillIsLive(f) && f.state !== 'failed')
  const groupFailed = g => !g.rows.some(r => r.state === 'active' || r.state === 'queued')
  const movingGroups = liveGroups.filter(g => !groupFailed(g))
  const failedGroups = liveGroups.filter(groupFailed)
  const movingLoose = liveLoose.filter(t => t.state !== 'failed')
  const failedLoose = liveLoose.filter(t => t.state === 'failed')
  const failedCount = failedRequests.length + failedGroups.length + failedLoose.length

  const motion = liveRequests.length + movingGroups.length + movingLoose.length
  const landed = landedRequests.length + doneGroups.length + doneLoose.length
  const c = summary?.transfers || transfers?.counts || {}
  const searching = liveRequests.filter(f => f.state === 'searching').length
  const sub = [
    `${c.active || 0} downloading`,
    `${c.queued || 0} queued`,
    searching ? `${searching} searching` : null,
  ].filter(Boolean).join(' · ')

  async function clearFinished() {
    try {
      await action('/api/downloads/clear')
      pushToast('Cleared the finished transfers')
    } catch (e) { pushToast(`Could not clear: ${e.message}`, 'error') }
  }

  const loading = !transfers
  const nowEmpty = !motion && !failedCount
  return (
    <Page title="Activity" sub={loading ? '' : sub}
      tabs={[['now', 'In motion', loading ? null : motion], ['landed', 'Landed', loading ? null : landed]]}
      tab={tab} onTab={k => navigate('activity', k)}>
      {loading ? <p className="muted">{state.pollError ? 'Waiting for lb-bot…' : 'Loading transfers…'}</p>
        : tab === 'now' ? (
          <>
            {nowEmpty ? (
              <p className="empty">Nothing is downloading. Start a fill from Needs you, an album page or Discover.</p>
            ) : motion > 0 ? (
              <div className="rows arrive">
                {liveRequests.map(f => <RequestRow key={f.releaseMbid} f={f} />)}
                {movingGroups.map(g => (
                  <AlbumGroup key={g.key} group={g} onDismiss={markDismissed} defaultOpen={g.rows.some(r => r.state === 'failed')} />
                ))}
                {movingLoose.map(t => <FileRow key={t.id} t={t} onDismiss={markDismissed} />)}
              </div>
            ) : <p className="muted">Nothing is downloading right now.</p>}
            {failedCount > 0 && (
              <>
                <Grp n={failedCount}>Failed</Grp>
                <div className="rows arrive">
                  {failedRequests.map(f => <RequestRow key={f.releaseMbid} f={f} />)}
                  {failedGroups.map(g => <AlbumGroup key={g.key} group={g} onDismiss={markDismissed} defaultOpen />)}
                  {failedLoose.map(t => <FileRow key={t.id} t={t} onDismiss={markDismissed} />)}
                </div>
              </>
            )}
            <p className="muted mt-3">A fill that fails or stalls is also listed under Needs you, To fix, where its actions are.</p>
          </>
        ) : (
          <>
            {!landed && !nameless.length ? (
              <p className="empty">Nothing has landed recently.</p>
            ) : (
              <>
                {landedRequests.length > 0 && (
                  <>
                    <Grp n={landedRequests.length}>Albums</Grp>
                    <div className="rows arrive">{landedRequests.map(f => <RequestRow key={f.releaseMbid} f={f} />)}</div>
                  </>
                )}
                {(doneGroups.length > 0 || doneLoose.length > 0) && (
                  <>
                    <Grp n={finishedFiles}
                      right={<button className="link" onClick={clearFinished}
                        title="Remove the finished transfers from this list">Clear finished</button>}>
                      Finished transfers
                    </Grp>
                    <div className="rows arrive">
                      {doneGroups.map(g => <AlbumGroup key={g.key} group={g} onDismiss={markDismissed} />)}
                      {doneLoose.map(t => <FileRow key={t.id} t={t} onDismiss={markDismissed} />)}
                    </div>
                  </>
                )}
                {nameless.length > 0 && (
                  <p className="muted mt-3" title={nameless.map(f => `${f.releaseMbid} · ${f.state}`).join('\n')}>
                    {nameless.length} older request(s) recorded no album name
                    {nameless.every(f => f.verifyGaveUp) ? '. They were placed on disk and never seen in Navidrome' : ''}.
                  </p>
                )}
              </>
            )}
          </>
        )}
    </Page>
  )
}
