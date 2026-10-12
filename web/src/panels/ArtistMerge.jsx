// One artist, two names (I-029, U3): Navidrome keys an artist by name, so two
// spellings of one MusicBrainz artist are two artists in the library. lb-bot
// can rewrite the files of one spelling to say the other; this is where the
// user asks for it — on the artist page, under "Also in your library as", and
// in the pane of a set under Needs you › To fix. One component, two homes.
//
// Which name is kept (the one with the most albums first), which spelling is
// merged into it (a set of three is merged a pair at a time, as the route takes
// them), what would change, the one fixed sentence, Merge — and then the job,
// which is the watch's (lib/jobWatch.js, kind `merge`): leaving stops nothing.
// What may be said is lib/merges.js; the rules are in CLAUDE.md § Artist name
// merge.
//
// Nothing pops: what opens folds (`Roll`), a height that changes moves there
// (`Fluid`), the preview is a skeleton of its answer, and what a press opens is
// brought into view (`useRevealOn`).
import { useEffect, useRef, useState } from 'react'
import { useApp, goArtist, replaceRoute } from '../App.jsx'
import { api, post } from '../lib/api.js'
import { jobs, useJobs } from '../lib/jobs.js'
import {
  afterMerge, keptDefault, mergeBody, mergeFrom, mergeLabel, mergePreviewView, mergeQuiet, mergeRest, mergeRowsNow, mergeView,
} from '../lib/merges.js'
import { notSaid, refusalView, startedId } from '../lib/overrides.js'
import { iso } from '../lib/text.js'
import Cover from '../components/Cover.jsx'
import { Fluid, Grp, Roll, useRevealOn } from '../components/Page.jsx'
import { Notice, Skeleton } from '../components/ui.jsx'
import { JobCard } from './AlbumOverride.jsx'

const enc = encodeURIComponent
const albumsText = n => `${n} album${n === 1 ? '' : 's'}`

