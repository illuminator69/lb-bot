import { useEffect, useState } from 'react'
import { useApp, navigate, replaceRoute, goArtist, goAlbum } from '../App.jsx'
import { api, post } from '../lib/api.js'
import Cover from '../components/Cover.jsx'
import Page, { Grp } from '../components/Page.jsx'
import { loadArtists } from './Artist.jsx'
import { EmptyState, Skeleton } from '../components/ui.jsx'

// Search. Three kinds of answer from one box:
//   - a pasted link  → /api/resolve-link (Spotify, Deezer, Apple Music, TIDAL,
//                      Qobuz, YouTube Music, MusicBrainz) → the album/artist page
//   - a Spotify *playlist* link → offer to scan it for missing tracks
//   - text           → your artists by name, plus MusicBrainz artists and albums
// Every result opens an existing page; acquisition happens there, through the
// one album pipeline.

const isLink = q => /^https?:\/\//i.test(q) || /^spotify:/i.test(q)
  || /^(open\.spotify|music\.apple|deezer|tidal|www\.qobuz|music\.youtube|musicbrainz)\./i.test(q)
const isSpotifyPlaylist = q => /spotify\.com\/playlist\/|^spotify:playlist:/i.test(q)

function LinkResult({ q }) {
  const { action, pushToast } = useApp()
  const [r, setR] = useState(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
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
      <div style={{ maxWidth: 640 }}>
        <p className="why quiet">
          A Spotify playlist. lb-bot can check which of its tracks your library is missing and put them in the gaps
          queue, under Found by: Spotify.
        </p>
        <div className="acts">
          <button className="primary" disabled={busy} aria-busy={busy} onClick={async () => {
            setBusy(true)
            try {
              await action('/api/spotify/scan', { playlist: q })
              pushToast('Scanning the playlist. Its missing tracks will appear in the gaps queue')
              navigate('needs', 'gaps')
            } catch (e) { pushToast(`Scan failed: ${e.message}`, 'error') }
            finally { setBusy(false) }
          }}>Scan it for missing tracks</button>
        </div>
      </div>
    )
  }
  if (err) return <EmptyState title="Couldn’t read that link" hint={err} />
  if (!r) return <p className="muted">Working out what that link is…</p>
  if (r.kind === 'unknown' || !r.confidence) {
    return <EmptyState title="Couldn’t work out what that link is" hint={r.reason || 'Try searching for the artist and album by name instead.'} />
  }
  return <LinkAnswer r={r} sure={r.confidence >= 0.85} />
}

// A link resolved by id (MusicBrainz, or the store's own API) just opens —
// pasting it meant "open this". A page-title match (confidence ~0.7) shows a
// row first, because the name it went on may be the wrong record.
function LinkAnswer({ r, sure }) {
  useEffect(() => {
    if (!sure) return
    if (r.rgid) replaceRoute('album', '-', r.rgid)
    else if (r.kind === 'artist' && r.mbid) replaceRoute('artist', `mb:${r.mbid}`)
  }, [r, sure])
  return (
    <div style={{ maxWidth: 720 }}>
      {!sure && (
        <p className="why">Matched by name from the store’s page, not by an id. Check it is the right one before opening it.</p>
      )}
      <div className="rows">
        <div className="row">
          {r.rgid && <Cover url={`https://coverartarchive.org/release-group/${r.rgid}/front-250`} name={r.title || '?'} size={48} />}
          <div className="grow">
            <div className="t clip">{r.title || r.artist || (r.kind === 'artist' ? 'An artist' : 'An album')}</div>
            <div className="s clip">{[r.title && r.artist, r.kind, `from ${r.provider}`].filter(Boolean).join(' · ')}</div>
          </div>
          {r.rgid
            ? <button className="primary sm" onClick={() => goAlbum(r.rgid)}>Open album</button>
            : r.kind === 'artist' && r.mbid
              ? <button className="primary sm" onClick={() => goArtist(`mb:${r.mbid}`)}>Open artist</button>
              : <button className="sm" onClick={() => navigate('search', [r.artist, r.title].filter(Boolean).join(' '))}>Search by name</button>}
        </div>
      </div>
    </div>
  )
}

const skeletonRows = n => (
  <div className="rows">
    {Array.from({ length: n }, (_, i) => (
      <div key={i} className="row"><Skeleton style={{ width: 40, height: 40 }} /><Skeleton className="h-3" style={{ width: `${40 - i * 6}%` }} /></div>
    ))}
  </div>
)

