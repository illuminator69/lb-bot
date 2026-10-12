// Run: node --test web/src/lib/jobWatch.test.mjs   (no dependency, no network)
// The one job watch (I-029): a job is followed once, outside any page, by
// whatever surfaces show it — with a fake clock and a scripted lb-bot.
import test from 'node:test'
import assert from 'node:assert/strict'
// A name in a sentence is isolated (lib/text.js): what an expectation writes around one.
const I = s => '\u2068' + s + '\u2069'
import { createJobWatch } from './jobWatch.js'
import { POLL_MS, SILENT_MS } from './overrides.js'
import { scanMachine, scanStart } from './artistMatch.js'

const row = (over = {}) => ({
  id: 'ov1', state: 'applying', releaseMbid: 'r-new', prevReleaseMbid: 'r-old', rgid: 'rg-1',
  artist: 'Tame Impala', album: 'Currents', albumId: '', coverUrl: '', setAt: 1000,
  done: 0, total: 6, failures: [], ...over,
})
const ok = (over = {}) => ({ ok: true, row: row(over), album: { albumId: over.albumId || '', override: row(over) } })
const gone = { ok: false, status: 404, code: 'no_override' }
const flush = () => new Promise(resolve => setImmediate(resolve))

// A watch on a clock the test moves, reading answers the test scripts. When
// the script runs out lb-bot repeats `idle`.
function harness(script = [], idle = { ok: false, status: 502, code: '' }) {
  const h = { t: 0, timers: [], reads: [], toasts: [], changes: 0 }
  const read = kind => async (id, state) => {
    h.reads.push(kind === 'album' ? id : `${kind}:${id}:${state?.taskId || ''}`)
    const answer = await (script.length ? script.shift() : idle)
    if (answer instanceof Error) throw answer
    return answer
  }
  h.watch = createJobWatch({
    readers: { album: read('album'), scan: read('scan') },
    machines: { scan: scanMachine },
    now: () => h.t,
    setTimer: (fn, ms) => { const timer = { fn, at: h.t + ms }; h.timers.push(timer); return timer },
    clearTimer: timer => { const i = h.timers.indexOf(timer); if (i >= 0) h.timers.splice(i, 1) },
  })
  h.watch.setNotifier(toast => h.toasts.push(toast))
  h.watch.subscribe(() => { h.changes += 1 })
  // Run the next timer at its own time; false when none is waiting.
  h.tick = async () => {
    const timer = h.timers.shift()
    if (!timer) return false
    h.t = timer.at
    timer.fn()
    await flush()
    return true
  }
  h.get = (id = 'ov1') => h.watch.get('album', id)
  return h
}

test('a job is followed from the POST’s own state, every two seconds, to its end', async () => {
  const h = harness([ok({ done: 2 }), ok({ done: 6 }), ok({ state: 'applied', done: 6, albumId: 'al-new' })])
  const detach = h.watch.attach('album', 'ov1')
  h.watch.start({ kind: 'album', id: 'ov1', op: 'apply', state: 'applying', row: row({ state: 'applied', releaseMbid: 'r-old' }) })
  // At once, before any poll: the job exists, and says what the POST said.
  assert.deepEqual([h.get().op, h.get().phase, h.get().row], ['apply', 'polling', null])
  assert.equal(h.get().startRow.releaseMbid, 'r-old')
  assert.equal(h.timers.length, 1)
  assert.equal(h.timers[0].at, POLL_MS)
  await h.tick()
  assert.deepEqual([h.get().phase, h.get().row.done, h.reads.length], ['polling', 2, 1])
  await h.tick()
  await h.tick()
  assert.deepEqual([h.get().phase, h.get().album.albumId, h.t], ['settled', 'al-new', 3 * POLL_MS])
  // Ended: nothing more is asked, and a surface that shows it gets no toast.
  assert.equal(h.timers.length, 0)
  assert.deepEqual(h.toasts, [])
  // It stays for the surface showing it, and goes when that surface does.
  assert.equal(h.get().phase, 'settled')
  detach()
  assert.equal(h.get(), undefined)
})

