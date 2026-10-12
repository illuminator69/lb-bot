// Run: node --test web/src/lib/overrides.test.mjs   (no dependency, no network)
// What lb-bot's own web UI may say about an album override (I-029): the
// contract's state list (§ Client behaviour, step 7) against the album page's
// three places — the line under the title, the edition block and its preview,
// the cover grid — and the toast for a job that ends away from its page.
import test from 'node:test'
import assert from 'node:assert/strict'
// A name in a sentence is isolated (lib/text.js): what an expectation writes around one.
const I = s => '\u2068' + s + '\u2069'
import {
  POLL_MS, SILENT_MS, parseMbRef, parseReleaseRef,
  jobStart, jobStep, opOfState, isRunning, writesOver,
  jobView, restView, changedParts, endToast, releaseUnchanged, rowGroup,
  startedId, notSaid, albumQuiet, undoSays, changeCoverLink, COVER_HELD,
  previewSummary, countsLine, releaseLine, slotLabel, previewRows, commitSentence, commitView,
  refusalView, poorMatchQuestion, coverQuery, coverThumbUrl,
  editionLine, variantName, variantOfRelease, carriesSelection, selectionOfRelease, albumLabel, NOT_LISTED_YET, NO_SLOT_NOTE, afterJobSteps, lineAtRest, variantInLine,
} from './overrides.js'

const REL = 'e063bd58-745a-4f7f-8340-3b273b561ff8'
const OTHER = '035d44de-deef-48d8-8590-f9fcff9a14cb'
const row = (over = {}) => ({
  id: 'ov1', state: 'applying', releaseMbid: REL, prevReleaseMbid: OTHER, rgid: 'rg-1',
  artist: 'Tame Impala', album: 'Currents', albumId: '', coverUrl: '', setAt: 1000,
  done: 0, total: 6, failures: [], ...over,
})
const ok = (over = {}) => ({ ok: true, row: row(over), album: { albumId: over.albumId || '', override: row(over) } })
const gone = { ok: false, status: 404, code: 'no_override' }
const down = { ok: false, status: 502, code: '' }

// ── a pasted id or address ───────────────────────────────────────────────────
test('a release is named by its id or by its MusicBrainz address, and by nothing else', () => {
  for (const text of [REL, ` ${REL.toUpperCase()} `, `https://musicbrainz.org/release/${REL}`,
                      `musicbrainz.org/release/${REL}/cover-art`, `https://beta.musicbrainz.org/release/${REL}?x=1#y`,
                      `http://musicbrainz.org/release/${REL}/`]) {
    assert.equal(parseReleaseRef(text), REL, text)
  }
  for (const text of ['', '   ', 'Currents', REL.slice(1), `${REL}0`,
                      `https://musicbrainz.org/release-group/${REL}`, `https://musicbrainz.org/artist/${REL}`,
                      `https://musicbrainz.org.evil.example/release/${REL}`, `https://evil.example/release/${REL}`,
                      `https://notmusicbrainz.org/release/${REL}`, `https://a.b.musicbrainz.org/release/${REL}`,
                      `https://musicbrainz.org/release/${REL}x`, null, undefined, 7]) {
    assert.equal(parseReleaseRef(text), '', String(text))
  }
  // The artist twin, for the artist picker: an artist address is no release.
  assert.equal(parseMbRef(`https://musicbrainz.org/artist/${REL}`, 'artist'), REL)
  assert.equal(parseMbRef(`https://musicbrainz.org/release/${REL}`, 'artist'), '')
  assert.equal(parseMbRef(REL, 'artist'), REL)
})

// ── the poll, as one machine (X1, X2, X3, X4) ────────────────────────────────
test('an apply is followed from the POST’s own state to applied', () => {
  let job = jobStart('apply', 0, 'applying')
  assert.deepEqual([job.phase, job.sawActive, job.row], ['polling', true, null])
  job = jobStep(job, ok({ done: 2 }), POLL_MS)
  assert.deepEqual([job.phase, job.row.done], ['polling', 2])
  job = jobStep(job, ok({ state: 'applied', done: 6, albumId: 'al-new' }), 2 * POLL_MS)
  assert.equal(job.phase, 'settled')
  assert.equal(job.album.albumId, 'al-new')
  // An ended job takes no further answer.
  assert.equal(jobStep(job, ok({ state: 'failed' }), 3 * POLL_MS), job)
})

test('a job already failed at the first tick ends as failed (X1)', () => {
  const apply = jobStep(jobStart('apply', 0, 'applying'), ok({ state: 'failed', failures: [{ title: 'x', reason: 'y' }] }), POLL_MS)
  assert.equal(apply.phase, 'failed')
  // A remove: lb-bot flips the row to `removing` before it answers the POST,
  // and can reach `failed` before the first poll. The POST said `removing`.
  const remove = jobStep(jobStart('remove', 0, 'removing'), ok({ state: 'failed' }), POLL_MS)
  assert.equal(remove.phase, 'failed')
  // Without that seed an `applied` read is the row before the flip, not an end.
  const early = jobStep(jobStart('remove', 0, ''), ok({ state: 'applied' }), POLL_MS)
  assert.equal(early.phase, 'polling')
  // Once the active state was seen, a row back at rest is a remove that failed.
  const back = jobStep(jobStep(early, ok({ state: 'removing' }), 2 * POLL_MS), ok({ state: 'applied' }), 3 * POLL_MS)
  assert.equal(back.phase, 'failed')
})

test('a remove ends on the 404 that says the row is gone; an apply’s row vanishing is its own ending', () => {
  let job = jobStart('remove', 0, 'removing')
  job = jobStep(job, ok({ state: 'removing', done: 3 }), POLL_MS)
  job = jobStep(job, gone, 2 * POLL_MS)
  assert.equal(job.phase, 'settled')
  // The last row is kept: the result is said of the album it names.
  assert.equal(job.row.album, 'Currents')
  assert.equal(jobStep(jobStart('apply', 0, 'applying'), gone, POLL_MS).phase, 'gone')
  // A 404 with another code, or none, is no answer about the row.
  assert.equal(jobStep(jobStart('remove', 0, 'removing'), { ok: false, status: 404, code: '' }, POLL_MS).phase, 'polling')
  assert.equal(jobStep(jobStart('remove', 0, 'removing'), { ok: false, status: 404, code: 'unknown_album' }, POLL_MS).phase, 'polling')
})

