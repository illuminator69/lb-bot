import { useEffect, useMemo, useRef, useState } from 'react'
import { useApp, navigate } from '../App.jsx'
import { api } from '../lib/api.js'
import Cover from '../components/Cover.jsx'
import {
  AlbumStateChip, ArtistTile, Chip, EmptyState, PageTitle, ProgressBar,
  ReleaseTile, Skeleton, SortToggle, SourceRow, TrackList,
} from '../components/ui.jsx'

// ── Routing ──────────────────────────────────────────────────────────────────
// This panel has no parser of its own — App is the only router. It reads
// state.routeParams for the route it was mounted by:
//   #/artist                      → ArtistIndex
//   #/artist/<artistId>           → DiscographyView
//   #/artist/<artistId>/<rgid>    → AlbumDetail

// Hand an album off to the Fill gaps screen. navigate() pushes a history entry,
// so Back comes back here.
function openGaps(groupId) {
  navigate('Fill gaps', groupId)
}

// ── Discography data ─────────────────────────────────────────────────────────
// Scan results keyed by artist id, session-lived, so index ↔ discography ↔
// album navigation doesn't re-run the (minutes-long, MusicBrainz-bound) scan.
const discoCache = new Map()

// Artists found via MusicBrainz search that aren't in the library, keyed by
// their `mb:<mbid>` route id, so the root can resolve them for full browse.
const externalArtists = new Map()

