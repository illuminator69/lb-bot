import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useApp, navigate, replaceRoute, goArtist } from '../App.jsx'
import { api, post } from '../lib/api.js'
import { indexLineStands, indexLineView } from '../lib/frame.js'
import { useViewState } from '../lib/useView.js'
import { albumState, countsMeasured, inLibrary as inLibraryStatus } from '../lib/indexStatus.js'
import { selectionOfRelease } from '../lib/overrides.js'
import { SHARED_NOTE, pageRead, quietLine } from '../lib/artistMatch.js'
import { setOf } from '../lib/merges.js'
import {
  askedText, sourcesAgeText, sourceKey, SAMPLED_NOTE, extraFilesText, downloadAllLabel,
} from '../lib/albumSources.js'
import Cover from '../components/Cover.jsx'
import Page, { Dot, Grp, Meter, Pick, Roll, Seg, St } from '../components/Page.jsx'
import {
  AlbumStateChip, ArtistTile, Chip, EMPTY_SOURCE_FILTERS, EmptyState, filterSources, Skeleton,
  SourceFilters, SourceRow, STATUS, TrackList,
} from '../components/ui.jsx'
import { ChangeCover, ChangedLine, EditionBlock, useAlbumOverride } from './AlbumOverride.jsx'
import { ArtistMatchView, matchJobRunning, useArtistMatch, useOverridesRead } from './ArtistMatch.jsx'
import ArtistMerge from './ArtistMerge.jsx'
import GapDetail, { GapFoot, GapTracks } from './GapDetail.jsx'
import { RequestFailure, RequestProgress, useRequest } from './requests.jsx'

// ── Routing ──────────────────────────────────────────────────────────────────
// Artist and album pages belong to no section: they open on top of whatever
// page you were on, and Back names that page.
//   #/artist/<artistId>                    → ArtistPage
//   #/album/<artistId>/<rgid>[/sources]    → AlbumPage ('sources' starts the search)
//   #/album/<artistId>/<rgid>/edition      → AlbumPage with the edition chips in view
//                                            (a gap's "Wrong edition?")
//   #/album/-/<rgid>[/sources|/edition]    → AlbumResolver: an album known only by
//                                            its release-group (search, charts, wishlist)
// artistId is a Navidrome id for an owned artist, or `mb:<mbid>`.
// The list of your artists is Library › Artists (ArtistIndex, below).

const caaCover = rgid => `https://coverartarchive.org/release-group/${rgid}/front-250`

// ── Discography data ─────────────────────────────────────────────────────────
// Scan results keyed by artist id, session-lived.
const discoCache = new Map()
/** Lets go of what the session kept of these artists' discographies (`ids`:
 *  Navidrome artist ids) — or, with no list, of all of it: the caller knows
 *  something changed and cannot say whose (an album's undo on System ›
 *  Overrides; a job that ended with no page showing it). The one way in from
 *  outside this file (F1): a match set or removed, a merge or an undo that is
 *  answered or seen to end anywhere but on the artist's own page left this
 *  holding what it read before, and the page then showed that — "scanned
 *  yesterday", the match from before — with no request at all. The cache is a
 *  convenience: a page that reads once more is right. */
export function forgetDiscography(ids = null) {
  if (!Array.isArray(ids)) { discoCache.clear(); return }
  for (const id of ids) if (id) discoCache.delete(id)
}
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
// The list again, now: an artist's MusicBrainz match was set or removed, and
// its row — `mbid`, `tagMbid` — is what every page here goes by. Every view
// that holds the list gets the new one; what it shows stays until then.
const artistsHeld = new Set()
export function reloadArtists() {
  artistsPromise = null
  return loadArtists().then(list => { artistsHeld.forEach(set => set(list)); return list })
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
    artistsHeld.add(setArtists)
    return () => { dead = true; artistsHeld.delete(setArtists) }
  }, [])
  return [artists, error]
}

// What the artist page says when lb-bot had no scan to follow. Shared-with is
// claimed only for `covered: true` — the one answer that supports it
// (`SHARED_NOTE`, lib/artistMatch.js: the match picker says it too).
const NOT_STARTED = 'lb-bot did not start the scan, and has no discography stored for this artist.'
const SCAN_LOST_NOTE = 'The scan did not finish: lb-bot restarted while it ran. This is what it has stored; rescan to walk it again.'
const SCAN_LOST = 'The scan was lost: lb-bot restarted while it ran.'

// Runs (or reuses) the discography scan for one artist. Index-first: a
// previously scanned artist renders instantly.
function useDiscography(artist, { autoScan = true } = {}) {
  const { action } = useApp()
  const [disco, setDisco] = useState(() => (artist ? discoCache.get(artist.id) || null : null))
  const [progress, setProgress] = useState(null)
  const [error, setError] = useState(null)
  // One muted line beside a discography that is shown without a scan behind it.
  const [note, setNote] = useState(null)
  const [nonce, setNonce] = useState(0)
  // Whether the server HAS an index for this artist: true / false / null while
  // asking. `disco` alone can't answer that during a multi-minute scan.
  const [indexed, setIndexed] = useState(null)
  // The page looked the artist up by name and MusicBrainz named no candidate.
  // It is shown as an error of the scan, and it is an answer all the same:
  // "no match" is true then (lib `pageRead`).
  const [nobody, setNobody] = useState(false)
  const pollRef = useRef(null)
  const forceScanRef = useRef(false)

  const artistId = artist?.id
  useEffect(() => {
    if (!artistId) return
    const cached = discoCache.get(artistId)
    setDisco(cached || null)
    setError(null)
    setNobody(false)
    setNote(null)
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
          if (!mbid) {
            if (!dead) setNobody(true)
            throw new Error(`No MusicBrainz match for “${artist.name}”`)
          }
        }
        // No name for an `mb:` page is fine: the server looks the real one up.
        // Posting the mbid in its place stored the UUID as the artist's name.
        const r = await action('/api/artist/discography',
          { mbid, name: artist.name || '', nd_id: artistId, external: !!artist.external })
        if (dead) return
        // With no scan to follow, show what the server has stored. Asked for
        // under the mbid that was POSTED: `artist.mbid` is empty for an
        // untagged artist, whose mbid came from the lookup above.
        const settle = async (why, otherwise) => {
          const idx = await api('/api/artist/discography'
            + `?mbid=${encodeURIComponent(mbid)}&nd_id=${encodeURIComponent(artistId)}`)
          if (dead) return
          setIndexed(idx.indexed === true)
          if (idx.indexed) {
            discoCache.set(artistId, idx)
            setDisco(idx)
            setNote(why)
          } else {
            setError(otherwise)
          }
        }
        // A blank task id is "nothing was started" — a covered artist, whose
        // discography is another library artist's row. Polling it asked for
        // `/api/tasks/`, a 404, for as long as the page stayed open.
        if (!r.task_id) {
          await settle(r.covered ? SHARED_NOTE : null, NOT_STARTED)
          return
        }
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
          } catch (e) {
            // No such task: lb-bot restarted mid-scan and forgot it. The poll
            // would never end; anything else is worth asking again.
            if (e.status === 404) {
              try {
                await settle(SCAN_LOST_NOTE, SCAN_LOST)
              } catch (e2) {
                if (!dead) setError(e2.message)
              }
              return
            }
          }
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
  // The stored index again, with what is on the page left there until the
  // answer is in: `refreshIndex` blanks the album to "Checking your library…"
  // for as long as the read takes.
  // `mbidNow`: the artist's MusicBrainz id as it is now, where the caller
  // knows it better than this render's `artist` (its match was just changed).
  const reread = async mbidNow => {
    if (!artistId) return
    try {
      const idx = await api('/api/artist/discography'
        + `?mbid=${encodeURIComponent(mbidNow ?? artist.mbid ?? '')}`
        + `&nd_id=${encodeURIComponent(artistId)}`)
      if (!idx.indexed) return
      if (idx.scanned_at) discoCache.set(artistId, idx)
      setIndexed(true)
      setDisco(idx)
      // What an earlier scan said went wrong is not said of this answer.
      setError(null)
      setNote(null)
    } catch { /* what is shown stays */ }
  }
  return { disco, indexed, progress, error, nobody, note, rescan, refreshIndex, reread }
}

// ── Artist index (Library › Artists) ──────────────────────────────────────────
const TILE_CHUNK = 120

function SkeletonTileGrid({ tiles = 12, round = false }) {
  return (
    <div className="grid">
      {Array.from({ length: tiles }, (_, i) => (
        <div key={i} className="tile">
          <Skeleton className={round ? '!rounded-pill' : ''} style={{ width: '100%', aspectRatio: '1 / 1' }} />
          <Skeleton className="h-3" style={{ width: `${70 - (i % 3) * 15}%` }} />
          <Skeleton className="h-2.5 w-1/3" />
        </div>
      ))}
    </div>
  )
}

