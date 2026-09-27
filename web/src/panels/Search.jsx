import { useEffect, useState } from 'react'
import { useApp, navigate, replaceRoute, goArtist, goAlbum } from '../App.jsx'
import { api, post } from '../lib/api.js'
import Cover from '../components/Cover.jsx'
import { loadArtists } from './Artist.jsx'
import { EmptyState, Notice, PageTitle, SectionHeader, Skeleton } from '../components/ui.jsx'

// The header search. Three kinds of answer from one box:
//   - a pasted link  → /api/resolve-link (Spotify, Deezer, Apple Music, TIDAL,
//                      Qobuz, YouTube Music, MusicBrainz) → the album/artist page
//   - a Spotify *playlist* link → offer to scan it for missing tracks
//   - text           → your artists by name, plus MusicBrainz artists and albums
// Every result opens an existing page; acquisition happens there, through the
// one album pipeline. The old Library "Add music" box ran a second, legacy slskd
// search next to it and sent pasted links to MusicBrainz as text.

const isLink = q => /^https?:\/\//i.test(q) || /^spotify:/i.test(q)
  || /^(open\.spotify|music\.apple|deezer|tidal|www\.qobuz|music\.youtube|musicbrainz)\./i.test(q)
const isSpotifyPlaylist = q => /spotify\.com\/playlist\/|^spotify:playlist:/i.test(q)

function LinkResult({ q }) {
  const { action, pushToast } = useApp()
  const [r, setR] = useState(null)
  const [err, setErr] = useState('')
  const playlist = isSpotifyPlaylist(q)
  useEffect(() => {
    if (playlist) return
    let dead = false
    setR(null); setErr('')
    post('/api/resolve-link', { url: q }).then(x => !dead && setR(x)).catch(e => !dead && setErr(e.message))
    return () => { dead = true }
  }, [q, playlist])

  if (playlist) {
    return (
      <Notice tone="quiet" title="A Spotify playlist"
        actions={<button className="primary" onClick={async () => {
          try {
            await action('/api/spotify/scan', { playlist: q })
            pushToast('Scanning the playlist — its missing tracks will appear in Fill gaps under “Spotify”')
            navigate('Fill gaps')
          } catch (e) { pushToast(`Scan failed: ${e.message}`, 'error') }
        }}>Scan it for missing tracks</button>}>
        lb-bot can check which of its tracks your library is missing and queue them in Fill gaps.
      </Notice>
    )
  }
  if (err) return <EmptyState title="Couldn't read that link" hint={err} />
  if (!r) return <p className="text-caption text-muted">Working out what that link is…</p>
  if (r.kind === 'unknown' || !r.confidence) {
    return <EmptyState title="Couldn't work out what that link is" hint={r.reason || 'Try searching for the artist and album by name instead.'} />
  }
  const sure = r.confidence >= 0.85
  return <LinkAnswer r={r} sure={sure} />
}

// A link resolved by id (MusicBrainz, or the store's own API) just opens —
// pasting it meant "open this". A page-title match (confidence ~0.7) shows a
// card first, because the name it went on may be the wrong record.
function LinkAnswer({ r, sure }) {
  useEffect(() => {
    if (!sure) return
    if (r.rgid) replaceRoute('Library', 'artists', '-', r.rgid)
    else if (r.kind === 'artist' && r.mbid) replaceRoute('Library', 'artists', `mb:${r.mbid}`)
  }, [r, sure])
  return (
    <div className="workspace-card flex flex-wrap items-center gap-4">
      {r.rgid && <Cover url={`https://coverartarchive.org/release-group/${r.rgid}/front-250`} name={r.title} size={96} />}
      <div className="min-w-[200px] flex-1">
        <div className="text-caption font-semibold uppercase tracking-[.1em] text-muted">{r.kind} · from {r.provider}</div>
        <div className="text-title font-semibold">{r.title || r.artist || (r.kind === 'artist' ? 'An artist' : 'An album')}</div>
        {r.title && r.artist && <div className="text-small text-muted">{r.artist}</div>}
        {!sure && (
          <div className="mt-1 text-caption" style={{ color: 'var(--decide-fg)' }}>
            Matched by name from the store's page, not by an id — check it's the right one.
          </div>
        )}
      </div>
      {r.rgid
        ? <button className="primary" onClick={() => goAlbum(r.rgid)}>Open album →</button>
        : r.kind === 'artist' && r.mbid
          ? <button className="primary" onClick={() => goArtist(`mb:${r.mbid}`)}>Open artist →</button>
          : <button onClick={() => navigate('Search', [r.artist, r.title].filter(Boolean).join(' '))}>Search by name</button>}
    </div>
  )
}