test('the snapshot is the same object until something changes', async () => {
  const h = harness([ok({ done: 2 }), ok({ done: 2 }), ok({ done: 3 })])
  h.watch.attach('album', 'ov1')
  h.watch.start({ kind: 'album', id: 'ov1', op: 'apply', state: 'applying' })
  await h.tick()
  const first = h.get()
  const changes = h.changes
  const version = h.watch.version()
  await h.tick()
  assert.equal(h.get(), first)
  assert.equal(h.changes, changes)
  assert.equal(h.watch.version(), version)
  await h.tick()
  assert.notEqual(h.get(), first)
  assert.equal(h.get().row.done, 3)
  assert.ok(h.changes > changes)
  // The version a surface with several jobs reads moves with every change.
  assert.equal(h.watch.version() - version, h.changes - changes)
})

test('a job that ends with nobody showing it is one toast that names the album, and is forgotten', async () => {
  const h = harness([ok({ done: 3 }), ok({ state: 'applied', done: 6 })])
  const detach = h.watch.attach('album', 'ov1')
  h.watch.start({ kind: 'album', id: 'ov1', op: 'apply', state: 'applying' })
  await h.tick()
  // The page is left while the job runs: the watch goes on.
  detach()
  assert.equal(h.get().phase, 'polling')
  await h.tick()
  assert.deepEqual(h.toasts, [{ text: I('Currents') + ': lb-bot wrote the tags of 6 files.', level: 'info' }])
  assert.equal(h.get(), undefined)
  assert.equal(h.timers.length, 0)
})

test('two surfaces and one job: it is polled once', async () => {
  const h = harness([ok({ done: 1 }), ok({ done: 2 })])
  h.watch.attach('album', 'ov1')
  h.watch.attach('album', 'ov1')
  h.watch.start({ kind: 'album', id: 'ov1', op: 'apply', state: 'applying' })
  // A second surface reads the album and finds the job running: it is the
  // same job, as it was — not followed afresh (which would start its
  // three minutes of silence over at every such read).
  const before = h.get()
  const changes = h.changes
  assert.equal(h.watch.adopt('album', row({ done: 1 })), true)
  assert.equal(h.watch.adopt('album', row({ done: 1 })), true)
  assert.equal(h.get(), before)
  assert.equal(h.changes, changes)
  assert.equal(h.timers.length, 1)
  await h.tick()
  await h.tick()
  assert.equal(h.reads.length, 2)
})

test('a job replaced while it is being asked about: the old one’s poll and answer are nobody’s', async () => {
  // Replaced before its first poll: one timer, one read per tick.
  const h = harness([ok({ state: 'removing', done: 1 })])
  h.watch.attach('album', 'ov1')
  h.watch.start({ kind: 'album', id: 'ov1', op: 'apply', state: 'applying' })
  h.watch.start({ kind: 'album', id: 'ov1', op: 'remove', state: 'removing' })
  assert.equal(h.timers.length, 1)
  await h.tick()
  assert.equal(h.reads.length, 1)
  assert.deepEqual([h.get().op, h.get().row.done, h.timers.length], ['remove', 1, 1])

  // Replaced while its read is out. That answer is the old job's: it neither
  // moves the new one nor keeps the old one polling beside it…
  let answer
  const late = harness([new Promise(resolve => { answer = resolve }), ok({ state: 'removing', done: 2 })])
  late.watch.attach('album', 'ov1')
  late.watch.start({ kind: 'album', id: 'ov1', op: 'apply', state: 'applying' })
  await late.tick()                                   // the apply's poll is out
  late.watch.start({ kind: 'album', id: 'ov1', op: 'remove', state: 'removing' })
  answer(ok({ state: 'applying', done: 3 }))
  await flush()
  assert.deepEqual([late.get().op, late.get().phase, late.get().row], ['remove', 'polling', null])
  assert.equal(late.timers.length, 1)
  await late.tick()
  assert.deepEqual([late.get().op, late.get().row.done, late.timers.length], ['remove', 2, 1])
  // …nor, when it says the old job ended and nobody is looking, is it a
  // toast, or the end of the job that replaced it.
  const unseen = harness([new Promise(resolve => { answer = resolve })])
  unseen.watch.start({ kind: 'album', id: 'ov1', op: 'apply', state: 'applying' })
  await unseen.tick()
  unseen.watch.start({ kind: 'album', id: 'ov1', op: 'remove', state: 'removing' })
  answer(ok({ state: 'applied', done: 6 }))
  await flush()
  assert.deepEqual(unseen.toasts, [])
  assert.deepEqual([unseen.get()?.op, unseen.get()?.phase], ['remove', 'polling'])
})

