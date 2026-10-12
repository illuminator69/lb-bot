import { useEffect, useMemo, useState } from 'react'
import { useApp, navigate, goArtist, goAlbum, lsGet, lsSet } from '../App.jsx'
import { useViewState } from '../lib/useView.js'
import { api, post } from '../lib/api.js'
import Cover from '../components/Cover.jsx'
import Page, { Grp, Pick, Seg } from '../components/Page.jsx'
import { ago, Chip, EmptyState, fmtDuration, Skeleton } from '../components/ui.jsx'

// Discover is what you don't have yet: New releases (the ListenBrainz feed),
// Charts (Deezer's chart and editors' picks), and the Wishlist — albums nobody
// was sharing, which lb-bot keeps looking for.
const TABS = ['new', 'charts', 'wishlist']

function DiscoverPage({ tab, sub, bar, children }) {
  const { state } = useApp()
  const wished = state.wishlist?.total ?? state.wishlist?.wishlist?.length
  return (
    <Page title="Discover" sub={sub}
      tabs={[['new', 'New releases'], ['charts', 'Charts'], ['wishlist', 'Wishlist', wished || null]]}
      tab={tab} onTab={k => navigate('discover', k)} bar={bar}>
      {children}
    </Page>
  )
}

const tileSkeleton = (
  <div className="grid">
    {Array.from({ length: 12 }, (_, i) => (
      <div key={i} className="tile">
        <Skeleton style={{ width: '100%', aspectRatio: '1 / 1' }} />
        <Skeleton className="h-3 w-3/5" />
        <Skeleton className="h-2.5 w-2/5" />
      </div>
    ))}
  </div>
)

/** Add / on-the-wishlist toggle. Adding is idempotent on the server. */
function WishButton({ wished, disabled, onAdd, className = 'sm' }) {
  return (
    <button className={`${className}${wished ? ' done-state' : ''}`} disabled={disabled || wished} aria-pressed={wished}
      title="lb-bot searches for it again every few hours until a peer shares it" onClick={onAdd}>
      {wished ? 'On the wishlist' : 'Add to wishlist'}
    </button>
  )
}

// ── New releases ─────────────────────────────────────────────────────────────
const DAYS = [['7', 'Last 7 days'], ['30', 'Last 30 days'], ['90', 'Last 90 days']]
const SORTS = [['date', 'Newest first'], ['artist', 'Artist A to Z']]
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

const stored = (key, fallback) => lsGet('fresh.' + key, fallback)

function fmtDate(iso, withYear) {
  if (!iso) return ''
  const d = new Date(iso + 'T00:00:00')
  if (isNaN(d)) return iso
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', ...(withYear ? { year: 'numeric' } : {}) })
}

// Which weekly bucket a release falls into, by whole days between its release
// date and today. Future dates land in "Upcoming"; everything undated sinks to
// "Earlier" so it still renders somewhere.
function ageBucket(iso) {
  if (!iso) return 4
  const d = new Date(iso + 'T00:00:00')
  if (isNaN(d)) return 4
  const days = Math.floor((Date.now() - d.getTime()) / 86400000)
  if (days < 0) return -1
  if (days < 7) return 0
  if (days < 14) return 1
  if (days < 21) return 2
  if (days < 28) return 3
  return 4
}

const BUCKET_LABEL = {
  '-1': 'Upcoming', 0: 'This week', 1: 'Last week', 2: 'Two weeks ago', 3: 'Three weeks ago', 4: 'Earlier',
}

