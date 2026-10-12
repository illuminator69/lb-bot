// Which MusicBrainz artist a library artist is (I-029, U2) — one picker, two
// homes: the pane of an artist under Needs you › To fix, and the artist page,
// behind the quiet line by its hero ("MusicBrainz match: … · Fix match").
//
// It shows what lb-bot uses now and where that comes from; searches MusicBrainz
// for another artist, by name or by a pasted id or address; sets the user's
// choice or removes it; and then is the job — the scan lb-bot starts — until
// that has ended. What may be said is decided in lib/artistMatch.js; the rules
// behind it are in CLAUDE.md § Artist MBID overrides.
//
// Nothing here pops: what appears folds open (`Roll`) or fades in, a height
// that changes while it is open moves there (`Fluid`), a lookup is a skeleton
// of its answer, and what a press opens is brought into view (`useRevealOn`).
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, post } from '../lib/api.js'
import {
  NOT_KNOWN, SEARCH_PAUSE_MS, lookupView, matchNow, nowView, pastedCandidate, scanStart, scanView, searchRef, setBody,
} from '../lib/artistMatch.js'
import { iso } from '../lib/text.js'
import { jobs, useJobs } from '../lib/jobs.js'
import { refusalView } from '../lib/overrides.js'
import Cover from '../components/Cover.jsx'
import { Fluid, Grp, Meter, Roll, St, useRevealOn } from '../components/Page.jsx'
import { Notice, Skeleton } from '../components/ui.jsx'

const enc = encodeURIComponent
const LOOKUP_TIMEOUT_MS = 20_000
const READ_TIMEOUT_MS = 15_000

// ── `GET /api/overrides`, read by whoever shows the picker ───────────────────
// The list under To fix and the picker read one answer; the artist page reads
// it when the picker is first opened. A re-read leaves what is shown in place
// until its answer is in, and a failed one keeps the last answer beside its
// error.
//
//   state   { status: idle | loading | ok | error, data, error, reading, asked }
//           `reading`: a read is out — the first one, or one behind what is held
//           `asked`: a press asked for a read (`read({ asked: true })`, a
//           surface's own Try again) and no read has ended since: the surface
//           shows the press until one has. A read nobody asked for that
//           starts behind it keeps it — its answer is the one the press
//           gets. Kept here, with the read, so that nothing has to remember
//           it beside it.
export function useOverridesRead(enabled = true) {
  const [state, setState] = useState({ status: 'idle', data: null, error: null, reading: false, asked: false })
  const alive = useRef(true)
  const serial = useRef(0)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const read = useCallback(async opts => {
    // (Also handed to buttons as their handler: only a plain `{ asked: true }` asks.)
    const asked = opts?.asked === true
    const mine = ++serial.current
    setState(s => ({ ...s, status: s.data ? s.status : 'loading', error: null, reading: true, asked: asked || s.asked }))
    try {
      const data = await api('/api/overrides', { timeoutMs: READ_TIMEOUT_MS })
      if (alive.current && mine === serial.current) setState({ status: 'ok', data, error: null, reading: false, asked: false })
    } catch (e) {
      if (alive.current && mine === serial.current) setState(s => ({ status: 'error', data: s.data, error: refusalView(e), reading: false, asked: false }))
    }
  }, [])
  const asked = useRef(false)
  useEffect(() => {
    if (!enabled || asked.current) return
    asked.current = true
    read()
  }, [enabled, read])
  return { state, read }
}

// ── A scan that outlives the pane ────────────────────────────────────────────
// The scan lb-bot starts is a job of the one watch (lib/jobWatch.js, kind
// `scan`, by the artist's id): the two homes read it and never poll it twice,
// leaving the pane stops nothing, and a scan that ends with nobody showing it
// is one toast that names the artist.

/** Whether a scan the picker started for this artist is still running. */
export const matchJobRunning = id => !!jobs.get('scan', id)?.running

/** The MusicBrainz artist of `mb` in the shape the lib reads, or null. */
const mbOf = meta => (meta ? { name: meta.name || '', disambiguation: meta.disambiguation || '', type: meta.type || '',
                               country: meta.country || '', area: meta.area || '' } : null)