test('a running job found by a read is followed from its row, which shows at once', async () => {
  const h = harness([gone])
  h.watch.attach('album', 'ov1')
  const album = { albumId: 'al-1', override: row({ state: 'removing', done: 2, total: 4 }) }
  assert.equal(h.watch.adopt('album', album.override, album), true)
  assert.deepEqual([h.get().op, h.get().phase, h.get().row.done, h.get().album.albumId], ['remove', 'polling', 2, 'al-1'])
  await h.tick()
  assert.equal(h.get().phase, 'settled')
  // A row that is not running is not a job.
  for (const state of ['applied', 'failed']) {
    assert.equal(h.watch.adopt('album', row({ id: 'ov2', state })), false)
  }
  assert.equal(h.get('ov2'), undefined)
})

test('an undo that failed before the first poll is seen (X1)', async () => {
  const h = harness([ok({ state: 'failed', failures: [{ title: 'Nangs', reason: 'not found; press Undo again' }] })])
  h.watch.attach('album', 'ov1')
  h.watch.start({ kind: 'album', id: 'ov1', op: 'remove', state: 'removing' })
  await h.tick()
  assert.equal(h.get().phase, 'failed')
  assert.equal(h.get().row.failures[0].reason, 'not found; press Undo again')
})

test('three minutes of silence stop the watch and say where to look', async () => {
  const h = harness([], ok({ done: 2 }))
  h.watch.start({ kind: 'album', id: 'ov1', op: 'apply', state: 'applying' })
  while (await h.tick()) { /* until nothing is scheduled */ }
  assert.equal(h.t, SILENT_MS)
  assert.equal(h.reads.length, SILENT_MS / POLL_MS)
  assert.deepEqual(h.toasts, [{ text: I('Currents') + ': still working — see System › Overrides', level: 'info' }])
  // Shown by a surface, it stays as "still working" and is asked no more.
  const shown = harness([], ok({ done: 2 }))
  shown.watch.attach('album', 'ov1')
  shown.watch.start({ kind: 'album', id: 'ov1', op: 'apply', state: 'applying' })
  while (await shown.tick()) { /* … */ }
  assert.equal(shown.get().phase, 'timeout')
  assert.deepEqual(shown.toasts, [])
  // lb-bot still reports it running: a later read may pick it up again (X3).
  assert.equal(shown.watch.adopt('album', row({ done: 2 })), true)
  assert.equal(shown.get().phase, 'polling')
})

test('a read that fails is no answer: the job is asked again', async () => {
  const h = harness([new Error('boom'), { ok: false, status: 500, code: '' }, ok({ state: 'applied', done: 6 })])
  h.watch.attach('album', 'ov1')
  h.watch.start({ kind: 'album', id: 'ov1', op: 'apply', state: 'applying' })
  await h.tick()
  await h.tick()
  assert.equal(h.get().phase, 'polling')
  await h.tick()
  assert.equal(h.get().phase, 'settled')
})

test('a read older than a job’s end does not bring the job back; a new job on the row does', async () => {
  const h = harness([ok({ state: 'applied', done: 6 })])
  const detach = h.watch.attach('album', 'ov1')
  h.watch.start({ kind: 'album', id: 'ov1', op: 'apply', state: 'applying' })
  await h.tick()
  assert.equal(h.get().phase, 'settled')
  // A page's own read of the album, begun before the end, lands after it.
  assert.equal(h.watch.adopt('album', row({ done: 5 })), false)
  assert.equal(h.get().phase, 'settled')
  detach()
  assert.equal(h.watch.adopt('album', row({ done: 5 })), false)
  assert.equal(h.get(), undefined)
  // The same row under a new job — applied again (a new stamp), or being undone.
  assert.equal(h.watch.adopt('album', row({ setAt: 2000 })), true)
  assert.equal(h.watch.adopt('album', row({ id: 'ov1', state: 'removing' })), true)
  // And much later the row is believed again, whatever it says.
  const late = harness([ok({ state: 'applied', done: 6 })])
  late.watch.start({ kind: 'album', id: 'ov1', op: 'apply', state: 'applying' })
  await late.tick()
  late.t += 60_000
  assert.equal(late.watch.adopt('album', row()), true)
})