test('three minutes of silence end the watch; a job that moves is never timed out (X2)', () => {
  let job = jobStart('apply', 0, 'applying')
  // done moves every two minutes: five minutes in, still followed.
  job = jobStep(job, ok({ done: 1 }), 120_000)
  job = jobStep(job, ok({ done: 2 }), 240_000)
  job = jobStep(job, ok({ done: 2 }), 300_000)
  assert.equal(job.phase, 'polling')
  // Nothing moves for three minutes after the last change.
  job = jobStep(job, ok({ done: 2 }), 240_000 + SILENT_MS - 1)
  assert.equal(job.phase, 'polling')
  job = jobStep(job, ok({ done: 2 }), 240_000 + SILENT_MS)
  assert.equal(job.phase, 'timeout')
  // A change of state alone is movement too.
  let wait = jobStep(jobStart('remove', 0, 'removing'), ok({ state: 'removing' }), 1000)
  wait = jobStep(wait, ok({ state: 'failed' }), 170_000)
  assert.equal(wait.phase, 'failed')
  // Polls that fail are not answers: only the clock moves, and the last row stays.
  let dark = jobStep(jobStart('apply', 0, 'applying'), ok({ done: 4 }), 1000)
  dark = jobStep(dark, down, 100_000)
  assert.deepEqual([dark.phase, dark.row.done], ['polling', 4])
  dark = jobStep(dark, down, SILENT_MS)
  assert.equal(dark.phase, 'timeout')
  // An answer that lands at the deadline is still judged first.
  const late = jobStep(jobStep(jobStart('apply', 0, 'applying'), ok(), 1000), ok({ state: 'applied' }), SILENT_MS + 5000)
  assert.equal(late.phase, 'settled')
})

test('a running job found by a read is followed by what its row says', () => {
  assert.equal(opOfState('applying'), 'apply')
  assert.equal(opOfState('removing'), 'remove')
  for (const state of ['applied', 'failed', '', undefined, 'merging']) assert.equal(opOfState(state), '', String(state))
  assert.ok(isRunning('applying') && isRunning('removing'))
  assert.ok(!isRunning('applied') && !isRunning('failed') && !isRunning(undefined))
})

test('“the writes are over” counts failures, and only while the row is still active (X4)', () => {
  assert.equal(writesOver(row({ done: 6, total: 6 })), true)
  assert.equal(writesOver(row({ done: 5, total: 6, failures: [{ title: 'x', reason: 'y' }] })), true)
  assert.equal(writesOver(row({ done: 5, total: 6 })), false)
  assert.equal(writesOver(row({ done: 0, total: 0 })), false)
  assert.equal(writesOver(row({ state: 'removing', done: 6, total: 6 })), true)
  assert.equal(writesOver(row({ state: 'applied', done: 6, total: 6 })), false)
  assert.equal(writesOver(null), false)
  // A row with no list of failures is a row with none.
  assert.equal(writesOver({ state: 'applying', done: 6, total: 6 }), true)
})

// ── what a job says: applying · waiting · applied · partial · failed · removing · restored · silent ──
test('applying: the whole job’s count, then the wait for Navidrome with no bar and no count', () => {
  const start = jobView('apply', 'polling', null)
  assert.deepEqual([start.tone, start.word, start.line, start.bar, start.count, start.ended],
                   ['run', 'Retagging', 'Writing tags…', false, '', false])
  const mid = jobView('apply', 'polling', row({ done: 3 }))
  assert.deepEqual([mid.line, mid.bar, mid.percent, mid.count], ['Writing tags… 3 of 6', true, 50, '3 of 6'])
  const wait = jobView('apply', 'polling', row({ done: 6 }))
  assert.deepEqual([wait.word, wait.line, wait.bar, wait.count],
                   ['Waiting for Navidrome', 'Waiting for Navidrome to read the album again…', false, ''])
  // One failed file does not hold the bar below the end for ever.
  assert.equal(jobView('apply', 'polling', row({ done: 5, failures: [{ title: 'x', reason: 'y' }] })).bar, false)
  // Nothing to write: no "0 of 0", and no claim about Navidrome either.
  const none = jobView('apply', 'polling', row({ total: 0 }))
  assert.deepEqual([none.line, none.bar, none.count], ['Working…', false, ''])
  assert.equal(start.canUndo || mid.canUndo || wait.canUndo, false)
})

test('applied says what was written; with failures it counts problems, not files (X9)', () => {
  const done = jobView('apply', 'settled', row({ state: 'applied', done: 6 }))
  assert.deepEqual([done.tone, done.word, done.line, done.problems, done.problemsHeading, done.ended, done.canUndo, done.undoLabel],
                   ['ok', 'Retagged', 'lb-bot wrote the tags of 6 files.', [], '', true, true, 'Undo'])
  assert.equal(jobView('apply', 'settled', row({ state: 'applied', done: 1, total: 1 })).line, 'lb-bot wrote the tags of 1 file.')
  const part = jobView('apply', 'settled', row({ state: 'applied', done: 5, failures: [
    { title: 'Nangs', reason: 'the file is read-only' }, { title: 'Cover image', reason: 'the Archive answered 500' }] }))
  assert.deepEqual([part.tone, part.line, part.problemsHeading],
                   ['wait', 'lb-bot wrote the tags of 5 files.', '2 problems'])
  assert.deepEqual(part.problems, [{ title: I('Nangs'), reason: 'the file is read-only' },
                                   { title: I('Cover image'), reason: 'the Archive answered 500' }])
  assert.equal(jobView('apply', 'settled', row({ state: 'applied', done: 5, failures: [{ title: 'a', reason: 'b' }] })).problemsHeading, '1 problem')
  // Applied, 0 of 0: the row says nothing was written, and not why (R11).
  assert.equal(jobView('apply', 'settled', row({ state: 'applied', total: 0 })).line, 'Nothing was rewritten.')
})

