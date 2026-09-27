import { useEffect, useState } from 'react'
import { useApp, navigate, goArtist, goAlbum, lsGet, lsSet } from '../App.jsx'
import { api } from '../lib/api.js'
import Cover from '../components/Cover.jsx'
import Fresh from './Fresh.jsx'
import { EmptyState, PageTitle, SectionHeader, SelectField, Skeleton, SubTabs } from '../components/ui.jsx'

// Discover is what you don't have yet. New releases is the ListenBrainz feed
// that used to be the Fresh tab; Charts is Deezer's chart and editorial picks,
// which the backend has served to the other clients since 2026-09-23 and the
// web UI never showed.
const SUBS = [['new', 'New releases'], ['charts', 'Charts']]

// A Deezer row has no MusicBrainz ids unless the library index could resolve
// it by name. For the rest, tapping asks MusicBrainz — fielded, because a
// free-text query ranks parodies above the real album. The server builds the
// query (quoted, with a free-text fallback and an exact-title re-rank); built
// here, a '"Heroes"' or a title with an edition suffix found nothing.
async function resolveRgid(row) {
  if (row.rgid) return row.rgid
  const r = await api(`/api/album/lookup?artist=${encodeURIComponent(row.artist || '')}`
    + `&album=${encodeURIComponent(row.title || '')}`)
  return r.candidates?.[0]?.rgid || ''
}

function ChartAlbum({ a }) {
  const { pushToast } = useApp()
  const [busy, setBusy] = useState(false)
  async function open() {
    setBusy(true)
    try {
      const rgid = await resolveRgid(a)
      if (rgid) goAlbum(rgid)
      else pushToast(`MusicBrainz has no album called “${a.title}” by ${a.artist}`, 'info')
    } catch (e) {
      pushToast(`Lookup failed: ${e.message}`, 'error')
    } finally { setBusy(false) }
  }
  return (
    <button onClick={open} disabled={busy} aria-busy={busy}
      className="!flex flex-col !rounded-card border !border-line !bg-panel !p-2.5 text-left">
      <div className="relative mb-2 w-full">
        <Cover url={a.coverUrl || a.imageUrl} name={a.title} fluid />
        {a.position ? (
          <span className="absolute left-1.5 top-1.5 rounded-pill px-2 py-0.5 font-mono text-micro font-semibold"
            style={{ background: 'var(--surface)', color: 'var(--text2)' }}>#{a.position}</span>
        ) : null}
      </div>
      <div className="w-full truncate text-small font-semibold">{a.title}</div>
      <div className="w-full truncate text-micro text-muted">{a.artist}</div>
      <div className="mt-1 flex flex-wrap items-center gap-1.5 text-micro text-faint">
        {a.recordType && <span>{a.recordType}</span>}
        {a.releaseOwned && <span className="chip good !my-0 !py-0">in library</span>}
      </div>
    </button>
  )
}

function Charts() {
  const [genres, setGenres] = useState(null)
  const [genre, setGenreState] = useState(() => lsGet('discover.genre', '0'))
  const setGenre = g => { setGenreState(g); lsSet('discover.genre', g) }
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

  const skeleton = (
    <div className="tile-grid">
      {Array.from({ length: 12 }, (_, i) => (
        <div key={i} className="rounded-card border border-line bg-panel p-2.5">
          <Skeleton style={{ width: '100%', aspectRatio: '1 / 1' }} />
          <Skeleton className="mt-2 h-3 w-3/5" />
        </div>
      ))}
    </div>
  )

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <SelectField label="Genre" value={genre}
          options={(genres?.length ? genres : [{ id: '0', name: 'All' }]).map(g => [String(g.id), g.name])}
          onChange={setGenre} />
        <span className="text-caption text-muted">
          From Deezer. The chart follows the country lb-bot's server is in, not yours — Deezer offers no way to pick one.
        </span>
      </div>
      {error ? <EmptyState title="Deezer didn't answer" hint={error} /> : (
        <>
          <SectionHeader label="Top albums" sub={chart?.albums?.length || ''} />
          {chart === null ? skeleton : !chart.albums?.length
            ? <p className="text-caption text-muted">Nothing charting in this genre.</p>
            : <div className="tile-grid">{chart.albums.map(a => <ChartAlbum key={a.deezerId} a={a} />)}</div>}

          {chart?.artists?.length > 0 && (
            <div className="mt-6">
              <SectionHeader label="Top artists" />
              <div className="flex flex-wrap gap-1.5">
                {chart.artists.map(a => (
                  <button key={a.deezerId} className="sm"
                    style={a.owned ? { borderColor: 'var(--green-bd)' } : undefined}
                    title={a.owned ? 'In your library' : a.mbid ? 'Open on MusicBrainz' : 'Search for this artist'}
                    onClick={() => a.owned && a.artistId ? goArtist(a.artistId)
                      : a.mbid ? goArtist(`mb:${a.mbid}`) : navigate('Search', a.name)}>
                    {a.name}{a.owned ? ' ✓' : ''}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="mt-6">
            <SectionHeader label="Editors' picks" sub={editorial?.albums?.length || ''} />
            {editorial === null ? skeleton : !editorial.albums?.length
              ? <p className="text-caption text-muted">No editorial picks right now.</p>
              : <div className="tile-grid">{editorial.albums.map(a => <ChartAlbum key={a.deezerId} a={a} />)}</div>}
          </div>
        </>
      )}
    </>
  )
}

export default function Discover() {
  const { state } = useApp()
  const sub = SUBS.some(([k]) => k === state.routeParams[0]) ? state.routeParams[0] : 'new'
  return (
    <>
      <div className="mb-4"><PageTitle eyebrow="Discover" title={sub === 'charts' ? 'Charts' : 'New releases'} /></div>
      <SubTabs items={SUBS} value={sub} onChange={k => navigate('Discover', k)} label="Discover sections" />
      {sub === 'new' ? <Fresh /> : <Charts />}
    </>
  )
}