function SkeletonRows({ rows = 6 }) {
  return (
    <div className="rows">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="row">
          <Skeleton style={{ width: 40, height: 40 }} />
          <div className="grow"><Skeleton className="h-3" style={{ width: `${55 - (i % 3) * 12}%` }} /><Skeleton className="mt-2 h-2.5 w-1/5" /></div>
        </div>
      ))}
    </div>
  )
}

// One line on how complete the library index is. Building it is on System ›
// Status; the auto-index worker does it in the background.
//
// Its place is there from the page's first frame (N3). Drawn only once lb-bot
// had answered, it made the filter row a row taller on a phone a moment after
// every arrival, and the grid dropped by that much. Until the answer the same
// button stands there with the line it will most likely carry, not shown, and
// a skeleton over it; lb-bot's last answer is kept for the session, so a later
// visit draws the line at once and reads again behind it. A read that fails
// with nothing kept folds the place away (`indexLineStands`).
let indexStatusKept = null
export function IndexLine({ guess }) {
  const [s, setS] = useState(indexStatusKept)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let alive = true
    api('/api/library-index/status')
      .then(now => { indexStatusKept = now; if (alive) { setS(now); setFailed(false) } })
      .catch(() => { if (alive) setFailed(true) })
    return () => { alive = false }
  }, [])
  const v = indexLineView(s, guess)
  return (
    <Roll open={indexLineStands({ status: s, failed })}>
      {v.known ? (
        <button className="quiet sm block" onClick={() => navigate('system', 'status')}
          title="How many of your artists have their full discography indexed">
          <span className="num">{v.counts}</span> indexed{v.tail}
        </button>
      ) : (
        <button className="quiet sm block" inert style={{ position: 'relative' }}>
          <span style={{ visibility: 'hidden' }}><span className="num">{v.counts}</span> indexed</span>
          <Skeleton style={{ position: 'absolute', left: 9, right: 9, top: '50%', height: 10, marginTop: -5 }} />
        </button>
      )}
    </Roll>
  )
}

/** Your artists as a grid. `frame` wraps it in the Library page frame; it is
 *  called as a function, never rendered as a component: a component made
 *  inline is a new type on every render, and React rebuilt the whole grid
 *  (2,474 covers) on each poll. */
export function ArtistIndex({ frame }) {
  const [artists, error] = useArtists()
  // Kept with the history entry (N1): the page comes back with its filter, its
  // order and as many tiles as it had drawn.
  const [sort, setSort] = useViewState('lib.sort', 'az')
  const [q, setQ] = useViewState('lib.q', '')
  const [limit, setLimit] = useViewState('lib.tiles', TILE_CHUNK)
  const hasPlays = (artists || []).some(a => a.plays != null)
  const needle = q.trim().toLowerCase()

  const shown = useMemo(() => {
    let list = artists || []
    if (needle) list = list.filter(a => (a.name || '').toLowerCase().includes(needle))
    list = [...list]
    if (sort === 'plays' && hasPlays) list.sort((a, b) => (b.plays || 0) - (a.plays || 0) || a.name.localeCompare(b.name))
    else if (sort === 'releases') list.sort((a, b) => (b.releaseCount || 0) - (a.releaseCount || 0) || a.name.localeCompare(b.name))
    else list.sort((a, b) => a.name.localeCompare(b.name))
    return list
  }, [artists, needle, sort, hasPlays])
  // (When the filter or the order changes — not at mount, where a page that
  // comes back draws the tiles it had.)
  const drawnFor = useRef(`${needle}|${sort}`)
  useEffect(() => {
    const now = `${needle}|${sort}`
    if (drawnFor.current === now) return
    drawnFor.current = now
    setLimit(TILE_CHUNK)
  }, [needle, sort])

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

  const bar = (
    <>
      <input className="find" type="search" placeholder="Filter artists" aria-label="Filter your artists"
        value={q} onChange={e => setQ(e.target.value)} />
      <Pick label="Sort" value={sort} onChange={setSort}
        options={[['az', 'A to Z'], ...(hasPlays ? [['plays', 'Most played']] : []), ['releases', 'Most releases']]} />
      {needle && artists ? <span className="num muted">{shown.length.toLocaleString()} match{shown.length === 1 ? '' : 'es'}</span> : null}
      <span className="spacer" />
      <IndexLine guess={artists?.length} />
    </>
  )

  return frame({
    bar,
    children: (
      <>
      {artists === null ? <SkeletonTileGrid round />
        : error ? <EmptyState title="Couldn’t load your artists" hint={error} />
        : !artists.length ? <EmptyState title="No artists in your library yet" hint="Once Navidrome has scanned some music, every artist shows up here." />
        : !shown.length ? (
          <EmptyState title={`None of your artists match “${q.trim()}”`} hint="Search everywhere to open an artist you don’t own yet.">
            <button onClick={() => navigate('search', q.trim())}>Search for “{q.trim()}”</button>
          </EmptyState>
        ) : (
          <>
            <div className="grid">
              {shown.slice(0, limit).map(a => (
                <ArtistTile key={a.id} name={a.name} coverUrl={a.coverUrl}
                  sub={`${a.releaseCount} release${a.releaseCount === 1 ? '' : 's'}${a.plays ? ` · ${a.plays.toLocaleString()} play${a.plays === 1 ? '' : 's'}` : ''}`}
                  onClick={() => goArtist(a.id)} />
              ))}
            </div>
            {limit < shown.length && <div ref={sentinelRef} className="muted p-4 text-center">Loading more…</div>}
          </>
        )}
      </>
    ),
  })
}

// ── About ────────────────────────────────────────────────────────────────────
// Editorial text from Wikipedia/Wikidata via /api/meta/*. Three lines until
// asked for more.
function About({ meta }) {
  const [open, setOpen] = useState(false)
  // The text is always all there; the box around it is three lines tall until
  // opened, and its height is what moves. (It used to swap a clamped paragraph
  // for the whole text in one frame.)
  const boxRef = useRef(null)
  const [full, setFull] = useState(0)
  const [overflows, setOverflows] = useState(false)
  useLayoutEffect(() => {
    const el = boxRef.current
    if (!el) return
    const measure = () => {
      const collapsed = parseFloat(getComputedStyle(el).fontSize) * ABOUT_LINES_EM
      setFull(el.scrollHeight)
      setOverflows(el.scrollHeight > collapsed + 4)
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el.firstElementChild || el)
    return () => ro.disconnect()
  }, [meta])
  if (!meta?.found) return null
  const paras = meta.paragraphs?.length ? meta.paragraphs : (meta.summary ? [meta.summary] : [])
  if (!paras.length && !(meta.links || []).length) return null
  return (
    <div className="fadein" style={{ maxWidth: '75ch' }}>
      <div ref={boxRef} className={`about-text${open ? ' open' : ''}${overflows ? ' more' : ''}`}
        style={{ maxHeight: open ? (full || 'none') : `${ABOUT_LINES_EM}em`, color: 'var(--fg2)' }}>
        <div>{paras.map((p, i) => <p key={i} className={i ? 'mt-2.5' : ''}>{p}</p>)}</div>
      </div>
      <div className="s mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1">
        {(overflows || open) && (
          <button className="link" aria-expanded={open} onClick={() => setOpen(o => !o)}>{open ? 'Less' : 'More'}</button>
        )}
        {meta.source?.url && (
          <a href={meta.source.url} target="_blank" rel="noreferrer" className="muted">
            From {meta.source.name}{meta.source.license ? ` · ${meta.source.license}` : ''}
          </a>
        )}
        {(meta.links || []).slice(0, 8).map(l => (
          <a key={l.url} href={l.url} target="_blank" rel="noreferrer" className="muted">{l.label}</a>
        ))}
      </div>
    </div>
  )
}
// Three lines of body text (line-height 1.45).
const ABOUT_LINES_EM = 4.35