test('failed says how far it got, with lb-bot’s own lines, and offers the undo', () => {
  const v = jobView('apply', 'failed', row({ state: 'failed', done: 2, failures: [
    { title: '', reason: 'lb-bot was restarted while it worked; undo this change, or apply it again' }] }))
  assert.deepEqual([v.tone, v.word, v.line, v.problemsHeading, v.ended, v.canUndo],
                   ['bad', 'Did not finish', 'lb-bot did not finish writing the tags (2 of 6 files written).', '1 problem', true, true])
  assert.equal(v.problems[0].reason, 'lb-bot was restarted while it worked; undo this change, or apply it again')
  // A job that wrote nothing (X14).
  assert.equal(jobView('apply', 'failed', row({ state: 'failed', total: 0 })).line, 'lb-bot did not retag this album.')
})

test('removing, restored, and an undo that could not finish', () => {
  const mid = jobView('remove', 'polling', row({ state: 'removing', done: 1, total: 4 }))
  assert.deepEqual([mid.word, mid.line, mid.percent, mid.count], ['Restoring', 'Putting the previous tags back… 1 of 4', 25, '1 of 4'])
  assert.equal(jobView('remove', 'polling', row({ state: 'removing', done: 4, total: 4 })).line,
               'Waiting for Navidrome to read the album again…')
  const done = jobView('remove', 'settled', row({ state: 'removing', done: 4, total: 4 }))
  assert.deepEqual([done.tone, done.word, done.line, done.ended, done.canUndo], ['ok', 'Restored', 'Restored.', true, false])
  // Its row is gone even when no poll ever read it.
  assert.equal(jobView('remove', 'settled', null).line, 'Restored.')
  const bad = jobView('remove', 'failed', row({ state: 'failed', done: 3, total: 4, failures: [
    { title: 'Nangs', reason: 'the file was not found; press Undo again to give up on it' }] }))
  assert.deepEqual([bad.tone, bad.word, bad.line, bad.canUndo, bad.undoLabel],
                   ['bad', 'Not restored', 'lb-bot could not put everything back.', true, 'Undo again'])
  assert.equal(bad.problems[0].reason, 'the file was not found; press Undo again to give up on it')
})

test('silent for three minutes, and a change lb-bot no longer has', () => {
  for (const op of ['apply', 'remove']) {
    const v = jobView(op, 'timeout', row({ done: 2 }))
    assert.deepEqual([v.tone, v.word, v.line, v.ended, v.bar, v.canUndo, v.seeOverrides],
                     ['wait', 'Still working', 'Still working — see System › Overrides', true, false, false, true])
  }
  // The row is gone: a 404 says that, and no more — not who undid it (R11).
  const v = jobView('apply', 'gone', row())
  assert.deepEqual([v.tone, v.word, v.line, v.ended, v.canUndo],
                   ['', 'No longer there', 'lb-bot no longer has this change.', true, false])
})

test('no raw state reaches the page: every phase of every job has words', () => {
  for (const op of ['apply', 'remove']) {
    for (const phase of ['polling', 'settled', 'failed', 'gone', 'timeout']) {
      for (const r of [null, row(), row({ state: 'something-new', done: 1 })]) {
        const v = jobView(op, phase, r)
        assert.ok(v.word && v.line, `${op} ${phase}`)
        assert.ok(!/applying|removing|applied|something-new|undefined|NaN/.test(v.word + v.line), v.line)
      }
    }
  }
})

// ── a row nobody is following (read with the album) ──────────────────────────
test('a row at rest: nothing more to say when it is simply applied', () => {
  assert.equal(restView(null), null)
  assert.equal(restView(row({ state: 'applied', done: 6 })), null)
  const part = restView(row({ state: 'applied', done: 5, failures: [{ title: 'Nangs', reason: 'read-only' }] }))
  assert.deepEqual([part.line, part.problemsHeading, part.canUndo], ['lb-bot wrote the tags of 5 files.', '1 problem', true])
  // `failed` does not say which job failed: the sentence does not either.
  const bad = restView(row({ state: 'failed', done: 2, failures: [{ title: '', reason: 'restarted; undo, or apply again' }] }))
  assert.deepEqual([bad.tone, bad.word, bad.line, bad.canUndo], ['bad', 'Did not finish', 'A change lb-bot made to this album did not finish.', true])
  assert.equal(bad.problems[0].reason, 'restarted; undo, or apply again')
  // A state this page does not know — a newer lb-bot's — says nothing, and
  // breaks nothing (R6): not when it carries no problems,
  assert.equal(restView(row({ state: 'something-new' })), null)
  // and what it does list is still said.
  assert.equal(restView(row({ state: 'something-new', failures: [{ title: 'a', reason: 'b' }] })).problemsHeading, '1 problem')
  // A running row reads as its job (the watch picks it up).
  assert.equal(restView(row({ done: 3 })).line, 'Writing tags… 3 of 6')
  assert.equal(restView(row({ state: 'removing', done: 3 })).line, 'Putting the previous tags back… 3 of 6')
})

test('the line under the title says what was changed, and when', () => {
  const now = 1000 + 5 * 60
  const ART = 'https://coverartarchive.org/release/x/1.jpg'
  assert.deepEqual(changedParts(row({ state: 'applied' }), now), { what: 'Tags changed by lb-bot', when: '5 min ago' })
  assert.deepEqual(changedParts(row({ state: 'applied', coverUrl: ART }), now), { what: 'Tags and cover changed by lb-bot', when: '5 min ago' })
  // The release is the one it was: only the cover was changed (X7).
  const cover = row({ state: 'applied', prevReleaseMbid: REL, coverUrl: ART })
  assert.equal(releaseUnchanged(cover), true)
  assert.deepEqual(changedParts(cover, now), { what: 'Cover changed by lb-bot', when: '5 min ago' })
  for (const [secs, said] of [[0, 'just now'], [59, 'just now'], [60, '1 min ago'], [3599, '59 min ago'],
                              [3600, '1 h ago'], [86399, '23 h ago'], [86400, '1 d ago'], [10 * 86400, '10 d ago']]) {
    assert.deepEqual(changedParts(row({ state: 'applied' }), 1000 + secs), { what: 'Tags changed by lb-bot', when: said })
  }
  // No time on record, or a clock behind lb-bot's: nothing is made up.
  assert.deepEqual(changedParts(row({ state: 'applied', setAt: 0 }), now), { what: 'Tags changed by lb-bot', when: '' })
  assert.deepEqual(changedParts(row({ state: 'applied', setAt: now + 90 }), now), { what: 'Tags changed by lb-bot', when: 'just now' })
  assert.deepEqual(changedParts(null, now), { what: '', when: '' })
  // Which list of System › Overrides a row belongs to.
  assert.equal(rowGroup(cover), 'covers')
  assert.equal(rowGroup(row()), 'editions')
})