// One new release. The cover and name open the *album* page for this
// release-group (which resolves editions, tracklist and sources); Find sources
// opens the same page with the search already running.
function FreshCard({ r, onOpenArtist, onOpenAlbum, onFindSources, wished, onWishlist }) {
  const upcoming = r.releaseDate && r.releaseDate > new Date().toISOString().slice(0, 10)
  const open = () => (r.releaseGroupMbid ? onOpenAlbum(r) : onOpenArtist(r))
  return (
    <div className="tile">
      <button className="art" onClick={open} aria-label={r.releaseName}>
        <Cover url={r.coverUrl} name={r.releaseName} fluid />
      </button>
      <div className="t clip font-medium">{r.releaseName}</div>
      <div className="muted clip text-caption">
        {r.artist}{r.type && typeBucket(r) !== 'album' ? ` · ${r.type}` : ''}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-1.5">
        {/* Only the exact release being on disk earns "In library" — an owned
            artist with a brand-new album is still a download. */}
        {r.releaseOwned ? <span className="st ok">In library</span>
          : upcoming ? <span className="st">Out {fmtDate(r.releaseDate)}</span>
          : <span className="st" title={fmtDate(r.releaseDate, true)}>{fmtDate(r.releaseDate)}</span>}
        {r.releaseOwned ? (
          // Already in the library — don't offer a duplicate download.
          <button className="sm" onClick={() => onOpenArtist(r)}>View</button>
        ) : upcoming ? (
          // Not out yet: the wishlist searches until someone shares it.
          <WishButton wished={wished} disabled={!r.releaseGroupMbid} onAdd={() => onWishlist(r)} />
        ) : (
          <button className="sm" disabled={!r.releaseGroupMbid} onClick={() => onFindSources(r)}>Find sources</button>
        )}
      </div>
    </div>
  )
}

