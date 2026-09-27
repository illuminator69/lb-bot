import { useEffect, useMemo, useRef, useState } from 'react'
import { useApp, navigate, replaceRoute, goArtist } from '../App.jsx'
import { api, post } from '../lib/api.js'
import Cover from '../components/Cover.jsx'
import {
  AlbumStateChip, ArtistTile, Chip, EmptyState, ProgressBar, ReleaseTile,
  SectionHeader, Skeleton, SortToggle, SourceRow, StatusChip, TrackList, PageTitle,
} from '../components/ui.jsx'

// ── Routing ──────────────────────────────────────────────────────────────────
// Rendered by Library for #/library/artists/…; `params` is what follows:
//   []                          → ArtistIndex
//   [artistId]                  → DiscographyView
//   [artistId, rgid, 'sources'?] → AlbumDetail (picker open with 'sources')
//   ['-', rgid, …]              → AlbumResolver: an album known only by its
//                                 release-group (search, charts, wishlist)
// artistId is a Navidrome id for an owned artist, or `mb:<mbid>`.

const caaCover = rgid => `https://coverartarchive.org/release-group/${rgid}/front-250`

// ── Discography data ─────────────────────────────────────────────────────────
// Scan results keyed by artist id, session-lived.
const discoCache = new Map()
// Owned artists, shared by every view here and refetched once a minute. It
// used to be fetched once per session, so an artist a fill or a scan added
// read "Artist not found" — and AlbumResolver sent them to an `mb:` page —
// until a reload.
const ARTISTS_TTL_MS = 60_000
let artistsPromise = null
let artistsAt = 0
export function loadArtists() {
  if (!artistsPromise || Date.now() - artistsAt > ARTISTS_TTL_MS) {
    artistsAt = Date.now()
    artistsPromise = api('/api/artists').then(r => (Array.isArray(r) ? r : []))
      .catch(e => { artistsPromise = null; throw e })
  }
  return artistsPromise
}

// A credit and a tag spell the same artist differently often enough ("JAY‐Z"
// with U+2010, "Beyoncé"/"Beyonce") that name equality is only a fallback.
const nameKey = s => (s || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
  .toLowerCase().replace(/[\u2010-\u2015\u2212]/g, '-').replace(/\s+/g, ' ').trim()

// The library artist a MusicBrainz artist (and credit name) stands for, or null.
// By mbid first — exact — and by name only when exactly one artist has it.
export function ownedArtistFor(artists, mbid, name) {
  if (mbid) {
    const byMbid = (artists || []).filter(a => a.mbid && a.mbid === mbid)
    if (byMbid.length === 1) return byMbid[0]
  }
  const key = nameKey(name)
  if (!key) return null
  const byName = (artists || []).filter(a => nameKey(a.name) === key)
  // A same-name artist with a different mbid is someone else.
  return byName.length === 1 && !(mbid && byName[0].mbid && byName[0].mbid !== mbid) ? byName[0] : null
}

function useArtists() {
  const [artists, setArtists] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => {
    let dead = false
    loadArtists().then(a => !dead && setArtists(a)).catch(e => { if (!dead) { setError(e.message); setArtists([]) } })
    return () => { dead = true }
  }, [])
  return [artists, error]
}