test('a cover-only change on an album with no release tag is a cover row; a row without the ids is not known (F2)', () => {
  const now = 1000 + 5 * 60
  const ART = 'https://coverartarchive.org/release-group/x/1.jpg'
  // 1. Both ids present and both blank: lb-bot's cover route wrote the row (an apply always names its release) —
  //    untagged before and after. No tag was touched.
  const untagged = row({ state: 'applied', releaseMbid: '', prevReleaseMbid: '', coverUrl: ART })
  assert.equal(releaseUnchanged(untagged), true)
  assert.deepEqual(changedParts(untagged, now), { what: 'Cover changed by lb-bot', when: '5 min ago' })
  assert.equal(rowGroup(untagged), 'covers')
  // 2. Blank before, named now: untagged before, tagged now — the tags changed (as it was).
  const tagged = row({ state: 'applied', releaseMbid: REL, prevReleaseMbid: '' })
  assert.equal(releaseUnchanged(tagged), false)
  assert.deepEqual(changedParts(tagged, now), { what: 'Tags changed by lb-bot', when: '5 min ago' })
  assert.deepEqual(changedParts({ ...tagged, coverUrl: ART }, now), { what: 'Tags and cover changed by lb-bot', when: '5 min ago' })
  assert.equal(rowGroup(tagged), 'editions')
  // 3. A row that does not carry the fields at all: not known — never "unchanged".
  for (const missing of [{ releaseMbid: undefined, prevReleaseMbid: undefined }, { releaseMbid: null, prevReleaseMbid: null },
                         { releaseMbid: '', prevReleaseMbid: undefined }, { releaseMbid: undefined, prevReleaseMbid: '' }]) {
    const bare = row({ state: 'applied', coverUrl: ART, ...missing })
    assert.equal(releaseUnchanged(bare), false, JSON.stringify(missing))
    assert.equal(rowGroup(bare), 'editions')
    assert.equal(changedParts(bare, now).what, 'Tags and cover changed by lb-bot')
  }
  const { releaseMbid: _a, prevReleaseMbid: _b, ...without } = row({ state: 'applied', coverUrl: ART })
  assert.equal(releaseUnchanged(without), false)
  assert.equal(releaseUnchanged(null), false)
})

test('a write’s answer that names no job is not a job (F5): nothing to follow, and one sentence to say', () => {
  assert.equal(startedId({ ok: true, id: 'ov1', state: 'applying' }), 'ov1')
  assert.equal(startedId({ ok: true, id: ' mg7 ' }), 'mg7')
  assert.equal(startedId({ ok: true, id: 12 }), '12')
  for (const answer of [{ ok: true, state: 'removing' }, { id: '' }, { id: '   ' }, { id: null }, { id: {} }, { id: NaN }, null, undefined, 'ov1']) {
    assert.equal(startedId(answer), '', JSON.stringify(answer) + ' names no job')
  }
  // Where the id is the row's own by contract (the album page's undo), the row's is the fallback.
  assert.equal(startedId({ ok: true, state: 'removing' }, 'ov1'), 'ov1')
  assert.equal(startedId({ ok: true, id: 'ov2' }, 'ov1'), 'ov2')
  assert.equal(startedId({}, ''), '')
  // What the surface says, in the place its refusals go: in words, with nothing to send again.
  const said = notSaid()
  assert.equal(said.text, 'lb-bot did not say what it started. See System › Overrides.')
  assert.equal(said.retry, false)
  assert.ok(!/\bid\b|undefined|null/.test(said.text), 'no field name, no raw value')
  // On System › Overrides itself there is no "there" to point at.
  assert.equal(notSaid({ here: true }).text, 'lb-bot did not say what it started.')
})

test('“Change cover” keeps its place: it can be pressed for an album lb-bot is not changing; while lb-bot is, it stays, cannot be pressed, and says why (G1)', () => {
  // `canCover`: the page holds an album lb-bot is not changing just now (albumQuiet, above, says which).
  assert.deepEqual(changeCoverLink(true), { pressable: true, why: '' })
  assert.deepEqual(changeCoverLink(false), { pressable: false, why: COVER_HELD })
  // The reason, in words: for a pointer that rests on the link and for a screen reader that is on it.
  assert.equal(COVER_HELD, 'Not while lb-bot is changing this album’s files.')
  assert.ok(!/in_progress|timeout|aria|undefined/.test(COVER_HELD), 'no code, no field name')
})

test('while an album’s job runs — or has gone silent — nothing that starts another is offered for it (F6)', () => {
  const applied = row({ state: 'applied', done: 6 })
  // A job the page follows: running, and after the three-minute give-up (which is no end).
  assert.equal(albumQuiet(applied, { op: 'apply', phase: 'polling', running: true }), true)
  assert.equal(albumQuiet(applied, { op: 'apply', phase: 'timeout', running: false }), true, 'gone silent: lb-bot would answer in_progress')
  assert.equal(albumQuiet(null, { op: 'remove', phase: 'timeout', running: false }), true)
  // It ended: offered again.
  for (const phase of ['settled', 'failed', 'gone']) assert.equal(albumQuiet(applied, { op: 'apply', phase, running: false }), false, phase)
  // No job followed: the album's own state says.
  assert.equal(albumQuiet(row({ state: 'applying' }), null), true)
  assert.equal(albumQuiet(row({ state: 'removing' }), null), true)
  assert.equal(albumQuiet(applied, null), false)
  assert.equal(albumQuiet(row({ state: 'failed' }), null), false)
  assert.equal(albumQuiet(null, null), false, 'an album lb-bot has not changed')
  // A row read while it ran, beside a job that has just ended: still running as far as the row says.
  assert.equal(albumQuiet(row({ state: 'applying' }), { op: 'apply', phase: 'settled', running: false }), true)
})

