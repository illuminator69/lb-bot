// The one job watch (I-029). A job lb-bot runs in the background — an album
// retag or its undo, an artist name merge or its undo, the discography scan
// that follows an artist match — is followed HERE, outside any component, by
// its id (an override's or a merge's row id; for a scan, the artist's):
//
//   - every surface that shows a job reads this, so two surfaces never
//     disagree about it and never poll it twice;
//   - the job outlives the page that started it: leaving stops nothing, and
//     coming back finds it where it is;
//   - when it ends with nobody showing it, one toast says what happened.
//
// What a poll means is the job's own kind's — its MACHINE: for an album job
// and a merge `lib/overrides.js`'s (`jobStep`: X1 the POST's own state, X2 the
// silence rule, X4 the writes being over), for a scan `lib/artistMatch.js`'s
// (`scanStep`). This file is the clock, the reads and the subscribers.
// `createJobWatch` takes all three from its caller, so jobWatch.test.mjs
// drives it with a fake clock and a scripted lb-bot. The app's own instance
// is `jobs` in lib/jobs.js.
import { POLL_MS, endToast, jobStart, jobStep, opOfState } from './overrides.js'

/** How long a job's end is remembered against a read that is older than it. */
const RECENT_MS = 30_000

/** A kind of job, as the watch needs it:
 *    start(op, now, seed)   the state a job begins in (`seed`: what the caller knows — the POST's own state)
 *    step(state, answer, now)   one poll's answer taken in
 *    running(state)         it is still to be asked about
 *    sig(state)             what of it a surface could show: a poll that changes none of it re-renders nothing
 *    opOf(row)              which job a row found by a read says is running ('' for none) — `adopt`
 *    restKey(state, row)    what to remember its end by against an older read, or '' for nothing
 *    toast(state, row)      the one toast for an end nobody was showing, or null
 *  The album job's is the default, and what a row-and-album job (a merge) is built from. */
export const rowMachine = ({ toast = endToast } = {}) => ({
  start: (op, now, seed) => jobStart(op, now, seed),
  step: jobStep,
  running: state => state.phase === 'polling',
  sig: state => [state.op, state.phase, state.row, state.album?.albumId, state.album?.releaseMbid],
  opOf: row => opOfState(row?.state),
  // At rest (not "still working"): a read older than this must not bring it
  // back. Remembered by the row the job began from when no poll ever read
  // one — an undo whose row was gone at its first poll, which a stale read
  // then followed again and toasted "restored." a second time.
  restKey: (state, row) => ((state.phase === 'settled' || state.phase === 'failed') && row ? `${row.setAt}@${state.op}` : ''),
  toast: (state, row) => toast(state.op, state.phase, row),
})