// Runs (or reuses) the discography scan for one artist. The scan needs a
// MusicBrainz artist mbid; Navidrome supplies one for tagged libraries, and
// we silently resolve the top text-search hit otherwise.
function useDiscography(artist) {
  const { action } = useApp()
  const [disco, setDisco] = useState(() => (artist ? discoCache.get(artist.id) || null : null))
  const [progress, setProgress] = useState(null) // { done, total, current }
  const [error, setError] = useState(null)
  const [nonce, setNonce] = useState(0)
  const pollRef = useRef(null)
  const forceScanRef = useRef(false)

  const artistId = artist?.id
  useEffect(() => {
    if (!artistId) return
    const cached = discoCache.get(artistId)
    setDisco(cached || null)
    setError(null)
    setProgress(null)
    if (cached) return
    let dead = false
    const forceScan = forceScanRef.current
    forceScanRef.current = false
    async function start() {
      try {
        // Index-first: the server keeps a persistent per-artist index, so a
        // previously scanned artist renders instantly. Rescan bypasses it.
        if (!forceScan) {
          const idx = await api('/api/artist/discography'
            + `?mbid=${encodeURIComponent(artist.mbid || '')}`
            + `&nd_id=${encodeURIComponent(artistId)}`)
          if (dead) return
          if (idx.indexed) {
            discoCache.set(artistId, idx)
            setDisco(idx)
            return
          }
        }
        let mbid = artist.mbid
        if (!mbid) {
          const r = await api('/api/artist/lookup?q=' + encodeURIComponent(artist.name))
          mbid = (r.candidates || [])[0]?.mbid
          if (!mbid) throw new Error(`No MusicBrainz match for “${artist.name}”`)
        }
        const r = await action('/api/artist/discography',
          { mbid, name: artist.name, nd_id: artistId })
        if (dead) return
        // A discography scan is MusicBrainz-bound and can run for minutes, so
        // the 2s poll backs off toward 15s rather than hammering the whole time.
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
            if (t.status === 'error') {
              setError(t.error || 'Discography scan failed')
              return
            }
            setProgress({ done: t.done, total: t.total, current: t.current })
          } catch { /* poll again */ }
          if (dead) return
          delay = Math.min(delay * 1.4, 15000)
          pollRef.current = setTimeout(tick, delay)
        }
        pollRef.current = setTimeout(tick, delay)
      } catch (e) {
        if (!dead) setError(e.message)
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
  return { disco, progress, error, rescan }
}

// ── Artist index ─────────────────────────────────────────────────────────────
const SORTS = [['az', 'A–Z'], ['plays', 'Most listened']]

// Bulk index build: kick the background task and surface its progress. The
// task is resumable — fresh artists are skipped, so re-running after a
// cancel/crash fast-forwards.
function IndexBuildControl() {
  const { action, pushToast } = useApp()
  const [status, setStatus] = useState(null)
  const [cancelling, setCancelling] = useState(false)

  // Self-scheduling rather than an interval keyed on render state: the cadence
  // follows the value just fetched, so the poll can't tear down and restart on
  // every status object, and can't keep running at 5s through a live build.
  useEffect(() => {
    let dead = false
    let timer = null
    const load = async () => {
      let building = false
      try {
        const s = await api('/api/library-index/status')
        if (dead) return
        building = !!s.building
        setStatus(s)
        if (!building) setCancelling(false)
      } catch { /* poll again */ }
      if (!dead) timer = setTimeout(load, building ? 2000 : 5000)
    }
    load()
    return () => { dead = true; if (timer) clearTimeout(timer) }
  }, [])

  async function cancelBuild(id) {
    if (!id) return
    setCancelling(true)
    try {
      await action(`/api/tasks/${id}/cancel`)
      pushToast('Index build cancelling…')
    } catch (e) {
      setCancelling(false)
      pushToast(`Cancel failed: ${e.message}`, 'error')
    }
  }

  if (!status) return null
  if (status.building) {
    const t = status.task || {}
    const pct = t.total ? (t.done / t.total) * 100 : 3
    return (
      <span className="flex min-w-0 items-center gap-2.5">
        <span className="flex min-w-0 items-center gap-2 text-[12px] text-faint">
          <span className="inline-block h-1.5 w-1.5 shrink-0 rounded-pill" style={{ background: 'var(--green)' }} />
          <span className="truncate">
            indexing {t.done ?? 0}/{t.total ?? '?'}{t.current ? ` · ${t.current}` : ''}
          </span>
        </span>
        <span className="w-[110px] shrink-0"><ProgressBar value={pct} /></span>
        <button className="!py-1 !text-[12px]" disabled={cancelling || !t.id}
          onClick={() => cancelBuild(t.id)}>
          {cancelling ? 'Cancelling…' : 'Cancel'}
        </button>
      </span>
    )
  }
  const complete = status.artistsTotal > 0 && status.artistsIndexed >= status.artistsTotal
  return (
    <span className="flex items-center gap-2">
      <span className="text-[12px] text-faint">
        {status.artistsIndexed}/{status.artistsTotal} indexed
        {status.artistsStale ? ` · ${status.artistsStale} stale` : ''}
      </span>
      {(!complete || status.artistsStale > 0) && (
        <button onClick={async () => {
          try {
            await action('/api/library-index/build')
            setStatus(s => ({ ...s, building: true, task: null }))
            pushToast('Index build started — it runs in the background at MusicBrainz pace')
          } catch (e) {
            pushToast(`Index build failed to start: ${e.message}`, 'error')
          }
        }}>
          {complete ? 'Refresh index' : 'Build full index'}
        </button>
      )}
    </span>
  )
}

function SkeletonTileGrid({ tiles = 12, round = false }) {
  return (
    <div className="tile-grid">
      {Array.from({ length: tiles }, (_, i) => (
        <div key={i} className="rounded-[12px] border border-line bg-panel p-2.5">
          <Skeleton className={round ? '!rounded-pill' : ''} style={{ width: '100%', aspectRatio: '1 / 1' }} />
          <Skeleton className="mt-2 h-3" style={{ width: `${70 - (i % 3) * 15}%` }} />
          <Skeleton className="mt-1.5 h-2.5 w-1/3" />
        </div>
      ))}
    </div>
  )
}

function ArtistIndex({ artists, error, onPick }) {
  const [sort, setSort] = useState('az')
  const [q, setQ] = useState('')
  // MusicBrainz artist search for artists you don't own yet.
  const [mb, setMb] = useState({ q: '', loading: false, results: null })
  const hasPlays = (artists || []).some(a => a.plays != null)

  const needle = q.trim()
  async function searchMb() {
    if (!needle) return
    setMb({ q: needle, loading: true, results: null })
    try {
      const r = await api('/api/artist/lookup?q=' + encodeURIComponent(needle))
      setMb({ q: needle, loading: false, results: r.candidates || [] })
    } catch {
      setMb({ q: needle, loading: false, results: [] })
    }
  }
  const mbFresh = mb.q === needle
  const ownedNames = useMemo(
    () => new Set((artists || []).map(a => (a.name || '').toLowerCase())),
    [artists])

  const shown = useMemo(() => {
    let list = artists || []
    const needle = q.trim().toLowerCase()
    if (needle) list = list.filter(a => (a.name || '').toLowerCase().includes(needle))
    list = [...list]
    if (sort === 'plays' && hasPlays) {
      list.sort((a, b) => (b.plays || 0) - (a.plays || 0) || a.name.localeCompare(b.name))
    } else {
      list.sort((a, b) => a.name.localeCompare(b.name))
    }
    return list
  }, [artists, q, sort, hasPlays])

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-2.5">
        <input placeholder="Filter or search for an artist…" className="w-60" value={q}
          onChange={e => setQ(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') searchMb() }} />
        {hasPlays && <SortToggle value={sort} options={SORTS} onChange={setSort} />}
        <span className="spacer" />
        <IndexBuildControl />
      </div>

      {artists === null ? (
        <SkeletonTileGrid round />
      ) : error ? (
        <EmptyState title="Couldn't load your artists" hint={error} />
      ) : !artists.length ? (
        <EmptyState title="No artists in your library yet"
          hint="Once Navidrome has scanned some music, every artist shows up here for browsing." />
      ) : !shown.length ? (
        <EmptyState title={`No artist matches “${needle}”`}
          hint="Nothing in your library — search MusicBrainz below to browse an artist you don't own yet." />
      ) : (
        <div className="tile-grid">
          {shown.map(a => (
            <ArtistTile key={a.id} name={a.name} releaseCount={a.releaseCount}
              plays={a.plays} coverUrl={a.coverUrl} onClick={() => onPick(a)} />
          ))}
        </div>
      )}

      {needle && artists !== null && (
        <div className="mt-7">
          <SectionRule label="Not in your library" sub="MusicBrainz" />
          {!mbFresh || mb.results === null ? (
            <button className="primary" disabled={mb.loading} onClick={searchMb}>
              {mb.loading ? 'Searching…' : `Search MusicBrainz for “${needle}”`}
            </button>
          ) : !mb.results.length ? (
            <EmptyState title={`No MusicBrainz artist for “${needle}”`}
              hint="Try a different spelling." />
          ) : (
            <div className="tile-grid">
              {mb.results
                .filter(c => c.mbid && !ownedNames.has((c.name || '').toLowerCase()))
                .map(c => (
                  <button key={c.mbid}
                    className="rounded-[12px] border border-line bg-panel p-3 text-left hover:border-[var(--accent-bd)]"
                    onClick={() => onPick({ id: 'mb:' + c.mbid, name: c.name, mbid: c.mbid, external: true })}>
                    <div className="truncate text-[14px] font-semibold">{c.name}</div>
                    <div className="muted mt-0.5 truncate text-[12px]">
                      {[c.disambiguation, c.type, c.area || c.country].filter(Boolean).join(' · ') || 'artist'}
                    </div>
                  </button>
                ))}
            </div>
          )}
        </div>
      )}
    </>
  )
}

// ── Discography ──────────────────────────────────────────────────────────────
// Sections follow the server's `effective_type`, which already resolves a
// release's MusicBrainz secondary types (a greatest-hits set reads
// "compilation", not "album"). Anything unrecognised falls into Other rather
// than being folded into Albums, so a new MusicBrainz type can't hide.
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

function SectionRule({ label, sub }) {
  return (
    <div className="mb-[11px] mt-5 flex items-center gap-2.5 first:mt-0">
      <div className="text-[13px] font-semibold uppercase tracking-[.06em]" style={{ color: 'var(--text2)' }}>{label}</div>
      <span className="h-px flex-1" style={{ background: 'var(--border)' }} />
      {sub && <span className="text-[11.5px] text-faint">{sub}</span>}
    </div>
  )
}

function DiscographyView({ artist, onOpenAlbum, onBack }) {
  const { disco, progress, error, rescan } = useDiscography(artist)
  const [filter, setFilter] = useState('all')

  const releases = disco?.releases || []
  const counts = useMemo(() => {
    const c = { all: releases.length }
    for (const r of releases) c[r.status] = (c[r.status] || 0) + 1
    return c
  }, [releases])
  const shown = filter === 'all' ? releases : releases.filter(r => r.status === filter)

  // Every tile opens the album's own detail page, including the ones with
  // gaps. Sending "2 missing" straight to Fill gaps hijacked the global queue
  // cursor: you clicked one album in a discography and landed on a different
  // screen pointed at a different album. The detail page keeps the Fill-gaps
  // deep link as an explicit action instead.
  const openRelease = (r) => onOpenAlbum(r.rgid)

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-2.5">
        <button onClick={onBack}>← All artists</button>
        <span className="spacer" />
        {disco?.scanned_at ? (
          <span className="text-[12px] text-faint">
            scanned {relTime(disco.scanned_at)}
            {disco.stale ? ' · ' : ''}
            {disco.stale && <span style={{ color: 'var(--decide-fg)' }}>may be out of date</span>}
          </span>
        ) : null}
        {disco && <button onClick={rescan}>Rescan discography</button>}
      </div>

      {error ? (
        <EmptyState title="Discography scan failed" hint={error}>
          <button className="primary" onClick={rescan}>Try again</button>
        </EmptyState>
      ) : !disco ? (
        <>
          <div className="mb-4 flex flex-wrap items-center gap-3 rounded-[12px] border border-line bg-panel px-4 py-3">
            <div className="min-w-[180px] flex-1">
              <ProgressBar value={progress?.total ? (progress.done / progress.total) * 100 : 4} />
            </div>
            <span className="text-[12.5px]" style={{ color: 'var(--text2)' }}>
              {progress?.total
                ? `Checking ${progress.done}/${progress.total}`
                : 'Pulling discography from MusicBrainz…'}
            </span>
            {progress?.current && (
              <span className="min-w-0 truncate text-[12px] text-faint">{progress.current}</span>
            )}
          </div>
          <SkeletonTileGrid />
        </>
      ) : (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-2">
            {STATUS_FILTERS.map(([k, label]) => (
              <Chip key={k} active={filter === k} count={counts[k] || 0}
                onClick={() => setFilter(k)}>{label}</Chip>
            ))}
          </div>
          {!shown.length ? (
            <EmptyState title="No releases match this filter" />
          ) : (
            TYPE_GROUPS.map(([type, label]) => {
              const group = shown.filter(r => groupKey(r) === type)
              if (!group.length) return null
              return (
                <div key={type || 'other'}>
                  <SectionRule label={label} sub={`${group.length}`} />
                  <div className="tile-grid">
                    {group.map(r => (
                      <ReleaseTile key={r.rgid} title={r.title} year={r.year}
                        state={r.status}
                        count={r.status === 'incomplete' ? (r.total || 0) - (r.present || 0) : null}
                        coverUrl={`https://coverartarchive.org/release-group/${r.rgid}/front-250`}
                        onClick={() => openRelease(r)} />
                    ))}
                  </div>
                </div>
              )
            })
          )}
        </>
      )}
    </>
  )
}

// ── Album detail ─────────────────────────────────────────────────────────────
// Manual source panel, toggled from the hero's "Pick a source manually" button
// (kept beside "Get this album" so both paths sit together). Mirrors the
// Fill-gaps flow: "Get this album" is the zero-friction auto path (server picks
// the best-ranked peer); this is the deliberate override — it runs the slskd
// search, ranks folders, and downloads the exact one you choose through the same
// album pipeline (so failover to alt_sources still applies).
function AlbumSourcePicker({ rgid, open, onDownloaded }) {
  const { action, pushToast } = useApp()
  const [sources, setSources] = useState(null)   // null=loading, []=none
  const [chosen, setChosen] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!open) return
    let dead = false
    setSources(null)
    api('/api/album/sources?rgid=' + encodeURIComponent(rgid))
      .then(r => !dead && setSources(r.sources || []))
      .catch(e => { if (!dead) { setSources([]); pushToast(`Source search failed: ${e.message}`, 'error') } })
    return () => { dead = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, rgid])

  async function useSource(src) {
    setBusy(true)
    setChosen(src.id)
    try {
      await action('/api/album/download',
        { rgid, sourceUsername: src.peer, sourceFolder: src.folder })
      pushToast('Queued from @' + src.peer + ' — see Downloads')
      onDownloaded?.()
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
      <SectionRule label="Available on slskd" sub="ranked by your source preferences" />
      {sources === null ? (
        <div className="flex flex-col gap-2">
          {Array.from({ length: 3 }, (_, i) => (
            <div key={i} className="flex items-center gap-3.5 rounded-[12px] border border-line p-3.5"
              style={{ background: 'var(--inset-warm)' }}>
              <Skeleton className="h-11 w-11 !rounded-[9px]" />
              <div className="flex-1">
                <Skeleton className="h-3.5 w-2/5" />
                <Skeleton className="mt-2 h-3 w-3/5" />
              </div>
            </div>
          ))}
          <p className="text-[12px] text-faint">Searching peers — results can take ~10s to trickle in.</p>
        </div>
      ) : !sources.length ? (
        <EmptyState title="No sources found on slskd"
          hint="No peer is sharing this album right now — try again later." />
      ) : (
        sources.map(s => (
          <SourceRow key={s.id} src={s} busy={busy}
            selected={chosen === s.id}
            actionLabel="Use this →"
            done={chosen === s.id && !busy}
            doneLabel="✓ Queued — see Downloads"
            onUse={() => useSource(s)} />
        ))
      )}
    </div>
  )
}

// Release / edition switcher: two axes, because they answer different
// questions. A *release* variant (Original / Remaster / Deluxe) changes the
// tracklist; an *edition* (Digital / CD / Vinyl) is the same tracklist pressed
// differently — and carries its own cover art, which is the point. The
// release-group's art is whichever release the Cover Art Archive picked, so a
// vinyl-tagged copy routinely shows a photo of the disc where the sleeve
// belongs. Digital is the default: it is the likeliest to have a correct front.
function EditionSwitcher({ variants, variantIdx, editionIdx, onPick }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    if (!open) return
    function onDocClick(e) { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    function onEsc(e) { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDocClick)
    document.addEventListener('keydown', onEsc)
    return () => {
      document.removeEventListener('mousedown', onDocClick)
      document.removeEventListener('keydown', onEsc)
    }
  }, [open])

  const variant = variants?.[variantIdx]
  const editions = variant?.editions || []
  if (!variants?.length) return null
  // Nothing to switch between on either axis — a chooser with one option in it
  // is just noise.
  if (variants.length < 2 && editions.length < 2) return null

  const variantLabel = (v, i) =>
    v.disambiguation || (i === 0 ? 'Original' : v.year || `Edition ${i + 1}`)
  const summary = [variantLabel(variant, variantIdx), editions[editionIdx]?.label]
    .filter(Boolean).join(' · ')

  const chipStyle = active => ({
    borderColor: active ? 'var(--accent)' : 'var(--border)',
    background: active ? 'var(--accent-tint)' : 'var(--inset-warm)',
    color: active ? 'var(--accent)' : 'var(--text2)',
    fontWeight: active ? 600 : 400,
  })

  return (
    <div className="relative" ref={ref}>
      <button className="!rounded-pill !py-1 !text-[12px]" aria-expanded={open}
        onClick={() => setOpen(o => !o)}>
        {summary} ▾
      </button>
      {open && (
        <div className="absolute left-0 top-[calc(100%+6px)] z-20 min-w-[240px] rounded-card border border-line bg-panel p-3 shadow-md">
          {variants.length > 1 && (
            <>
              <div className="mb-[7px] text-[11px] font-semibold uppercase tracking-[.08em] text-faint">
                Release
              </div>
              <div className="mb-3 flex flex-wrap gap-1.5">
                {variants.map((v, i) => (
                  <button key={v.releaseMbid || i} className="!rounded-pill !py-1 !text-[12px]"
                    style={chipStyle(i === variantIdx)}
                    onClick={() => onPick(i, 0)}>
                    {variantLabel(v, i)}
                    <span className="opacity-70"> · {v.trackCount}</span>
                  </button>
                ))}
              </div>
            </>
          )}
          <div className="mb-[7px] text-[11px] font-semibold uppercase tracking-[.08em] text-faint">
            Edition
          </div>
          <div className="flex flex-col gap-1.5">
            {editions.map((e, i) => (
              <button key={e.releaseMbid} className="!rounded-[10px] px-3 text-left"
                style={chipStyle(i === editionIdx)}
                onClick={() => { onPick(variantIdx, i); setOpen(false) }}>
                <div className="font-semibold">{e.label}</div>
                <div className="text-[11px] font-normal opacity-75">
                  {[e.format !== e.label ? e.format : null, e.year, e.country]
                    .filter(Boolean).join(' · ') || 'no pressing details'}
                </div>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// "Similar albums": one album per similar artist, all of them already in the
// library. Every entry is attributed to the artist you're looking at — this is
// "because you're on Radiohead", never an unattributed popularity claim. It
// renders nothing at all when the lookup finds no owned, indexed match, since
// an empty shelf teaches the reader nothing.
function SimilarAlbums({ artistMbid, artistName, rgid, onOpen }) {
  const [data, setData] = useState(null)

  useEffect(() => {
    if (!artistMbid && !artistName) return
    let dead = false
    setData(null)
    const q = new URLSearchParams({ artist_name: artistName || '', rgid })
    if (artistMbid) q.set('artist_mbid', artistMbid)
    api('/api/album/similar?' + q)
      .then(r => !dead && setData(r))
      .catch(() => !dead && setData({ albums: [] }))
    return () => { dead = true }
  }, [artistMbid, artistName, rgid])

  if (!data?.albums?.length) return null
  return (
    <div className="mb-6">
      <SectionRule label="Similar albums" sub={(data.sources || []).join(' + ')} />
      <div className="tile-grid">
        {data.albums.map(a => (
          <button key={`${a.artistId}-${a.rgid}`}
            className="!block w-full !border-0 !bg-transparent !p-0 text-left"
            title={`In your library — similar to ${a.because}`}
            onClick={() => onOpen(a.artistId, a.rgid)}>
            <Cover url={a.coverUrl} name={a.title} fluid />
            <div className="mt-2 truncate text-[13.5px] font-semibold">{a.title}</div>
            <div className="muted truncate text-[12px]">{a.artist}</div>
          </button>
        ))}
      </div>
    </div>
  )
}

function AlbumDetail({ artist, rgid, onBack }) {
  const { dispatch, action, pushToast } = useApp()
  const { disco } = useDiscography(artist)
  // Two axes: which release variant (tracklist) and which edition of it
  // (pressing + cover art). See EditionSwitcher.
  const [variants, setVariants] = useState(null)
  const [sel, setSel] = useState({ variant: 0, edition: 0 })
  const [tracks, setTracks] = useState(null)
  const [downloading, setDownloading] = useState(false)
  const [fillingGaps, setFillingGaps] = useState(false)
  const [pickOpen, setPickOpen] = useState(false)

  const release = (disco?.releases || []).find(r => r.rgid === rgid)
  const status = release?.status
  const readOnly = status === 'complete' || status === 'untagged'

  useEffect(() => {
    let dead = false
    setVariants(null)
    setSel({ variant: 0, edition: 0 })
    api('/api/album/releases?rgid=' + encodeURIComponent(rgid))
      .then(r => !dead && setVariants(r.releases || []))
      .catch(e => !dead && pushToast(`Release lookup failed: ${e.message}`, 'error'))
    return () => { dead = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rgid])

  // The tracklist carries per-track presence whenever we can name the
  // Navidrome albums this release resolves to — which is what turns the
  // tracklist from a MusicBrainz reference into an answer to "what am I
  // actually missing".
  const albumIds = (release?.navidrome_album_ids || []).join(',')
  const groupId = release?.group_id || ''
  const variant = variants?.[sel.variant]
  const edition = variant?.editions?.[sel.edition]
  // The tracklist follows the *variant*; editions of one variant share it, so
  // switching Digital → Vinyl must not re-fetch or flash a skeleton.
  const trackReleaseMbid = variant?.releaseMbid || ''
  useEffect(() => {
    if (!trackReleaseMbid) { setTracks(null); return }
    let dead = false
    setTracks(null)
    const q = new URLSearchParams({ release_mbid: trackReleaseMbid })
    if (albumIds) q.set('album_ids', albumIds)
    else if (groupId) q.set('group_id', groupId)
    api('/api/album/tracklist?' + q)
      .then(r => !dead && setTracks({ rows: r.tracks || [], presenceKnown: !!r.presenceKnown }))
      .catch(() => !dead && setTracks({ rows: [], presenceKnown: false }))
    return () => { dead = true }
  }, [trackReleaseMbid, albumIds, groupId])

  async function downloadBest() {
    setDownloading(true)
    try {
      await action('/api/album/download', { rgid })
    } catch (e) {
      // Only a failure un-latches the button — on success it deliberately
      // stays disabled reading "Requested". Resetting outside the try means a
      // throw from anything after the request can't strand it either.
      setDownloading(false)
      pushToast(`Download failed: ${e.message}`, 'error')
      return
    }
    pushToast(`Queued ${release?.title || 'album'} — see Downloads`)
  }

  // Auto-select on the review group: search, rank and download the best
  // source for the missing tracks only.
  async function fillGaps() {
    setFillingGaps(true)
    try {
      await action(`/api/gaps/${release.group_id}/auto`)
    } catch (e) {
      setFillingGaps(false)
      pushToast(`Could not start the fill: ${e.message}`, 'error')
      return
    }
    pushToast(`Looking for the missing tracks of ${release.title} — see Downloads`)
  }

  const trackCount = variant?.trackCount || 0
  const missingCount = Math.max(0, (release?.total || 0) - (release?.present || 0))
  const rgCover = `https://coverartarchive.org/release-group/${rgid}/front-250`

  const heroMeta = {
    missing: 'not in your library yet — pick an edition, then get the album.',
    complete: 'in your library, no gaps — nothing to do here.',
    untagged: 'in your library, but without MusicBrainz tags lb-bot can verify.',
    incomplete: 'in your library with gaps — the tracklist below marks which ones.',
  }[status] || ''

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-2.5">
        <button onClick={onBack}>← Back to discography</button>
      </div>

      <div className="hero mb-4 flex items-start gap-[22px] rounded-[14px] border border-line bg-panel p-5">
        <div className="shrink-0" style={{ boxShadow: '0 14px 34px -10px rgba(0,0,0,.65)', borderRadius: 12 }}>
          <div style={status === 'missing' ? { opacity: 0.75 } : undefined}>
            {/* The chosen edition's own sleeve, with the release-group's as the
                fallback for a pressing the Archive has no art for. */}
            <Cover url={edition?.coverUrl || rgCover} fallbackUrl={rgCover}
              name={release?.title || ''} size={132} />
          </div>
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-[12px] font-semibold uppercase tracking-[.12em]" style={{ color: 'var(--accent)' }}>
            {artist.name}
          </div>
          {!release ? (
            <>
              <Skeleton className="mb-2 mt-1.5 h-6 w-3/5" />
              <Skeleton className="h-3 w-2/5" />
            </>
          ) : (
            <>
              <div className="mb-1.5 mt-0.5 flex flex-wrap items-center gap-2.5">
                <span className="text-[24px] font-bold leading-[1.1]">{release.title}</span>
                <EditionSwitcher variants={variants} variantIdx={sel.variant}
                  editionIdx={sel.edition}
                  onPick={(variant, edition) => setSel({ variant, edition })} />
                <AlbumStateChip state={status}
                  count={status === 'incomplete' ? (release.total || 0) - (release.present || 0) : null} />
              </div>
              <div className="muted mb-3.5 text-[12.5px]">
                {[release.year, variant ? `${variant.trackCount} tracks` : null, heroMeta]
                  .filter(Boolean).join(' · ')}
              </div>
            </>
          )}

          {status === 'missing' && (
            <div className="flex flex-wrap items-center gap-2.5">
              {/* Soulseek first and primary: choosing the source is the real
                  decision here, and the one-tap auto path sits beside it. */}
              <button className="primary" disabled={!variant} onClick={() => setPickOpen(o => !o)}>
                {pickOpen ? 'Hide sources' : 'Find sources on Soulseek →'}
              </button>
              <button disabled={downloading || !variant} onClick={downloadBest}>
                {downloading ? '✓ Requested — see Downloads' : 'Get this album'}
              </button>
            </div>
          )}
          {/* An album with gaps is actionable from here too. The request is
              scoped to the review group, so it fetches only the missing
              tracks — never the whole release over the top of what you own.
              The manual picker is not rebuilt here; it lives in Fill gaps. */}
          {status === 'incomplete' && release.group_id && (
            <div className="flex flex-wrap items-center gap-2.5">
              <button className="primary" disabled={fillingGaps} onClick={fillGaps}>
                {fillingGaps
                  ? '✓ Requested — see Downloads'
                  : `Get ${missingCount} missing track${missingCount === 1 ? '' : 's'} →`}
              </button>
              <button onClick={() => openGaps(release.group_id)}>
                Find sources on Soulseek →
              </button>
            </div>
          )}
          {status === 'untagged' && (
            <button onClick={() => navigate('Import')}>
              File into library →
            </button>
          )}
        </div>
      </div>

      <SimilarAlbums artistMbid={artist.mbid || ''} artistName={artist.name}
        rgid={rgid} onOpen={(artistId, otherRgid) => navigate('Artist', artistId, otherRgid)} />

      <SectionRule label="Tracklist"
        sub={variant ? `${variant.trackCount} tracks${variant.year ? ` · ${variant.year}` : ''}` : ''} />
      {tracks && !tracks.rows.length
        ? <EmptyState title="No tracklist available"
            hint="MusicBrainz has no track data for this edition." />
        : <TrackList tracks={tracks?.rows} loading={tracks === null}
            presenceKnown={!!tracks?.presenceKnown}
            rows={Math.max(4, Math.min(trackCount || 8, 14))} />}

      {status === 'missing' && release && (
        <AlbumSourcePicker rgid={rgid} open={pickOpen} onDownloaded={() => setDownloading(true)} />
      )}
      {readOnly && (
        <p className="muted mt-3">
          Read-only view — this album is already in your library
          {status === 'untagged' ? ', it just needs tags before lb-bot can verify it.' : '.'}
        </p>
      )}
    </>
  )
}

// ── Root ─────────────────────────────────────────────────────────────────────
export default function Artist() {
  const { state } = useApp()
  // Drill-down state derives from the route; App's single hashchange listener
  // keeps it current.
  const route = { artistId: state.routeParams[0] || null,
                  rgid: state.routeParams[1] || null }
  const [artists, setArtists] = useState(null)
  const [artistsErr, setArtistsErr] = useState(null)

  useEffect(() => {
    let dead = false
    api('/api/artists')
      .then(r => !dead && setArtists(Array.isArray(r) ? r : []))
      .catch(e => { if (!dead) { setArtistsErr(e.message); setArtists([]) } })
    return () => { dead = true }
  }, [])

  const nav = (artistId, rgid) => navigate('Artist', artistId, rgid)
  const pick = (a) => { if (a.external) externalArtists.set(a.id, a); nav(a.id) }

  const artist = useMemo(() => {
    const id = route.artistId
    if (!id) return null
    // Owned artists first, then a MusicBrainz-searched external artist. A bare
    // `mb:<mbid>` deep link (refresh) resolves to a synthetic artist so browse
    // still works; DiscographyView upgrades the display name once the scan runs.
    const owned = (artists || []).find(a => a.id === id)
    if (owned) return owned
    // A completed scan carries the real artist name — prefer it over a bare mbid.
    const scannedName = discoCache.get(id)?.artist_name
    if (externalArtists.has(id)) {
      const ext = externalArtists.get(id)
      if (scannedName && ext.name !== scannedName) {
        const upgraded = { ...ext, name: scannedName }
        externalArtists.set(id, upgraded)
        return upgraded
      }
      return ext
    }
    if (id.startsWith('mb:')) {
      const mbid = id.slice(3)
      const syn = { id, mbid, name: scannedName || mbid, external: true }
      externalArtists.set(id, syn)
      return syn
    }
    return null
  }, [artists, route.artistId])

  let body
  if (!route.artistId) {
    body = <ArtistIndex artists={artists} error={artistsErr} onPick={pick} />
  } else if (artists === null) {
    body = <SkeletonTileGrid />
  } else if (!artist) {
    body = (
      <EmptyState title="Artist not found"
        hint="This link doesn't match an artist in your library anymore.">
        <button className="primary" onClick={() => nav()}>← All artists</button>
      </EmptyState>
    )
  } else if (route.rgid) {
    body = <AlbumDetail artist={artist} rgid={route.rgid} onBack={() => nav(artist.id)} />
  } else {
    body = <DiscographyView artist={artist} onOpenAlbum={rgid => nav(artist.id, rgid)}
      onBack={() => nav()} />
  }

  return (
    <>
      <div className="mb-[18px] flex items-end gap-4">
        <PageTitle eyebrow="Artist" title={artist ? artist.name : 'Your artists'} />
      </div>
      {body}
    </>
  )
}