function NewReleases() {
  const { state, pushToast, scheduleRefresh } = useApp()
  const [added, setAdded] = useState(() => new Set())
  const wishedSet = useMemo(
    () => new Set([...(state.wishlist?.wishlist || []).map(x => x.rgid), ...added]),
    [state.wishlist, added])
  async function wishlist(r) {
    try {
      await post('/api/wishlist', { rgid: r.releaseGroupMbid, artist: r.artist, title: r.releaseName })
      setAdded(s => new Set(s).add(r.releaseGroupMbid))
      pushToast(`${r.releaseName} is on the wishlist`)
      scheduleRefresh(400)
    } catch (e) {
      pushToast(`Could not add to the wishlist: ${e.message}`, 'error')
    }
  }
  const [days, setDays] = useState(() => String(Number(stored('days', 30)) || 30))
  const [scope, setScope] = useState(() => stored('scope', 'yours'))   // 'yours' | 'all'
  const [sort, setSort] = useState(() => stored('sort', 'date'))       // 'date' | 'artist'
  const [type, setType] = useState(() => stored('type', 'all'))        // all|album|ep|single|other
  const [data, setData] = useState(null)         // { releases } | null
  const [error, setError] = useState(null)

  useEffect(() => { lsSet('fresh.days', days) }, [days])
  useEffect(() => { lsSet('fresh.scope', scope) }, [scope])
  useEffect(() => { lsSet('fresh.sort', sort) }, [sort])
  useEffect(() => { lsSet('fresh.type', type) }, [type])

  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let dead = false
    setData(null); setError(null)
    api('/api/fresh-releases?days=' + days)
      .then(r => !dead && setData(r))
      .catch(e => !dead && setError(e))
    return () => { dead = true }
  }, [days, attempt])

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

  // Sort by artist → one flat A to Z list; sort by date → weekly groups, so
  // "this week" is set apart from "earlier".
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
  // album and landing on an artist index that predates it.
  function openArtist(r, { album = false, sources = false } = {}) {
    const id = artistRouteId(r)
    if (!id) { pushToast('No MusicBrainz artist for this release', 'info'); return }
    if (album && r.releaseGroupMbid) goArtist(id, r.releaseGroupMbid, sources)
    else goArtist(id)
  }

  const bar = (
    <>
      <Seg label="Whose releases" value={scope} onChange={setScope}
        options={[['yours', `Your artists${data ? ` ${ownedCount}` : ''}`], ['all', `Everyone${data ? ` ${releases.length}` : ''}`]]} />
      <Pick label="Period" value={days} onChange={setDays} options={DAYS} />
      <Pick label="Type" value={type} onChange={setType}
        options={TYPES.map(([k, l]) => [k, data ? `${l} (${typeCounts[k]})` : l])} />
      <Pick label="Sort" value={sort} onChange={setSort} options={SORTS} />
    </>
  )

  return (
    <DiscoverPage tab="new" bar={bar}
      sub="Recent and upcoming releases from ListenBrainz">
      {data?.stale && (
        <p className="why">
          ListenBrainz isn’t answering, so this is the list lb-bot fetched {ago(data.fetchedAt)} ago.{' '}
          <button className="link" onClick={() => setAttempt(n => n + 1)}>Try again</button>
        </p>
      )}
      {error ? (
        // The feed is ListenBrainz's own. When its API is down there is nothing
        // to show, and the raw upstream URL is not an explanation.
        <div className="empty">
          <div className="text-lead font-semibold text-ink">
            {error.status === 502 ? 'ListenBrainz isn’t answering' : 'Couldn’t load new releases'}
          </div>
          <p className="mt-1" style={{ maxWidth: '60ch', marginInline: 'auto' }}>
            {error.status === 502
              ? 'New releases come from ListenBrainz, and its service did not answer lb-bot just now. Nothing is wrong with your library. Charts and the Wishlist do not depend on it.'
              : error.message}
          </p>
          <div className="mt-3.5 flex flex-wrap justify-center gap-2.5">
            <button onClick={() => setAttempt(n => n + 1)}>Try again</button>
            <button className="quiet" onClick={() => navigate('discover', 'charts')}>Open Charts</button>
          </div>
          {error.status === 502 && (
            <details className="mx-auto mt-4 !border-0 !bg-transparent !p-0 text-left" style={{ maxWidth: '70ch' }}>
              <summary className="s !font-normal text-center">What ListenBrainz said</summary>
              <div className="logtail">{error.message}</div>
            </details>
          )}
        </div>
      ) : data === null ? tileSkeleton : !shown.length ? (
        <EmptyState
          title={type !== 'all' && scoped.length
            ? `No ${TYPES.find(t => t[0] === type)?.[1].toLowerCase() || 'releases'} here`
            : scope === 'yours' ? 'No new releases from your artists' : 'No new releases in this period'}
          hint={type !== 'all' && scoped.length
            ? 'Nothing of this type here. Clear the type filter to see the rest.'
            : scope === 'yours'
              ? 'None of your library artists have a release in this period. Widen it, or see everyone.'
              : 'Try a longer period.'}>
          {type !== 'all' && scoped.length > 0 ? (
            <button onClick={() => setType('all')}>Show all types</button>
          ) : scope === 'yours' && releases.length > 0 && (
            <button onClick={() => setScope('all')}>See everyone’s new releases</button>
          )}
        </EmptyState>
      ) : (
        groups.map(g => (
          <div key={g.key} className="mb-2">
            {g.label && <Grp n={g.items.length}>{g.label}</Grp>}
            <div className="grid arrive">
              {g.items.map(r => (
                <FreshCard key={r.releaseMbid || r.releaseGroupMbid} r={r}
                  wished={wishedSet.has(r.releaseGroupMbid)} onWishlist={wishlist}
                  onOpenArtist={openArtist}
                  onOpenAlbum={x => openArtist(x, { album: true })}
                  onFindSources={x => openArtist(x, { album: true, sources: true })} />
              ))}
            </div>
          </div>
        ))
      )}
    </DiscoverPage>
  )
}

// ── Charts ───────────────────────────────────────────────────────────────────
// A Deezer row has no MusicBrainz ids unless the library index could resolve
// it by name. For the rest, tapping asks MusicBrainz — fielded, because a
// free-text query ranks parodies above the real album. The server builds the
// query (quoted, with a free-text fallback and an exact-title re-rank).
async function resolveRgid(row) {
  if (row.rgid) return row.rgid
  const r = await api(`/api/album/lookup?artist=${encodeURIComponent(row.artist || '')}`
    + `&album=${encodeURIComponent(row.title || '')}`)
  return r.candidates?.[0]?.rgid || ''
}

