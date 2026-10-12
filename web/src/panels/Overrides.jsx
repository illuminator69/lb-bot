// System › Overrides (I-029, U3): everything lb-bot changed at the user's
// word, each with its undo — album editions, covers, artist matches, name
// merges. One read of `GET /api/overrides`; a list lb-bot left out "could not
// be read", an empty one is not shown, and with nothing at all the tab says
// where a change is made from.
//
// What a row says is lib/merges.js (`listRowView`, with its matrix). A row
// opens its page only by an id that is live NOW (`rowOpens`): an album by its
// own answer — a retag gave it another id than the row was made with — an
// artist by the artist list. An undo runs in its row, through the one job
// watch: the state word, the job's sentence, the whole job's meter. The row
// stays until the job ends, then folds away with one toast; an undo that
// failed stays, with lb-bot's own lines.
import { useEffect, useMemo, useRef, useState } from 'react'
import { useApp, navigate } from '../App.jsx'
import { api, post } from '../lib/api.js'
import { scanStart } from '../lib/artistMatch.js'
import { jobs, useJobs } from '../lib/jobs.js'
import { artistsTouched, heldRows, holdRow, listRowView, listedNoMore, nb, overridesGroups, rowKey, rowOpens, rowsShown, undoEnded } from '../lib/merges.js'
import { coverThumbUrl, notSaid, refusalView, startedId } from '../lib/overrides.js'
import Cover from '../components/Cover.jsx'
import { Grp, Meter, Roll, St, useRevealOn } from '../components/Page.jsx'
import { EmptyState, Notice, Skeleton } from '../components/ui.jsx'
import { forgetDiscography, loadArtists } from './Artist.jsx'
import { JobLine } from './AlbumOverride.jsx'
import { useOverridesRead } from './ArtistMatch.jsx'

const enc = encodeURIComponent
const FOLD_MS = 340                       // a Roll's fold, and a little
const READ_TIMEOUT_MS = 20_000
const caaCover = rgid => `https://coverartarchive.org/release-group/${rgid}/front-250`
const WRAP = { whiteSpace: 'normal', overflowWrap: 'anywhere' }
const UNDO = {
  album: { path: '/api/album/override/remove', body: row => ({ id: row.id }), op: 'remove' },
  merge: { path: '/api/artist/merge/undo', body: row => ({ id: row.id }), op: 'unmerge' },
}

// ── One row ──────────────────────────────────────────────────────────────────
//   view     lib `listRowView`'s answer: title, to, sub, link, v, undo
//   opens    () → the row's page, when its id is live; absent for a plain row
//   refusal  { text, retry } — lb-bot refused the undo
export function ListedRow({ art, view, opens, busy = false, refusal = null, onUndo, onDismiss }) {
  const { title, to, sub, link, v, undo } = view
  const [more, setMore] = useState(false)
  const problems = v?.problems || []
  const box = useRef(null)
  // What a press opens comes into view: lb-bot's lines, and — some time after
  // the press — the job's own line or a refusal.
  useRevealOn(box, [more && 'problems', refusal && '~refused', v && (v.bar || v.line) && '~job'].filter(Boolean))
  // The button that was pressed goes when its job begins (the job is the row
  // now): the keyboard stays on the row instead of falling off the page.
  const hadUndo = useRef(!!undo)
  useEffect(() => {
    const had = hadUndo.current
    hadUndo.current = !!undo
    if (!had || undo || typeof document === 'undefined') return
    const at = document.activeElement
    if (at && at !== document.body && !box.current?.contains(at)) return
    box.current?.focus({ preventScroll: true })
  }, [undo])
  return (
    <div className="row stack" ref={box} tabIndex={-1} style={{ outline: 'none' }}>
      {art}
      <div className="grow">
        <div className="t" style={WRAP}>
          {opens ? <button className="link plain" style={{ textAlign: 'left' }} onClick={opens}>{nb(title)}</button> : nb(title)}
          {to ? (
            <>
              {'\u00a0→\u00a0'}
              {to.naming ? <Skeleton className="inline-block h-3 w-28 align-middle" />
                : <a className="link-inline" href={to.href} target="_blank" rel="noreferrer">{to.text}</a>}
            </>
          ) : null}
        </div>
        <div className="s" style={WRAP}>
          {nb(sub)}
          {link ? <>{sub ? '\u00a0·\u00a0' : ''}<a className="link-inline" href={link.href} target="_blank" rel="noreferrer">{link.text}</a></> : null}
        </div>
        {/* The job, in the row: its sentence and the whole job's meter. */}
        <Roll open={!!v && (v.bar || !!v.line)}>
          <div className="pt-1.5">
            {v?.line ? <div className="s" style={WRAP}><JobLine line={v.line} /></div> : null}
            <Roll open={!!v?.bar}><div className="pt-1.5"><Meter value={v?.percent || 0} /></div></Roll>
          </div>
        </Roll>
        {/* lb-bot's own lines, behind their count. */}
        <Roll open={problems.length > 0}>
          <div className="pt-1">
            <button className="link" aria-expanded={more} onClick={() => setMore(x => !x)}>{v?.problemsHeading}</button>
            <Roll open={more}>
              <div className="pt-1">
                {problems.map((p, i) => (
                  <div key={i} className="s" style={WRAP}>{p.title ? `${p.title} — ${p.reason}` : p.reason}</div>
                ))}
              </div>
            </Roll>
          </div>
        </Roll>
        <Roll open={!!refusal}>
          <div className="pt-2">
            <Notice tone="danger" role="alert" actions={<>
              {refusal?.retry && <button className="sm" disabled={busy} onClick={onUndo}>Try again</button>}
              <button className="sm quiet" onClick={onDismiss}>Dismiss</button>
            </>}>{refusal?.text}</Notice>
          </div>
        </Roll>
      </div>
      {v?.word ? <St tone={v.tone}>{v.word}</St> : null}
      {/* Every row's button says "Undo": a screen reader is told which row's. */}
      {undo ? <button className="sm" disabled={busy} aria-busy={busy} aria-label={undo.says} onClick={onUndo}>{undo.label}</button> : null}
    </div>
  )
}