// ── The controller ───────────────────────────────────────────────────────────
//   set           the merge set (lib `setOf`): { mbid, artists: [{ndArtistId, name, albums}] }
//   merged        the set is one lb-bot lists no more: it was merged from here
//                 (To fix keeps its item, with the set as it was)
//   pageArtistId  the artist whose page this is, '' under To fix
//   merges        `GET /api/overrides`'s `merges`, when read: a merge of this
//                 set that is running — whoever started it — is picked up
//   art           Map of Navidrome artist id → portrait address, when the home has one
//   onRead        reads the overrides again
//   onChanged(job)  a merge or an undo ended: the library changed
export function useArtistMerge({ set, merged = false, pageArtistId = '', merges = null, art = null, onRead, onChanged }) {
  const { pushToast } = useApp()
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const readRef = useRef(onRead)
  readRef.current = onRead
  const changedRef = useRef(onChanged)
  changedRef.current = onChanged

  // The set as lb-bot last gave it: once the merge is done lb-bot lists it no
  // more, and the block still has a job — or a merge that can be undone — to show.
  const lastSet = useRef(set)
  if (set) lastSet.current = set
  const known = set || lastSet.current
  const live = merged ? null : set               // the set there is still something to merge within
  const artists = known?.artists || []
  const ids = artists.map(a => a.ndArtistId)
  const [open, setOpen] = useState(false)
  const [kept, setKept] = useState(() => keptDefault(known))
  const keptId = ids.includes(kept) ? kept : keptDefault(known)
  const choices = mergeFrom(known, keptId)
  const [from, setFrom] = useState('')
  const fromId = choices.some(a => a.ndArtistId === from) ? from : (choices[0]?.ndArtistId || '')

  // ── the preview: asked when the fold is open, again when the pair changes;
  // one request at a time (the one that is out is ended).
  const [pv, setPv] = useState(null)             // { key, status: loading | ok | error, data, refusal }
  const [nonce, setNonce] = useState(0)
  const pvKey = open && live && keptId && fromId ? `${keptId}|${fromId}` : ''
  useEffect(() => {
    if (!pvKey) { setPv(null); return undefined }
    const control = new AbortController()
    setPv({ key: pvKey, status: 'loading' })
    api(`/api/artist/merge/preview?nd_id=${enc(keptId)}&from_nd_id=${enc(fromId)}`, { signal: control.signal })
      // An answer for a pair that has been left is nobody's — also one that
      // arrives although its request was ended.
      .then(data => { if (!control.signal.aborted) setPv({ key: pvKey, status: 'ok', data }) })
      .catch(e => { if (!control.signal.aborted) setPv({ key: pvKey, status: 'error', refusal: refusalView(e, { always: true }) }) })
    return () => control.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pvKey, nonce])

  // ── the job: the watch's. A merge of these names that lb-bot lists as
  // running is followed, at every answer of its list — a reload, another
  // client, a pane come back to; and one this page had given up on is taken up
  // again by a fresh answer that still lists it running. A job this page gave
  // up on that a later answer no longer lists as running is let go of: the
  // list says how it ended.
  const [jobId, setJobId] = useState('')
  const near = pageArtistId ? [pageArtistId] : ids
  const all = (merges || []).filter(r => near.includes(r.into?.ndArtistId) || near.includes(r.from?.ndArtistId))
  // A job this page has itself seen end is over, whatever a list read BEFORE
  // that end still says of its row: the re-read the end starts can fail, and
  // the list kept through it would hold the names quiet, and draw a running
  // card, for a merge that is done (M1). Forgotten with the next answer.
  const [ended, setEnded] = useState({ list: null, ids: [] })
  const mine = mergeRowsNow(all, ended.list === merges ? new Set(ended.ids) : null)
  const answered = useRef(merges)
  useEffect(() => {
    if (!merges) return
    const first = answered.current === merges
    answered.current = merges
    for (const r of mine) if (jobs.adopt('merge', r)) { setJobId(r.id); return }
    if (!first && jobId && jobs.get('merge', jobId)?.phase === 'timeout') {
      // (However it ended, lb-bot is no longer at it — and nobody saw the end:
      // the home is told as for an end it did see, so that what it kept of
      // the two names is read again. F1.)
      const silent = jobs.get('merge', jobId)
      jobs.dismiss('merge', jobId)
      setJobId('')
      changedRef.current?.(silent)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [merges])
  const job = useJobs('merge', jobId ? [jobId] : [])[jobId] || null
  // At rest: a merge of these names that needs a look (left running, failed,
  // with problems) — and, for a set that was merged from here, the merge that
  // made it so, with its undo. (A merge that simply went through is otherwise
  // listed where every change is: System › Overrides.)
  const resting = job ? null
    : mine.map(r => ({ row: r, v: mergeRest(r) })).find(x => x.v)
      || (merged ? mine.filter(r => r.state === 'merged').map(r => ({ row: r, v: mergeView('merge', 'settled', r) }))[0] : null)
      || null

  const [busy, setBusy] = useState(false)
  const [refusal, setRefusal] = useState(null)   // { text, retry, again }
  const sent = useRef(false)
  const send = async (path, body, op, startRow) => {
    if (sent.current) return
    sent.current = true
    setBusy(true)
    setRefusal(null)
    try {
      const answer = await post(path, body)
      // An answer that names no job is not one (F5): nothing is followed, and
      // it is said where a refusal is — in the fold, which stays open for it.
      const id = startedId(answer)
      if (!id) {
        if (alive.current) setRefusal({ ...notSaid(), again: null })
        return
      }
      // X1: the job begins from the POST's own state.
      jobs.start({ kind: 'merge', id, op, state: answer.state, row: startRow })
      // (The fold goes with the names going quiet, below: the job is running from here on.)
      if (alive.current) setJobId(id)
    } catch (e) {
      if (alive.current) setRefusal({ ...refusalView(e), again: () => send(path, body, op, startRow) })
    } finally {
      sent.current = false
      if (alive.current) setBusy(false)
    }
  }
  const nameOf = id => artists.find(a => a.ndArtistId === id)?.name || ''
  const onMerge = () => send('/api/artist/merge', mergeBody(keptId, fromId), 'merge',
    { into: { ndArtistId: keptId, name: nameOf(keptId) }, from: { ndArtistId: fromId, name: nameOf(fromId) }, setAt: Date.now() / 1000, failures: [] })
  const onUndo = row => send('/api/artist/merge/undo', { id: row.id }, 'unmerge', row)

  // A job ended: lb-bot's lists are read again, and the page of the spelling
  // that is gone moves to the kept artist's — once per job and its ending.
  const told = useRef('')
  const endKey = job && !job.running ? `${job.id}:${job.op}:${job.phase}` : ''
  useEffect(() => {
    if (!endKey || told.current === endKey) return
    told.current = endKey
    if (job.phase === 'timeout') return
    setEnded(e => ({ list: merges, ids: [...(e.list === merges ? e.ids : []), job.id] }))
    readRef.current?.()
    // First: the home lets go of what it kept of both names (the page that is
    // moved to must not open on what it read before the merge).
    changedRef.current?.(job)
    const next = afterMerge(pageArtistId, job)
    if (next.go) {
      pushToast(next.toast)
      replaceRoute('artist', next.go)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [endKey])

  const preview = pvKey && pv?.key === pvKey ? pv : pvKey ? { key: pvKey, status: 'loading' } : null
  const card = job
    ? { v: mergeView(job.op, job.phase, job.row), row: job.row || job.startRow }
    : resting ? { v: resting.v, row: resting.row } : null
  const canMerge = !!live && artists.length > 1
  // lb-bot is — as far as this page knows — still merging these names: by the
  // list last read, or by the job followed here (running, or gone silent while
  // it ran). Nothing is offered for them meanwhile — after the three-minute
  // give-up as on arrival: lb-bot would refuse it.
  const quiet = mergeQuiet(mine, job)
  // The fold goes with the names going quiet — a merge begun here, or one
  // found running by a read (another client's) — and stays shut: it does not
  // open again by itself when that is over, and ask for a preview nobody
  // asked for (M4).
  useEffect(() => { if (quiet) setOpen(false) }, [quiet])
  const entry = a => ({ id: a.ndArtistId, name: a.name, sub: albumsText(Number(a.albums) || 0), art: art?.get?.(a.ndArtistId) || null })
  return {
    // There is something to show: a set to merge within, or a merge to follow or undo.
    has: canMerge || !!card,
    view: {
      canMerge, quiet,
      // Without a set to merge within, the names are the merge's own: what was merged into what.
      entries: canMerge ? artists.map(entry)
        : card?.row ? [{ id: '', name: card.row.from?.name || '', sub: `merged into ${iso(card.row.into?.name)}`, art: null }] : [],
      heading: !canMerge ? 'Merged by lb-bot' : pageArtistId ? 'Also in your library as' : 'The names',
      pageArtistId,
      open: open && canMerge && !quiet, onToggle: () => { setOpen(o => !o); setRefusal(null) },
      kept: keptId, onKept: id => { setKept(id); setRefusal(null) },
      choices: choices.map(entry),
      from: fromId, onFrom: id => { setFrom(id); setRefusal(null) },
      keptName: nameOf(keptId),
      preview: preview && { status: preview.status, view: preview.status === 'ok' ? mergePreviewView(preview.data) : null, refusal: preview.refusal || null },
      onRetry: () => setNonce(n => n + 1),
      busy, onMerge,
      refusal: refusal ? { text: refusal.text, retry: refusal.retry } : null,
      onRetryRefused: () => refusal?.again?.(), onDismiss: () => setRefusal(null),
      card: card && {
        title: mergeLabel(card.row), v: card.v, busy,
        // (The card offers Undo only where its view says it can be undone.)
        onUndo: card.row?.id ? () => onUndo(card.row) : undefined,
        // Letting go of the last hold on an ended job is what forgets it (`useJobs`).
        // A silent job is not an ended one: its card stays, and says where to look.
        onDone: job && !job.running && job.phase !== 'timeout' ? () => setJobId('') : undefined,
      },
    },
  }
}

// ── The parts ────────────────────────────────────────────────────────────────

// One name of several, as plain pressed buttons in a group — not a listbox,
// which would owe arrow keys.
function NameChooser({ question, rows, value, onChange, disabled = false }) {
  return (
    <>
      <p className="mb-1.5">{question}</p>
      <div className="rows" role="group" aria-label={question}>
        {rows.map(r => (
          <button key={r.id} className="row tap" aria-pressed={r.id === value} disabled={disabled} onClick={() => onChange(r.id)}>
            <Cover url={r.art} name={r.name} size={36} round />
            <span className="grow">
              <span className="t block" style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{r.name}</span>
              <span className="s block">{r.sub}</span>
            </span>
          </button>
        ))}
      </div>
    </>
  )
}

// The size of the common answer — the line, two albums, the fixed sentence,
// the buttons — so that the answer takes its place and little moves.
const PreviewSkeleton = () => (
  <div className="fadein" aria-busy="true" aria-label="Asking lb-bot what would change">
    <Skeleton className="h-4 w-3/5" />
    <div className="rows mt-2.5" aria-hidden="true">
      {[0, 1].map(i => (
        <div key={i} className="row"><div className="grow"><Skeleton className="h-3" style={{ width: `${48 - i * 12}%` }} /><Skeleton className="mt-2 h-2.5 w-1/6" /></div></div>
      ))}
    </div>
    <Skeleton className="mt-4 h-3 w-4/5" />
    <Skeleton className="mt-2.5 h-8 w-24" />
  </div>
)

//   entries     every spelling of the set: [{ id, name, sub, art }]
//   pageArtistId  on the artist page, that artist: the list is of the OTHER spellings, each a way to its page
//   quiet       lb-bot is still merging these names (as far as the page knows): nothing is offered for them
//   open        "Merge into one name…" is unfolded
//   kept / choices / from   the name that is kept, the spellings that can be merged into it, the one chosen
//   preview     null | { status: loading | ok | error, view: lib mergePreviewView's answer, refusal }
//   refusal     { text, retry } — lb-bot refused the merge or the undo
//   card        { v: lib mergeView / mergeRest, busy, onUndo, onDone } — a job, or a merge that needs a look
export function ArtistMergeView({
  entries, heading = 'Also in your library as', canMerge = true, quiet = false, pageArtistId = '', open, onToggle, kept, onKept, choices, from, onFrom,
  keptName,
  preview, onRetry, busy = false, onMerge, refusal = null, onRetryRefused, onDismiss, card = null,
}) {
  const box = useRef(null)
  const toggle = useRef(null)
  // A job's card on show and not ended, or lb-bot known to be merging these
  // names by other means (its list; a job that went silent): the card is the block.
  const running = quiet || (!!card && !card.v.ended)
  const shown = pageArtistId && canMerge ? entries.filter(e => e.id !== pageArtistId) : entries
  useRevealOn(box, [
    open && 'open',
    open && `pair:${kept}|${from}`,
    open && preview && `~answer:${preview.status}`,
    refusal && '~refused',
    card && `~card:${card.v.ended ? 'ended' : 'running'}`,
  ].filter(Boolean), open ? toggle : undefined)
  const pv = preview?.view
  const refused = (
    <Notice tone="danger" role="alert" actions={<>
      {refusal?.retry && <button className="sm" disabled={busy} onClick={onRetryRefused}>Try again</button>}
      <button className="sm quiet" onClick={onDismiss}>Dismiss</button>
    </>}>{refusal?.text}</Notice>
  )
  // The keyboard follows what the press did: to the job when the block becomes
  // it, and back to "Merge into one name…" when the fold shuts with no job, or
  // "Done" puts the job away. Only when it was in here — nothing is taken from
  // wherever else it is — and without the browser's own jump (`reveal` scrolls).
  const cardBox = useRef(null)
  const was = useRef({ open, card: !!card })
  useEffect(() => {
    const had = was.current
    was.current = { open, card: !!card }
    if ((had.open === open && had.card === !!card) || typeof document === 'undefined') return
    const at = document.activeElement
    if (at && at !== document.body && !box.current?.contains(at)) return
    if (card && !had.card) cardBox.current?.focus({ preventScroll: true })
    else if ((had.open && !open && !card) || (had.card && !card)) toggle.current?.focus({ preventScroll: true })
  }, [open, card])
  return (
    <div ref={box}>
      <Grp>{heading}</Grp>
      {/* What the job is doing, said once when its state word changes — not at every count. */}
      <span className="sr-only" role="status">{card ? [card.title, card.v.ended ? card.v.line : card.v.word].filter(Boolean).join(': ') : ''}</span>
      <div className="rows">
        {shown.map(e => (
          <div key={e.id} className="row stack">
            <Cover url={e.art} name={e.name} size={40} round />
            <div className="grow">
              <div className="t" style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>
                {!e.id || e.id === pageArtistId ? e.name : <button className="link plain" onClick={() => goArtist(e.id)}>{e.name}</button>}
              </div>
              <div className="s">{e.sub}</div>
            </div>
          </div>
        ))}
      </div>

      <Roll open={!!card}>
        <div className="pt-3" ref={cardBox} tabIndex={-1} style={{ outline: 'none' }}>{card ? <JobCard {...card} wrap /> : null}</div>
      </Roll>

      {/* lb-bot refused an undo: under the card it was asked from. (A refused merge is said in the fold, by its button.) */}
      <Roll open={!!refusal && !open}>
        <div className="pt-3">{refused}</div>
      </Roll>

      {/* While a job runs its card is the block: nothing else is offered for these names. */}
      <Roll open={canMerge && !running}>
        <div className="acts pt-3">
          <button ref={toggle} aria-expanded={open} disabled={busy} onClick={onToggle}>Merge into one name…</button>
        </div>
      </Roll>

      {/* `slow`: the chooser and the preview are half a screen. */}
      <Roll slow open={canMerge && open && !running}>
        <div className="pt-3">
          <NameChooser question="Which name is kept?" rows={entries} value={kept} onChange={onKept} disabled={busy} />
          {/* Three names: the route merges a pair at a time, so which one is merged into it is asked too. */}
          <Roll open={choices.length > 1}>
            <div className="pt-3">
              <NameChooser question={`Which spelling is merged into ${iso(keptName)}?`} rows={choices} value={from} onChange={onFrom} disabled={busy} />
            </div>
          </Roll>
          <div className="grp" style={{ margin: '14px 0 8px' }}>What changes</div>
          {/* The skeleton, its answer, a refusal, another pair's preview: the box moves to each one's height. */}
          <Fluid>
            <div key={`${kept}|${from}`}>
              {!preview || preview.status === 'loading' ? <PreviewSkeleton />
                : preview.status === 'error' ? (
                  <div className="fadein">
                    <Notice tone="quiet" role="status" title="lb-bot could not say what would change" actions={<>
                      {preview.refusal?.retry && <button className="sm" onClick={onRetry}>Try again</button>}
                      <button className="sm quiet" onClick={onToggle}>Cancel</button>
                    </>}>
                      {preview.refusal?.text}
                    </Notice>
                  </div>
                ) : (
                  <div className="fadein">
                    <p style={{ overflowWrap: 'anywhere' }}>{pv.line}</p>
                    {pv.sample.length > 0 && (
                      <div className="rows arrive mt-2.5">
                        {pv.sample.map(a => (
                          <div key={a.key} className="row"><div className="grow">
                            <div className="t" style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{a.title}</div>
                            <div className="s">{a.sub}</div>
                          </div></div>
                        ))}
                      </div>
                    )}
                    {pv.more ? <p className="muted mt-1.5">{pv.more}</p> : null}
                    {pv.fixed ? <p className="mt-4">{pv.fixed}</p> : null}
                    {/* lb-bot refused the merge: said by the button that asked for it. */}
                    <Roll open={!!refusal && open}>
                      <div className="pt-3">{refused}</div>
                    </Roll>
                    {/* The buttons come with the answer, where the skeleton held their place. */}
                    <div className="acts mt-3">
                      {pv.can && <button className="primary" disabled={busy} aria-busy={busy} onClick={onMerge}>{pv.button}</button>}
                      <button className="quiet" disabled={busy} onClick={onToggle}>Cancel</button>
                    </div>
                  </div>
                )}
            </div>
          </Fluid>
        </div>
      </Roll>
    </div>
  )
}

// The block with its own controller: folded away when there is nothing to
// show — no set holds the artist (which is also the case when lb-bot gave no
// list of sets) and no merge of its names is running or can be undone.
export default function ArtistMerge(options) {
  const merge = useArtistMerge(options)
  return (
    <Roll open={merge.has}>
      <ArtistMergeView {...merge.view} />
    </Roll>
  )
}
