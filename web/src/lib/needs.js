// What counts as "to fix", in one place — the sidebar badge, the tab count and
// the list itself all read these.
//
// A gap that failed or got stuck, a whole-album request that failed, and a
// downloaded folder waiting to be filed each need a decision before anything
// else happens to them. Artists lb-bot could not match are listed under To fix
// too, but are not counted: there are about a hundred of them on a real
// library and none blocks a download.

const FILL_LIVE = new Set(['searching', 'queued', 'downloading', 'placing'])
export const fillIsLive = f => FILL_LIVE.has(f.state)

export const failedGaps = gaps => (gaps?.items || []).filter(g => g.status === 'failed')

/** Failed whole-album requests that can still be named and acted on. */
export const failedRequests = fills =>
  Object.values(fills?.albums || {}).filter(f => f.state === 'failed' && (f.album || f.artist || f.rgid))

export const foldersToFile = transfers => transfers?.needsPlacement || []

/** The number on the sidebar. Each part comes from the summary when the server
 *  sends it there (so it is the same on every page) and from the page's own
 *  list otherwise. */
export function toFixCount(state) {
  const s = state.summary
  const gaps = s?.gaps?.failed ?? (state.gaps ? failedGaps(state.gaps).length : 0)
  const requests = s?.requests?.failed ?? (state.fills ? failedRequests(state.fills).length : 0)
  const folders = s?.transfers?.needsPlacement ?? foldersToFile(state.transfers).length
  return gaps + requests + folders
}

/** How far a gap fill has got, as a whole album: finished files plus the
 *  fraction of the ones in flight, over every file wanted. From the summary's
 *  `activeFills` (see `_active_gap_fills` on the server for why the transfers
 *  alone cannot say). null when the server does not send it. */
export function fillProgress(summary, groupId) {
  const f = (summary?.activeFills || []).find(x => x.groupId === groupId)
  if (!f || !f.total) return null
  return {
    ...f,
    pct: Math.min(100, Math.round(((f.done + (f.partial || 0)) / f.total) * 100)),
  }
}

/** The fill ledger's rows, each given the name of the album it is for.
 *
 *  A whole-album request records its artist and album. A gap fill (the queue's
 *  "Use this source") writes its row under the release id alone, with no name,
 *  no artist and no group: four albums filled in a row showed up on Landed as
 *  "4 older requests recorded no album name". The queue knows every one of them
 *  by that same release id, so the name comes from there. A row that still has
 *  none is left as it is. */
export function namedFills(fills, gaps) {
  const byRelease = new Map()
  for (const g of gaps?.items || []) if (g.releaseMbid && !byRelease.has(g.releaseMbid)) byRelease.set(g.releaseMbid, g)
  return Object.values(fills?.albums || {})
    .filter(f => f.state && f.state !== 'unknown')
    .map(f => {
      const g = byRelease.get(f.releaseMbid)
      // A newer server names the row itself and says so (`gapFill`).
      if (f.album || f.artist) {
        return f.gapFill ? { ...f, gap: true, stillMissing: g && g.status !== 'complete' ? (g.missingCount || 0) : 0 } : f
      }
      if (!g) return f
      return {
        ...f, album: g.album, artist: g.artist, groupId: f.groupId || g.id, albumId: g.albumId, gap: true,
        // The files that were fetched landed; the album may still have others missing.
        stillMissing: g.status !== 'complete' ? (g.missingCount || 0) : 0,
      }
    })
}

// How many files each album fill has had at most, for a backend that does not
// send `activeFills`: a finished file leaves the list, so the most ever seen
// is the best count of the whole there is.
const seenTotals = new Map()
/** Progress of one album's fill from the transfer rows still listed for it:
 *  { done, total, pct }. Only moves forward while the fill is in the list. */
export function trackedProgress(key, rows) {
  const left = rows.filter(r => r.state !== 'done')
  const prev = seenTotals.get(key)
  const total = Math.max(prev?.total || 0, rows.length)
  seenTotals.set(key, { total, at: Date.now() })
  // Forget fills that have not been seen for a while, so a later fill of the
  // same album starts from its own count.
  for (const [k, v] of seenTotals) if (Date.now() - v.at > 120000) seenTotals.delete(k)
  if (!total) return { done: 0, total: 0, pct: 0 }
  const done = total - left.length
  const partial = left.reduce((s, r) => s + (r.state === 'active' ? Math.min(100, r.pct || 0) / 100 : 0), 0)
  return { done, total, pct: Math.min(100, Math.round(((done + partial) / total) * 100)) }
}