// The size of a short list: a heading and three rows with art.
const ListSkeleton = () => (
  <div className="fadein" aria-busy="true" aria-label="Reading what lb-bot changed">
    <Skeleton className="h-2.5 w-28" />
    <div className="rows mt-2.5" aria-hidden="true">
      {[0, 1, 2].map(i => (
        <div key={i} className="row">
          <Skeleton style={{ width: 40, height: 40, flex: 'none' }} />
          <div className="grow"><Skeleton className="h-3" style={{ width: `${46 - i * 9}%` }} /><Skeleton className="mt-2 h-2.5 w-1/5" /></div>
        </div>
      ))}
    </div>
  </div>
)

// ── The tab ──────────────────────────────────────────────────────────────────
export default function OverridesTab() {
  const { pushToast } = useApp()
  const overrides = useOverridesRead()
  const data = overrides.state.data
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => { const t = setInterval(() => setNow(Date.now() / 1000), 30_000); return () => clearInterval(t) }, [])

  const groups = useMemo(() => overridesGroups(data), [data])
  const albumRows = useMemo(() => (Array.isArray(data?.albums) ? data.albums : []), [data])
  const mergeRows = useMemo(() => (Array.isArray(data?.merges) ? data.merges : []), [data])
  const artistRows = useMemo(() => (Array.isArray(data?.artists) ? data.artists : []), [data])

  // ── what is live now, and what MusicBrainz calls a matched artist ──
  // Asked one at a time, in the list's order: six rows are six small
  // requests, sixty are not sixty at once.
  const chain = useRef(Promise.resolve())
  const later = ask => { chain.current = chain.current.then(() => (alive.current ? ask() : undefined)).catch(() => {}) }
  // The artist list: which artist ids are live, and their portraits.
  const [artists, setArtists] = useState(null)
  useEffect(() => {
    if (!data) return undefined
    let dead = false
    loadArtists().then(list => { if (!dead) setArtists(list) }).catch(() => {})
    return () => { dead = true }
  }, [data])
  const liveArtists = useMemo(() => (artists ? new Set(artists.map(a => a.id)) : null), [artists])
  const art = useMemo(() => new Map((artists || []).map(a => [a.id, a.coverUrl])), [artists])
  // An album, by the id its row carries — which may be dead: `{albumId, rgid}`
  // as lb-bot describes the album at this moment, null when Navidrome lists
  // that id no more. Kept by row AND id: a row whose id moved is asked again.
  const [albumNow, setAlbumNow] = useState({})
  const askedAlbums = useRef(new Set())
  const [recheck, setRecheck] = useState(0)
  const albumKey = albumRows.map(r => `${r.id}:${r.albumId}`).join(',')
  useEffect(() => {
    for (const r of albumRows) {
      const k = `${r.id}:${r.albumId}`
      if (!r.albumId || askedAlbums.current.has(k)) continue
      askedAlbums.current.add(k)
      later(() => api(`/api/album/override?album_id=${enc(r.albumId)}`, { timeoutMs: READ_TIMEOUT_MS })
        .then(a => { if (alive.current) setAlbumNow(m => ({ ...m, [k]: { albumId: a.albumId || '', rgid: a.rgid || '' } })) })
        // Only "no such album" says the id is dead; lb-bot not answering says nothing.
        .catch(e => {
          if (alive.current && e.status === 404) setAlbumNow(m => ({ ...m, [k]: null }))
          else askedAlbums.current.delete(k)
        }))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [albumKey, recheck])
  // A matched artist's own name: neither list carries it (`name` is the library's).
  const [mbNames, setMbNames] = useState({})                // mbid → null (asked) | { name }
  const artistKey = artistRows.map(r => r.mbid).join(',')
  useEffect(() => {
    for (const r of artistRows) {
      if (!r.mbid || mbNames[r.mbid] !== undefined) continue
      setMbNames(m => (m[r.mbid] !== undefined ? m : { ...m, [r.mbid]: null }))
      later(() => api(`/api/meta/artist?mbid=${enc(r.mbid)}`, { timeoutMs: READ_TIMEOUT_MS })
        .then(meta => { if (alive.current) setMbNames(m => ({ ...m, [r.mbid]: { name: String(meta?.name || '') } })) })
        // Not named: the row still links out, and says nothing it does not know.
        .catch(() => { if (alive.current) setMbNames(m => ({ ...m, [r.mbid]: { name: '' } })) }))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [artistKey])

  // ── the jobs: the watch's.
  // The rows an undo is running for: kept while it runs, so that they are
  // still here — to say how it ended — when lb-bot lists them no more; each
  // with the place it had in its group when it was first held (`holdRow`), so
  // that it folds away where it stood.
  const [held, setHeld] = useState({})                      // key → { kind, group, row, at, leaving }
  const heldOf = kind => Object.values(held).filter(h => h.kind === kind)
  const ids = (rows, kind, of) => [...new Set([...rows.map(of), ...heldOf(kind).map(h => of(h.row))])]
  const albumJobs = useJobs('album', ids(albumRows, 'album', r => r.id))
  const mergeJobs = useJobs('merge', ids(mergeRows, 'merge', r => r.id))
  const scanJobs = useJobs('scan', heldOf('artist').map(h => h.row.ndArtistId))
  const jobOf = key => {
    const [kind, id] = [key.slice(0, key.indexOf(':')), key.slice(key.indexOf(':') + 1)]
    return (kind === 'album' ? albumJobs : kind === 'merge' ? mergeJobs : scanJobs)[id] || null
  }
  const watched = jobs.version()
  useEffect(() => {
    setHeld(h => heldRows(h, groups, jobOf))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watched, groups])

  // The row folds away, with one toast that times out.
  const leave = (key, toast) => {
    if (toast) pushToast(toast.text, toast.level)
    setHeld(h => (h[key] ? { ...h, [key]: { ...h[key], leaving: true } } : h))
    setTimeout(() => { if (alive.current) setHeld(h => { const { [key]: _gone, ...rest } = h; return rest }) }, FOLD_MS)
  }
  // Every answer of lb-bot's list — on arrival, after Try again, at a job's
  // end: a row it lists as running is followed, whoever started it, and one
  // this tab had given up on is taken up again (a silent job is not settled
  // by itself; a fresh answer that still lists it running may be followed).
  // A job this tab gave up on whose row lb-bot now lists at rest is let go of
  // — the list says how it ended — and a row held for an undo that went
  // silent, which lb-bot lists no more, was undone after all. "Lists no more"
  // is said only by an answer that gave that row's own list (F4): one that
  // left `albums` or `merges` out says nothing of the row, and it stays held.
  useEffect(() => {
    if (!data) return
    // A job that went silent and that this answer says is over — at rest, or
    // undone: nobody saw it end, so whose releases it changed is not asked;
    // what the session kept of the artists' discographies is let go of (F1).
    let over = false
    for (const [kind, rows] of [['album', albumRows], ['merge', mergeRows]]) {
      for (const r of rows) {
        if (jobs.adopt(kind, r)) continue
        if (jobs.get(kind, r.id)?.phase !== 'timeout') continue
        jobs.dismiss(kind, r.id)
        over = true
        // (…and its row is no longer one an undo holds: it is lb-bot's to list.)
        const key = rowKey(kind, r)
        setHeld(h => { if (!h[key]) return h; const { [key]: _rest, ...others } = h; return others })
      }
    }
    for (const [key, h] of Object.entries(held)) {
      if (h.leaving || !UNDO[h.kind] || !listedNoMore(data, h.kind, h.row) || jobOf(key)?.phase !== 'timeout') continue
      over = true
      leave(key, undoEnded(h.kind, h.row, { op: UNDO[h.kind].op, phase: 'settled', running: false, row: null }).toast)
    }
    if (over) forgetDiscography()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data])
  // A job that was running here is over: lb-bot's list is read again, and the
  // album's id asked about again (the files may have moved under it). If it was an
  // undo — undone: the row goes; not undone: it stays, lb-bot still lists it,
  // and says why itself.
  const wasRunning = useRef(new Set())
  // Every row that can have a job: what lb-bot lists, and what an undo holds.
  const followed = () => {
    const rows = new Map([...albumRows.map(r => [rowKey('album', r), { kind: 'album', row: r }]),
                          ...mergeRows.map(r => [rowKey('merge', r), { kind: 'merge', row: r }])])
    for (const [key, h] of Object.entries(held)) rows.set(key, h)
    return rows
  }
  useEffect(() => {
    for (const [key, { kind, row }] of followed()) {
      const job = jobOf(key)
      if (job?.running) { wasRunning.current.add(key); continue }
      // A job that went silent is no end: nothing is read for it, and its row
      // keeps its hold. The next answer of the list, whenever it comes, says more.
      if (job?.phase === 'timeout') { wasRunning.current.delete(key); continue }
      const h = held[key]
      if (h?.leaving) continue
      const end = h ? undoEnded(kind, row, job) : null
      if (!end && !(job && wasRunning.current.has(key))) continue
      wasRunning.current.delete(key)
      // What it changed of an artist's releases, the artist's page reads afresh
      // (F1): by the row's artists, or — an album's row names none by id — all.
      forgetDiscography(artistsTouched(kind, row))
      overrides.read()
      // (Not for a change that is undone: its row is on its way out.)
      if (kind === 'album' && !end?.gone) { askedAlbums.current.delete(`${row.id}:${row.albumId}`); setRecheck(n => n + 1) }
      // (The ended job goes with the row: letting go of the last hold on it forgets it.)
      if (end?.gone) leave(key, end.toast)
      else if (h) {
        setHeld(x => { const { [key]: _kept, ...rest } = x; return rest })
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watched, held, albumRows, mergeRows])

  // What a job is doing is said once, when its state word changes — not at
  // every count (the rows themselves are no live regions).
  const [spoken, setSpoken] = useState('')
  const words = useRef({})
  useEffect(() => {
    for (const [key, { kind, row }] of followed()) {
      const job = jobOf(key)
      if (!job) continue
      const view = listRowView(kind, row, job, 0)
      const said = !view.v ? '' : view.v.ended ? view.v.line || view.v.word : view.v.word
      if (!said || words.current[key] === said) continue
      words.current[key] = said
      setSpoken(`${view.title}: ${said}`)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watched, held, albumRows, mergeRows])

  // ── an undo: one press, one request — per row ──
  const [busy, setBusy] = useState({})                      // key → true while its request is out
  const [refused, setRefused] = useState({})                // key → { text, retry }
  const sending = useRef(new Set())
  const undo = async (group, row) => {
    const { kind } = group
    const key = rowKey(kind, row)
    if (sending.current.has(key)) return
    sending.current.add(key)
    setBusy(b => ({ ...b, [key]: true }))
    setRefused(r => ({ ...r, [key]: null }))
    try {
      if (kind === 'artist') {
        const answer = await post('/api/artist/override/remove', { nd_id: row.ndArtistId })
        // lb-bot has stored the remove. A scan follows, or none does — either
        // way the artist's page no longer goes by what it kept (F1).
        forgetDiscography(artistsTouched('artist', row))
        const started = scanStart('remove', answer, Date.now())
        if (started.phase === 'scanning') {
          jobs.start({ kind: 'scan', id: row.ndArtistId, op: 'remove', state: started, row: { name: row.name } })
          if (alive.current) setHeld(h => holdRow(h, group, row))
        } else if (alive.current) {
          setHeld(h => holdRow(h, group, row))
          leave(key, undoEnded('artist', row, { running: false, state: started }).toast)
        }
      } else {
        const answer = await post(UNDO[kind].path, UNDO[kind].body(row))
        // An answer that names no job is not one (F5): nothing is followed or
        // held, nothing will be said to have been restored — it is said in the
        // row, where a refusal is. The list, read again below, says what is so.
        const id = startedId(answer)
        if (!id) {
          if (alive.current) setRefused(r => ({ ...r, [key]: notSaid({ here: true }) }))
        } else {
          // X1: the job begins from the POST's own state.
          jobs.start({ kind, id, op: UNDO[kind].op, state: answer.state, row })
          if (alive.current) setHeld(h => holdRow(h, group, row))
        }
      }
      overrides.read()
    } catch (e) {
      if (alive.current) setRefused(r => ({ ...r, [key]: refusalView(e) }))
    } finally {
      sending.current.delete(key)
      if (alive.current) setBusy(b => { const { [key]: _done, ...rest } = b; return rest })
    }
  }

  if (!data) {
    if (overrides.state.status === 'error') {
      return (
        <Notice tone="danger" title="lb-bot could not say what it changed"
          actions={<button className="sm" onClick={overrides.read}>Try again</button>}>
          {overrides.state.error?.text}
        </Notice>
      )
    }
    return <ListSkeleton />
  }

  const live = {
    artists: liveArtists,
    albums: Object.fromEntries(albumRows.map(r => [r.id, albumNow[`${r.id}:${r.albumId}`]])),
  }
  const shown = groups.map(g => {
    // What lb-bot lists, with the rows an undo is still shown for put back
    // where each stood (acting on a row must not move it).
    const all = rowsShown(g, held)
    return { ...g, all, staying: all.filter(r => !held[rowKey(g.kind, r)]?.leaving).length }
  })
  const nothing = shown.every(g => !g.missing && !g.staying)
  const open = target => (target ? () => navigate(target.tab, ...target.params) : undefined)

  return (
    <>
      <span className="sr-only" role="status">{spoken}</span>
      <Roll open={overrides.state.status === 'error'}>
        <div style={{ paddingBottom: 18 }}>
          <Notice tone="quiet" title="lb-bot could not say again what it changed"
            actions={<button className="sm" onClick={overrides.read}>Try again</button>}>
            {overrides.state.error?.text}
          </Notice>
        </div>
      </Roll>
      {/* Each group folds away with its last row; `grps` (index.css) says why the block is pulled up. */}
      <div className="grps">
        {shown.map(g => (
          <Roll key={g.key} open={g.missing || g.staying > 0}>
            {/* The heading counts the rows shown — a row that is folding away is on its way out. */}
            <Grp n={g.missing ? '' : g.staying}>{g.title}</Grp>
            {g.missing ? <p className="muted text-caption">lb-bot left this list out of its answer: it could not be read just now.</p> : (
              <div className="rows each">
                {g.all.map(r => {
                  const key = rowKey(g.kind, r)
                  const job = jobOf(key)
                  const view = listRowView(g.kind, r, job, now, g.kind === 'artist' ? mbNames[r.mbid] : undefined)
                  const cover = g.kind === 'album'
                    ? <Cover url={r.coverUrl ? coverThumbUrl(r.coverUrl) : null} albumId={live.albums[r.id]?.albumId || r.albumId}
                        fallbackUrl={r.rgid ? caaCover(r.rgid) : null} name={r.album || r.artist || ''} size={40} />
                    : <Cover url={art.get(g.kind === 'merge' ? r.into?.ndArtistId : r.ndArtistId) || null}
                        name={(g.kind === 'merge' ? r.into?.name : r.name) || ''} size={40} round />
                  return (
                    // A row an undo took away folds shut; every other is simply there.
                    <Roll key={key} open={!held[key]?.leaving}>
                      <ListedRow art={cover} view={view}
                        // While a job changes the files, no id of the row is one to go by.
                        opens={job?.running ? undefined : open(rowOpens(g.kind, r, live))}
                        busy={!!busy[key]} refusal={refused[key] || null}
                        onUndo={() => undo(g, r)} onDismiss={() => setRefused(x => ({ ...x, [key]: null }))} />
                    </Roll>
                  )
                })}
              </div>
            )}
          </Roll>
        ))}
      </div>
      <Roll open={nothing}>
        <EmptyState title="Nothing changed"
          hint="An album’s edition and its cover are changed on the album’s page; an artist’s MusicBrainz match and a merge of two names, on the artist’s page. Every change is listed here, with its undo." />
      </Roll>
    </>
  )
}