// Runs (or reuses) the discography scan for one artist. Index-first: a
// previously scanned artist renders instantly.
function useDiscography(artist, { autoScan = true } = {}) {
  const { action } = useApp()
  const [disco, setDisco] = useState(() => (artist ? discoCache.get(artist.id) || null : null))
  const [progress, setProgress] = useState(null)
  const [error, setError] = useState(null)
  const [nonce, setNonce] = useState(0)
  // Whether the server HAS an index for this artist: true / false / null while
  // asking. `disco` alone can't answer that during a multi-minute scan.
  const [indexed, setIndexed] = useState(null)
  const pollRef = useRef(null)
  const forceScanRef = useRef(false)

  const artistId = artist?.id
  useEffect(() => {
    if (!artistId) return
    const cached = discoCache.get(artistId)
    setDisco(cached || null)
    setError(null)
    setProgress(null)
    setIndexed(cached ? true : null)
    if (cached) return
    let dead = false
    const forceScan = forceScanRef.current
    forceScanRef.current = false
    async function start() {
      try {
        if (!forceScan) {
          const idx = await api('/api/artist/discography'
            + `?mbid=${encodeURIComponent(artist.mbid || '')}`
            + `&nd_id=${encodeURIComponent(artistId)}`)
          if (dead) return
          setIndexed(idx.indexed === true)
          // A stub — the row an album page's single-release add or an adopted
          // orphan leaves, never scanned — is indexed but is not a discography:
          // rendering it showed a one-album artist and never scanned.
          const stub = idx.indexed && !idx.scanned_at
          if (idx.indexed && !stub) {
            discoCache.set(artistId, idx)
            setDisco(idx)
            return
          }
          if (stub) setDisco(idx)
        }
        // Whether to walk the artist's whole MusicBrainz discography (minutes)
        // is the caller's decision; an album page never does.
        if (!autoScan) return
        let mbid = artist.mbid
        if (!mbid) {
          const r = await api('/api/artist/lookup?q=' + encodeURIComponent(artist.name))
          mbid = (r.candidates || [])[0]?.mbid
          if (!mbid) throw new Error(`No MusicBrainz match for “${artist.name}”`)
        }
        // No name for an `mb:` page is fine: the server looks the real one up.
        // Posting the mbid in its place stored the UUID as the artist's name.
        const r = await action('/api/artist/discography',
          { mbid, name: artist.name || '', nd_id: artistId, external: !!artist.external })
        if (dead) return
        let delay = 2000
        const tick = async () => {
          try {
            const t = await api(`/api/tasks/${r.task_id}`)
            if (dead) return
            if (t.status === 'complete') {
              const result = t.result || { releases: [] }
              discoCache.set(artistId, result)
              setDisco(result)
              return
            }
            if (t.status === 'error') { setError(t.error || 'Discography scan failed'); return }
            setProgress({ done: t.done, total: t.total, current: t.current })
          } catch { /* poll again */ }
          if (dead) return
          delay = Math.min(delay * 1.4, 15000)
          pollRef.current = setTimeout(tick, delay)
        }
        pollRef.current = setTimeout(tick, delay)
      } catch (e) {
        if (dead) return
        setError(e.message)
        // A failed probe is not "still asking" — the album page's cheap
        // single-release add must still be able to run.
        setIndexed(false)
      }
    }
    start()
    return () => { dead = true; if (pollRef.current) clearTimeout(pollRef.current) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [artistId, nonce])

  const rescan = () => {
    if (artistId) discoCache.delete(artistId)
    forceScanRef.current = true
    setNonce(n => n + 1)
  }
  const refreshIndex = () => {
    if (artistId) discoCache.delete(artistId)
    setNonce(n => n + 1)
  }
  return { disco, indexed, progress, error, rescan, refreshIndex }
}

// ── Artist index ─────────────────────────────────────────────────────────────
const SORTS = [['az', 'A–Z'], ['plays', 'Most played']]
const TILE_CHUNK = 120

function SkeletonTileGrid({ tiles = 12, round = false }) {
  return (
    <div className="tile-grid">
      {Array.from({ length: tiles }, (_, i) => (
        <div key={i} className="rounded-card border border-line bg-panel p-2.5">
          <Skeleton className={round ? '!rounded-pill' : ''} style={{ width: '100%', aspectRatio: '1 / 1' }} />
          <Skeleton className="mt-2 h-3" style={{ width: `${70 - (i % 3) * 15}%` }} />
          <Skeleton className="mt-1.5 h-2.5 w-1/3" />
        </div>
      ))}
    </div>
  )
}

// One line on how complete the library index is. Building it moved to
// Settings → Status once the auto-index worker started doing it in the
// background.
function IndexLine() {
  const [s, setS] = useState(null)
  useEffect(() => { api('/api/library-index/status').then(setS).catch(() => {}) }, [])
  if (!s) return null
  return (
    <button className="quiet sm" onClick={() => navigate('Settings', 'status')}
      title="How many of your artists have their full discography indexed">
      {s.artistsIndexed.toLocaleString()}/{s.artistsTotal.toLocaleString()} indexed
      {s.artistsStale ? ` · ${s.artistsStale} stale` : ''}{s.building ? ' · building…' : ''}
    </button>
  )
}

function ArtistIndex() {
  const [artists, error] = useArtists()
  const [sort, setSort] = useState('az')
  const [q, setQ] = useState('')
  const [limit, setLimit] = useState(TILE_CHUNK)
  const hasPlays = (artists || []).some(a => a.plays != null)
  const needle = q.trim().toLowerCase()

  const shown = useMemo(() => {
    let list = artists || []
    if (needle) list = list.filter(a => (a.name || '').toLowerCase().includes(needle))
    list = [...list]
    if (sort === 'plays' && hasPlays) list.sort((a, b) => (b.plays || 0) - (a.plays || 0) || a.name.localeCompare(b.name))
    else list.sort((a, b) => a.name.localeCompare(b.name))
    return list
  }, [artists, needle, sort, hasPlays])
  useEffect(() => { setLimit(TILE_CHUNK) }, [needle, sort])

  // Grow the grid as the end comes into view — 2,500 round covers at once
  // was the slowest paint in the app.
  const sentinelRef = useRef(null)
  useEffect(() => {
    const node = sentinelRef.current
    if (!node) return
    const obs = new IntersectionObserver(([e]) => { if (e.isIntersecting) setLimit(n => n + TILE_CHUNK) }, { rootMargin: '600px' })
    obs.observe(node)
    return () => obs.disconnect()
  }, [shown.length, limit])

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-2.5">
        <input type="search" placeholder="Filter your artists…" aria-label="Filter your artists" className="w-60"
          value={q} onChange={e => setQ(e.target.value)} />
        {hasPlays && <SortToggle value={sort} options={SORTS} onChange={setSort} />}
        <span className="text-caption text-muted">{artists ? `${shown.length.toLocaleString()} artist(s)` : ''}</span>
        <span className="spacer" />
        <IndexLine />
      </div>

      {artists === null ? <SkeletonTileGrid round />
        : error ? <EmptyState title="Couldn't load your artists" hint={error} />
        : !artists.length ? <EmptyState title="No artists in your library yet" hint="Once Navidrome has scanned some music, every artist shows up here." />
        : !shown.length ? (
          <EmptyState title={`None of your artists match “${q.trim()}”`} hint="Search everywhere to open an artist you don't own yet.">
            <button className="primary" onClick={() => navigate('Search', q.trim())}>Search for “{q.trim()}”</button>
          </EmptyState>
        ) : (
          <>
            <div className="tile-grid">
              {shown.slice(0, limit).map(a => (
                <ArtistTile key={a.id} name={a.name} coverUrl={a.coverUrl}
                  sub={`${a.releaseCount} release${a.releaseCount === 1 ? '' : 's'}${a.plays != null ? ` · ${a.plays.toLocaleString()} play${a.plays === 1 ? '' : 's'}` : ''}`}
                  onClick={() => goArtist(a.id)} />
              ))}
            </div>
            {limit < shown.length && <div ref={sentinelRef} className="p-4 text-center text-caption text-faint">Loading more…</div>}
          </>
        )}
    </>
  )
}

// ── About ────────────────────────────────────────────────────────────────────
// Editorial text from Wikipedia/Wikidata via /api/meta/*. The clients have
// shown this since 2026-09-22; the web UI never did.
function About({ meta, compact = false }) {
  const [open, setOpen] = useState(false)
  if (!meta?.found) return null
  const paras = meta.paragraphs?.length ? meta.paragraphs : (meta.summary ? [meta.summary] : [])
  const shown = open ? paras : paras.slice(0, 1)
  return (
    <div className={compact ? '' : 'mb-6'}>
      {!compact && <SectionHeader label="About" sub={meta.wikidataDescription} />}
      <div className="max-w-[820px] text-small leading-[1.65]" style={{ color: 'var(--text2)' }}>
        {shown.map((p, i) => <p key={i} className={i ? 'mt-2.5' : ''}>{p}</p>)}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-caption">
        {paras.length > 1 && (
          <button className="link-inline !text-muted" onClick={() => setOpen(o => !o)}>{open ? 'Show less' : 'Read more'}</button>
        )}
        {meta.source?.url && (
          <a href={meta.source.url} target="_blank" rel="noreferrer" className="text-faint">
            From {meta.source.name}{meta.source.license ? ` · ${meta.source.license}` : ''}
          </a>
        )}
        {(meta.links || []).slice(0, 8).map(l => (
          <a key={l.url} href={l.url} target="_blank" rel="noreferrer"
            className="rounded-pill border border-line px-2 py-0.5 text-micro text-muted no-underline hover:text-[var(--accent)]">{l.label}</a>
        ))}
      </div>
    </div>
  )
}

function useMeta(path) {
  const [meta, setMeta] = useState(null)
  useEffect(() => {
    if (!path) { setMeta(null); return }
    let dead = false
    setMeta(null)
    api(path, { timeoutMs: 15000 }).then(m => !dead && setMeta(m)).catch(() => !dead && setMeta({ found: false }))
    return () => { dead = true }
  }, [path])
  return meta
}

// Similar artists, owned and not — ListenBrainz + Last.fm, with Deezer's
// related artists as the fallback when those know nobody.
function SimilarArtists({ mbid, name }) {
  const [data, setData] = useState(null)
  useEffect(() => {
    if (!mbid && !name) return
    let dead = false
    const q = new URLSearchParams({ name: name || '' })
    if (mbid) q.set('mbid', mbid)
    api(`/api/artist/similar?${q}`, { timeoutMs: 20000 })
      .then(r => (r.artists?.length ? r : api(`/api/artist/related?${q}`, { timeoutMs: 20000 })))
      .then(r => !dead && setData(r))
      .catch(() => !dead && setData({ artists: [] }))
    return () => { dead = true }
  }, [mbid, name])
  const rows = (data?.artists || []).filter(a => a.owned ? a.artistId : a.mbid).slice(0, 12)
  if (!rows.length) return null
  return (
    <div className="mb-6">
      <SectionHeader label="Similar artists" sub={(data.sources || []).join(' + ')} />
      <div className="flex flex-wrap gap-1.5">
        {rows.map(a => (
          <button key={a.mbid || a.artistId} className="sm"
            title={a.owned ? 'In your library' : 'Not in your library'}
            style={a.owned ? { borderColor: 'var(--green-bd)' } : undefined}
            onClick={() => goArtist(a.owned ? a.artistId : `mb:${a.mbid}`)}>
            {a.name}{a.owned ? ' ✓' : ''}
          </button>
        ))}
      </div>
    </div>
  )
}

// ── Discography ──────────────────────────────────────────────────────────────
const TYPE_GROUPS = [
  ['album', 'Albums'],
  ['ep', 'EPs'],
  ['single', 'Singles'],
  ['compilation', 'Compilations'],
  ['soundtrack', 'Soundtracks'],
  ['live', 'Live'],
  ['', 'Other'],
]
const STATUS_FILTERS = [
  ['all', 'All'],
  ['missing', 'Missing'],
  ['incomplete', 'Has gaps'],
  ['complete', 'Complete'],
  ['untagged', 'Untagged'],
]
// A section this long starts folded — 59 singles buried 7 albums.
const FOLD_OVER = 12

function groupKey(release) {
  const t = release.effective_type || release.primary_type || ''
  return TYPE_GROUPS.some(([k]) => k === t) && t !== '' ? t : ''
}

function relTime(epoch) {
  const days = Math.floor((Date.now() / 1000 - epoch) / 86400)
  if (days <= 0) return 'today'
  if (days === 1) return 'yesterday'
  if (days < 30) return `${days} days ago`
  return `${Math.floor(days / 30)} month(s) ago`
}

function TypeSection({ label, releases, onOpen }) {
  const [open, setOpen] = useState(releases.length <= FOLD_OVER)
  return (
    <div className="mb-5">
      <SectionHeader label={label} sub={releases.length}
        action={releases.length > FOLD_OVER && (
          <button className="link-inline !text-caption !text-muted" aria-expanded={open} onClick={() => setOpen(o => !o)}>
            {open ? 'Fold' : `Show all ${releases.length}`}
          </button>)} />
      <div className="tile-grid">
        {(open ? releases : releases.slice(0, 6)).map(r => (
          <ReleaseTile key={r.rgid} title={r.title} year={r.year} state={r.status}
            count={r.status === 'incomplete' ? (r.total || 0) - (r.present || 0) : null}
            coverUrl={caaCover(r.rgid)} onClick={() => onOpen(r.rgid)} />
        ))}
      </div>
    </div>
  )
}

function DiscographyView({ artist }) {
  const { disco, progress, error, rescan } = useDiscography(artist)
  const { requestConfirm, pushToast } = useApp()
  const [filter, setFilter] = useState('all')
  const mbid = artist.mbid || disco?.artist_mbid || ''
  const meta = useMeta(mbid ? `/api/meta/artist?mbid=${encodeURIComponent(mbid)}` : null)

  const releases = disco?.releases || []
  const counts = useMemo(() => {
    const c = { all: releases.length }
    for (const r of releases) c[r.status] = (c[r.status] || 0) + 1
    return c
  }, [releases])
  const shown = filter === 'all' ? releases : releases.filter(r => r.status === filter)
  const missingAlbums = releases.filter(r => r.status === 'missing' && ['album', 'ep'].includes(groupKey(r)))
  const name = (artist.external ? (disco?.artist_name || meta?.name || artist.name) : artist.name) || 'Artist'

  async function wishlistMissing() {
    const ok = await requestConfirm(
      `Add ${missingAlbums.length} missing album(s) and EP(s) by ${name} to the wishlist? lb-bot re-searches one every few hours.`,
      { confirmLabel: 'Add to wishlist' })
    if (!ok) return
    let added = 0
    for (const rel of missingAlbums) {
      try { await post('/api/wishlist', { rgid: rel.rgid, artist: artist.name || disco?.artist_name || meta?.name || '', title: rel.title }); added++ } catch { /* keep going */ }
    }
    pushToast(`Added ${added} album(s) to the wishlist`)
  }

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-2.5">
        <button onClick={() => navigate('Library', 'artists')}>← All artists</button>
        <span className="spacer" />
        {disco?.scanned_at ? (
          <span className="text-caption text-faint">
            scanned {relTime(disco.scanned_at)}
            {disco.stale && <span style={{ color: 'var(--decide-fg)' }}> · may be out of date</span>}
          </span>
        ) : disco ? (
          <span className="text-caption" style={{ color: 'var(--decide-fg)' }}>not fully scanned yet</span>
        ) : null}
        {disco && <button className="sm" onClick={rescan}>Rescan discography</button>}
      </div>

      <div className="hero mb-5 flex items-start gap-5">
        <div className="w-[120px] shrink-0">
          <Cover url={meta?.imageUrl || artist.coverUrl} name={name} fluid round />
        </div>
        <div className="min-w-0 flex-1">
          {/* An `mb:` route can still be an artist you own (a link from search or
              a similar-artists row), so ask the discography, not the route. */}
          <PageTitle eyebrow={disco && !releases.some(r => r.status !== 'missing') ? 'Not in your library' : 'Artist'} title={name} />
          {meta?.wikidataDescription && <div className="mt-1 text-small text-muted">{meta.wikidataDescription}</div>}
          {meta?.found && <div className="mt-3"><About meta={meta} compact /></div>}
        </div>
      </div>

      {error ? (
        <EmptyState title="Discography scan failed" hint={error}>
          <button className="primary" onClick={rescan}>Try again</button>
        </EmptyState>
      ) : !disco ? (
        <>
          <div className="mb-4 flex flex-wrap items-center gap-3 rounded-card border border-line bg-panel px-4 py-3">
            <div className="min-w-[180px] flex-1">
              <ProgressBar value={progress?.total ? (progress.done / progress.total) * 100 : 4} />
            </div>
            <span className="text-caption" style={{ color: 'var(--text2)' }}>
              {progress?.total ? `Checking ${progress.done}/${progress.total}` : 'Pulling the discography from MusicBrainz…'}
            </span>
            {progress?.current && <span className="min-w-0 truncate text-caption text-faint">{progress.current}</span>}
          </div>
          <SkeletonTileGrid />
        </>
      ) : (
        <>
          <div className="mb-4 flex flex-wrap items-center gap-2">
            {STATUS_FILTERS.map(([k, label]) => (
              <Chip key={k} active={filter === k} count={counts[k] || 0} onClick={() => setFilter(k)}>{label}</Chip>
            ))}
            <span className="spacer" />
            {missingAlbums.length > 0 && (filter === 'missing' || filter === 'all') && (
              <button className="sm tint" onClick={wishlistMissing}
                title="Albums and EPs you don't have; lb-bot will look for them slowly in the background">
                Wishlist {missingAlbums.length} missing album(s)
              </button>
            )}
          </div>
          {!shown.length ? <EmptyState title="No releases match this filter" /> : (
            TYPE_GROUPS.map(([type, label]) => {
              const group = shown.filter(r => groupKey(r) === type)
              return group.length
                ? <TypeSection key={type || 'other'} label={label} releases={group} onOpen={rgid => goArtist(artist.id, rgid)} />
                : null
            })
          )}
          <SimilarArtists mbid={mbid} name={name} />
        </>
      )}
    </>
  )
}