// ── The controller ───────────────────────────────────────────────────────────
//   artist     { id, name, mbid?, tagMbid? } — the library artist; the two ids
//              only where the caller has the artist list's row
//   open       the picker is on show: nothing is asked for it before that
//   overrides  useOverridesRead's `state`; `onRead` reads it again
//   indexMbid  what the artist page's discography read names ('' for nobody),
//              undefined while that read is still out; null where there is no
//              such read (the pane under To fix)
//   known      { mbid: what MusicBrainz says of that artist } — what the caller
//              has already read; `asking` is an id it is reading right now
//   onChanged  (what) — 'stored' once lb-bot has stored a set or a remove and
//              a scan follows; 'ended' once nothing more will change: the scan
//              is over, or none was started
//   onRetryIndex  the home's own retry of the read `indexMbid` comes from,
//              for when that read failed
export function useArtistMatch({ artist, open = true, overrides, onRead, indexMbid, onRetryIndex, known = null, asking = '', onChanged }) {
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const changedRef = useRef(onChanged)
  changedRef.current = onChanged
  const readRef = useRef(onRead)
  readRef.current = onRead
  const retryIndexRef = useRef(onRetryIndex)
  retryIndexRef.current = onRetryIndex

  const now = matchNow({ id: artist.id, overrides: overrides?.data || null, artist, indexMbid })

  // What MusicBrainz says of an artist: from the caller, from a candidate the
  // user picked, or asked for here — once per id, and only while on show.
  const [names, setNames] = useState({})
  const named = id => (known && known[id] ? mbOf(known[id]) : names[id])
  const mbid = now.mbid
  const mb = !mbid ? undefined : named(mbid) !== undefined ? named(mbid) : null
  const wantName = open && !!mbid && mb === null && asking !== mbid
  useEffect(() => {
    if (!wantName) return undefined
    let dead = false
    api(`/api/meta/artist?mbid=${enc(mbid)}`, { timeoutMs: READ_TIMEOUT_MS })
      .then(meta => { if (!dead) setNames(n => ({ ...n, [mbid]: mbOf(meta) })) })
      // Not named: the row still links out, and says nothing it does not know.
      .catch(() => { if (!dead) setNames(n => ({ ...n, [mbid]: { name: '' } })) })
    return () => { dead = true }
  }, [wantName, mbid])

  const view = nowView(now, mb)

  // ── the search ──
  const [chosen, setChosen] = useState(false)             // "Choose another" was pressed
  const [text, setText] = useState(artist.name || '')
  const [lookup, setLookup] = useState(null)
  // What is being asked for: `n` makes Enter, and Try again, ask the same again.
  const [query, setQuery] = useState(null)                // { value, n }
  const searchOpen = chosen || view.state === 'none'
  const asked = useRef('')                                // what the last search was for
  const pause = useRef(0)

  // One request at a time: another search, the search shutting, or the pane
  // being left ends the one that is out, and its answer is never shown.
  useEffect(() => {
    if (!query) return undefined
    const ref = searchRef(query.value)
    if (ref.kind === 'empty') { setLookup(null); return undefined }
    if (ref.kind === 'wrong') { setLookup({ status: 'wrong', kind: 'wrong', why: ref.why, list: [] }); return undefined }
    const control = new AbortController()
    let over = false
    const settle = next => { if (!over) { over = true; clearTimeout(timer); setLookup(next) } }
    // A connection that stalls is given up on, and said not to have answered.
    const timer = setTimeout(() => {
      settle({ status: 'error', kind: ref.kind, list: [], error: new Error('MusicBrainz took too long to answer.') })
      control.abort()
    }, LOOKUP_TIMEOUT_MS)
    // Candidates already on the page stay there, dimmed, until the answer is in.
    setLookup(was => ({ status: 'loading', kind: ref.kind,
                        list: (was?.status === 'ok' || was?.status === 'loading') && was.kind === ref.kind ? was.list : [] }))
    if (ref.kind === 'id') {
      // A pasted id is that artist: MusicBrainz is asked only for its name.
      api(`/api/meta/artist?mbid=${enc(ref.mbid)}`, { signal: control.signal })
        .then(meta => settle({ status: 'ok', kind: 'id', list: [pastedCandidate(ref.mbid, meta)] }))
        .catch(() => settle({ status: 'ok', kind: 'id', list: [pastedCandidate(ref.mbid, null)] }))
    } else {
      api(`/api/artist/lookup?q=${enc(ref.q)}`, { signal: control.signal })
        .then(r => settle({ status: 'ok', kind: 'name', list: Array.isArray(r.candidates) ? r.candidates : [] }))
        // Whatever stopped it, the search did not run: never "nobody by that name".
        .catch(error => settle({ status: 'error', kind: 'name', list: [], error }))
    }
    return () => { over = true; clearTimeout(timer); control.abort() }
  }, [query])
  useEffect(() => () => clearTimeout(pause.current), [])

  const search = value => {
    clearTimeout(pause.current)
    asked.current = String(value ?? '').trim()
    // The skeleton is drawn in the very render that asks: when the press also
    // opens the fold, the rows are in it from its first frame and the fold
    // alone moves them. (Set by the effect, they arrived a few frames into the
    // fold — 220 px in one step.)
    const ref = searchRef(value)
    if (ref.kind === 'name' || ref.kind === 'id') {
      setLookup(was => ({ status: 'loading', kind: ref.kind,
                          list: (was?.status === 'ok' || was?.status === 'loading') && was.kind === ref.kind ? was.list : [] }))
    }
    setQuery(q => ({ value, n: (q?.n || 0) + 1 }))
  }
  const onText = value => {
    setText(value)
    clearTimeout(pause.current)
    // Searched after a pause in typing — and not again for what was just asked.
    pause.current = setTimeout(() => { if (alive.current && value.trim() !== asked.current) search(value) }, SEARCH_PAUSE_MS)
  }
  const closeSearch = () => {
    clearTimeout(pause.current)
    asked.current = ''
    setChosen(false)
    setQuery(null)
    setLookup(null)
  }
  const onChoose = () => {
    if (chosen) { closeSearch(); return }
    setChosen(true)
    // The press is the request: the field holds the artist's name, and is searched.
    search(text)
  }

  // ── set, remove, and the scan that follows ──
  const [busy, setBusy] = useState('')                    // '' | 'keep' | 'back' | a candidate's id
  const [sending, setSending] = useState(null)            // the job while the request is out
  const [refusal, setRefusal] = useState(null)            // { text, retry, again }
  // The job: the watch's while a scan runs (and until its end is dismissed);
  // this pane's own when lb-bot's answer started none — there is nothing to
  // follow then, only something to say.
  const watched = useJobs('scan', [artist.id])[artist.id] || null
  const [unscanned, setUnscanned] = useState(null)
  const job = watched?.state || unscanned

  // One press, one request: a second press before the page has drawn the
  // first is not a second request.
  const sent = useRef(false)
  const send = async (op, mark, candidate) => {
    if (sent.current) return
    sent.current = true
    setBusy(mark)
    setRefusal(null)
    setSending({ op, phase: 'sending' })
    try {
      const answer = op === 'set'
        ? await post('/api/artist/override', setBody(artist, candidate))
        : await post('/api/artist/override/remove', { nd_id: artist.id })
      if (op === 'set' && candidate.name) {
        const id = String(answer.mbid || candidate.mbid).toLowerCase()
        if (alive.current) setNames(n => ({ ...n, [id]: mbOf(candidate) }))
      }
      const started = scanStart(op, answer, Date.now())
      if (started.phase === 'scanning') {
        jobs.start({ kind: 'scan', id: artist.id, op, state: started, row: { name: artist.name } })
        if (alive.current) setUnscanned(null)
      } else if (alive.current) {
        setUnscanned(started)
      }
      if (alive.current) closeSearch()
      // lb-bot has stored it: what it uses now, and the list, are read again.
      readRef.current?.()
      if (alive.current) changedRef.current?.(started.phase === 'scanning' ? 'stored' : 'ended')
    } catch (e) {
      if (!alive.current) return
      setRefusal({ ...refusalView(e), again: () => send(op, mark, candidate) })
      // lb-bot no longer has the override this pane offered to remove.
      if (e.status === 404 && e.payload?.code === 'no_override') readRef.current?.()
    } finally {
      sent.current = false
      if (alive.current) { setBusy(''); setSending(null) }
    }
  }
  const onUse = key => {
    const candidate = (lookup?.list || []).find(c => String(c.mbid).toLowerCase() === key)
    if (candidate) send('set', key, candidate)
  }
  const onKeep = () => send('set', 'keep', { mbid, ...(mb || {}) })
  const onBack = () => send('remove', 'back')

  // The scan is over (the watch says so, whoever was showing it meanwhile):
  // what lb-bot holds for the artist now is read again — once per scan, and
  // not for one lb-bot has stopped answering about.
  const told = useRef('')
  const endedTask = watched && !watched.running ? watched.state.taskId : ''
  const endedHow = watched?.phase || ''
  useEffect(() => {
    if (!endedTask || told.current === endedTask) return
    told.current = endedTask
    if (endedHow === 'silent') return
    readRef.current?.()
    changedRef.current?.('ended')
  }, [endedTask, endedHow])

  const onDone = () => { jobs.dismiss('scan', artist.id); setUnscanned(null) }

  return {
    now, mb,
    view: {
      // Until lb-bot's own answer is in the picker says nothing provisional;
      // an answer that could not be read again is not shown as what is so; and
      // an answer lb-bot left its list out of is said as that (Y1) — never as
      // "its own search", which is a claim about a list it did not give. A
      // page read that failed (R3, R3a) is said too — `now.state` is `noread`,
      // with the home's Try again — and the picker stays a picker: the search
      // is the one thing that mends a name MusicBrainz's search does not find.
      nowState: overrides?.status === 'error' || now.why === 'nolist' ? 'error'
        : !overrides?.data || view.state === 'unknown' ? 'loading' : 'ok',
      nowError: overrides?.status === 'error' ? overrides.error || null
        : now.why === 'nolist' ? { text: NOT_KNOWN.nolist } : null,
      // Try again reads again what could not be read: lb-bot's lists, or the home's own read.
      onRetryNow: () => (overrides?.status !== 'error' && now.why === 'noread' ? retryIndexRef.current?.() : readRef.current?.()),
      now: view,
      busy,
      sending: sending ? scanView(sending, { name: artist.name }) : null,
      search: { open: searchOpen, toggled: chosen, text, view: lookupView(lookup, now) },
      onChoose, onText, onSearch: () => search(text), onUse, onKeep, onBack,
      refusal: refusal ? { text: refusal.text, retry: refusal.retry } : null,
      onRetryRefused: () => refusal?.again?.(),
      onDismissRefusal: () => setRefusal(null),
      job: job ? scanView(job, { name: artist.name }) : null,
      onDone,
    },
  }
}

