// Run: node --test web/src/lib/placeAnyway.test.mjs   (no dependency, no network)
// What Fill gaps says before "Place anyway" places a refused file (B-087, J5).
import test from 'node:test'
import assert from 'node:assert/strict'
import { placeAnywayConfirm, placeAnywayBody, placeAnywayFailure } from './placeAnyway.js'

test('a duplicate is said to add a second copy, as it always was', () => {
  for (const t of [{ canForcePlace: true, forcePlaceKind: 'duplicate', forcePlaceConflict: '02 - Two.flac' },
                   // An older lb-bot sends no kind with its offer: the one there was then.
                   { canForcePlace: true, forcePlaceConflict: '02 - Two.flac' }]) {
    const text = placeAnywayConfirm(t)
    assert.ok(text.includes('same audio as 02 - Two.flac'), text)
    assert.ok(text.includes('second copy'), text)
  }
  assert.ok(placeAnywayConfirm({ canForcePlace: true, forcePlaceKind: 'duplicate' }).includes('another file'))
})

test('a recording refusal is said for what it is, in the row’s own words', () => {
  const found = 'AcoustID knows this audio as “Requiem (extended version)” by King Crimson (12:15), '
    + 'not this album’s “Requiem” (6:30) — confidence 0.97'
  for (const prefix of ['not placed: ', 'placement failed: ', '']) {
    const text = placeAnywayConfirm({ title: 'Requiem', canForcePlace: true, forcePlaceKind: 'recording',
                                      downloadError: prefix + found })
    assert.ok(text.includes(found), text)
    assert.ok(!text.includes('not placed:') && !text.includes('placement failed:'), text)
    assert.ok(text.includes('filed as “Requiem” anyway'), text)
    // Not the duplicate's sentence: nothing here adds a second copy.
    assert.ok(!text.includes('second copy'), text)
  }
  // With no text on the row it still says what kind of refusal it overrules.
  const bare = placeAnywayConfirm({ title: 'Requiem', canForcePlace: true, forcePlaceKind: 'recording' })
  assert.ok(/another recording/.test(bare), bare)
  assert.ok(bare.includes('filed as “Requiem” anyway'), bare)
})

test('every confirmation opens with the question', () => {
  for (const t of [{ forcePlaceKind: 'recording', title: 'x' }, { forcePlaceKind: 'duplicate' }, {}, null]) {
    assert.ok(placeAnywayConfirm(t).startsWith('Place this file anyway?\n\n'))
  }
})

// J19: the press names the row it was drawn for, and a press the server turned
// away because another row is at that index now is said in the server's words.
test('the press sends the row the page drew: its recording id and its title', () => {
  assert.deepEqual(placeAnywayBody({ index: 3, recordingMbid: 'e063bd58-745a-4f7f-8340-3b273b561ff8',
                                     title: 'Requiem', canForcePlace: true, downloadError: 'not placed: x' }),
                   { recordingMbid: 'e063bd58-745a-4f7f-8340-3b273b561ff8', title: 'Requiem' })
  // A row with no id on record still names itself by its title; nothing is
  // sent as `undefined` (JSON would drop the key) or as null.
  assert.deepEqual(placeAnywayBody({ index: 0, title: 'Requiem' }), { recordingMbid: '', title: 'Requiem' })
  assert.deepEqual(placeAnywayBody({ recordingMbid: null, title: undefined }), { recordingMbid: '', title: '' })
  assert.deepEqual(placeAnywayBody(null), { recordingMbid: '', title: '' })
})

test('a press the server turned away says why, in the server’s words when the row has moved', () => {
  const moved = 'This album\'s tracks were read again since the page was drawn — open it again.'
  // api.js: the message is the body's `error`, the body itself is `payload`.
  const err = Object.assign(new Error(moved), { status: 409, payload: { ok: false, error: moved, code: 'row_moved' } })
  assert.equal(placeAnywayFailure(err), moved)
  // Any other refusal keeps the page's own lead-in, as before.
  for (const other of [Object.assign(new Error('This track has no refused placement to override'), { status: 400, payload: { ok: false } }),
                       Object.assign(new Error('busy'), { status: 409, payload: { ok: false, code: 'conflict' } }),
                       new Error('Failed to fetch')]) {
    assert.equal(placeAnywayFailure(other), `Place anyway failed: ${other.message}`)
  }
})