// ── Album detail ─────────────────────────────────────────────────────────────
// Ceiling for the two MusicBrainz-backed lookups the album page blocks on.
const LOOKUP_TIMEOUT_MS = 10000

// The release the user has on screen, in the shape /api/album/sources and
// /api/album/download accept. Sending it keeps the server from overruling the
// edition the user picked.
function releaseOverride(release, variant, artistName) {
  if (!variant?.releaseMbid) return {}
  return {
    release_mbid: variant.releaseMbid,
    artist: artistName || '',
    album: release?.title || variant.title || '',
    total: variant.trackCount || 0,
  }
}

// Where an album request has got to, straight after you made it — so the page
// doesn't just say "see Downloads".
function RequestStatus({ releaseMbid }) {
  const [f, setF] = useState(null)
  useEffect(() => {
    if (!releaseMbid) return
    let dead = false
    let timer
    const tick = async () => {
      try {
        const r = await api(`/api/fills?release_mbids=${encodeURIComponent(releaseMbid)}`)
        if (dead) return
        const v = r.albums?.[releaseMbid]
        setF(v)
        if (v && ['verified', 'failed', 'cancelled'].includes(v.state)) return
      } catch { /* poll again */ }
      if (!dead) timer = setTimeout(tick, 3000)
    }
    tick()
    return () => { dead = true; clearTimeout(timer) }
  }, [releaseMbid])
  if (!f || f.state === 'unknown') return <p className="mt-2 text-caption text-muted">Requested — looking for a source…</p>
  return (
    <div className="mt-2.5 flex max-w-[460px] flex-wrap items-center gap-2 text-caption">
      <StatusChip status={f.state} />
      <span className="text-muted">
        {f.total ? `${f.done}/${f.total} files` : ''}{f.reason ? ` · ${f.reason}` : ''}
      </span>
      <button className="link-inline !text-muted" onClick={() => navigate('Downloads')}>Downloads</button>
      {['searching', 'queued', 'downloading', 'placing'].includes(f.state) && (
        <div className="w-full"><ProgressBar value={f.percent || 0} height={5} /></div>
      )}
    </div>
  )
}