test('an undo that ended at its first poll is not brought back by an older read (one toast)', async () => {
  // The row is gone at the very first poll, so no poll ever read it: the end
  // is remembered by the row the undo began from.
  const h = harness([gone, gone])
  h.watch.start({ kind: 'album', id: 'ov1', op: 'remove', state: 'removing', row: row({ state: 'applied', done: 6 }) })
  await h.tick()
  assert.deepEqual(h.toasts, [{ text: I('Currents') + ': restored.', level: 'info' }])
  // A page's read of the album, begun before the end, still says `removing`.
  assert.equal(h.watch.adopt('album', row({ state: 'removing', done: 3 })), false)
  assert.equal(h.get(), undefined)
  while (await h.tick()) { /* nothing is scheduled */ }
  assert.equal(h.toasts.length, 1)
})

test('what the watch remembers of ended jobs does not grow for ever', async () => {
  const h = harness([], gone)
  for (let n = 0; n < 5; n++) {
    h.watch.start({ kind: 'album', id: `ov${n}`, op: 'remove', state: 'removing', row: row({ id: `ov${n}` }) })
  }
  // All five end (their rows are gone); a minute later a sixth does.
  for (let n = 0; n < 5; n++) await h.tick()
  assert.equal(h.watch.remembered(), 5)
  h.t += 60_000
  h.watch.start({ kind: 'album', id: 'ov9', op: 'remove', state: 'removing', row: row({ id: 'ov9' }) })
  await h.tick()
  assert.equal(h.watch.remembered(), 1)
})

test('a job this page starts replaces whatever was there for the row', async () => {
  const h = harness([ok({ state: 'applied', done: 6 }), ok({ state: 'removing', done: 1 }), gone])
  h.watch.attach('album', 'ov1')
  h.watch.start({ kind: 'album', id: 'ov1', op: 'apply', state: 'applying' })
  await h.tick()
  assert.equal(h.get().phase, 'settled')
  // Undo, pressed on the result.
  h.watch.start({ kind: 'album', id: 'ov1', op: 'remove', state: 'removing', row: row({ state: 'applied', done: 6 }) })
  assert.deepEqual([h.get().op, h.get().phase], ['remove', 'polling'])
  assert.equal(h.timers.length, 1)
  await h.tick()
  await h.tick()
  assert.deepEqual([h.get().op, h.get().phase], ['remove', 'settled'])
  // The row the undo began from is what says whether the album's id moved (X7).
  assert.equal(h.get().startRow.state, 'applied')
})

test('an ended job a surface is done with is dropped; one still running is not', async () => {
  const h = harness([ok({ state: 'applied', done: 6 })])
  h.watch.attach('album', 'ov1')
  h.watch.start({ kind: 'album', id: 'ov1', op: 'apply', state: 'applying' })
  h.watch.dismiss('album', 'ov1')
  assert.equal(h.get().phase, 'polling')
  await h.tick()
  h.watch.dismiss('album', 'ov1')
  assert.equal(h.get(), undefined)
})

test('a kind nobody can read is not followed', () => {
  const h = harness()
  assert.throws(() => h.watch.start({ kind: 'merge', id: 'm1', op: 'apply', state: 'applying' }), /no reader/)
})

// ── another kind of job on the same watch: the scan that follows an artist match (W3) ──
const RESULT = { library: { album_ids: ['a1', 'a2', 'a3', 'a4', 'a5'] }, releases: [
  { status: 'complete', navidrome_album_ids: ['a1', 'a2'] }, { status: 'incomplete', navidrome_album_ids: ['a3', 'a4'] }] }
const task = (over = {}) => ({ ok: true, task: { status: 'running', done: 0, total: 0, current: '', ...over } })
const scan = (h, op = 'set') => h.watch.start({ kind: 'scan', id: 'nd-1', op, state: scanStart(op, { ok: true, task_id: 't1' }, h.t), row: { name: 'The Band' } })