function ChartAlbum({ a }) {
  const { pushToast } = useApp()
  const [busy, setBusy] = useState('')
  async function open(pick) {
    setBusy(pick ? 'find' : 'open')
    try {
      const rgid = await resolveRgid(a)
      if (rgid) goAlbum(rgid, pick)
      else pushToast(`MusicBrainz has no album called “${a.title}” by ${a.artist}`, 'info')
    } catch (e) {
      pushToast(`Lookup failed: ${e.message}`, 'error')
    } finally { setBusy('') }
  }
  return (
    <div className="tile">
      <button className="art relative" onClick={() => open(false)} disabled={!!busy} aria-label={a.title}>
        <Cover url={a.coverUrl || a.imageUrl} name={a.title} fluid />
        {a.position ? (
          <span className="num absolute left-1.5 top-1.5 rounded-ctl px-1.5"
            style={{ background: 'var(--surface)', color: 'var(--fg2)' }}>{a.position}</span>
        ) : null}
      </button>
      <div className="t clip font-medium">{a.title}</div>
      <div className="muted clip text-caption">
        {a.artist}{a.recordType && a.recordType !== 'album' ? ` · ${a.recordType}` : ''}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-1.5">
        {a.releaseOwned ? <span className="st ok">In library</span> : <span className="st">Not in library</span>}
        {a.releaseOwned
          ? <button className="sm" disabled={!!busy} onClick={() => open(false)}>View</button>
          : <button className="sm" disabled={!!busy} aria-busy={busy === 'find'} onClick={() => open(true)}>Find sources</button>}
      </div>
    </div>
  )
}

const GENRES_SHOWN = 9

function Charts() {
  const [genres, setGenres] = useState(null)
  const [genre, setGenreState] = useState(() => lsGet('discover.genre', '0'))
  const setGenre = g => { setGenreState(g); lsSet('discover.genre', g) }
  const [allGenres, setAllGenres] = useViewState('discover.allGenres', false)
  const [chart, setChart] = useState(null)
  const [editorial, setEditorial] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    api('/api/deezer/genres').then(r => setGenres(r.genres || [])).catch(() => setGenres([]))
  }, [])
  useEffect(() => {
    let dead = false
    setChart(null); setEditorial(null); setError('')
    Promise.all([
      api(`/api/deezer/chart?limit=30&genre=${encodeURIComponent(genre)}`, { timeoutMs: 30000 }),
      api(`/api/deezer/editorial?limit=20&genre=${encodeURIComponent(genre)}`, { timeoutMs: 30000 }).catch(() => ({ albums: [] })),
    ]).then(([c, e]) => { if (!dead) { setChart(c); setEditorial(e) } })
      .catch(e => { if (!dead) setError(e.message) })
    return () => { dead = true }
  }, [genre])

  const list = (genres?.length ? genres : [{ id: '0', name: 'All genres' }])
    .map(g => [String(g.id), String(g.id) === '0' ? 'All genres' : g.name])
  // One row of genres; the rest behind More. The chosen one always shows.
  const shownGenres = allGenres ? list : list.filter(([k], i) => i < GENRES_SHOWN || k === genre)
  const bar = (
    <div className="chips" style={{ flex: 1 }}>
      {shownGenres.map(([k, l]) => <Chip key={k} active={genre === k} onClick={() => setGenre(k)}>{l}</Chip>)}
      {list.length > GENRES_SHOWN && (
        <button className="chip" style={{ borderStyle: 'dashed', color: 'var(--muted)' }}
          onClick={() => setAllGenres(v => !v)}>{allGenres ? 'Less' : 'More…'}</button>
      )}
    </div>
  )

  return (
    <DiscoverPage tab="charts" bar={bar} sub="From Deezer">
      {error ? <EmptyState title="Deezer didn’t answer" hint={error} /> : (
        <>
          <Grp n={chart?.albums?.length || ''}>Top albums</Grp>
          {chart === null ? tileSkeleton : !chart.albums?.length
            ? <p className="muted">Nothing charting in this genre.</p>
            : <div className="grid arrive">{chart.albums.map(a => <ChartAlbum key={a.deezerId} a={a} />)}</div>}

          {chart?.artists?.length > 0 && (
            <>
              <Grp n={chart.artists.length}>Top artists</Grp>
              <div className="chips">
                {chart.artists.map(a => (
                  <button key={a.deezerId} className="chip"
                    title={a.owned ? 'In your library' : a.mbid ? 'Open this artist' : 'Search for this artist'}
                    onClick={() => a.owned && a.artistId ? goArtist(a.artistId)
                      : a.mbid ? goArtist(`mb:${a.mbid}`) : navigate('search', a.name)}>
                    {a.owned && <span className="st ok mr-1.5" aria-label="In library" />}{a.name}
                  </button>
                ))}
              </div>
            </>
          )}

          <Grp n={editorial?.albums?.length || ''}>Editors’ picks</Grp>
          {editorial === null ? tileSkeleton : !editorial.albums?.length
            ? <p className="muted">No editors’ picks right now.</p>
            : <div className="grid">{editorial.albums.map(a => <ChartAlbum key={a.deezerId} a={a} />)}</div>}

          <p className="muted mt-4 text-caption" style={{ maxWidth: '70ch' }}>
            The chart follows the country lb-bot’s server is in, not yours. Deezer offers no way to pick one, which
            is why this page has a genre filter and no country filter.
          </p>
        </>
      )}
    </DiscoverPage>
  )
}

