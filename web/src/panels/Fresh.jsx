import { useEffect, useMemo, useState } from 'react'
import { useApp, goArtist, lsGet, lsSet } from '../App.jsx'
import { api, post } from '../lib/api.js'
import Cover from '../components/Cover.jsx'
import { Chip, EmptyState, SectionHeader, Skeleton, SortToggle } from '../components/ui.jsx'

const DAYS = [[7, '7 days'], [30, '30 days'], [90, '90 days']]
const SORTS = [['date', 'Newest'], ['artist', 'Artist A–Z']]
const TYPES = [['all', 'All types'], ['album', 'Albums'], ['ep', 'EPs'], ['single', 'Singles'], ['other', 'Other']]

// Which type bucket a release falls into, from its MusicBrainz primary type.
// Anything that isn't a plain Album/EP/Single (broadcasts, compilations,
// untyped rows) lands in "Other" so nothing silently disappears from a filter.
function typeBucket(r) {
  const t = String(r.type || '').toLowerCase()
  if (t === 'album') return 'album'
  if (t === 'ep') return 'ep'
  if (t === 'single') return 'single'
  return 'other'
}

// Restore a saved Fresh preference so scope/sort/window/type survive tab
// switches and reloads (the mock treats these as sticky filters).
const stored = (key, fallback) => lsGet('fresh.' + key, fallback)