function TextResults({ q }) {
  const [owned, setOwned] = useState(null)
  const [artists, setArtists] = useState(null)
  const [albums, setAlbums] = useState(null)

  useEffect(() => {
    let dead = false
    setOwned(null); setArtists(null); setAlbums(null)
    const m = q.split(/\s+[–—-]\s+/)
    // "Artist – Album" matches your artists on the artist half.
    const needle = (m.length === 2 ? m[0] : q).toLowerCase()
    loadArtists()
      .then(list => !dead && setOwned(list.filter(a => (a.name || '').toLowerCase().includes(needle)).slice(0, 12)))
      .catch(() => !dead && setOwned([]))
    api('/api/artist/lookup?q=' + encodeURIComponent(m.length === 2 ? m[0] : q))
      .then(r => !dead && setArtists(r.candidates || [])).catch(() => !dead && setArtists([]))
    // "Artist – Album" is asked fielded, which MusicBrainz ranks far better
    // than free text (free text puts parodies first). The server quotes it and
    // falls back to free text.
    const url = m.length === 2
      ? `/api/album/lookup?artist=${encodeURIComponent(m[0])}&album=${encodeURIComponent(m[1])}`
      : '/api/album/lookup?q=' + encodeURIComponent(q)
    api(url)
      .then(r => !dead && setAlbums(r.candidates || [])).catch(() => !dead && setAlbums([]))
    return () => { dead = true }
  }, [q])

  // Hide only the MusicBrainz rows that ARE a listed library artist (same
  // mbid). A same-name artist is someone else — the UK Nirvana is not the
  // Seattle one — and must stay reachable. An owned artist without an mbid tag
  // still shows here; the server keeps a scan from its `mb:` page off the
  // owned artist's index row.
  const ownedMbids = new Set((owned || []).map(a => a.mbid).filter(Boolean))
  const others = (artists || []).filter(c => c.mbid && !ownedMbids.has(c.mbid))
  const loading = n => Array.from({ length: n }, (_, i) => <Skeleton key={i} className="h-9 w-40" />)

  return (
    <>
      <SectionHeader label="Your artists" sub={owned?.length || ''} />
      <div className="mb-6 flex flex-wrap gap-1.5">
        {owned === null ? loading(3)
          : !owned.length ? <p className="text-caption text-muted">None of your artists match.</p>
          : owned.map(a => (
            <button key={a.id} className="!flex items-center gap-2 !py-1.5" onClick={() => goArtist(a.id)}>
              <Cover url={a.coverUrl} name={a.name} size={24} round /> {a.name}
            </button>
          ))}
      </div>

      <SectionHeader label="Albums" sub="MusicBrainz" />
      <div className="mb-6">
        {albums === null ? <div className="tile-grid">{Array.from({ length: 6 }, (_, i) => <Skeleton key={i} style={{ aspectRatio: '1 / 1.25' }} />)}</div>
          : !albums.length ? <p className="text-caption text-muted">No albums found. Try “Artist – Album”.</p>
          : (
            <div className="tile-grid">
              {albums.map(c => (
                <button key={c.rgid} onClick={() => goAlbum(c.rgid)}
                  className="!flex flex-col !rounded-card border !border-line !bg-panel !p-2.5 text-left">
                  <div className="mb-2 w-full"><Cover url={c.coverUrl} name={c.title} fluid /></div>
                  <div className="w-full truncate text-small font-semibold">{c.title}</div>
                  <div className="w-full truncate text-micro text-muted">{c.artist}</div>
                  <div className="mt-1 flex flex-wrap items-center gap-1.5 text-micro text-faint">
                    {[c.year, c.primary_type].filter(Boolean).join(' · ')}
                    {c.releaseOwned && <span className="chip good !my-0 !py-0">in library</span>}
                  </div>
                </button>
              ))}
            </div>
          )}
      </div>

      <SectionHeader label="Other artists" sub="MusicBrainz" />
      <div className="flex flex-wrap gap-1.5">
        {artists === null ? loading(4)
          : !others.length ? <p className="text-caption text-muted">No other artists found.</p>
          : others.map(c => (
            <button key={c.mbid} className="!py-1.5 text-left" onClick={() => goArtist(`mb:${c.mbid}`)}
              title={[c.disambiguation, c.type, c.area || c.country].filter(Boolean).join(' · ')}>
              {c.name}
              {(c.disambiguation || c.area) && <span className="text-caption text-muted"> · {c.disambiguation || c.area}</span>}
            </button>
          ))}
      </div>
    </>
  )
}

export default function Search() {
  const { state } = useApp()
  const q = (state.routeParams[0] || '').trim()
  return (
    <>
      <div className="mb-5"><PageTitle eyebrow="Search" title={q ? `“${q.length > 60 ? q.slice(0, 57) + '…' : q}”` : 'Search'} /></div>
      {!q ? <EmptyState title="Search for an artist or album" hint="Or paste a link from Spotify, Apple Music, Deezer, TIDAL, Qobuz, YouTube Music or MusicBrainz." />
        : isLink(q) ? <LinkResult q={q} /> : <TextResults q={q} />}
    </>
  )
}