// ── The picker ───────────────────────────────────────────────────────────────

const RowSkeleton = ({ rows = 1 }) => (
  <div className="rows" aria-hidden="true">
    {Array.from({ length: rows }, (_, i) => (
      <div key={i} className="row">
        <Skeleton className="!rounded-pill" style={{ width: 40, height: 40, flex: 'none' }} />
        <div className="grow">
          <Skeleton className="h-3" style={{ width: `${46 - (i % 3) * 9}%` }} />
          <Skeleton className="mt-2 h-2.5" style={{ width: `${28 + (i % 2) * 8}%` }} />
        </div>
      </div>
    ))}
  </div>
)

// A MusicBrainz artist as a row: who it is, what MusicBrainz says of it, the
// way out to its page there, and — for a candidate — the one thing to press.
function ArtistRow({ title, sub, href, naming = false, children }) {
  return (
    <div className="row stack">
      <Cover name={naming ? '' : title} size={40} round />
      <div className="grow">
        {naming ? <Skeleton className="h-3 w-2/5" />
          : <div className="t" style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{title}</div>}
        {sub ? <div className="s" style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{sub}</div> : null}
      </div>
      <a className="link-inline text-caption" href={href} target="_blank" rel="noreferrer"
        aria-label={naming ? 'Open on MusicBrainz' : `${iso(title)} on MusicBrainz`}>MusicBrainz</a>
      {children}
    </div>
  )
}

