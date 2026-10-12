import { useEffect, useState } from 'react'
import { useApp } from '../App.jsx'
import { api, post } from '../lib/api.js'
import { Meter, St } from '../components/Page.jsx'
import { failWords, fmtDuration, STATUS } from '../components/ui.jsx'
import { fillSourceBeyond, leftOutText } from '../lib/albumSources.js'

// A whole-album request: an album the library does not have, asked for from an
// album page, Discover or the wishlist. Shared by the album page (which shows
// its own request as it runs) and Needs you › To fix (which lists the failed ones).

/** Follows one request in the fill ledger until it settles.
 *
 *  `made` counts the requests this page has made for the release (0: one it
 *  found on arrival). Each is followed afresh, and **an ended fill is believed
 *  only at the second look**: the first poll after a new request can still
 *  read the row the request before it left behind — its failure, or a fill
 *  that finished — and the page then showed that one and stopped asking. */
export function useRequest(releaseMbid, made = 0) {
  const [f, setF] = useState(null)
  useEffect(() => {
    setF(null)
    if (!releaseMbid) return
    let dead = false
    let timer
    let first = made > 0
    const tick = async () => {
      try {
        const r = await api(`/api/fills?release_mbids=${encodeURIComponent(releaseMbid)}`)
        if (dead) return
        const v = r.albums?.[releaseMbid]
        // A failed row with a retry pending is still moving.
        const over = !!v && (['verified', 'cancelled'].includes(v.state) || (v.state === 'failed' && !v.retryAt))
        const stale = first && over
        first = false
        if (!stale) setF(v || null)
        if (over && !stale) return
      } catch { /* poll again */ }
      if (!dead) timer = setTimeout(tick, 3000)
    }
    tick()
    return () => { dead = true; clearTimeout(timer) }
  }, [releaseMbid, made])
  return f
}

export function useRequestActions(f, { onRetried } = {}) {
  const { action, pushToast, scheduleRefresh } = useApp()
  const [busy, setBusy] = useState(false)
  async function run(fn, ok, bad) {
    setBusy(true)
    try {
      const r = await fn()
      const msg = typeof ok === 'function' ? ok(r) : ok
      if (msg) pushToast(msg)
      return r
    } catch (e) { pushToast(`${bad}: ${e.message}`, 'error') }
    finally { setBusy(false); scheduleRefresh(600) }
  }
  const retry = (extra = {}) => run(
    async () => {
      const r = await action('/api/album/download', {
        rgid: f.rgid, release_mbid: f.releaseMbid, artist: f.artist, album: f.album, total: f.total, ...extra,
      })
      onRetried?.(r)
      return r
    }, `Trying ${f.album || 'the album'} again`, 'Retry failed')
  // A 200 is not a cancel: once placement has claimed the row the server
  // answers cancelled:false with the status, and the files land anyway.
  const cancel = () => run(() => action('/api/album/cancel', { release_mbid: f.releaseMbid }),
    r => (r?.cancelled === false
      ? `Too late to cancel ${f.album || 'it'}. It is already ${r.status || 'being placed'}`
      : `Cancelled ${f.album || 'the request'}`), 'Cancel failed')
  const wishlist = () => run(() => post('/api/wishlist', { rgid: f.rgid, artist: f.artist, title: f.album }),
    `${f.album} is on the wishlist. lb-bot will keep looking`, 'Could not add to the wishlist')
  return { busy, retry, cancel, wishlist }
}