function AlbumSourcePicker({ rgid, open, onDownloaded, override }) {
  const { action, pushToast } = useApp()
  const [sources, setSources] = useState(null)
  const [chosen, setChosen] = useState(null)
  const [busy, setBusy] = useState(false)
  const overrideKey = JSON.stringify(override || {})

  useEffect(() => {
    if (!open) return
    let dead = false
    setSources(null)
    const q = new URLSearchParams({ rgid })
    for (const [k, v] of Object.entries(JSON.parse(overrideKey))) if (v) q.set(k, String(v))
    api('/api/album/sources?' + q)
      .then(r => !dead && setSources(r.sources || []))
      .catch(e => { if (!dead) { setSources([]); pushToast(`Source search failed: ${e.message}`, 'error') } })
    return () => { dead = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, rgid, overrideKey])

  async function useSource(src) {
    setBusy(true)
    setChosen(src.id)
    try {
      // The peer is a preference: lb-bot floats it to the front of its ranked
      // list and keeps the rest as failover.
      const r = await action('/api/album/download',
        { rgid, ...(override || {}), sourceUsername: src.peer, sourceFolder: src.folder })
      pushToast('Queued from @' + src.peer)
      onDownloaded?.(r)
    } catch (e) {
      setChosen(null)
      pushToast(`Download failed: ${e.message}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  if (!open) return null
  return (
    <div className="mt-5">
      <SectionHeader label="Available on Soulseek" sub="ranked by your source preferences" />
      {sources === null ? (
        <div className="flex flex-col gap-2">
          {Array.from({ length: 3 }, (_, i) => (
            <div key={i} className="flex items-center gap-3.5 rounded-card border border-line p-3.5" style={{ background: 'var(--inset-warm)' }}>
              <Skeleton className="h-11 w-11" />
              <div className="flex-1"><Skeleton className="h-3.5 w-2/5" /><Skeleton className="mt-2 h-3 w-3/5" /></div>
            </div>
          ))}
          <p className="text-caption text-faint">Searching peers — results can take ~10s to trickle in.</p>
        </div>
      ) : !sources.length ? (
        <EmptyState title="No peer is sharing this album right now"
          hint="Add it to the wishlist and lb-bot will look again every few hours." />
      ) : sources.map(s => (
        <SourceRow key={s.id} src={s} busy={busy} selected={chosen === s.id}
          actionLabel="Use this →" done={chosen === s.id && !busy} doneLabel="✓ Queued"
          onUse={() => useSource(s)} />
      ))}
    </div>
  )
}

// Release / edition switcher: a *release* variant changes the tracklist; an
// *edition* is the same tracklist pressed differently, with its own cover.
function EditionSwitcher({ variants, variantIdx, editionIdx, onPick }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  useEffect(() => {
    if (!open) return
    function onDocClick(e) { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    function onEsc(e) { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDocClick)
    document.addEventListener('keydown', onEsc)
    return () => { document.removeEventListener('mousedown', onDocClick); document.removeEventListener('keydown', onEsc) }
  }, [open])

  const variant = variants?.[variantIdx]
  const editions = variant?.editions || []
  if (!variants?.length) return null
  if (variants.length < 2 && editions.length < 2) return null

  const variantLabel = (v, i) => v.disambiguation || (i === 0 ? 'Original' : v.year || `Edition ${i + 1}`)
  const summary = [variantLabel(variant, variantIdx), editions[editionIdx]?.label].filter(Boolean).join(' · ')
  const chipStyle = active => ({
    borderColor: active ? 'var(--accent)' : 'var(--border)',
    background: active ? 'var(--accent-tint)' : 'var(--inset-warm)',
    color: active ? 'var(--accent)' : 'var(--text2)',
    fontWeight: active ? 600 : 400,
  })

  return (
    <div className="relative" ref={ref}>
      <button className="sm !rounded-pill" aria-expanded={open} aria-haspopup="true" onClick={() => setOpen(o => !o)}>{summary} ▾</button>
      {open && (
        <div className="absolute left-0 top-[calc(100%+6px)] z-20 min-w-[240px] rounded-panel border border-line bg-panel p-3"
          style={{ boxShadow: '0 20px 48px -14px rgba(0,0,0,.6)' }}>
          {variants.length > 1 && (
            <>
              <div className="mb-[7px] text-micro font-semibold uppercase tracking-[.08em] text-faint">Release</div>
              <div className="mb-3 flex flex-wrap gap-1.5">
                {variants.map((v, i) => (
                  <button key={v.releaseMbid || i} className="sm !rounded-pill" style={chipStyle(i === variantIdx)} onClick={() => onPick(i, 0)}>
                    {variantLabel(v, i)}<span className="opacity-70"> · {v.trackCount}</span>
                  </button>
                ))}
              </div>
            </>
          )}
          <div className="mb-[7px] text-micro font-semibold uppercase tracking-[.08em] text-faint">Edition</div>
          <div className="flex flex-col gap-1.5">
            {editions.map((e, i) => (
              <button key={e.releaseMbid} className="px-3 text-left" style={chipStyle(i === editionIdx)}
                onClick={() => { onPick(variantIdx, i); setOpen(false) }}>
                <div className="font-semibold">{e.label}</div>
                <div className="text-micro font-normal opacity-75">
                  {[e.format !== e.label ? e.format : null, e.year, e.country].filter(Boolean).join(' · ') || 'no pressing details'}
                </div>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// One album per similar artist, all already in the library.
function SimilarAlbums({ artistMbid, artistName, rgid }) {
  const [data, setData] = useState(null)
  useEffect(() => {
    if (!artistMbid && !artistName) return
    let dead = false
    setData(null)
    const q = new URLSearchParams({ artist_name: artistName || '', rgid })
    if (artistMbid) q.set('artist_mbid', artistMbid)
    api('/api/album/similar?' + q).then(r => !dead && setData(r)).catch(() => !dead && setData({ albums: [] }))
    return () => { dead = true }
  }, [artistMbid, artistName, rgid])
  if (!data?.albums?.length) return null
  return (
    <div className="mb-6">
      <SectionHeader label="Similar albums you have" sub={(data.sources || []).join(' + ')} />
      <div className="tile-grid">
        {data.albums.map(a => (
          <button key={`${a.artistId}-${a.rgid}`} className="!block w-full !border-0 !bg-transparent !p-0 text-left"
            title={`In your library — similar to ${a.because}`} onClick={() => goArtist(a.artistId, a.rgid)}>
            <Cover url={a.coverUrl} name={a.title} fluid />
            <div className="mt-2 truncate text-small font-semibold">{a.title}</div>
            <div className="truncate text-caption text-muted">{a.artist}</div>
          </button>
        ))}
      </div>
    </div>
  )
}

function AlbumDetail({ artist, rgid, autoPick = false }) {
  const { action, pushToast } = useApp()
  // autoScan:false — an album page never starts a whole-artist MusicBrainz walk.
  const { disco, indexed, error: discoError, refreshIndex } = useDiscography(artist, { autoScan: false })
  const [variants, setVariants] = useState(null)
  const [groupTitle, setGroupTitle] = useState('')
  const [releaseArtist, setReleaseArtist] = useState('')
  const [groupMeta, setGroupMeta] = useState({ primaryType: '', year: '' })
  const [addedRelease, setAddedRelease] = useState(null)
  const [sel, setSel] = useState({ variant: 0, edition: 0 })
  const [tracks, setTracks] = useState(null)
  const [requested, setRequested] = useState(null)   // release mbid of our request
  const [downloading, setDownloading] = useState(false)
  const [fillingGaps, setFillingGaps] = useState(false)
  const [wishlisted, setWishlisted] = useState(false)
  const [pickOpen, setPickOpen] = useState(autoPick)
  const [indexAdd, setIndexAdd] = useState(null)
  const [addError, setAddError] = useState('')
  const [addNonce, setAddNonce] = useState(0)
  const addedRef = useRef('')
  const meta = useMeta(`/api/meta/album?rgid=${encodeURIComponent(rgid)}`)

  useEffect(() => { if (autoPick) setPickOpen(true) }, [autoPick, rgid])
  useEffect(() => {
    setAddedRelease(null); setIndexAdd(null); setAddError(''); setRequested(null)
    setDownloading(false); setFillingGaps(false); setWishlisted(false)
  }, [rgid])

  const indexedRelease = (disco?.releases || []).find(r => r.rgid === rgid)
  // Three sources, best first; an unindexed release the library demonstrably
  // does not list is `missing`.
  const release = indexedRelease || addedRelease
    || (indexAdd === 'failed' ? { rgid, title: groupTitle, status: 'missing' } : null)
  const status = release?.status
  const headerTitle = release?.title || groupTitle
  const displayArtist = artist.external ? (releaseArtist || artist.name) : artist.name

  // A release the artist's stored index predates (anything from Fresh): add
  // the one row rather than rescanning the whole artist.
  useEffect(() => {
    if (indexed === null || indexedRelease || addedRelease || variants === null) return
    if (addedRef.current === rgid) return
    addedRef.current = rgid
    let dead = false
    setIndexAdd('adding')
    setAddError('')
    action('/api/artist/release', {
      rgid,
      mbid: artist.mbid || '',
      nd_id: artist.id || '',
      title: groupTitle || '',
      artist: releaseArtist || '',
      type: groupMeta.primaryType || '',
      year: groupMeta.year || '',
      name: releaseArtist || (artist.external ? '' : artist.name || ''),
      external: !!artist.external,
    })
      .then(r => {
        if (dead) return
        setIndexAdd(null)
        if (r?.release) setAddedRelease(r.release)
        refreshIndex()
      })
      .catch(e => {
        if (dead) return
        setIndexAdd('failed')
        setAddError(e.message)
        addedRef.current = ''
      })
    return () => { dead = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [indexed, indexedRelease, addedRelease, rgid, releaseArtist, variants, addNonce])

  const retryIndexAdd = () => { addedRef.current = ''; setIndexAdd(null); setAddError(''); setAddNonce(n => n + 1) }

  useEffect(() => {
    let dead = false
    setVariants(null); setGroupTitle(''); setReleaseArtist(''); setGroupMeta({ primaryType: '', year: '' })
    setSel({ variant: 0, edition: 0 })
    api('/api/album/releases?rgid=' + encodeURIComponent(rgid), { timeoutMs: LOOKUP_TIMEOUT_MS })
      .then(r => {
        if (dead) return
        setVariants(r.releases || [])
        setGroupTitle(r.title || '')
        setReleaseArtist(r.artist && r.artist !== '?' ? r.artist : '')
        setGroupMeta({ primaryType: r.primaryType || '', year: r.year || '' })
      })
      .catch(e => {
        if (dead) return
        // [] not null: null means "still asking", and the index add waits on it.
        setVariants([])
        pushToast(`Release lookup failed: ${e.message}`, 'error')
      })
    return () => { dead = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rgid])

  const albumIds = (release?.navidrome_album_ids || []).join(',')
  const groupId = release?.group_id || ''
  const variant = variants?.[sel.variant]
  const edition = variant?.editions?.[sel.edition]
  const trackReleaseMbid = variant?.releaseMbid || ''
  useEffect(() => {
    if (variants === null) { setTracks(null); return }
    if (!trackReleaseMbid) { setTracks({ rows: [], presenceKnown: false, reason: 'no-release' }); return }
    let dead = false
    setTracks(null)
    const q = new URLSearchParams({ release_mbid: trackReleaseMbid })
    if (albumIds) q.set('album_ids', albumIds)
    else if (groupId) q.set('group_id', groupId)
    api('/api/album/tracklist?' + q, { timeoutMs: LOOKUP_TIMEOUT_MS })
      .then(r => !dead && setTracks({ rows: r.tracks || [], presenceKnown: !!r.presenceKnown }))
      .catch(e => !dead && setTracks({ rows: [], presenceKnown: false, reason: e.timeout ? 'timeout' : 'error', error: e.message }))
    return () => { dead = true }
  }, [variants, trackReleaseMbid, albumIds, groupId])

  async function downloadBest() {
    setDownloading(true)
    try {
      const r = await action('/api/album/download', { rgid, ...releaseOverride(release, variant, displayArtist) })
      setRequested(r?.resolved?.release_mbid || r?.status?.releaseMbid || variant?.releaseMbid || '')
    } catch (e) {
      setDownloading(false)
      pushToast(`Download failed: ${e.message}`, 'error')
    }
  }

  async function fillGaps() {
    setFillingGaps(true)
    try {
      await action(`/api/gaps/${release.group_id}/auto`)
      pushToast(`Looking for the missing tracks of ${release.title}`)
    } catch (e) {
      setFillingGaps(false)
      pushToast(`Could not start the fill: ${e.message}`, 'error')
    }
  }

  async function addToWishlist() {
    try {
      await post('/api/wishlist', { rgid, artist: displayArtist, title: headerTitle })
      setWishlisted(true)
      pushToast(`${headerTitle} is on the wishlist`)
    } catch (e) {
      pushToast(`Could not add to the wishlist: ${e.message}`, 'error')
    }
  }

  const trackCount = variant?.trackCount || 0
  const missingCount = Math.max(0, (release?.total || 0) - (release?.present || 0))
  const rgCover = caaCover(rgid)
  const heroMeta = {
    missing: 'not in your library yet',
    complete: 'in your library, complete',
    untagged: 'in your library, without MusicBrainz tags',
    incomplete: 'in your library, with gaps',
  }[status] || ''

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-2.5">
        <button onClick={() => goArtist(artist.id)}>← {displayArtist || 'Discography'}</button>
      </div>

      <div className="hero mb-5 flex items-start gap-[22px] rounded-panel border border-line bg-panel p-5">
        <div className="shrink-0" style={{ boxShadow: '0 14px 34px -10px rgba(0,0,0,.65)', borderRadius: 12 }}>
          <div style={status === 'missing' ? { opacity: 0.75 } : undefined}>
            <Cover url={edition?.coverUrl || rgCover} fallbackUrl={rgCover} name={headerTitle} size={132} />
          </div>
        </div>
        <div className="min-w-0 flex-1">
          <button className="!border-0 !bg-transparent !p-0 text-caption font-semibold uppercase tracking-[.12em] hover:underline"
            style={{ color: 'var(--accent)' }} onClick={() => goArtist(artist.id)}>{displayArtist}</button>
          {!headerTitle ? (
            <><Skeleton className="mb-2 mt-1.5 h-6 w-3/5" /><Skeleton className="h-3 w-2/5" /></>
          ) : (
            <>
              <div className="mb-1.5 mt-0.5 flex flex-wrap items-center gap-2.5">
                <h1 className="text-heading font-bold">{headerTitle}</h1>
                <EditionSwitcher variants={variants} variantIdx={sel.variant} editionIdx={sel.edition}
                  onPick={(v, e) => setSel({ variant: v, edition: e })} />
                {status && <AlbumStateChip state={status} count={status === 'incomplete' ? missingCount : null} />}
              </div>
              <div className="mb-3.5 text-caption text-muted">
                {[release?.year || groupMeta.year, variant ? `${variant.trackCount} tracks` : null, heroMeta].filter(Boolean).join(' · ')}
              </div>
            </>
          )}

          {(addError || (discoError && !release)) && (
            <div className="mb-3 text-caption text-muted">
              {addError ? `Could not add this release to the index: ${addError}` : `Could not read this artist's index: ${discoError}`}
              {' '}<button className="link-inline" onClick={retryIndexAdd}>Try again</button>
            </div>
          )}

          {status === 'missing' && (
            <>
              <div className="flex flex-wrap items-center gap-2.5">
                <button className="primary" onClick={() => setPickOpen(o => !o)}>
                  {pickOpen ? 'Hide sources' : 'Find sources on Soulseek →'}
                </button>
                <button disabled={downloading} onClick={downloadBest}
                  title="Let lb-bot pick the best-ranked source and download it">
                  {downloading ? '✓ Requested' : 'Get the best source'}
                </button>
                <button className="tint" disabled={wishlisted} onClick={addToWishlist}
                  title="Nobody sharing it? lb-bot re-searches the wishlist every few hours">
                  {wishlisted ? '✓ On the wishlist' : 'Add to wishlist'}
                </button>
              </div>
              {requested && <RequestStatus releaseMbid={requested} />}
            </>
          )}
          {status === 'incomplete' && release.group_id && (
            <div className="flex flex-wrap items-center gap-2.5">
              <button className="primary" disabled={fillingGaps} onClick={fillGaps}>
                {fillingGaps ? '✓ Requested — see Downloads' : `Get ${missingCount} missing track${missingCount === 1 ? '' : 's'} →`}
              </button>
              <button onClick={() => navigate('Fill gaps', release.group_id)}>Choose the source yourself →</button>
            </div>
          )}
          {status === 'untagged' && (
            <p className="text-small text-muted">
              lb-bot can't check this album against MusicBrainz because its files carry no MusicBrainz tags. Tag them
              (MusicBrainz Picard does it), then rescan the discography.
            </p>
          )}
          {status === 'complete' && <p className="text-small text-muted">Nothing to do here.</p>}
        </div>
      </div>

      {status === 'missing' && release && (
        <AlbumSourcePicker rgid={rgid} open={pickOpen}
          override={releaseOverride(release, variant, displayArtist)}
          onDownloaded={r => { setDownloading(true); setRequested(r?.resolved?.release_mbid || variant?.releaseMbid || '') }} />
      )}

      <div className="mt-6"><About meta={meta} /></div>

      <SectionHeader label="Tracklist" sub={variant ? `${variant.trackCount} tracks${variant.year ? ` · ${variant.year}` : ''}` : ''} />
      <div className="mb-6">
        {tracks && !tracks.rows.length
          ? <EmptyState title="No tracklist available"
              hint={{
                timeout: 'MusicBrainz did not answer within 10 seconds. The tracklist is only a reference — finding sources and downloading still work.',
                error: `Could not load the tracklist${tracks.error ? `: ${tracks.error}` : ''}. Finding sources and downloading still work.`,
                'no-release': 'MusicBrainz lists no release for this album, so there is no tracklist to show. You can still search Soulseek for it.',
              }[tracks.reason] || 'MusicBrainz has no track data for this edition.'} />
          : <TrackList tracks={tracks?.rows} loading={tracks === null} presenceKnown={!!tracks?.presenceKnown}
              rows={Math.max(4, Math.min(trackCount || 8, 14))} />}
      </div>

      <SimilarAlbums artistMbid={artist.mbid || ''} artistName={displayArtist} rgid={rgid} />
    </>
  )
}

// An album known only by its release-group: ask MusicBrainz who made it, then
// replace this route with the real album page.
function AlbumResolver({ rgid, pick }) {
  const [artists] = useArtists()
  const [failed, setFailed] = useState('')
  useEffect(() => {
    if (artists === null) return
    let dead = false
    api('/api/album/releases?rgid=' + encodeURIComponent(rgid), { timeoutMs: 15000 })
      .then(r => {
        if (dead) return
        // Owned by mbid, else by an unambiguous name; otherwise the `mb:` page
        // (which redirects to the owned page if the library turns out to have
        // that mbid after all).
        const owned = ownedArtistFor(artists, r.artistMbid, r.artist)
        const id = owned ? owned.id : (r.artistMbid ? `mb:${r.artistMbid}` : '')
        if (!id) { setFailed('MusicBrainz names no artist for this album.'); return }
        replaceRoute('Library', 'artists', id, rgid, pick ? 'sources' : undefined)
      })
      .catch(e => !dead && setFailed(e.message))
    return () => { dead = true }
  }, [artists, rgid, pick])
  if (failed) {
    return (
      <EmptyState title="Couldn't open this album" hint={failed}>
        <button onClick={() => history.back()}>← Back</button>
      </EmptyState>
    )
  }
  return <p className="text-caption text-muted">Finding the album…</p>
}

// ── Root ─────────────────────────────────────────────────────────────────────
// Synthetic artists for `mb:<mbid>` routes, remembered for the session so the
// display name upgrades once a scan or the meta lookup names them.
const externalArtists = new Map()

export default function Artist({ params }) {
  const [artistId, rgid, pickParam] = params
  const pick = pickParam === 'sources'
  const [artists, artistsErr] = useArtists()

  // An `mb:` route for an artist the library has (a search row, a pasted link,
  // a similar-artists row, a credit spelled differently from the tag): open
  // the owned page. On the `mb:` one, Rescan ran an external scan over the
  // owned artist's own index row.
  const ownedForMb = useMemo(() => {
    if (!artistId?.startsWith('mb:') || !artists) return null
    const mbid = artistId.slice(3)
    const hits = artists.filter(a => a.mbid === mbid)
    return hits.length === 1 ? hits[0] : null
  }, [artists, artistId])
  useEffect(() => {
    if (ownedForMb) replaceRoute('Library', 'artists', ownedForMb.id, rgid, pickParam)
  }, [ownedForMb, rgid, pickParam])

  const artist = useMemo(() => {
    if (!artistId || artistId === '-') return null
    const owned = (artists || []).find(a => a.id === artistId)
    if (owned) return owned
    const scannedName = discoCache.get(artistId)?.artist_name
    if (artistId.startsWith('mb:')) {
      const prev = externalArtists.get(artistId)
      const mbid = artistId.slice(3)
      const syn = { id: artistId, mbid, name: scannedName || prev?.name || '', external: true }
      externalArtists.set(artistId, syn)
      return syn
    }
    return null
  }, [artists, artistId])

  if (!artistId) return <ArtistIndex />
  if (artistId === '-' && rgid) return <AlbumResolver rgid={rgid} pick={pick} />
  if (artists === null || ownedForMb) return <SkeletonTileGrid />
  if (!artist) {
    return (
      <EmptyState title="Artist not found" hint={artistsErr || "This link doesn't match an artist in your library anymore."}>
        <button className="primary" onClick={() => navigate('Library', 'artists')}>← All artists</button>
      </EmptyState>
    )
  }
  if (rgid) return <AlbumDetail key={`${artist.id}-${rgid}`} artist={artist} rgid={rgid} autoPick={pick} />
  return <DiscographyView key={artist.id} artist={artist} />
}