test('a scan is followed by the artist’s id, asked about by its task, once — however many homes show it', async () => {
  const h = harness([task({ done: 3, total: 9, current: 'First' }), task({ done: 9, total: 9 }), task({ status: 'complete', result: RESULT })])
  const pane = h.watch.attach('scan', 'nd-1')
  const page = h.watch.attach('scan', 'nd-1')       // the artist page shows the same artist
  scan(h)
  const job = () => h.watch.get('scan', 'nd-1')
  assert.deepEqual([job().op, job().phase, job().running, job().state.taskId], ['set', 'scanning', true, 't1'])
  assert.equal(h.timers.length, 1, 'one poll, for two surfaces')
  await h.tick()
  assert.deepEqual([job().state.done, job().state.total, job().state.current], [3, 9, 'First'])
  assert.deepEqual(h.reads, ['scan:nd-1:t1'])
  await h.tick()
  await h.tick()
  assert.deepEqual([job().phase, job().running, job().state.lined], ['done', false, { held: 4, total: 5 }])
  assert.equal(h.timers.length, 0, 'an ended scan is not asked about again')
  assert.equal(h.reads.length, 3)
  assert.deepEqual(h.toasts, [], 'a surface is showing it: no toast')
  // It stays for as long as one of them shows it.
  pane()
  assert.ok(job())
  page()
  assert.equal(job(), undefined)
})

test('leaving the pane stops nothing: a scan that ends with nobody showing it is one toast that names the artist', async () => {
  const h = harness([task({ done: 1, total: 9 }), task({ status: 'complete', result: RESULT })])
  const pane = h.watch.attach('scan', 'nd-1')
  scan(h)
  await h.tick()
  pane()                                               // the pane is left
  assert.equal(h.watch.get('scan', 'nd-1').phase, 'scanning', 'still followed')
  await h.tick()
  assert.deepEqual(h.toasts, [{ text: I('The Band') + ': 4 of 5 albums line up.', level: 'info' }])
  assert.equal(h.watch.get('scan', 'nd-1'), undefined)
  assert.equal(h.timers.length, 0)
})

test('a scan’s other endings, unwatched: what the card would have said, once', async () => {
  const end = async (answers, op = 'set') => {
    const h = harness(answers)
    scan(h, op)
    for (let i = 0; i < answers.length + 2 && h.timers.length; i++) await h.tick()
    return h.toasts
  }
  assert.deepEqual(await end([task({ status: 'error', error: 'MusicBrainz did not answer' })]),
                   [{ text: I('The Band') + ': MusicBrainz did not answer', level: 'error' }])
  assert.deepEqual(await end([{ ok: false, status: 404 }], 'remove'),
                   [{ text: I('The Band') + ': The scan did not finish: lb-bot restarted while it ran.', level: 'error' }])
  assert.deepEqual(await end([task({ status: 'complete', result: { releases: [] } })], 'remove'),
                   [{ text: I('The Band') + ': lb-bot has read the discography again.', level: 'info' }])
})

test('a scan lb-bot stops answering about: asked again for three minutes since its last answer, then said, and the asking stops', async () => {
  const h = harness([task({ done: 1, total: 9 })])    // then lb-bot answers nothing
  const pane = h.watch.attach('scan', 'nd-1')
  scan(h)
  await h.tick()
  const heard = h.t
  while (h.t - heard < SILENT_MS - POLL_MS) await h.tick()
  assert.equal(h.watch.get('scan', 'nd-1').phase, 'scanning')
  await h.tick()
  await h.tick()
  assert.equal(h.watch.get('scan', 'nd-1').phase, 'silent')
  assert.equal(h.timers.length, 0)
  pane()
})

test('an album job and a scan with the same id are two jobs', async () => {
  const h = harness([ok({ done: 2 }), task({ done: 1, total: 2 })])
  h.watch.attach('album', 'x')
  h.watch.attach('scan', 'x')
  h.watch.start({ kind: 'album', id: 'x', op: 'apply', state: 'applying' })
  h.watch.start({ kind: 'scan', id: 'x', op: 'set', state: scanStart('set', { ok: true, task_id: 't9' }, h.t), row: { name: 'X' } })
  await h.tick()
  await h.tick()
  assert.equal(h.watch.get('album', 'x').row.done, 2)
  assert.equal(h.watch.get('scan', 'x').state.done, 1)
})