export function createJobWatch({ readers, machines = {}, now = Date.now, setTimer = setTimeout, clearTimer = clearTimeout }) {
  const fallback = rowMachine()
  const machine = kind => machines[kind] || fallback
  const jobs = new Map()      // "kind:id" → { key, kind, id, state, startRow, view, sig, timer, gen }
  const viewers = new Map()   // "kind:id" → how many surfaces are showing it
  const ended = new Map()     // "kind:id@setAt@op" → when that job ended at rest
  const subscribers = new Set()
  let notify = null
  let generation = 0
  let version = 0             // moves whenever anything a surface could show did

  const keyOf = (kind, id) => `${kind}:${id}`
  const emit = () => { version += 1; subscribers.forEach(fn => fn()) }
  const shown = key => (viewers.get(key) || 0) > 0

  // The snapshot a surface reads: replaced only when something it shows moved,
  // so a poll that brings the same row re-renders nothing.
  function publish(job) {
    const { state } = job
    const sig = JSON.stringify(machine(job.kind).sig(state))
    if (sig === job.sig) return false
    job.sig = sig
    job.view = { kind: job.kind, id: job.id, op: state.op, phase: state.phase, row: state.row,
                 album: state.album, startRow: job.startRow, state, running: machine(job.kind).running(state) }
    return true
  }

  function stop(job) {
    if (job.timer) clearTimer(job.timer)
    job.timer = null
  }

  function schedule(job) {
    job.timer = setTimer(() => { job.timer = null; tick(job) }, POLL_MS)
  }

  async function tick(job) {
    const gen = job.gen
    let answer
    try {
      answer = await readers[job.kind](job.id, job.state)
    } catch {
      answer = { ok: false, status: 0, code: '' }      // no answer: asked again
    }
    // Replaced or dropped while the read was out: its answer is nobody's.
    if (jobs.get(job.key) !== job || job.gen !== gen) return
    const m = machine(job.kind)
    job.state = m.step(job.state, answer, now())
    const changed = publish(job)
    if (m.running(job.state)) {
      schedule(job)
      if (changed) emit()
      return
    }
    finish(job)
  }

  function finish(job) {
    const m = machine(job.kind)
    const known = job.state.row || job.startRow
    for (const [key, at] of ended) if (now() - at >= RECENT_MS) ended.delete(key)
    const rest = m.restKey(job.state, known)
    if (rest) ended.set(`${job.key}@${rest}`, now())
    if (!shown(job.key)) {
      jobs.delete(job.key)
      const toast = m.toast(job.state, known)
      if (toast && notify) notify(toast)
    }
    emit()
  }

  function follow({ kind, id, op, seed, row = null, album = null, startRow = null }) {
    if (!readers[kind]) throw new Error(`jobWatch: no reader for a job of kind "${kind}"`)
    const key = keyOf(kind, id)
    const old = jobs.get(key)
    if (old) stop(old)
    const state = { ...machine(kind).start(op, now(), seed), row, album }
    const job = { key, kind, id, state, startRow, view: null, sig: '', timer: null, gen: ++generation }
    jobs.set(key, job)
    publish(job)
    schedule(job)
    emit()
    return job
  }

  return {
    /** A job this page began. `state` is the POST's own (X1); `row` the row as
     *  it was before, when there was one (X7 reads it at the job's end). */
    start({ kind, id, op, state, row = null }) {
      follow({ kind, id, op, seed: state, startRow: row })
    },

    /** A row a read found. True when it is a running job, which is then
     *  followed — once, however many surfaces find it. */
    adopt(kind, row, album = null) {
      const m = machine(kind)
      const op = m.opOf(row)
      if (!op || !row.id) return false
      const key = keyOf(kind, row.id)
      const job = jobs.get(key)
      if (job && m.running(job.state) && job.state.op === op) return true
      const endedAt = ended.get(`${key}@${row.setAt}@${op}`)
      if (endedAt != null && now() - endedAt < RECENT_MS) return false
      follow({ kind, id: row.id, op, seed: row.state, row, album, startRow: row })
      return true
    },

    get(kind, id) {
      return jobs.get(keyOf(kind, id))?.view
    },

    subscribe(fn) {
      subscribers.add(fn)
      return () => { subscribers.delete(fn) }
    },

    /** A number that changes with every change: what a surface that shows
     *  several jobs subscribes to (`useSyncExternalStore`'s snapshot). */
    version: () => version,

    /** A surface is showing this job (whether or not it exists yet). An ended
     *  job stays for as long as one is, and goes with the last of them. */
    attach(kind, id) {
      const key = keyOf(kind, id)
      viewers.set(key, (viewers.get(key) || 0) + 1)
      let attached = true
      return () => {
        if (!attached) return
        attached = false
        const left = (viewers.get(key) || 1) - 1
        if (left > 0) { viewers.set(key, left); return }
        viewers.delete(key)
        const job = jobs.get(key)
        if (job && !machine(job.kind).running(job.state)) { jobs.delete(key); emit() }
      }
    },

    /** Forget an ended job (its surface is done with it). */
    dismiss(kind, id) {
      const key = keyOf(kind, id)
      const job = jobs.get(key)
      if (job && !machine(job.kind).running(job.state)) { jobs.delete(key); emit() }
    },

    /** Where the toast for a job that ended unwatched goes: `fn({text, level})`. */
    setNotifier(fn) { notify = fn },

    /** How many ended jobs are still remembered against an older read. */
    remembered: () => ended.size,
  }
}