/** Why a request failed, and what can be done about it. `extra` adds buttons. */
export function RequestFailure({ f, extra, onRetried }) {
  const { busy, retry, cancel, wishlist } = useRequestActions(f, { onRetried })
  // Where it came from when that is not the peer that was picked, and the
  // files a plain download left in the folder: as on the running request's row.
  const also = [fillSourceBeyond(f), leftOutText(f)].filter(Boolean).join(' · ')
  const waiting = f.retryAt && f.serverTime ? f.retryAt - f.serverTime : 0
  const canRetry = f.retryable && !f.retryAt
  const noSource = f.failureKind === 'no_source' && f.rgid
  return (
    <>
      <div className="why bad">
        <div className="font-semibold">Failed · {failWords(f.failureKind)}</div>
        {f.reason ? <div className="mt-0.5">{f.reason}</div> : null}
        {f.source ? (
          <div className="mt-0.5">
            Last tried <span className="num">@{f.source}</span>{f.total ? `, ${f.done} of ${f.total} files arrived` : ''}.
          </div>
        ) : null}
        {also ? <div className="mt-0.5">{also}</div> : null}
        {waiting > 0 && <div className="mt-0.5">lb-bot retries on its own in {fmtDuration(waiting)}.</div>}
      </div>
      <div className="acts">
        {canRetry && (
          <button className="primary" disabled={busy} onClick={() => retry(f.source ? { excludeUsers: [f.source] } : {})}>
            {f.source ? `Try again, skipping @${f.source}` : 'Try again'}
          </button>
        )}
        {noSource && (
          <button className={canRetry ? '' : 'primary'} disabled={busy} onClick={wishlist}
            title="Search again slowly, every few hours, until a peer shares it">Add to wishlist</button>
        )}
        {f.mp3WouldHelp && !f.allowMp3 && (
          <button disabled={busy} onClick={() => retry({ allowMp3: true })}
            title="Only MP3 copies were found. Accept MP3 for this album only.">Allow MP3 and search</button>
        )}
        {f.cancellable && <button disabled={busy} onClick={cancel}>Cancel the automatic retry</button>}
        {extra}
      </div>
    </>
  )
}

// The request this page made (or found running), as the one row it is: the
// peer, where it has got to, and Cancel. `asked` is the peer this page
// clicked, for the moment before the fill has a row.
//
// The row's title is the peer the fill has now. What there is to say beyond
// that name (`fillSourceBeyond`: lb-bot moved on from the peer that was
// picked) and the files a plain download left in the folder (`leftOutText`)
// go in the line under it.
export function RequestProgress({ f, asked = '' }) {
  const { busy, cancel } = useRequestActions(f || {})
  if (!f || f.state === 'unknown') {
    return (
      <div className="rows"><div className="row">
        <St tone="run">{asked ? `Asked @${asked} for it…` : 'Requested. Looking for a source…'}</St>
      </div></div>
    )
  }
  const live = ['searching', 'queued', 'downloading', 'placing'].includes(f.state)
  const pct = f.percent || (f.total ? Math.round((f.done / f.total) * 100) : 0)
  const meta = STATUS[f.state] || { tone: '', word: f.state }
  const word = f.state === 'downloading' ? `Downloading ${pct}%` : f.state === 'queued' ? 'Queued remotely' : meta.word
  const line = [
    f.total ? `${f.done} of ${f.total} files` : null,
    f.failed ? `${f.failed} failed` : null,
    f.speedBps ? `${(f.speedBps / 1024 / 1024).toFixed(1)} MB/s` : null,
    fillSourceBeyond(f) || null,
    leftOutText(f) || null,
  ].filter(Boolean).join(' · ')
  return (
    <div className="rows">
      <div className="row col">
        <div className="flex min-w-0 items-center gap-3">
          <div className="grow">
            <div className="t num clip">{f.source ? `@${f.source}` : f.state === 'searching' ? 'Searching Soulseek' : 'Source'}</div>
            <div className="s" style={{ whiteSpace: 'normal' }}>{[f.quality, line].filter(Boolean).join(' · ') || (f.reason || '')}</div>
          </div>
          <span className={`st ${meta.tone}`}>{word}</span>
        </div>
        {live && (
          <div className="flex items-center gap-3 pt-2.5">
            <div className="grow flex flex-col gap-1.5">
              <Meter lg value={pct} />
              <div className="s">Filed into the library when the download finishes.</div>
            </div>
            {f.cancellable && <button className="sm" disabled={busy} onClick={cancel}>Cancel</button>}
          </div>
        )}
        {f.verifyGaveUp && <div className="s mt-1">On disk, but Navidrome has not listed it yet.</div>}
      </div>
    </div>
  )
}