// The About while it is being looked up (Wikipedia by way of MusicBrainz and
// Wikidata, a second or two the first time): three lines and a row of links,
// the size of what will replace them, so the page does not jump when it lands.
function AboutLoading() {
  return (
    <div style={{ maxWidth: '75ch' }} aria-busy="true" aria-label="Loading the About">
      <div className="skel-lines">
        <i className="skeleton" style={{ width: '96%' }} />
        <i className="skeleton" style={{ width: '100%' }} />
        <i className="skeleton" style={{ width: '71%' }} />
      </div>
      <div className="skel-lines mt-2.5"><i className="skeleton" style={{ width: '42%' }} /></div>
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
// related artists as the fallback when those know nobody. A row of portraits:
// the library's own image for an artist you have, Deezer's (asked for with
// `images=1`) for one you don't, and their initials when there is neither.
function SimilarArtists({ mbid, name }) {
  const [data, setData] = useState(null)
  const [owned] = useArtists()
  useEffect(() => {
    if (!mbid && !name) return
    let dead = false
    setData(null)
    const q = new URLSearchParams({ name: name || '' })
    if (mbid) q.set('mbid', mbid)
    api(`/api/artist/similar?${q}&images=1`, { timeoutMs: 25000 })
      .then(r => (r.artists?.length ? r : api(`/api/artist/related?${q}`, { timeoutMs: 20000 })))
      .then(r => !dead && setData(r))
      .catch(() => !dead && setData({ artists: [] }))
    return () => { dead = true }
  }, [mbid, name])
  const coverOf = useMemo(() => new Map((owned || []).map(a => [a.id, a.coverUrl])), [owned])
  if (!mbid && !name) return null
  const rows = (data?.artists || []).filter(a => a.owned ? a.artistId : a.mbid).slice(0, 14)
  if (data && !rows.length) return null
  return (
    <>
      <Grp>Similar artists<span className="note">{(data?.sources || []).join(' + ')}</span></Grp>
      {data === null ? (
        <div className="strip" aria-busy="true">
          {Array.from({ length: 8 }, (_, i) => (
            <div key={i} className="who">
              <Skeleton className="!rounded-pill" style={{ width: '100%', aspectRatio: '1 / 1' }} />
              <Skeleton className="mx-auto h-3" style={{ width: `${80 - (i % 3) * 14}%` }} />
            </div>
          ))}
        </div>
      ) : (
        <div className="strip arrive">
          {rows.map(a => (
            <button key={a.mbid || a.artistId} className="who" title={a.owned ? 'In your library' : 'Not in your library'}
              onClick={() => goArtist(a.owned ? a.artistId : `mb:${a.mbid}`)}>
              <Cover url={(a.owned && coverOf.get(a.artistId)) || a.imageUrl || null} name={a.name} fluid round initials />
              <span className="nm">{a.name}</span>
              <span className={`st ${a.owned ? 'ok' : ''}`}>{a.owned ? 'In library' : 'Not in library'}</span>
            </button>
          ))}
        </div>
      )}
    </>
  )
}

// ── Artist page ──────────────────────────────────────────────────────────────
const TYPES = [
  ['album', 'Albums', 'Album'],
  ['ep', 'EPs', 'EP'],
  ['single', 'Singles', 'Single'],
  ['compilation', 'Compilations', 'Compilation'],
  ['soundtrack', 'Soundtracks', 'Soundtrack'],
  ['live', 'Live', 'Live'],
  ['other', 'Other', 'Other'],
]
// The page groups by what you have; the switch above it filters by type.
const STATE_GROUPS = [
  ['complete', 'In library'],
  ['incomplete', 'Incomplete'],
  ['untagged', 'Untagged'],
  ['missing', 'Not in library'],
]
const TYPE_RANK = { album: 0, ep: 1, compilation: 2, soundtrack: 3, live: 4, single: 5, other: 6 }
const DISCO_SORTS = [['new', 'Newest first'], ['old', 'Oldest first'], ['az', 'A to Z']]
// A group this long starts folded — 59 singles buried 7 albums.
const FOLD_OVER = 12

function typeKey(release) {
  const t = release.effective_type || release.primary_type || ''
  return TYPES.some(([k]) => k === t) && t !== 'other' ? t : 'other'
}

function relTime(epoch) {
  const days = Math.floor((Date.now() / 1000 - epoch) / 86400)
  if (days <= 0) return 'today'
  if (days === 1) return 'yesterday'
  if (days < 30) return `${days} days ago`
  const months = Math.floor(days / 30)
  return `${months} month${months === 1 ? '' : 's'} ago`
}

function ReleaseRow({ r, artistId }) {
  const type = typeKey(r)
  const missing = Math.max(0, (r.total || 0) - (r.present || 0))
  const open = pick => goArtist(artistId, r.rgid, pick)
  return (
    <div className="row stack">
      <div style={r.status === 'missing' ? { opacity: 0.6 } : undefined}>
        <Cover url={caaCover(r.rgid)} name={r.title} size={40} />
      </div>
      <div className="grow">
        <button className="link plain t clip block max-w-full" onClick={() => open(false)}>{r.title}</button>
        <div className="s clip">{r.year || 'no date'}{type !== 'album' ? ` · ${TYPES.find(t => t[0] === type)[2]}` : ''}</div>
      </div>
      {/* The row's own counts, where a scan measured them for the status it
          has: an album lb-bot filled since (`present`) still carries the
          numbers from before the fill. */}
      {r.total > 0 && countsMeasured(r.status) && <span className="num muted hide-sm">{r.present || 0}/{r.total}</span>}
      <AlbumStateChip state={r.status} count={r.status === 'incomplete' ? missing : null} />
      {(r.status === 'missing' || r.status === 'incomplete') && (
        <button className="sm" onClick={() => open(true)}>Find sources</button>
      )}
    </div>
  )
}

// How many rows of a long group show before the rest is asked for.
const FOLD_SHOW = 6

function StateGroup({ label, releases, artistId, right }) {
  const long = releases.length > FOLD_OVER
  const [open, setOpen] = useState(!long)
  const head = long ? releases.slice(0, FOLD_SHOW) : releases
  const rest = long ? releases.slice(FOLD_SHOW) : []
  return (
    <>
      <Grp n={releases.length} right={
        <>
          {right}
          {long && (
            // A button in its own box: beside a text link (Wishlist…) a second
            // text link read as part of it.
            <button className="sm" aria-expanded={open} onClick={() => setOpen(o => !o)}>
              {open ? 'Show fewer' : `Show all ${releases.length}`}
            </button>
          )}
        </>
      }>{label}</Grp>
      <div className="rows">
        {head.map(r => <ReleaseRow key={r.rgid} r={r} artistId={artistId} />)}
        {/* The rest folds open and shut in place, both ways. */}
        {long && (
          <Roll open={open}>
            {rest.map(r => <ReleaseRow key={r.rgid} r={r} artistId={artistId} />)}
          </Roll>
        )}
      </div>
    </>
  )
}

function DiscographyView({ artist }) {
  const { disco, progress, error, nobody, note, rescan, reread } = useDiscography(artist)
  const { requestConfirm, pushToast } = useApp()
  const [type, setType] = useViewState('artist.type', 'all')
  const [sort, setSort] = useViewState('artist.sort', 'new')
  const mbid = artist.mbid || disco?.artist_mbid || ''
  const meta = useMeta(mbid ? `/api/meta/artist?mbid=${encodeURIComponent(mbid)}` : null)

  // Which MusicBrainz artist this is, and the way to say otherwise: a quiet
  // line by the hero that unfolds the match picker (ArtistMatch.jsx). Not on
  // an `mb:` page: that is no library artist.
  // Open on arrival when a scan the picker started is still running: the job
  // is shown where it is.
  const [fixOpen, setFixOpen] = useState(() => !artist.external && matchJobRunning(artist.id))
  // What lb-bot changed at the user's word, read once on arrival: it says
  // whether this artist is in the library under another name too (`siblings`,
  // for the block below) and which merges are running; the picker reads the
  // same answer when it opens. The picker's own requests wait for it to open.
  const overrides = useOverridesRead(!artist.external)
  const match = useArtistMatch({
    artist, open: fixOpen, overrides: overrides.state, onRead: overrides.read,
    // What this page's own read names: an id, nobody (also when its lookup
    // by name answered with no candidate), not yet — or, when the read failed,
    // not known (never "no match": nothing in a failure says so).
    indexMbid: pageRead(disco, error, nobody), onRetryIndex: rescan,
    known: mbid && meta ? { [mbid]: meta } : null, asking: mbid && meta === null ? mbid : '',
    // lb-bot stored the change: the artist's row is read again, so the page
    // goes by the id it has now. Once nothing more will change — the scan is
    // over, or none was started — so is the discography, under that id (this
    // render's `artist` may still be the old row), behind what is shown.
    onChanged: async what => {
      let row = null
      try { row = (await reloadArtists()).find(a => a.id === artist.id) || null } catch { /* the list as it was */ }
      if (what === 'ended') reread(row ? row.mbid || '' : undefined)
    },
  })
  const matchLine = artist.external ? null : quietLine(match.now, match.mb)

  // One artist, two names (ArtistMerge.jsx). When a merge or its undo has
  // ended, what the session kept of both names' discographies is let go of —
  // the page a merge moves to must not open on what it read before — and this
  // page, if it stays, reads its own again: at once, and twice more while
  // lb-bot's index catches up (it scans the artist after the merge), each time
  // behind what is shown. Nothing is read for a page that has been left.
  const [artistList] = useArtists()
  const art = useMemo(() => new Map((artistList || []).map(a => [a.id, a.coverUrl])), [artistList])
  const here = useRef(true)
  const mergeTimers = useRef([])
  useEffect(() => {
    here.current = true
    return () => { here.current = false; mergeTimers.current.forEach(clearTimeout) }
  }, [])
  const afterMerge = async job => {
    const row = job?.row || job?.startRow
    forgetDiscography([row?.into?.ndArtistId, row?.from?.ndArtistId])
    let me = null
    try { me = (await reloadArtists()).find(a => a.id === artist.id) || null } catch { /* the list as it was */ }
    if (!here.current || !me) return
    const once = () => { if (here.current) reread(me.mbid || '') }
    mergeTimers.current.forEach(clearTimeout)
    once()
    mergeTimers.current = [6000, 20000].map(ms => setTimeout(once, ms))
  }

  const releases = disco?.releases || []
  const typeCounts = useMemo(() => {
    const c = { all: releases.length }
    for (const r of releases) c[typeKey(r)] = (c[typeKey(r)] || 0) + 1
    return c
  }, [releases])
  const shown = useMemo(() => {
    const rows = type === 'all' ? [...releases] : releases.filter(r => typeKey(r) === type)
    const year = r => Number(r.year) || 0
    const title = (a, b) => (a.title || '').localeCompare(b.title || '')
    // With every type shown, albums come before EPs before singles: sorted by
    // date alone, 59 singles buried 7 albums.
    const rank = r => (type === 'all' ? TYPE_RANK[typeKey(r)] ?? 9 : 0)
    if (sort === 'az') rows.sort((a, b) => rank(a) - rank(b) || title(a, b))
    else if (sort === 'old') rows.sort((a, b) => rank(a) - rank(b) || (year(a) || 9999) - (year(b) || 9999) || title(a, b))
    else rows.sort((a, b) => rank(a) - rank(b) || year(b) - year(a) || title(a, b))
    return rows
  }, [releases, type, sort])
  const owned = releases.filter(r => inLibraryStatus(r.status)).length
  const missingAlbums = releases.filter(r => r.status === 'missing' && ['album', 'ep'].includes(typeKey(r)))
  const name = (artist.external ? (disco?.artist_name || meta?.name || artist.name) : artist.name) || 'Artist'

  async function wishlistMissing() {
    const ok = await requestConfirm(
      `Add ${missingAlbums.length} missing album(s) and EP(s) by ${name} to the wishlist? lb-bot searches for one every few hours.`,
      { confirmLabel: 'Add to wishlist' })
    if (!ok) return
    let added = 0
    for (const rel of missingAlbums) {
      try { await post('/api/wishlist', { rgid: rel.rgid, artist: artist.name || disco?.artist_name || meta?.name || '', title: rel.title }); added++ } catch { /* keep going */ }
    }
    pushToast(`Added ${added} album(s) to the wishlist`)
  }

  // An `mb:` route can still be an artist you own (a link from search or a
  // similar-artists row), so ask the discography, not the route.
  const sub = !disco ? (error ? undefined : 'Reading the discography…')
    : [
        owned ? `${owned} of ${releases.length} releases in your library` : 'Not in your library',
        disco.scanned_at ? `scanned ${relTime(disco.scanned_at)}${disco.stale ? ', may be out of date' : ''}` : 'not fully scanned yet',
      ].join(' · ')

  const typeOptions = [['all', `All ${typeCounts.all || 0}`],
    ...TYPES.filter(([k]) => typeCounts[k]).map(([k, label]) => [k, `${label} ${typeCounts[k]}`])]

  return (
    <Page title={name} sub={sub}
      fallbackBack={{ label: 'Library', onBack: () => navigate('library', 'artists') }}
      action={disco && <button onClick={rescan} title="Walk this artist’s MusicBrainz discography again">Rescan</button>}
      bar={disco && releases.length > 0 && (
        <>
          <div style={{ overflowX: 'auto', maxWidth: '100%' }}>
            <Seg label="Type" value={type} onChange={setType} options={typeOptions} />
          </div>
          <Pick label="Sort" value={sort} onChange={setSort} options={DISCO_SORTS} />
        </>
      )}>
      <div className="hero" style={{ marginBottom: 6 }}>
        <div style={{ width: 96 }}><Cover url={meta?.imageUrl || artist.coverUrl} name={name} fluid round /></div>
        <div className="grow" style={{ maxWidth: '75ch' }}>
          <p className="muted">
            {meta?.wikidataDescription
              || (owned ? 'Discography from MusicBrainz, checked against your Navidrome library.' : 'Discography from MusicBrainz.')}
          </p>
          {mbid && meta === null && <div className="mt-2"><AboutLoading /></div>}
          {meta?.found && <div className="mt-2"><About meta={meta} /></div>}
          {note && <p className="muted mt-2">{note}</p>}
          {matchLine && (
            // A <div>: the skeleton that stands for the name is a block, which a <p> cannot hold.
            <div className="muted fadein mt-2">
              {/* The dot starts no line: the word before it wraps with it. */}
              <Dot after={matchLine.text}
                last={matchLine.naming ? <Skeleton className="inline-block h-3 w-28 align-middle" /> : null}>
                <button className="link" aria-expanded={fixOpen} onClick={() => setFixOpen(v => !v)}>Fix match</button>
              </Dot>
            </div>
          )}
        </div>
      </div>

      {!artist.external && (
        // `slow`, like the search fold inside it: with its candidates listed the
        // picker is most of a screen, and shut in the ordinary time it was twice
        // as brisk as everything around it.
        <Roll slow open={fixOpen}>
          <div className="next" style={{ margin: '10px 0 6px' }}>
            <ArtistMatchView {...match.view} open={fixOpen} boxed />
          </div>
        </Roll>
      )}

      {!artist.external && (
        <ArtistMerge set={setOf(overrides.state.data, artist.id)} pageArtistId={artist.id}
          merges={overrides.state.data?.merges} art={art} onRead={overrides.read} onChanged={afterMerge} />
      )}

      <SimilarArtists mbid={mbid} name={artist.external && !disco ? '' : name} />

      {error ? (
        <EmptyState title="Discography scan failed" hint={error}>
          <button className="primary" onClick={rescan}>Try again</button>
        </EmptyState>
      ) : !disco ? (
        <>
          <div className="box mb-4 mt-4">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <St tone="run">
                {progress?.total ? `Checking ${progress.done} of ${progress.total}` : 'Pulling the discography from MusicBrainz'}
              </St>
              {progress?.current && <span className="muted clip grow">{progress.current}</span>}
            </div>
            <Meter value={progress?.total ? (progress.done / progress.total) * 100 : 4} />
            <p className="s muted mt-2">A first walk of a discography takes a few minutes. MusicBrainz allows one request per second.</p>
          </div>
          <SkeletonRows />
        </>
      ) : !releases.length ? (
        <p className="empty">MusicBrainz lists no releases for this artist.</p>
      ) : !shown.length ? (
        <p className="empty">No releases of this type.</p>
      ) : (
        STATE_GROUPS.map(([state, label]) => {
          // By what the index says read through `albumState`: an album lb-bot
          // filled (`present`) is "In library", not in no group at all.
          const group = shown.filter(r => albumState(r.status) === state)
          if (!group.length) return null
          return (
            <StateGroup key={`${state}-${type}`} label={label} releases={group} artistId={artist.id}
              right={state === 'missing' && missingAlbums.length > 0 && (type === 'all' || type === 'album' || type === 'ep') && (
                <button className="link" onClick={wishlistMissing}
                  title="Albums and EPs you don’t have. lb-bot looks for them slowly in the background">
                  Wishlist {missingAlbums.length} missing album{missingAlbums.length === 1 ? '' : 's'}
                </button>
              )} />
          )
        })
      )}
    </Page>
  )
}

// ── Album page ───────────────────────────────────────────────────────────────
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

// Release / edition chips. A *release* changes the tracklist; an *edition* is
// the same tracklist pressed differently, with its own cover.
const CHIPS_SHOWN = 5
function EditionChips({ variants, variantIdx, editionIdx, heldVariant = -1, onPick }) {
  const [allV, setAllV] = useState(false)
  const [allE, setAllE] = useState(false)
  // One index, or several: the library can hold two releases of one album.
  const heldSet = new Set([].concat(heldVariant))
  const variant = variants?.[variantIdx]
  const editions = variant?.editions || []
  if (!variants?.length || (variants.length < 2 && editions.length < 2)) return null
  const variantLabel = (v, i) => v.disambiguation || (i === 0 ? 'Original' : v.year || `Release ${i + 1}`)
  const more = (all, set, n) => n > CHIPS_SHOWN && (
    <button className="chip" style={{ borderStyle: 'dashed', color: 'var(--muted)' }} onClick={() => set(v => !v)}>
      {all ? 'Less' : 'More…'}
    </button>
  )
  const row = { display: 'flex', gap: 10, alignItems: 'flex-start', marginBottom: 10 }
  const lab = { color: 'var(--muted)', paddingTop: 5, width: 58, flex: 'none' }
  return (
    <>
      {variants.length > 1 && (
        <div style={row}>
          <span style={lab}>Release</span>
          <div className="chips grow">
            {variants.map((v, i) => (allV || i < CHIPS_SHOWN || i === variantIdx || heldSet.has(i)) && (
              <Chip key={v.releaseMbid || i} active={i === variantIdx} onClick={() => onPick(i, 0)}
                title={heldSet.has(i) ? 'The release in your library' : 'A different release has a different tracklist'}>
                {heldSet.has(i) && <span className="st ok mr-1.5" aria-label="In your library" />}
                {variantLabel(v, i)} · {v.trackCount} tracks
              </Chip>
            ))}
            {more(allV, setAllV, variants.length)}
          </div>
        </div>
      )}
      {editions.length > 1 && (
        <div style={row} key={variant.releaseMbid || variantIdx} className="fadein">
          <span style={lab}>Edition</span>
          <div className="chips grow">
            {editions.map((e, i) => (allE || i < CHIPS_SHOWN || i === editionIdx) && (
              <Chip key={e.releaseMbid} active={i === editionIdx} onClick={() => onPick(variantIdx, i)}
                title={[e.format !== e.label ? e.format : null, e.year, e.country].filter(Boolean).join(' · ') || 'Same tracklist, different pressing'}>
                {[e.label, e.year, e.country].filter(Boolean).join(' · ')}
              </Chip>
            ))}
            {more(allE, setAllE, editions.length)}
          </div>
        </div>
      )}
    </>
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
    <>
      <Grp>Similar albums you have<span className="note">{(data.sources || []).join(' + ')}</span></Grp>
      <div className="strip arrive">
        {data.albums.map(a => (
          <button className="who sq" key={`${a.artistId}-${a.rgid}`} title={`In your library. Similar to ${a.because}`}
            onClick={() => goArtist(a.artistId, a.rgid)}>
            <Cover url={a.coverUrl} name={a.title} fluid />
            <span className="nm">{a.title}</span>
            <span className="s clip">{a.artist}</span>
          </button>
        ))}
      </div>
    </>
  )
}

// The gap behind an incomplete album, polled while the page is open.
function useGap(groupId) {
  const [detail, setDetail] = useState(null)
  useEffect(() => {
    setDetail(null)
    if (!groupId) return
    let dead = false
    let timer
    const tick = async () => {
      let next = 5000
      try {
        const d = await api(`/api/gaps/${groupId}`)
        if (dead) return
        setDetail(d)
        if (d.status === 'downloading' || d.sourceTask?.status === 'running') next = 2000
      } catch { /* poll again */ }
      if (!dead) timer = setTimeout(tick, next)
    }
    tick()
    return () => { dead = true; clearTimeout(timer) }
  }, [groupId])
  return [detail, setDetail]
}

function AlbumDetail({ artist, rgid, autoPick = false, toEdition = false }) {
  const { state, action, pushToast } = useApp()
  // autoScan:false — an album page never starts a whole-artist MusicBrainz walk.
  const { disco, indexed, error: discoError, refreshIndex, reread: rereadIndex } = useDiscography(artist, { autoScan: false })
  const [variants, setVariants] = useState(null)
  const [groupTitle, setGroupTitle] = useState('')
  const [releaseArtist, setReleaseArtist] = useState('')
  const [groupMeta, setGroupMeta] = useState({ primaryType: '', year: '' })
  const [addedRelease, setAddedRelease] = useState(null)
  const [sel, setSel] = useState({ variant: 0, edition: 0 })
  // The release the library holds, once known: { variant, exact }. `exact` is
  // false when it was only inferred from which tracklist the library fills.
  const [held, setHeld] = useState(null)
  // The user chose a release themselves; nothing moves the selection after that.
  const pickedRef = useRef(false)
  const pick = (v, e) => { pickedRef.current = true; setSel({ variant: v, edition: e }) }
  // Whether the review has been asked which release the library holds (the
  // path for a server that does not say; see further down).
  const [reviewAsked, setReviewAsked] = useState(false)
  const [tracks, setTracks] = useState(null)
  const [requested, setRequested] = useState('')     // release mbid of the request shown here
  const [starting, setStarting] = useState(false)
  const [wishlisted, setWishlisted] = useState(false)
  // null = not searched; 'loading'; or the list
  const [sources, setSources] = useState(null)
  // When the list on the page was searched and whether it is a sample; whether
  // "Search again" is running; and a clock for the list's age.
  const [srcMeta, setSrcMeta] = useState(null)
  const [again, setAgain] = useState(false)
  const [now, setNow] = useState(() => Date.now())
  const listReq = useRef(0)                 // only the newest answer is shown
  // Which request of this visit is followed (each one afresh), and the peer it
  // named when it was a click on a source.
  const [req, setReq] = useState({ n: 0, asked: '' })
  const [srcFilters, setSrcFilters] = useState(EMPTY_SOURCE_FILTERS)
  const [indexAdd, setIndexAdd] = useState(null)
  const [addError, setAddError] = useState('')
  const [addNonce, setAddNonce] = useState(0)
  const addedRef = useRef('')
  const meta = useMeta(`/api/meta/album?rgid=${encodeURIComponent(rgid)}`)
  const request = useRequest(requested, req.n)

  // What is already true of this album: a request in flight or failed, and
  // whether it is on the wishlist. Both were only known for requests made on
  // this visit.
  useEffect(() => {
    let dead = false
    api('/api/fills?recent=1').then(r => {
      if (dead) return
      const mine = Object.values(r.albums || {}).filter(f => f.rgid === rgid)
        .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))[0]
      if (mine && ['searching', 'queued', 'downloading', 'placing', 'failed'].includes(mine.state)) {
        setRequested(v => v || mine.releaseMbid)
      }
    }).catch(() => {})
    api('/api/wishlist').then(w => {
      if (!dead && (w.wishlist || []).some(x => x.rgid === rgid)) setWishlisted(true)
    }).catch(() => {})
    return () => { dead = true }
  }, [rgid])

  const indexedRelease = (disco?.releases || []).find(r => r.rgid === rgid)
  // Three sources, best first; an unindexed release the library demonstrably
  // does not list is `missing`.
  const release = indexedRelease || addedRelease
    || (indexAdd === 'failed' ? { rgid, title: groupTitle, status: 'missing' } : null)
  // The index row's word, and the state the page shows for it (`present` — an
  // album lb-bot filled, not scanned since — is in the library: B-091).
  const indexStatus = release?.status
  const status = albumState(indexStatus)
  const headerTitle = release?.title || groupTitle
  const displayArtist = artist.external ? (releaseArtist || artist.name) : artist.name

  // A release the artist's stored index predates (anything from New releases):
  // add the one row rather than rescanning the whole artist.
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
    setHeld(null)
    pickedRef.current = false
    api('/api/album/releases?rgid=' + encodeURIComponent(rgid), { timeoutMs: LOOKUP_TIMEOUT_MS })
      .then(r => {
        if (dead) return
        const list = r.releases || []
        setVariants(list)
        // Open on the release the library holds, not on whichever MusicBrainz
        // lists first: a complete 12-track album used to be shown against the
        // 13-track original, with one row "Missing".
        const v = list.findIndex(x => x.owned)
        if (v >= 0) {
          const e = Math.max(0, (list[v].editions || []).findIndex(x => x.label === list[v].ownedEdition))
          setSel({ variant: v, edition: e })
          setHeld({ variant: v, exact: true })
        }
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
  const inLibrary = inLibraryStatus(indexStatus)
  // An album with gaps is filled by its gap: the same next step, sources and
  // per-track detail as in the queue, here on the album's own page.
  const incomplete = status === 'incomplete' && !!groupId
  const [gap, setGap] = useGap(incomplete ? groupId : '')

  // ── The override (I-029): which release these files are, the undo, the cover ──
  // The library's albums in this release group, by the only ids the page has:
  // the index row's, and the gap's own. /api/album/releases marks which
  // *release* is held, not which Navidrome album.
  const gapAlbumId = gap?.albumId || ''
  const indexIds = useMemo(
    () => (inLibrary ? [...new Set([...(albumIds ? albumIds.split(',') : []), gapAlbumId].filter(Boolean))] : []),
    [inLibrary, albumIds, gapAlbumId])
  // A job changed the library. lb-bot writes `applied` before it has told its
  // own index (the release is refreshed a few seconds later), so the page reads
  // again at once and twice more — each time leaving what is shown in place.
  const selRef = useRef(sel)
  selRef.current = sel
  const variantsRef = useRef(variants)
  variantsRef.current = variants
  const rereadTimers = useRef([])
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; rereadTimers.current.forEach(clearTimeout) }
  }, [])
  async function rereadReleases() {
    try {
      const r = await api('/api/album/releases?rgid=' + encodeURIComponent(rgid), { timeoutMs: LOOKUP_TIMEOUT_MS })
      const list = r.releases || []
      if (!list.length) return
      // The selection is a release, not a place in the list: a release the
      // library now holds can join the list and move the others.
      const cur = variantsRef.current?.[selRef.current.variant]
      const curEdition = cur?.editions?.[selRef.current.edition]?.releaseMbid
      let v = curEdition ? list.findIndex(x => (x.editions || []).some(e => e.releaseMbid === curEdition)) : -1
      if (v < 0 && cur?.releaseMbid) v = list.findIndex(x => x.releaseMbid === cur.releaseMbid)
      setVariants(list)
      if (v >= 0) {
        setSel({ variant: v, edition: Math.max(0, (list[v].editions || []).findIndex(x => x.releaseMbid === curEdition)) })
      } else {
        setSel(x => ({ variant: Math.min(x.variant, list.length - 1), edition: 0 }))
      }
      const owned = list.findIndex(x => x.owned)
      if (owned >= 0) setHeld({ variant: owned, exact: true })
    } catch { /* what is on the page stays */ }
  }
  function rereadAfterJob() {
    if (!mounted.current) return
    const once = () => { if (mounted.current) { rereadIndex(); rereadReleases() } }
    rereadTimers.current.forEach(clearTimeout)
    once()
    rereadTimers.current = [6000, 20000].map(ms => setTimeout(once, ms))
  }
  const ov = useAlbumOverride({
    rgid, indexIds, variants, sel, tint: incomplete, onChanged: rereadAfterJob,
    openAlbum: rg => goArtist(artist.id, rg),
  })
  // Read presence by the albums as they are listed now, once a job has moved
  // one — and by the index's ids while the page holds none of its own (after
  // an undo the album is found again only when the index names its id: until
  // then the tracklist kept its rows and lost every "In library").
  const liveAlbumIds = ov.moved && ov.albumIds.length ? ov.albumIds.join(',') : albumIds

  // On arrival the chips select the release the album itself names (V7). The
  // selection is first taken from the release list's `owned` mark, which is
  // lb-bot's index — behind the files for a while after a retag — and the
  // page then arrived on the old chip with the block open, offering to change
  // the album back. One album, a release among the chips, and no chip picked
  // by the user on this visit: a pick is never overridden.
  useEffect(() => {
    if (!ov.only || pickedRef.current || !variants) return
    const own = selectionOfRelease(variants, ov.only)
    if (!own) return
    if (own.variant !== sel.variant || own.edition !== sel.edition) setSel(own)
    // The album has said which release it is: nothing is left to work out.
    setHeld(h => (h?.exact && h.variant === own.variant ? h : { variant: own.variant, exact: true }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ov.only, variants])

  // The release whose recordings the files are tagged with, so presence lines
  // up exactly: the one the album carries when it is this chip's, else the
  // library's own pressing where the server names it.
  const trackReleaseMbid = ov.carriedIn(sel.variant) || (variant?.owned && variant.ownedReleaseMbid) || variant?.releaseMbid || ''
  // The library has this album, it has more than one release, and which one is
  // still being worked out: wait for that rather than show the first release's
  // tracklist and then swap it. Never for long: the lookup is two small
  // requests, and a timer ends the wait if they do not come back.
  const holding = !!variants && variants.length > 1 && inLibrary && !held && !reviewAsked
    && !!headerTitle && !pickedRef.current
  useEffect(() => {
    if (!holding) return
    const t = setTimeout(() => setReviewAsked(true), 6000)
    return () => clearTimeout(t)
  }, [holding])
  // What is showing stays, dimmed, until the tracklist that replaces it is
  // here: switching release used to blank the list to skeleton rows and back.
  const wantKey = variants === null ? null : `${trackReleaseMbid}|${liveAlbumIds}|${groupId}`
  useEffect(() => {
    if (variants === null || holding) return
    if (!trackReleaseMbid) { setTracks({ key: wantKey, rows: [], presenceKnown: false, reason: 'no-release' }); return }
    let dead = false
    const q = new URLSearchParams({ release_mbid: trackReleaseMbid })
    if (liveAlbumIds) q.set('album_ids', liveAlbumIds)
    else if (groupId) q.set('group_id', groupId)
    api('/api/album/tracklist?' + q, { timeoutMs: LOOKUP_TIMEOUT_MS })
      .then(r => !dead && setTracks({ key: wantKey, rows: r.tracks || [], presenceKnown: !!r.presenceKnown }))
      .catch(e => !dead && setTracks({ key: wantKey, rows: [], presenceKnown: false, reason: e.timeout ? 'timeout' : 'error', error: e.message }))
    return () => { dead = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [variants, trackReleaseMbid, liveAlbumIds, groupId, holding])
  const tracksLoading = tracks !== null && tracks.key !== wantKey

  // Arriving by a gap's "Wrong edition?": the chips are what was asked for.
  const editionRef = useRef(null)
  const broughtRef = useRef(false)
  useEffect(() => {
    if (!toEdition || broughtRef.current || variants === null || tracks === null || !editionRef.current) return
    broughtRef.current = true
    const calm = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    editionRef.current.scrollIntoView({ block: 'center', behavior: calm ? 'auto' : 'smooth' })
  }, [toEdition, variants, tracks])

  const override = releaseOverride(release, variant, displayArtist)
  const overrideKey = JSON.stringify(override)
  // lb-bot keeps the list it answers for a release (fifteen minutes): asked
  // again it answers that list, with its age, and searches nothing. "Search
  // again" (`refresh`) is the new search: what it finds is added to the list,
  // and nothing is taken off the page — not when it fails either. A click on a
  // source is for the folder that was clicked: lb-bot takes it out of this
  // list and searches nothing.
  async function findSources(refresh = false) {
    const mine = ++listReq.current
    const q = new URLSearchParams({ rgid })
    for (const [k, v] of Object.entries(JSON.parse(overrideKey))) if (v) q.set(k, String(v))
    if (refresh) {
      q.set('refresh', '1')
      setAgain(true)
    } else {
      setSources('loading')
      setSrcMeta(null)
    }
    try {
      const r = await api('/api/album/sources?' + q)
      if (listReq.current !== mine) return
      setSources(r.sources || [])
      setSrcMeta({ searchedAt: r.searchedAt, serverTime: r.serverTime, receivedMs: Date.now(),
                   cached: !!r.cached, sampled: !!r.sampled })
      setNow(Date.now())
    } catch (e) {
      if (listReq.current !== mine) return
      if (!refresh) setSources(null)
      pushToast(`Source search failed: ${e.message}`, 'error')
    } finally {
      if (listReq.current === mine) setAgain(false)
    }
  }
  // A different edition is a different search.
  useEffect(() => { listReq.current++; setSources(null); setSrcMeta(null); setAgain(false) }, [overrideKey])
  // The list's age is read again every half minute while the list is on the
  // page: nothing else re-renders it, and "just now" would stand over a list
  // lb-bot has since let go of.
  const listShown = Array.isArray(sources)
  useEffect(() => {
    if (!listShown) return
    const t = setInterval(() => setNow(Date.now()), 30_000)
    return () => clearInterval(t)
  }, [listShown])
  // Arriving by "Find sources" starts the search once the album is known.
  const autoRef = useRef(false)
  useEffect(() => {
    if (!autoPick || autoRef.current || status !== 'missing' || !release || variants === null || requested) return
    autoRef.current = true
    findSources()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoPick, status, release, variants, requested])

  // `say` words the toast from the route's answer; `asked` is the peer a click
  // named.
  async function download(extra, say, asked = '') {
    setStarting(true)
    // A request of its own: followed afresh, and its first look at the ledger
    // is not believed when it reads an ended fill (`useRequest`).
    setReq(q => ({ n: q.n + 1, asked }))
    // Known before the route answers, so the progress row opens on the click.
    if (variant?.releaseMbid) setRequested(variant.releaseMbid)
    try {
      const r = await action('/api/album/download', { rgid, ...override, ...extra })
      // A fill that was already running is not this click's.
      if (r?.existing) setReq(q => ({ ...q, asked: '' }))
      setRequested(r?.resolved?.release_mbid || r?.status?.releaseMbid || variant?.releaseMbid || '')
      const text = say ? say(r) : ''
      if (text) pushToast(text)
    } catch (e) {
      setRequested('')
      pushToast(`Download failed: ${e.message}`, 'error')
    } finally {
      setStarting(false)
    }
  }
  // A click downloads the source that was clicked. What is true when the
  // route answers is that the album was asked for from this peer — or that a
  // download was already running; whether it is queued, and from whom, is the
  // request's own row (`askedText`, never "Queued from").
  // `allFiles` is "Download all": every audio file of the folder, not only
  // the album's tracks. The answer echoes whether lb-bot took it, and the
  // toast says "all files" only on that echo.
  const useSource = (src, allFiles = false) => download(
    { sourceUsername: src.peer, sourceFolder: src.folder, ...(allFiles ? { allFiles: true } : {}) },
    r => askedText(r, src.peer, allFiles ? { allFiles: true, fileCount: src.fileCount } : null),
    src.peer)

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
  // How many tracks, and how many of them the library has: counted off the
  // tracklist on screen whenever it says which are present, so the number above
  // the list cannot disagree with its rows. (The index's own count lags a fresh
  // fill: it read 0/12 above twelve rows marked "In library".)
  const listed = tracks?.rows?.length ? tracks.rows : null
  const totalN = listed ? listed.length : (release?.total || trackCount || 0)
  // Without a tracklist that says, a scanned `complete` is all of them; a
  // row lb-bot marked `present` after a fill has no count of its own.
  const presentN = listed && tracks.presenceKnown ? listed.filter(r => r.present === true).length
    : !countsMeasured(indexStatus) ? null
    : status === 'complete' ? totalN
    : (release.present ?? null)
  const rgCover = caaCover(rgid)
  const pt = groupMeta.primaryType || ''
  const typeWord = pt && pt.toLowerCase() !== 'album' ? pt[0].toUpperCase() + pt.slice(1) : ''
  const year = release?.year || groupMeta.year

  const reqLive = !!request && ['searching', 'queued', 'downloading', 'placing', 'placed'].includes(request.state)
  const reqFailed = request?.state === 'failed'
  const reqPending = !!requested && (!request || request.state === 'unknown')
  const searching = sources === 'loading'
  const found = Array.isArray(sources) ? sources : null
  const filtered = found ? filterSources(found, srcFilters) : []
  const listAge = sourcesAgeText(srcMeta, now)
  const showActs = status === 'missing' && !reqLive && !reqPending && !reqFailed
  const have = !totalN ? '' : presentN == null ? `${totalN} tracks` : `${presentN} of ${totalN} tracks`

  // ── Which release the library holds, for a server that does not say ───────
  // /api/album/releases marks it (`owned`). A build from before that does not,
  // so the page works it out: from the review, which records the release each
  // library album is tagged as, and failing that from which release's
  // tracklist the library fills completely.
  const adopt = mbid => {
    if (!mbid || pickedRef.current || !variants) return false
    for (let v = 0; v < variants.length; v++) {
      const e = (variants[v].editions || []).findIndex(x => x.releaseMbid === mbid)
      if (variants[v].releaseMbid === mbid || e >= 0) {
        setSel({ variant: v, edition: Math.max(0, e) })
        setHeld({ variant: v, exact: true })
        return true
      }
    }
    return false
  }
  useEffect(() => {
    if (!variants?.length || held || reviewAsked || !inLibrary || !headerTitle) return
    if (incomplete && !gap) return          // the gap is on its way and knows
    let dead = false
    ;(async () => {
      try {
        const ids = albumIds ? albumIds.split(',') : []
        let mbid = gap?.canonicalMbid || gap?.releaseMbid
          || (state.gaps?.items || []).find(g => ids.includes(g.albumId))?.releaseMbid
        if (!mbid && ids.length) {
          const lib = await api('/api/library?' + new URLSearchParams({ filter: 'all', q: headerTitle, page: 0, sort: '' }))
          const row = (lib.items || []).find(x => ids.includes(x.id) && x.groupId)
          if (row) {
            const g = await api(`/api/gaps/${row.groupId}`)
            mbid = g.canonicalMbid || g.releaseMbid
          }
        }
        if (!dead) adopt(mbid)
      } catch { /* the page still works, on the first release */ }
      if (!dead) setReviewAsked(true)
    })()
    return () => { dead = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [variants, held, reviewAsked, inLibrary, headerTitle, incomplete, gap, albumIds])

  // Last resort, for a complete album the review has no record of: if the
  // release on screen leaves rows missing, look for the release the library
  // fills completely. Marked as inferred, not as known.
  const inferredRef = useRef(false)
  useEffect(() => {
    if (inferredRef.current || pickedRef.current || held || !reviewAsked) return
    if (status !== 'complete' || !albumIds || !variants || variants.length < 2) return
    if (!tracks || tracksLoading || !tracks.presenceKnown || presentN == null || presentN >= totalN) return
    inferredRef.current = true
    let dead = false
    const others = variants.map((v, i) => ({ v, i })).filter(x => x.i !== sel.variant && x.v.releaseMbid)
      .sort((a, b) => Math.abs(a.v.trackCount - presentN) - Math.abs(b.v.trackCount - presentN) || a.i - b.i)
      .slice(0, 3)
    ;(async () => {
      for (const { v, i } of others) {
        try {
          const r = await api('/api/album/tracklist?' + new URLSearchParams({ release_mbid: v.releaseMbid, album_ids: albumIds }),
            { timeoutMs: LOOKUP_TIMEOUT_MS })
          if (dead || pickedRef.current) return
          const rows = r.tracks || []
          if (r.presenceKnown && rows.length && rows.every(t => t.present === true)) {
            setSel({ variant: i, edition: 0 })
            setHeld({ variant: i, exact: false })
            return
          }
        } catch { /* try the next */ }
      }
    })()
    return () => { dead = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [held, reviewAsked, status, albumIds, variants, tracks, tracksLoading, presentN, totalN])

  return (
    <Page title={headerTitle || 'Album'} crumb={typeWord || 'Album'}
      fallbackBack={{ label: displayArtist || 'Artist', onBack: () => goArtist(artist.id) }}>
      <div style={{ maxWidth: 980 }}>
        <div className="hero">
          <div style={status === 'missing' ? { opacity: 0.8 } : { width: 132 }}>
            {/* The cover lb-bot wrote for this album, when it wrote one; else the edition's. */}
            <Cover url={ov.heroCover || edition?.coverUrl || rgCover}
              fallbackUrl={ov.heroCover ? (edition?.coverUrl || rgCover) : rgCover}
              name={headerTitle || '?'} size={132} />
            {/* For an album the library holds: its cover is the library's to change. */}
            {/* While lb-bot is changing the album's files it would refuse (F6):
                the link then cannot be pressed — and keeps its place (G1). This
                column sets the hero's height, so a line taken out of it moves
                everything under the hero. `aria-disabled`, not `disabled`: it
                keeps the keyboard's place, and its reason can be reached. */}
            {ov.has && (
              <>
                <button ref={ov.coverLink} className="link plain fadein mt-1.5 block w-full" style={{ textAlign: 'center' }}
                  aria-expanded={ov.coverOpen} aria-disabled={ov.coverOffer.pressable ? undefined : true}
                  aria-describedby={ov.coverOffer.why ? 'change-cover-why' : undefined}
                  title={ov.coverOffer.why || undefined} onClick={ov.openCover}>
                  Change cover
                </button>
                {ov.coverOffer.why && <span id="change-cover-why" className="sr-only">{ov.coverOffer.why}</span>}
              </>
            )}
          </div>
          <div className="grow">
            {!headerTitle ? (
              <><Skeleton className="mb-2.5 h-6 w-3/5" /><Skeleton className="mb-2 h-3 w-2/5" /><Skeleton className="h-3 w-1/4" /></>
            ) : (
              <div className="fadein">
                <h1>{headerTitle}</h1>
                <p className="mt-1">
                  <button className="link" onClick={() => goArtist(artist.id)}>{displayArtist}</button>
                  <span className="muted">{[year, typeWord].filter(Boolean).map(x => ` · ${x}`).join('')}</span>
                </p>
                <p className="mt-2 flex flex-wrap items-center gap-x-2.5 gap-y-1">
                  {reqLive ? <St tone="run">{request.state === 'downloading' ? `Downloading ${request.percent || 0}%` : STATUS[request.state]?.word}</St>
                    : status ? <AlbumStateChip state={status}
                        count={status === 'incomplete' ? (presentN != null && totalN ? totalN - presentN : missingCount) : null} />
                    : <span className="st">Checking your library…</span>}
                  {have && <span className="muted num">{have}</span>}
                </p>
                {/* What lb-bot changed about this album, with its undo — or the job, while one runs. */}
                <Roll open={ov.lines.length > 0}>
                  {ov.lines.map(({ key, ...l }) => <ChangedLine key={key} {...l} />)}
                </Roll>
              </div>
            )}

            {(addError || (discoError && !release)) && (
              <p className="s mt-2">
                {addError ? `Could not add this release to the index: ${addError}` : `Could not read this artist’s index: ${discoError}`}
                {' '}<button className="link" onClick={retryIndexAdd}>Try again</button>
              </p>
            )}

            {status === 'untagged' && (
              <p className="why !mb-0">
                lb-bot can’t check this album against MusicBrainz because its files carry no MusicBrainz tags. Tag them
                (MusicBrainz Picard does it), then rescan the artist.
                {ov.has ? ' Or pick the edition under Tracks: lb-bot can write the tags itself.' : ''}
              </p>
            )}
          </div>
        </div>
        <ChangeCover {...ov.changeCover} />
        {/* What a job is doing, said once when its state changes (not with
            every count): the line under the title and the card show it. */}
        <span className="sr-only" role="status">{ov.spoken}</span>

        {(meta === null || meta.found) && (
          <>
            <Grp>About<span className="note">{meta?.wikidataDescription}</span></Grp>
            {meta === null ? <AboutLoading /> : <About meta={meta} />}
          </>
        )}

        <SimilarAlbums artistMbid={artist.mbid || ''} artistName={displayArtist} rgid={rgid} />

        {/* What to do about this album sits here: under what it is, above its
            tracks. */}
        {status === 'missing' && (
          <div className="next">
            <Grp>Next step<span className="note">
              {reqLive || reqPending ? 'file the album into the library'
                : reqFailed ? 'the last try failed'
                : searching ? 'searching Soulseek'
                : found ? 'pick a source below' : 'get the album'}
            </span></Grp>
            <Roll open={showActs}>
              <div className="acts" style={{ paddingTop: 10 }}>
                <button className={found ? '' : 'primary'} disabled={searching || starting || again} aria-busy={searching || again}
                  title={found ? 'Searches Soulseek again and adds what it finds to this list' : undefined}
                  onClick={() => findSources(!!found)}>
                  {searching ? 'Searching Soulseek…' : again ? 'Searching again…' : found ? 'Search again' : 'Find sources for the album'}
                </button>
                <button disabled={searching || starting} onClick={() => download({})}
                  title="Let lb-bot pick the best-ranked source and download it">
                  Download the best match
                </button>
                <button className={wishlisted ? 'done-state' : ''} disabled={wishlisted} aria-pressed={wishlisted} onClick={addToWishlist}
                  title="Nobody sharing it? lb-bot searches the wishlist again every few hours">
                  {wishlisted ? 'On the wishlist' : 'Add to wishlist'}
                </button>
              </div>
            </Roll>
          </div>
        )}
        {reqFailed && (
          <RequestFailure f={request}
            onRetried={r => {
              setReq(q => ({ n: q.n + 1, asked: '' }))
              setRequested(r?.resolved?.release_mbid || request.releaseMbid)
            }}
            extra={<button onClick={() => { setRequested(''); findSources() }}>Pick a source</button>} />
        )}
        <Roll open={reqLive || reqPending}>
          <Grp>Source<span className="note">{request ? request.state : 'starting'}</span></Grp>
          <RequestProgress f={request} asked={req.asked} />
        </Roll>

        <Roll open={showActs && searching}>
          <Grp>Sources<span className="note">searching peers, this can take up to a minute</span></Grp>
          <SkeletonRows rows={3} />
        </Roll>
        <Roll open={showActs && !!found}>
          <Grp n={found?.length ?? ''} right={found?.length > 0 && <SourceFilters value={srcFilters} onChange={setSrcFilters} />}>
            Sources<span className="note">ranked by <button className="link plain underline"
              onClick={() => navigate('system', 'sources')}>your preferences</button>{listAge ? ` · ${listAge}` : ''}</span>
          </Grp>
          {found && srcMeta?.sampled && <p className="muted mb-2">{SAMPLED_NOTE}</p>}
          {!found?.length ? (
            <p className="why quiet">No peer is sharing this album right now. Add it to the wishlist and lb-bot will look again every few hours.</p>
          ) : !filtered.length ? (
            <p className="muted">No source matches these filters.</p>
          ) : (
            <div className="rows">
              {/* Keyed by peer and folder: "Search again" re-ranks the list, so a
                  row's position is not what it is. */}
              {filtered.map(s => (
                <SourceRow key={sourceKey(s)} src={s} busy={starting} primary={s.recommended || (s.rank ?? s.id) === (found[0].rank ?? found[0].id)}
                  onUse={() => useSource(s)}
                  note={extraFilesText(s)} secondaryLabel={downloadAllLabel(s)}
                  onSecondary={() => useSource(s, true)} />
              ))}
            </div>
          )}
        </Roll>

        {incomplete && (
          gap
            ? <GapDetail key={groupId} bare flowOnly row={gap} detail={gap} transfers={state.transfers}
                autoFind={autoPick} onDetail={setGap} />
            : <p className="muted mt-4">Loading what is missing…</p>
        )}


        {/* One tracklist. For an album with gaps it is still the whole
            tracklist, and each missing row is the gap's own: its state, its
            error, and the ways to repair it by hand. */}
        {/* With no tracklist to show for an album with gaps, the list is its
            missing tracks alone, and the heading says so. */}
        {incomplete && gap && tracks && !listed
          ? <Grp n={(gap.tracks || []).filter(t => t.state !== 'present').length || ''}>Missing tracks</Grp>
          : <Grp n={tracks === null || !totalN ? '' : presentN == null ? totalN : `${presentN}/${totalN}`}
              right={ov.has && (
                <button className="link plain underline fadein" title="Say which MusicBrainz release your files are"
                  onClick={ov.openPaste}>Edition not listed?</button>
              )}>Tracks</Grp>}
        <div ref={editionRef}>
          {/* The chip the files carry is marked from what the album itself says
              once that is read; until then, from what the release list marks. */}
          <EditionChips variants={variants} variantIdx={sel.variant} editionIdx={sel.edition}
            heldVariant={ov.known ? ov.heldVariants : (held?.exact ? held.variant : -1)} onPick={pick} />
          <EditionBlock {...ov.block}
            focusChip={() => editionRef.current?.querySelector?.('button.chip[aria-pressed="true"]')?.focus({ preventScroll: true })} />
        </div>
        {tracks === null ? (
          <TrackList loading rows={Math.max(4, Math.min(trackCount || 8, 14))} />
        ) : (
          <div className={tracksLoading ? 'dimmed' : 'undimmed'} aria-busy={tracksLoading || undefined}>
            <div key={tracks.key} className="fadein">
              {!tracks.rows.length && (
                <p className="why quiet">
                  {{
                    timeout: 'MusicBrainz did not answer within 10 seconds. The tracklist is only a reference. Finding sources and downloading still work.',
                    error: `Could not load the tracklist${tracks.error ? `: ${tracks.error}` : ''}. Finding sources and downloading still work.`,
                    'no-release': 'MusicBrainz lists no release for this album, so there is no tracklist to show. You can still search Soulseek for it.',
                  }[tracks.reason] || 'MusicBrainz has no track data for this edition.'}
                </p>
              )}
              {incomplete && gap
                // Without a reference tracklist this is the missing tracks alone.
                ? <GapTracks detail={gap} reference={tracks.rows.length ? tracks : null} />
                : tracks.rows.length > 0 && (
                    <TrackList tracks={tracks.rows} presenceKnown={!!tracks.presenceKnown}
                      absentWord={status === 'missing' ? 'Not in library' : 'Missing'} />
                  )}
            </div>
          </div>
        )}
        {incomplete && gap && <GapFoot detail={gap} bare />}

      </div>
    </Page>
  )
}

// An album known only by its release-group: ask MusicBrainz who made it, then
// replace this route with the real album page.
function AlbumResolver({ rgid, param }) {
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
        replaceRoute('album', id, rgid, param)
      })
      .catch(e => !dead && setFailed(e.message))
    return () => { dead = true }
  }, [artists, rgid, param])
  return (
    <Page title="Album" sub={failed ? undefined : 'Finding the album…'}>
      {failed
        ? <EmptyState title="Couldn’t open this album" hint={failed} />
        : <SkeletonRows rows={4} />}
    </Page>
  )
}

// ── Roots ────────────────────────────────────────────────────────────────────
// Synthetic artists for `mb:<mbid>` routes, remembered for the session so the
// display name upgrades once a scan or the meta lookup names them.
const externalArtists = new Map()

function useRouteArtist(route, artistId, rgid, pickParam) {
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
    if (!ownedForMb) return
    if (route === 'album') replaceRoute('album', ownedForMb.id, rgid, pickParam)
    else replaceRoute('artist', ownedForMb.id)
  }, [ownedForMb, route, rgid, pickParam])

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

  return { artists, artistsErr, artist, ownedForMb }
}

function ArtistMissing({ title, hint }) {
  return (
    <Page title={title} fallbackBack={{ label: 'Library', onBack: () => navigate('library', 'artists') }}>
      <EmptyState title="Artist not found" hint={hint || 'This link doesn’t match an artist in your library anymore.'}>
        <button onClick={() => navigate('library', 'artists')}>All artists</button>
      </EmptyState>
    </Page>
  )
}

export function ArtistPage() {
  const { state } = useApp()
  const [artistId] = state.routeParams
  const { artists, artistsErr, artist, ownedForMb } = useRouteArtist('artist', artistId)
  if (!artistId) return <ArtistMissing title="Artist" hint="No artist was named." />
  if (artists === null || ownedForMb) return <Page title="Artist" sub="Loading…"><SkeletonRows /></Page>
  if (!artist) return <ArtistMissing title="Artist" hint={artistsErr} />
  return <DiscographyView key={artist.id} artist={artist} />
}

export function AlbumPage() {
  const { state } = useApp()
  const [artistId, rgid, pickParam] = state.routeParams
  const pick = pickParam === 'sources'
  const known = pickParam === 'sources' || pickParam === 'edition' ? pickParam : undefined
  const { artists, artistsErr, artist, ownedForMb } = useRouteArtist('album', artistId, rgid, pickParam)
  if (!rgid) return <ArtistMissing title="Album" hint="No album was named." />
  if (artistId === '-') return <AlbumResolver rgid={rgid} param={known} />
  if (artists === null || ownedForMb) return <Page title="Album" sub="Loading…"><SkeletonRows rows={4} /></Page>
  if (!artist) return <ArtistMissing title="Album" hint={artistsErr} />
  return <AlbumDetail key={`${artist.id}-${rgid}`} artist={artist} rgid={rgid} autoPick={pick}
    toEdition={pickParam === 'edition'} />
}
