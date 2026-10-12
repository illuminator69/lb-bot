// Run: node --test web/src/lib/indexStatus.test.mjs   (no dependency, no network)
// What the web UI reads of an index row's `status` (B-091). The index has five
// words and the pages knew four: an album lb-bot had just filled — `present` —
// had no chip, was in no group of its artist's page, and was not "in the
// library" to its own page.
import test from 'node:test'
import assert from 'node:assert/strict'
import { albumState, countsMeasured, inLibrary } from './indexStatus.js'

test('the four words a scan produces are themselves', () => {
  for (const status of ['complete', 'incomplete', 'untagged', 'missing']) assert.equal(albumState(status), status)
})

test('present — filled by lb-bot, not scanned since — is in the library', () => {
  assert.equal(albumState('present'), 'complete')
  assert.equal(inLibrary('present'), true)
})

test('in the library: everything but missing, and nothing lb-bot did not say', () => {
  assert.deepEqual(['complete', 'incomplete', 'untagged', 'present', 'missing'].map(inLibrary), [true, true, true, true, false])
  for (const status of ['', undefined, null, 'a-word-from-tomorrow']) {
    assert.equal(inLibrary(status), false, String(status))
    assert.equal(albumState(status), '', String(status))
  }
})

test('a present row’s own counts are from before the fill: they are not shown as its counts', () => {
  // `_index_mark_release_present` leaves `present` / `total` as the last scan
  // wrote them ("nothing here knows the real numbers").
  assert.equal(countsMeasured('present'), false)
  assert.deepEqual(['complete', 'incomplete', 'untagged'].map(countsMeasured), [true, true, true])
  // A missing row has nothing to count, and an unknown word says nothing.
  assert.equal(countsMeasured('missing'), false)
  assert.equal(countsMeasured(undefined), false)
})