test('an Undo button on the album page says what it undoes (F7): its word and the album, the name isolated', () => {
  assert.equal(undoSays('Undo', 'Currents'), 'Undo ' + I('Currents'))
  assert.equal(undoSays('Undo again', 'להקת הדוגמה'), 'Undo again ' + I('להקת הדוגמה'))
  // No name in hand: the word alone (never "Undo undefined").
  assert.equal(undoSays('Undo', ''), 'Undo')
  assert.equal(undoSays('Undo', undefined), 'Undo')
})

test('a job that ends away from its page is one toast that names the album', () => {
  const label = albumLabel(row())
  assert.equal(label, 'Currents')
  assert.equal(albumLabel(row({ album: '' })), 'Tame Impala')
  assert.equal(albumLabel(null), 'The album')
  assert.deepEqual(endToast('apply', 'settled', row({ state: 'applied', done: 6 })),
                   { text: I('Currents') + ': lb-bot wrote the tags of 6 files.', level: 'info' })
  assert.deepEqual(endToast('apply', 'settled', row({ state: 'applied', done: 5, failures: [{ title: 'a', reason: 'b' }] })),
                   { text: I('Currents') + ': lb-bot wrote the tags of 5 files. 1 problem — open the album to see it.', level: 'error' })
  assert.deepEqual(endToast('apply', 'failed', row({ state: 'failed', done: 2 })),
                   { text: I('Currents') + ': lb-bot did not finish writing the tags (2 of 6 files written).', level: 'error' })
  assert.deepEqual(endToast('remove', 'settled', row({ state: 'removing' })), { text: I('Currents') + ': restored.', level: 'info' })
  assert.deepEqual(endToast('remove', 'failed', row({ state: 'failed' })),
                   { text: I('Currents') + ': lb-bot could not put everything back.', level: 'error' })
  assert.deepEqual(endToast('apply', 'timeout', row()), { text: I('Currents') + ': still working — see System › Overrides', level: 'info' })
  assert.equal(endToast('apply', 'polling', row()), null)
})

// ── the preview ──────────────────────────────────────────────────────────────
const slot = (disc, track, title) => ({ disc, track, title })
const preview = (over = {}) => ({
  albumId: 'al-1', releaseMbid: REL,
  release: { title: 'Currents', disambiguation: 'deluxe', year: '2015', country: 'GB', format: 'CD', trackCount: 13, discCount: 1 },
  files: [
    { from: slot(1, 1, 'Let It Happen'), to: slot(1, 1, 'Let It Happen'), basis: 'mbid', changed: true },
    { from: slot(1, 2, 'Nangs'), to: slot(1, 2, 'Nangs'), basis: 'exact', changed: false },
    { from: slot(1, 3, 'The Moment'), to: slot(1, 4, 'The Moment'), basis: 'exact', changed: true },
    { from: slot(0, 0, 'Hidden Demo'), to: null, basis: '', changed: false },
  ],
  matched: 3, unmatched: 1, missing: [slot(1, 3, 'Yes I’m Changing'), slot(1, 5, 'Eventually')],
  ...over,
})

test('the three counts are one line, and each is what the preview says', () => {
  const s = previewSummary(preview())
  assert.deepEqual(s, { changing: 2, unchanged: 1, noSlot: 1, missing: 2, matched: 3, total: 4 })
  assert.equal(countsLine(s), '2 files change · 1 file with no place on this release · 2 tracks still missing afterwards')
  // A count of nothing is not said (V4).
  assert.equal(countsLine({ changing: 8, noSlot: 0, missing: 2 }), '8 files change · 2 tracks still missing afterwards')
  assert.equal(countsLine({ changing: 1, noSlot: 0, missing: 1 }), '1 file changes · 1 track still missing afterwards')
  assert.equal(countsLine({ changing: 8, noSlot: 0, missing: 0 }), '8 files change')
  assert.equal(countsLine({ changing: 0, noSlot: 3, missing: 0 }), '3 files with no place on this release')
  assert.equal(countsLine({ changing: 0, noSlot: 0, missing: 4 }), '4 tracks still missing afterwards')
  // Nothing at all to count is no line (the commit's own line says why).
  assert.equal(countsLine({ changing: 0, noSlot: 0, missing: 0 }), '')
  // A file with a slot that is not marked changed is "already right", never "changing".
  const same = previewSummary(preview({ files: preview().files.map(f => ({ ...f, changed: false })) }))
  assert.deepEqual([same.changing, same.unchanged], [0, 3])
  // An answer with no lists is an answer with nothing in them.
  assert.deepEqual(previewSummary({}), { changing: 0, unchanged: 0, noSlot: 0, missing: 0, matched: 0, total: 0 })
})

test('the release line names what the preview’s release fields say', () => {
  assert.equal(releaseLine(preview().release), I('Currents') + ' (' + I('deluxe') + ') · 2015 · GB · CD · 13 tracks')
  assert.equal(releaseLine({ ...preview().release, discCount: 2 }), I('Currents') + ' (' + I('deluxe') + ') · 2015 · GB · CD · 13 tracks · 2 discs')
  assert.equal(releaseLine({ title: 'Currents', trackCount: 1 }), I('Currents') + ' · 1 track')
  assert.equal(releaseLine({}), '')
  assert.equal(releaseLine(null), '')
})