// ── Wishlist ─────────────────────────────────────────────────────────────────
const caaCover = rgid => rgid ? `https://coverartarchive.org/release-group/${rgid}/front-250` : null

function Wishlist() {
  const { state, pushToast, scheduleRefresh } = useApp()
  const data = state.wishlist
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
  const cooldown = data?.cooldownSeconds ? fmtDuration(data.cooldownSeconds) : '12 h'
  return (
    <DiscoverPage tab="wishlist" sub="Albums nobody was sharing, searched again on a slow schedule">
      {data === null ? <p className="muted">Loading the wishlist…</p> : !rows.length ? (
        <p className="empty">
          The wishlist is empty. Albums with no source go here, and lb-bot searches for one of them every {every}.
        </p>
      ) : (
        <>
          <div className="rows arrive">
            {rows.map(r => (
              <div key={r.rgid} className="row stack">
                <Cover url={caaCover(r.rgid)} name={r.title || '?'} size={40} />
                <div className="grow">
                  <button className="link plain t clip block max-w-full" onClick={() => goAlbum(r.rgid)}>{r.title || r.rgid}</button>
                  <div className="s clip">
                    {r.artist}{' · added '}{ago(r.addedAt)} ago
                    {r.lastTriedAt ? ` · searched ${ago(r.lastTriedAt)} ago` : ' · not searched yet'}
                    {r.attempts ? ` · ${r.attempts} search${r.attempts === 1 ? '' : 'es'}` : ''}
                  </div>
                  {r.lastReason && <div className="s clip">{r.lastReason}</div>}
                </div>
                <button className="sm" onClick={() => goAlbum(r.rgid, true)}>Find sources</button>
                <button className="sm quiet" disabled={busy === r.rgid} onClick={() => remove(r)}>Remove</button>
              </div>
            ))}
          </div>
          <p className="muted mt-3" style={{ maxWidth: '70ch' }}>
            lb-bot searches for one of these every {every}, and for each at most every {cooldown}. When one lands it
            leaves the list on its own. Add albums from an album page, from New releases, or from a failed request
            under Needs you.
          </p>
        </>
      )}
    </DiscoverPage>
  )
}

export default function Discover() {
  const { state } = useApp()
  const tab = TABS.includes(state.routeParams[0]) ? state.routeParams[0] : 'new'
  return tab === 'charts' ? <Charts /> : tab === 'wishlist' ? <Wishlist /> : <NewReleases />
}