function TextResults({ q }) {
  const [owned, setOwned] = useState(null)
  const [artists, setArtists] = useState(null)
  // Why the MusicBrainz artist search has no rows, when it failed: an empty
  // list alone read "No other artists found" for a search that never ran.
  const [artistsError, setArtistsError] = useState(null)
  const [albums, setAlbums] = useState(null)
  const [albumsError, setAlbumsError] = useState(null)

  useEffect(() => {
    let dead = false
    setOwned(null); setArtists(null); setArtistsError(null); setAlbums(null); setAlbumsError(null)
    const m = q.split(/\s+[–—-]\s+/)
    // "Artist – Album" matches your artists on the artist half.
    const needle = (m.length === 2 ? m[0] : q).toLowerCase()
    loadArtists()
      .then(list => !dead && setOwned(list.filter(a => (a.name || '').toLowerCase().includes(needle)).slice(0, 12)))
      .catch(() => !dead && setOwned([]))
    api('/api/artist/lookup?q=' + encodeURIComponent(m.length === 2 ? m[0] : q))
      .then(r => !dead && setArtists(r.candidates || []))
      .catch(e => { if (!dead) { setArtists([]); setArtistsError(e.message || 'the search failed') } })
    // "Artist – Album" is asked fielded, which MusicBrainz ranks far better
    // than free text (free text puts parodies first). The server quotes it and
    // falls back to free text.
    const url = m.length === 2
      ? `/api/album/lookup?artist=${encodeURIComponent(m[0])}&album=${encodeURIComponent(m[1])}`
      : '/api/album/lookup?q=' + encodeURIComponent(q)
    api(url)
      .then(r => !dead && setAlbums(r.candidates || []))
      .catch(e => { if (!dead) { setAlbums([]); setAlbumsError(e.message || 'the search failed') } })
    return () => { dead = true }
  }, [q])

  // Hide only the MusicBrainz rows that ARE a listed library artist (same
  // mbid). A same-name artist is someone else — the UK Nirvana is not the
  // Seattle one — and must stay reachable. An owned artist without an mbid tag
  // still shows here; the server keeps a scan from its `mb:` page off the
  // owned artist's index row.
  const ownedMbids = new Set((owned || []).map(a => a.mbid).filter(Boolean))
  const others = (artists || []).filter(c => c.mbid && !ownedMbids.has(c.mbid))
  const artistRows = [
    ...(owned || []).map(a => ({ key: a.id, name: a.name, cover: a.coverUrl, owned: true, open: () => goArtist(a.id),
      note: `${a.releaseCount} release${a.releaseCount === 1 ? '' : 's'}` })),
    ...others.map(c => ({ key: `mb:${c.mbid}`, name: c.name, owned: false, open: () => goArtist(`mb:${c.mbid}`),
      note: [c.disambiguation, c.type, c.area || c.country].filter(Boolean).join(' · ') })),
  ]
  const artistsLoading = owned === null || artists === null

  return (
    <>
      <Grp n={artistsLoading ? '' : artistRows.length}>Artists</Grp>
      {artistsLoading && !artistRows.length ? skeletonRows(3)
        : !artistRows.length ? <p className="muted">No artist matches, in your library or on MusicBrainz.</p>
        : (
          <div className="rows">
            {artistRows.map(a => (
              <button key={a.key} className="row tap" onClick={a.open}>
                <Cover url={a.cover} name={a.name} size={40} round />
                <span className="grow">
                  <span className="t clip block">{a.name}</span>
                  {a.note && <span className="s clip block">{a.note}</span>}
                </span>
                <span className={`st ${a.owned ? 'ok' : ''}`}>{a.owned ? 'In library' : 'Not in library'}</span>
              </button>
            ))}
          </div>
        )}
      {artistsError && <p className="bad mt-2 text-caption">Couldn’t search MusicBrainz for artists: {artistsError}</p>}

      <Grp n={albums?.length || ''}>Albums</Grp>
      {albums === null ? skeletonRows(4)
        : albumsError ? <p className="bad text-caption">Couldn’t search MusicBrainz for albums: {albumsError}</p>
        : !albums.length ? <p className="muted">No albums found. Try “Artist – Album”.</p>
        : (
          <div className="rows">
            {albums.map(c => (
              <div key={c.rgid} className="row stack">
                <Cover url={c.coverUrl} name={c.title} size={40} />
                <div className="grow">
                  <button className="link plain t clip block max-w-full" onClick={() => goAlbum(c.rgid)}>{c.title}</button>
                  <div className="s clip">{[c.artist, c.year, c.primary_type && c.primary_type.toLowerCase() !== 'album' ? c.primary_type : null].filter(Boolean).join(' · ')}</div>
                </div>
                <span className={`st ${c.releaseOwned ? 'ok' : ''}`}>{c.releaseOwned ? 'In library' : 'Not in library'}</span>
                {c.releaseOwned
                  ? <button className="sm" onClick={() => goAlbum(c.rgid)}>View</button>
                  : <button className="sm" onClick={() => goAlbum(c.rgid, true)}>Find sources</button>}
              </div>
            ))}
          </div>
        )}
    </>
  )
}

export default function Search() {
  const { state } = useApp()
  const q = (state.routeParams[0] || '').trim()
  const [draft, setDraft] = useState(q)
  useEffect(() => { setDraft(q) }, [q])
  const shownQ = q.length > 60 ? q.slice(0, 57) + '…' : q
  return (
    <Page title="Search" sub={q ? (isLink(q) ? 'A pasted link' : `Results for “${shownQ}”`) : undefined}
      bar={(
        <form className="flex min-w-0 flex-1 gap-2" role="search"
          onSubmit={e => { e.preventDefault(); if (draft.trim()) navigate('search', draft.trim()) }}>
          <input className="find" style={{ flex: 1, width: 'auto' }} type="search" autoFocus={!q}
            placeholder="Artist, album, or a streaming link" aria-label="Search"
            value={draft} onChange={e => setDraft(e.target.value)} />
          <button type="submit">Search</button>
        </form>
      )}>
      {!q ? (
        <p className="empty">
          Type an artist or album, or paste a link from Spotify, Apple Music, Deezer, TIDAL, Qobuz, YouTube Music or
          MusicBrainz.
        </p>
      ) : isLink(q) ? <LinkResult q={q} /> : <TextResults q={q} />}
    </Page>
  )
}