test('rows are from → to: the changed listed, the unchanged counted, the placeless apart', () => {
  const rows = previewRows(preview())
  assert.equal(rows.multiDisc, false)
  // A changed file whose number and title move is a from → to row.
  assert.deepEqual(rows.differ.map(r => [r.from, r.to]), [['03 The Moment', '04 The Moment']])
  // One that changes only the release it names would read "01 X → 01 X":
  // it is counted, and listed behind that count.
  assert.deepEqual(rows.sameLabel.map(r => [r.from, r.to]), [['01 Let It Happen', '01 Let It Happen']])
  assert.equal(rows.sameLabelText, '1 file keeps its number and title; only the release it is tagged with changes')
  assert.deepEqual(rows.unchanged.map(r => r.from), ['02 Nangs'])
  // A file with no number says its title alone.
  assert.deepEqual(rows.noSlot.map(r => r.from), ['Hidden Demo'])
  assert.equal(rows.unchangedLabel, '1 file is already right')
  const all = previewRows(preview({ files: preview().files.map(f => ({ ...f, changed: false })) }))
  assert.equal(all.unchangedLabel, '3 files are already right')
  assert.deepEqual([all.differ, all.sameLabel], [[], []])
  // Another pressing of the same tracklist: every file changes, none moves.
  const pressing = previewRows(preview({ files: preview().files.filter(f => f.to).map(f => ({ ...f, to: f.from, changed: true })) }))
  assert.deepEqual([pressing.differ.length, pressing.sameLabel.length], [0, 3])
  assert.equal(pressing.sameLabelText, '3 files keep their number and title; only the release they are tagged with changes')
  // The three lists and the count line are the same files: nothing is in two.
  assert.equal(rows.differ.length + rows.sameLabel.length, previewSummary(preview()).changing)
  // Every row has a key of its own, also for two files of one title.
  const keys = [...rows.differ, ...rows.sameLabel, ...rows.unchanged, ...rows.noSlot].map(r => r.key)
  assert.equal(new Set(keys).size, keys.length)
  assert.equal(NO_SLOT_NOTE, 'Left untouched. Navidrome will list them as an album of their own.')
  // The disc is said only where there is more than one.
  const two = previewRows(preview({ release: { ...preview().release, discCount: 2 },
    files: [{ from: slot(1, 3, 'A'), to: slot(2, 1, 'A'), basis: 'exact', changed: true }] }))
  assert.deepEqual([two.multiDisc, two.differ[0].from, two.differ[0].to], [true, '1-03 A', '2-01 A'])
  assert.equal(slotLabel(slot(2, 7, 'B'), false), '07 B')
  assert.equal(slotLabel(slot(0, 7, 'B'), true), '07 B')
})

test('the commit: one fixed sentence, and a button that says what it will do', () => {
  assert.equal(commitSentence(6), 'lb-bot rewrites the tags of 6 files and keeps the previous ones, so this can be undone.')
  assert.equal(commitSentence(1), 'lb-bot rewrites the tags of 1 file and keeps the previous ones, so this can be undone.')
  const s = previewSummary(preview())
  assert.deepEqual(commitView(s, false), { kind: 'apply', label: 'Retag 2 files', disabled: false, line: commitSentence(2) })
  assert.deepEqual(commitView(s, true), { kind: 'apply', label: 'Retag 2 files', disabled: false, line: commitSentence(2) })
  assert.equal(commitView({ ...s, changing: 1 }, false).label, 'Retag 1 file')
  // Nothing to change: the files already carry it. With a cover picked the button saves the cover.
  const same = { ...s, changing: 0, unchanged: 3, matched: 3 }
  assert.deepEqual(commitView(same, false), { kind: 'none', label: 'Retag 0 files', disabled: true, line: 'The files already carry this release.' })
  assert.deepEqual(commitView(same, true), { kind: 'cover', label: 'Save the cover', disabled: false, line: 'The files already carry this release.' })
  // Nothing to change because nothing has a place there: that is not "already carry".
  const none = { ...s, changing: 0, unchanged: 0, matched: 0, noSlot: 4 }
  assert.deepEqual(commitView(none, false), { kind: 'none', label: 'Retag 0 files', disabled: true, line: 'None of this album’s files has a place on this release.' })
  assert.equal(commitView(none, true).kind, 'cover')
  // No preview yet: nothing can be committed.
  assert.deepEqual(commitView(null, true), { kind: 'none', label: 'Retag', disabled: true, line: '' })
})

// ── refusals: 502 · poor_match · busy / in_progress / files_unreachable · anything else ──
const refusal = (status, payload = {}) => Object.assign(new Error(payload.error ?? `HTTP ${status}`), { status, payload })

test('poor_match is a question that names both numbers', () => {
  const v = refusalView(refusal(409, { error: 'Only 2 of this album’s 12 files are on that release', code: 'poor_match', matched: 2, total: 12 }))
  assert.deepEqual(v, { kind: 'confirm', text: 'Only 2 of this album’s 12 files have a place on this release. Retag them anyway?', retry: false, matched: 2, total: 12 })
  assert.equal(poorMatchQuestion(1, 1), 'Only 1 of this album’s 1 files have a place on this release. Retag them anyway?')
  // Without both numbers there is nothing to name: lb-bot's own text.
  assert.equal(refusalView(refusal(409, { error: 'Only a few files are on that release', code: 'poor_match' })).kind, 'say')
})

test('a dependency that did not answer offers Try again; lb-bot’s own refusals are said in its words', () => {
  for (const err of [refusal(502, { error: 'MusicBrainz did not answer; nothing was written', retryable: true }),
                     refusal(503, { error: 'busy' }), refusal(504),
                     new TypeError('Failed to fetch'), Object.assign(new Error('Timed out after 10s'), { timeout: true })]) {
    const v = refusalView(err)
    assert.deepEqual([v.kind, v.retry], ['retry', true], err.message)
    assert.ok(v.text)
  }
  assert.equal(refusalView(refusal(502, { error: 'MusicBrainz did not answer; nothing was written', retryable: true })).text,
               'MusicBrainz did not answer; nothing was written')
  // No answer at all: nothing is claimed about what was or was not written,
  // and a status line is not shown for words.
  assert.equal(refusalView(new TypeError('Failed to fetch')).text, 'lb-bot did not answer.')
  assert.equal(refusalView(refusal(504)).text, 'lb-bot did not answer.')
  for (const [code, text] of [['busy', 'A download for this album is still running (queued); try again when it has finished'],
                              ['in_progress', 'This album is already being retagged or restored; try again when that has finished'],
                              ['files_unreachable', 'lb-bot can find 3 of this album\'s 12 files in its music folder…'],
                              ['unknown_release', 'MusicBrainz has no such release'],
                              ['a_code_from_tomorrow', 'Something this page has never heard of']]) {
    const v = refusalView(refusal(code.startsWith('unknown') ? 404 : 409, { error: text, code }))
    assert.deepEqual(v, { kind: 'say', text, retry: false, matched: 0, total: 0 }, code)
    // The code itself is never the text.
    assert.ok(!v.text.includes(code))
  }
  // A preview's refusal offers Try again whatever it is (X6).
  assert.equal(refusalView(refusal(409, { error: 'x', code: 'files_unreachable' }), { always: true }).retry, true)
  // An answer with no words of its own still says something, and never a code.
  assert.equal(refusalView(refusal(409, { code: 'busy' })).text, 'lb-bot refused that (409).')
  assert.equal(refusalView(refusal(400, { error: 'release_mbid must be a MusicBrainz release id (a UUID)' })).text,
               'release_mbid must be a MusicBrainz release id (a UUID)')
})