function fmtDate(iso) {
  if (!iso) return ''
  const d = new Date(iso + 'T00:00:00')
  if (isNaN(d)) return iso
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

// Which weekly bucket a release falls into, by whole days between its release
// date and today. Future dates land in "Upcoming"; everything undated sinks to
// "Older" so it still renders somewhere.
function ageBucket(iso) {
  if (!iso) return 4
  const d = new Date(iso + 'T00:00:00')
  if (isNaN(d)) return 4
  const days = Math.floor((Date.now() - d.getTime()) / 86400000)
  if (days < 0) return -1     // upcoming
  if (days < 7) return 0      // this week
  if (days < 14) return 1     // last week
  if (days < 21) return 2     // two weeks ago
  if (days < 28) return 3     // three weeks ago
  return 4                    // older
}

const BUCKET_LABEL = {
  '-1': 'Upcoming',
  0: 'This week',
  1: 'Last week',
  2: 'Two weeks ago',
  3: 'Three weeks ago',
  4: 'Earlier',
}

// One fresh release: cover + name → the *album* page for this release-group
// (which resolves editions, tracklist and sources), plus a Get that goes to the
// same page with the source picker open.
//
// "Get" used to POST {rgid} straight from here — no source review, and no
// `release_mbid`, so one transient MusicBrainz 503 hard-failed that album for
// the next five minutes with nothing the user could do. The album page is the
// review step, and it sends the release it resolved.
function FreshCard({ r, onOpenArtist, onOpenAlbum, onGetSources, wishlisted, onWishlist }) {
  const upcoming = r.releaseDate && r.releaseDate > new Date().toISOString().slice(0, 10)

  return (
    <div className="flex flex-col rounded-card border border-line bg-panel p-2.5">
      <button className="!block !border-0 !bg-transparent !p-0 text-left"
        onClick={() => (r.releaseGroupMbid ? onOpenAlbum(r) : onOpenArtist(r))}
        title={r.releaseGroupMbid ? `Open ${r.releaseName}` : `Open ${r.artist}`}>
        <div className="mb-2"><Cover url={r.coverUrl} name={r.releaseName} fluid /></div>
        <div className="truncate text-small font-semibold">{r.releaseName}</div>
        <div className="truncate text-micro text-muted">{r.artist}</div>
      </button>
      <div className="mt-1 flex flex-wrap items-center gap-1.5 text-micro text-faint">
        <span>{fmtDate(r.releaseDate)}</span>
        {r.type ? <span>· {r.type}</span> : null}
        {/* Only the exact release being on disk earns "in library" — an owned
            artist with a brand-new album is still a download. */}
        {r.releaseOwned ? <span className="chip good !my-0 !py-0">in library</span> : null}
        {upcoming ? <span className="chip !my-0 !py-0">upcoming</span> : null}
      </div>
      <span className="spacer" />
      {r.releaseOwned ? (
        // Already in the library — don't offer a duplicate download.
        <button className="sm mt-2.5" onClick={() => onOpenArtist(r)}>View in library →</button>
      ) : upcoming ? (
        // Not out yet: the wishlist re-searches until someone shares it. This
        // was a disabled "Get this album" on every upcoming tile.
        <button className="sm tint mt-2.5" disabled={!r.releaseGroupMbid || wishlisted}
          title="lb-bot will look for it every few hours once it's out"
          onClick={() => onWishlist(r)}>
          {wishlisted ? '✓ On the wishlist' : 'Add to wishlist'}
        </button>
      ) : (
        <button className="sm primary mt-2.5" disabled={!r.releaseGroupMbid}
          title="Review sources, then download" onClick={() => onGetSources(r)}>
          Get this album →
        </button>
      )}
    </div>
  )
}

export default function Fresh() {
  const { pushToast } = useApp()
  const [wishlisted, setWishlisted] = useState(() => new Set())
  async function wishlist(r) {
    try {
      await post('/api/wishlist', { rgid: r.releaseGroupMbid, artist: r.artist, title: r.releaseName })
      setWishlisted(s => new Set(s).add(r.releaseGroupMbid))
      pushToast(`${r.releaseName} is on the wishlist`)
    } catch (e) {
      pushToast(`Could not add to the wishlist: ${e.message}`, 'error')
    }
  }
  // Already-wishlisted rows read as such on arrival.
  useEffect(() => {
    api('/api/wishlist').then(w => setWishlisted(new Set((w.wishlist || []).map(x => x.rgid)))).catch(() => {})
  }, [])
  const [days, setDays] = useState(() => Number(stored('days', 30)))
  const [scope, setScope] = useState(() => stored('scope', 'yours'))   // 'yours' | 'all'
  const [sort, setSort] = useState(() => stored('sort', 'date'))       // 'date' | 'artist'
  const [type, setType] = useState(() => stored('type', 'all'))        // all|album|ep|single|other
  const [data, setData] = useState(null)         // { releases } | null
  const [error, setError] = useState(null)

  // Persist the sticky filters whenever they change.
  useEffect(() => { lsSet('fresh.days', days) }, [days])
  useEffect(() => { lsSet('fresh.scope', scope) }, [scope])
  useEffect(() => { lsSet('fresh.sort', sort) }, [sort])
  useEffect(() => { lsSet('fresh.type', type) }, [type])

  useEffect(() => {
    let dead = false
    setData(null); setError(null)
    api('/api/fresh-releases?days=' + days)
      .then(r => !dead && setData(r))
      .catch(e => !dead && setError(e.message))
    return () => { dead = true }
  }, [days])

  const releases = data?.releases || []
  // "Your artists" means artists already in the library — that's artistOwned,
  // NOT releaseOwned (which is only the exact album being on disk).
  const ownedCount = useMemo(() => releases.filter(r => r.artistOwned).length, [releases])
  const scoped = scope === 'yours' ? releases.filter(r => r.artistOwned) : releases
  const typeCounts = useMemo(() => {
    const c = { all: scoped.length, album: 0, ep: 0, single: 0, other: 0 }
    for (const r of scoped) c[typeBucket(r)]++
    return c
  }, [scoped])
  const shown = type === 'all' ? scoped : scoped.filter(r => typeBucket(r) === type)

  // Sort by artist → one flat A–Z list; sort by date → ordered weekly buckets
  // with divider headers so "this week" is visually separated from "earlier".
  const groups = useMemo(() => {
    if (sort === 'artist') {
      const sorted = [...shown].sort((a, b) =>
        (a.artist || '').localeCompare(b.artist || '') ||
        (a.releaseName || '').localeCompare(b.releaseName || ''))
      return sorted.length ? [{ key: 'all', label: null, items: sorted }] : []
    }
    const byBucket = new Map()
    for (const r of shown) {
      const b = ageBucket(r.releaseDate)
      if (!byBucket.has(b)) byBucket.set(b, [])
      byBucket.get(b).push(r)
    }
    return [-1, 0, 1, 2, 3, 4]
      .filter(b => byBucket.has(b))
      .map(b => ({
        key: String(b),
        label: BUCKET_LABEL[b],
        items: byBucket.get(b).sort((a, c) => (c.releaseDate || '').localeCompare(a.releaseDate || '')),
      }))
  }, [shown, sort])

  // The artist id the album page needs: the owned Navidrome one where we have
  // it, else the `mb:` form the discography scan already understands for
  // artists off the library.
  function artistRouteId(r) {
    return r.artistOwned && r.artistId
      ? r.artistId
      : (r.artistMbids?.[0] ? 'mb:' + r.artistMbids[0] : null)
  }

  // Carrying the release-group is the whole difference between landing on the
  // album and landing on an artist index that predates it. The album page adds
  // the single index row itself when it finds the rgid missing — which it will,
  // for anything fresh, because a stored discography is served immediately even
  // when stale, by design.
  function openArtist(r, { album = false, sources = false } = {}) {
    const id = artistRouteId(r)
    if (!id) { pushToast('No MusicBrainz artist for this release', 'info'); return }
    if (album && r.releaseGroupMbid) goArtist(id, r.releaseGroupMbid, sources)
    else goArtist(id)
  }

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-4">
        <div className="flex items-center gap-1.5">
          <Chip active={scope === 'yours'} count={ownedCount} onClick={() => setScope('yours')}>Your artists</Chip>
          <Chip active={scope === 'all'} count={releases.length} onClick={() => setScope('all')}>All</Chip>
        </div>
        <div className="flex items-center gap-1.5">
          {DAYS.map(([d, label]) => (
            <Chip key={d} variant="solid" active={days === d} onClick={() => setDays(d)}>{label}</Chip>
          ))}
        </div>
        <SortToggle value={sort} options={SORTS} onChange={setSort} />
      </div>
      <div className="mb-4 flex flex-wrap items-center gap-1.5">
        {TYPES.map(([k, label]) => (
          <Chip key={k} active={type === k} count={typeCounts[k]} onClick={() => setType(k)}>{label}</Chip>
        ))}
      </div>
      <p className="mb-4 text-caption text-muted">
        Recent and upcoming releases from ListenBrainz. “Your artists” shows only artists already in your library.
      </p>

      {error ? (
        <EmptyState title="Couldn't load fresh releases" hint={error} />
      ) : data === null ? (
        <div className="tile-grid">
          {Array.from({ length: 12 }, (_, i) => (
            <div key={i} className="rounded-card border border-line bg-panel p-2.5">
              <Skeleton style={{ width: '100%', aspectRatio: '1 / 1' }} />
              <Skeleton className="mt-2 h-3 w-3/5" />
              <Skeleton className="mt-1.5 h-2.5 w-2/5" />
            </div>
          ))}
        </div>
      ) : !shown.length ? (
        <EmptyState
          title={type !== 'all' && scoped.length
            ? `No ${TYPES.find(t => t[0] === type)?.[1].toLowerCase() || 'releases'} here`
            : scope === 'yours' ? 'No fresh releases from your artists' : 'No fresh releases in this window'}
          hint={type !== 'all' && scoped.length
            ? 'Nothing of this type in the current scope — clear the type filter to see the rest.'
            : scope === 'yours'
              ? 'None of your library artists have a release in this window — widen the range or see everyone.'
              : 'Try a wider date range.'}>
          {type !== 'all' && scoped.length > 0 ? (
            <button className="primary" onClick={() => setType('all')}>Show all types</button>
          ) : scope === 'yours' && releases.length > 0 && (
            <button className="primary" onClick={() => setScope('all')}>See all new releases</button>
          )}
        </EmptyState>
      ) : (
        groups.map(g => (
          <div key={g.key} className="mb-6">
            {g.label && <SectionHeader label={g.label} sub={g.items.length} />}
            <div className="tile-grid">
              {g.items.map(r => (
                <FreshCard key={r.releaseMbid || r.releaseGroupMbid} r={r}
                  wishlisted={wishlisted.has(r.releaseGroupMbid)} onWishlist={wishlist}
                  onOpenArtist={openArtist}
                  onOpenAlbum={x => openArtist(x, { album: true })}
                  onGetSources={x => openArtist(x, { album: true, sources: true })} />
              ))}
            </div>
          </div>
        ))
      )}
    </>
  )
}