// The scan as the one card it is: the sentence, the state word, the whole
// scan's meter, a muted second line, and — once it has ended — where to go.
function MatchJob({ v, onDone, onOpenArtist }) {
  return (
    <div className="rows">
      <div className="row col">
        <div className="flex min-w-0 items-center gap-3">
          <div className="grow">
            <div className="t" style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{v.line}</div>
            {/* The release being checked changes with every poll: one line, cut, so the card keeps its height. */}
            {v.note ? <div className={`s${v.ended ? '' : ' clip'}`} style={v.ended ? { whiteSpace: 'normal' } : undefined}>{v.note}</div> : null}
          </div>
          {v.count ? <span className="num muted hide-sm">{v.count}</span> : null}
          <St tone={v.tone}>{v.word}</St>
        </div>
        <Roll open={v.bar}>
          {/* Before lb-bot has said how many there are, the bar is only started. */}
          <div className="pt-2.5"><Meter lg value={v.percent == null ? 4 : v.percent} /></div>
        </Roll>
        <Roll open={v.ended}>
          <div className="acts pt-2.5">
            {onOpenArtist && <button className="sm" onClick={onOpenArtist}>Open artist page</button>}
            {onDone && <button className="sm quiet" onClick={onDone}>Done</button>}
          </div>
        </Roll>
      </div>
    </div>
  )
}