// ── covers ───────────────────────────────────────────────────────────────────
test('covers for a release that is not a listed edition are asked by the release alone (X8)', () => {
  const listed = new Set([REL])
  assert.deepEqual(coverQuery({ picked: REL, current: OTHER, listed, rgid: 'rg-1' }), { release_mbid: REL, rgid: 'rg-1' })
  assert.deepEqual(coverQuery({ picked: OTHER, current: REL, listed, rgid: 'rg-1' }), { release_mbid: OTHER })
  // "Change cover": the release the files carry, and its group.
  assert.deepEqual(coverQuery({ picked: '', current: REL, listed, rgid: 'rg-1' }), { release_mbid: REL, rgid: 'rg-1' })
  // An untagged album has only its group to ask by.
  assert.deepEqual(coverQuery({ picked: '', current: '', listed, rgid: 'rg-1' }), { rgid: 'rg-1' })
  assert.equal(coverQuery({ picked: '', current: '', listed, rgid: '' }), null)
})

test('a chosen cover is shown small: the Archive’s own 500 px version of that image', () => {
  assert.equal(coverThumbUrl(`https://coverartarchive.org/release/${REL}/829521842.jpg`),
               `https://coverartarchive.org/release/${REL}/829521842-500.jpg`)
  assert.equal(coverThumbUrl(`https://coverartarchive.org/release/${REL}/829521842.png`),
               `https://coverartarchive.org/release/${REL}/829521842-500.jpg`)
  // Anything else is shown as it is; nothing is not an address.
  for (const url of [`https://archive.org/download/mbid-${REL}/x.jpg`, `https://coverartarchive.org/release/${REL}/front`,
                     `https://coverartarchive.org.evil.example/release/${REL}/1.jpg`]) {
    assert.equal(coverThumbUrl(url), url)
  }
  assert.equal(coverThumbUrl(''), '')
  assert.equal(coverThumbUrl(null), '')
})

// ── editions: which chip the files are, and whether the selected one is it ───
const variants = [
  { title: 'Currents', disambiguation: '', year: '2015', trackCount: 13, releaseMbid: 'r-dig',
    owned: true, ownedReleaseMbid: 'r-cd-2', ownedEdition: 'CD',
    editions: [{ releaseMbid: 'r-dig', label: 'Digital', year: '2015', country: 'XW' },
               { releaseMbid: 'r-cd', label: 'CD', year: '2015', country: 'GB' }] },
  { title: 'Currents', disambiguation: 'collector’s edition', year: '2017', trackCount: 16, releaseMbid: 'r-box',
    editions: [{ releaseMbid: 'r-box', label: 'Vinyl', year: '2017', country: 'US' }] },
]

test('the chip the files carry is found by any id the variant goes by', () => {
  assert.equal(variantOfRelease(variants, 'r-dig'), 0)
  assert.equal(variantOfRelease(variants, 'r-cd'), 0)
  // A pressing folded into an edition of the same label.
  assert.equal(variantOfRelease(variants, 'R-CD-2'), 0)
  assert.equal(variantOfRelease(variants, 'r-box'), 1)
  assert.equal(variantOfRelease(variants, 'r-elsewhere'), -1)
  assert.equal(variantOfRelease(variants, ''), -1)
  assert.equal(variantOfRelease(null, 'r-dig'), -1)
})

test('the block is for an edition the files do not carry', () => {
  const on = (variant, edition, carried) => carriesSelection(variants, { variant, edition }, carried)
  assert.equal(on(0, 1, 'r-cd'), true)
  assert.equal(on(0, 0, 'r-cd'), false)
  // The files are a pressing folded into the CD chip: that chip is theirs.
  assert.equal(on(0, 1, 'r-cd-2'), true)
  assert.equal(on(0, 0, 'r-cd-2'), false)
  assert.equal(on(1, 0, 'r-cd-2'), false)
  assert.equal(on(1, 0, 'r-box'), true)
  // An untagged album carries no edition.
  assert.equal(on(0, 0, ''), false)
  // A release with no editions listed is its own.
  assert.equal(carriesSelection([{ releaseMbid: 'r-one', editions: [] }], { variant: 0, edition: 0 }, 'r-one'), true)
  assert.equal(carriesSelection(null, { variant: 0, edition: 0 }, 'r-one'), false)
})

test('on arrival the selection is the release the album itself names (V7)', () => {
  // The edition whose id it is,
  assert.deepEqual(selectionOfRelease(variants, 'r-cd'), { variant: 0, edition: 1 })
  assert.deepEqual(selectionOfRelease(variants, 'R-DIG'), { variant: 0, edition: 0 })
  assert.deepEqual(selectionOfRelease(variants, 'r-box'), { variant: 1, edition: 0 })
  // a pressing folded into an edition of the same label: that edition's chip,
  assert.deepEqual(selectionOfRelease(variants, 'r-cd-2'), { variant: 0, edition: 1 })
  // a release with no editions listed: the release itself.
  assert.deepEqual(selectionOfRelease([{ releaseMbid: 'r-one', editions: [] }], 'r-one'), { variant: 0, edition: 0 })
  // A folded pressing whose label no chip carries still has its release's chip.
  assert.deepEqual(selectionOfRelease([{ releaseMbid: 'r-a', ownedReleaseMbid: 'r-b', ownedEdition: 'Cassette',
                                         editions: [{ releaseMbid: 'r-a', label: 'CD' }] }], 'r-b'), { variant: 0, edition: 0 })
  // Not among the chips, or no release named: no selection is made of it.
  assert.equal(selectionOfRelease(variants, 'r-elsewhere'), null)
  assert.equal(selectionOfRelease(variants, ''), null)
  assert.equal(selectionOfRelease(null, 'r-cd'), null)
  // What it selects is what `carriesSelection` calls the files' own chip.
  for (const mbid of ['r-cd', 'r-dig', 'r-box', 'r-cd-2']) {
    assert.equal(carriesSelection(variants, selectionOfRelease(variants, mbid), mbid), true, mbid)
  }
})

test('an edition’s line says which release and which pressing', () => {
  assert.equal(editionLine(variants[0], variants[0].editions[1], 0), 'Original · CD · 2015 · GB · 13 tracks')
  assert.equal(editionLine(variants[1], variants[1].editions[0], 1), I('collector’s edition') + ' · Vinyl · 2017 · US · 16 tracks')
  assert.equal(editionLine({ title: 'X', year: '1999', trackCount: 1, editions: [] }, undefined, 2), '1999 · 1 track')
  assert.equal(editionLine(null, null, 0), '')
  // A release chip's own name, as the chips say it.
  assert.deepEqual([variantName(variants[0], 0), variantName(variants[1], 1)], ['Original', 'collector’s edition'])
  assert.equal(variantName({ year: '1999' }, 3), '1999')
  assert.equal(variantName({}, 3), 'Release 4')
  assert.equal(variantName(null, 0), '')
})

test('an album Navidrome has not listed again says so', () => {
  assert.equal(NOT_LISTED_YET, 'Navidrome has not listed the album again yet.')
})

// ── after a job: where the album is read again from ──────────────────────────
test('after a job the album is read under the id the row now carries, else looked for', () => {
  const job = (op, phase, album) => ({ op, phase, album, row: album?.override || null })
  const listed = { albumId: 'al-new', releaseMbid: REL, override: row({ state: 'applied', albumId: 'al-new' }) }
  // Applied, and Navidrome lists it: that answer is the album; the id it had is dead.
  assert.deepEqual(afterJobSteps('al-old', job('apply', 'settled', listed)), { use: listed, dead: 'al-old', steps: [] })
  // The id did not move (the release the files already carried, a cover saved with it).
  assert.deepEqual(afterJobSteps('al-new', job('apply', 'settled', listed)), { use: listed, dead: '', steps: [] })
  // Applied with no id yet (the minute ran out): its old id may still be it, else the row says.
  const unlisted = { albumId: '', override: row({ state: 'applied' }) }
  assert.deepEqual(afterJobSteps('al-old', job('apply', 'settled', unlisted)), { use: null, dead: '', steps: ['album', 'row'] })
  // Failed: the row's own album id is blank from the start of an apply, so it is not believed.
  assert.deepEqual(afterJobSteps('al-old', job('apply', 'failed', unlisted)), { use: null, dead: '', steps: ['album', 'row'] })
  assert.deepEqual(afterJobSteps('al-old', job('remove', 'failed', unlisted)), { use: null, dead: '', steps: ['album', 'row'] })
  // Restored: the row is gone; the album is under the id it has, or under one the index will name.
  assert.deepEqual(afterJobSteps('al-new', job('remove', 'settled', null)), { use: null, dead: '', steps: ['album'] })
  // Undone from somewhere else: the same.
  assert.deepEqual(afterJobSteps('al-new', job('apply', 'gone', null)), { use: null, dead: '', steps: ['album'] })
  // An album with no id on record has only its row.
  assert.deepEqual(afterJobSteps('', job('apply', 'failed', unlisted)), { use: null, dead: '', steps: ['row'] })
  assert.deepEqual(afterJobSteps('', job('remove', 'settled', null)), { use: null, dead: '', steps: [] })
  // Still running, or silent: nothing is read again — lb-bot has not said it ended.
  for (const phase of ['polling', 'timeout']) {
    assert.deepEqual(afterJobSteps('al-old', job('apply', phase, unlisted)), { use: null, dead: '', steps: null })
  }
})

test('the line under the title, as a decision (R6): running, changed, ended — and nothing for a state this page does not know', () => {
  const applied = row({ state: 'applied', done: 6 })
  assert.deepEqual(lineAtRest(applied, null).kind, 'changed')
  assert.equal(lineAtRest(null, null), null, 'an album lb-bot has not changed')
  // A job that runs is its own line; one that has ended says how.
  assert.equal(lineAtRest(applied, { op: 'apply', phase: 'polling', row: row({ state: 'applying', done: 1 }) }).kind, 'running')
  const ended = lineAtRest(applied, { op: 'remove', phase: 'settled', row: null })
  assert.deepEqual([ended.kind, ended.v.word], ['ended', 'Restored'])
  // A running row nobody follows is shown as running; a failed one says so.
  assert.equal(lineAtRest(row({ state: 'removing' }), null).kind, 'running')
  assert.deepEqual([lineAtRest(row({ state: 'failed' }), null).kind, lineAtRest(row({ state: 'failed' }), null).v.word], ['ended', 'Did not finish'])
  // Applied with problems is still "changed" — with their count beside it.
  const partial = lineAtRest(row({ state: 'applied', failures: [{ title: 'a', reason: 'b' }] }), null)
  assert.deepEqual([partial.kind, partial.v.problemsHeading], ['changed', '1 problem'])
  // A state another build wrote: nothing is said, and nothing is read off a view that is not there.
  for (const state of ['paused', '', undefined, 'APPLIED']) assert.equal(lineAtRest(row({ state }), null), null, String(state))
})

test('a release named inside a sentence: what MusicBrainz says of it is isolated, the page’s own words are not (M5)', () => {
  // "your files are tagged as …": the chip's name — a MusicBrainz disambiguation, when the release has one.
  assert.equal(variantInLine({ disambiguation: 'מהדורה מיוחדת' }, 1), I('מהדורה מיוחדת'))
  assert.equal(variantInLine({ disambiguation: 'collector’s edition', year: '2017' }, 1), I('collector’s edition'))
  // The page's own words for a release with none are words, not a name.
  assert.equal(variantInLine({ disambiguation: '', year: '2015' }, 0), 'Original')
  assert.equal(variantInLine({ disambiguation: '', year: '2017' }, 2), '2017')
  assert.equal(variantInLine({ disambiguation: '' }, 2), 'Release 3')
  assert.equal(variantInLine(null, 0), '')
})