//   open         the picker is on show (the artist page folds it; under To fix it always is)
//   boxed        it sits in a `.next` box, whose heading has no space of its own under it
//   nowState     loading | error | ok — the read of what lb-bot uses
//   now          lib nowView's answer
//   busy         '' | 'keep' | 'back' | the id of the candidate being set: one request at a time
//   sending      the job's words while that request is out
//   search       { open, toggled, text, view: lib lookupView's answer }
//   refusal      { text, retry } — lb-bot refused the set or the remove
//   job          lib scanView's answer while there is a job to show
//   onOpenArtist where "Open artist page" goes; absent on the artist page itself
export function ArtistMatchView({
  open = true, boxed = false, nowState, nowError, onRetryNow, now, busy = '', sending = null, search,
  onChoose, onText, onSearch, onUse, onKeep, onBack, refusal = null, onRetryRefused, onDismissRefusal,
  job = null, onDone, onOpenArtist,
}) {
  const box = useRef(null)
  const field = useRef(null)
  const choose = useRef(null)
  const look = search.view
  useRevealOn(box, !open ? [] : [
    'open',
    search.open && 'search',
    // `~`: what arrives after the press. Not revealed once the user has scrolled away.
    search.open && (look.rows.length || look.empty || look.error) && `~answer:${look.rows.length}:${look.empty}:${look.error?.title || ''}`,
    // Not for the request being out: the press moves nothing — its answer does.
    refusal && '~refused',
    job && `~job:${job.ended ? 'ended' : 'running'}`,
  ].filter(Boolean), search.open && !job ? field : undefined)
  // The keyboard follows what the press did: to the job when the picker
  // becomes it, and back to the picker when "Done" shuts the job. Only when
  // it was in here — nothing is taken from wherever else it is.
  const card = useRef(null)
  const hadJob = useRef(!!job)
  useEffect(() => {
    const had = hadJob.current
    hadJob.current = !!job
    if (had === !!job || typeof document === 'undefined') return
    const at = document.activeElement
    if (at && at !== document.body && !box.current?.contains(at)) return
    // Without the browser's own jump to the focused element: bringing into view is `reveal`'s.
    if (job) card.current?.focus({ preventScroll: true })
    else (choose.current || field.current)?.focus({ preventScroll: true })
  }, [job])
  // The control that was pressed keeps the keyboard while its request is out
  // (`aria-disabled`: a disabled button drops it); the others cannot be pressed.
  const held = mark => ({ disabled: !!busy && busy !== mark, 'aria-disabled': busy === mark || undefined, 'aria-busy': busy === mark })
  return (
    <div ref={box}>
      <Grp>MusicBrainz match<span className="note">{job || nowState !== 'ok' ? '' : now.note}</span></Grp>
      {/* What the job is doing, said once when its state changes. */}
      <span className="sr-only" role="status">{job ? `${job.word}: ${job.line}` : sending ? sending.line : ''}</span>

      <Roll open={!!job}>
        <div className={boxed ? 'pb-1 pt-2.5' : 'pb-1'} ref={card} tabIndex={-1} style={{ outline: 'none' }}>
          {job ? <MatchJob v={job} onDone={onDone} onOpenArtist={onOpenArtist} /> : null}
        </div>
      </Roll>

      <Roll open={!job}>
        <div className={boxed ? 'pt-2.5' : undefined}>
          {/* The skeleton, the artist, a sentence, a note: each takes the last one's place, and the box moves there. */}
          <Fluid>
            {nowState === 'loading' ? <RowSkeleton />
              : nowState === 'error' ? (
                <Notice tone="quiet" title="Couldn’t read what lb-bot uses for this artist"
                  actions={<button className="sm" onClick={onRetryNow}>Try again</button>}>
                  {nowError?.text}
                </Notice>
              ) : (
                <div className="fadein">
                  {/* The home's own read failed: said, with its Try again — and "Choose another" below it. */}
                  {now.state === 'noread' && (
                    <Notice tone="quiet" title="Couldn’t read what lb-bot uses for this artist"
                      actions={<button className="sm" onClick={onRetryNow}>Try again</button>}>
                      {now.line}
                    </Notice>
                  )}
                  {now.state === 'match' && (
                    <div className="rows"><ArtistRow title={now.title} sub={now.sub} href={now.href} naming={now.naming} /></div>
                  )}
                  {/* Why the artist is worth a look is the frame's "why" note; that
                      lb-bot goes by its own search is only said. */}
                  {!now.line || now.state === 'noread' ? null
                    : now.state === 'search' ? <p className="muted">{now.line}</p>
                    : <div className={now.state === 'match' ? 'pt-2.5' : undefined}><p className="why !my-0">{now.line}</p></div>}
                </div>
              )}
          </Fluid>

          <Roll open={nowState === 'ok' && (now.keep || now.choose || !!now.back)}>
            <div className="acts pt-3">
              {now.keep && <button {...held('keep')} onClick={onKeep}>Keep this match</button>}
              {now.choose && (
                <button ref={choose} aria-expanded={search.toggled} disabled={!!busy} onClick={onChoose}>Choose another</button>
              )}
              {now.back && <button className="quiet" {...held('back')} onClick={onBack}>{now.back}</button>}
            </div>
          </Roll>

          <Roll open={!!refusal}>
            <div className="pt-3">
              <Notice tone="danger" role="alert" actions={<>
                {refusal?.retry && <button className="sm" disabled={!!busy} onClick={onRetryRefused}>Try again</button>}
                <button className="sm quiet" onClick={onDismissRefusal}>Dismiss</button>
              </>}>{refusal?.text}</Notice>
            </div>
          </Roll>

          {/* `slow`: the field and its eight rows are half a screen. */}
          <Roll slow open={nowState === 'ok' && search.open}>
            <div className="pt-3">
              <form className="acts" onSubmit={e => { e.preventDefault(); onSearch() }}>
                <input ref={field} className="grow" style={{ minWidth: 160 }} value={search.text} onChange={e => onText(e.target.value)}
                  placeholder="An artist’s name, or a MusicBrainz artist link"
                  aria-label="An artist’s name, or a MusicBrainz artist id or address" />
                <button type="submit" disabled={!search.text.trim()}>Search</button>
              </form>
              {/* Skeleton rows become candidates, a sentence, or a note: the box moves to whichever height that is. */}
              <Fluid>
                {look.skeleton ? <div className="pt-2.5"><RowSkeleton rows={look.skeleton} /></div> : null}
                {look.error ? (
                  <div className="fadein pt-2.5">
                    <Notice tone="quiet" title={look.error.title}
                      actions={<button className="sm" onClick={onSearch}>Try again</button>}>
                      {look.error.text}
                    </Notice>
                  </div>
                ) : null}
                {look.empty ? <p className="muted fadein pt-2.5">{look.empty}</p> : null}
                {look.rows.length ? (
                  <div className="pt-2.5">
                    <div className={`rows${look.busy ? ' dimmed' : ' arrive'}`} aria-busy={look.busy}>
                      {look.rows.map(r => (
                        <ArtistRow key={r.key} title={r.title} sub={r.sub} href={r.href}>
                          {r.word ? <St tone="ok">{r.word}</St> : (
                            <button className="sm" {...held(r.key)} disabled={look.busy || (!!busy && busy !== r.key)}
                              onClick={() => onUse(r.key)}>
                              {r.label}
                            </button>
                          )}
                        </ArtistRow>
                      ))}
                    </div>
                  </div>
                ) : null}
              </Fluid>
            </div>
          </Roll>

          {/* Last, so that it pushes nothing: the control pressed stays under the pointer. */}
          <Roll open={!!sending}>
            <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 pt-2.5">
              <St tone={sending?.tone}>{sending?.word}</St>
              <span className="muted">{sending?.line}</span>
            </div>
          </Roll>
        </div>
      </Roll>
    </div>
  )
}

// The picker with its own controller: what a home that has nothing else to do
// with the match mounts (the pane under Needs you › To fix).
export default function ArtistMatch({ onOpenArtist, ...options }) {
  const match = useArtistMatch(options)
  return <ArtistMatchView {...match.view} onOpenArtist={onOpenArtist} />
}
