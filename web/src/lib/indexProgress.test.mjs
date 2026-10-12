// Run: node --test web/src/lib/indexProgress.test.mjs   (no dependency, no network)
// The state matrix of the index indicator (ruling U1): what the header and the
// Settings card may say for each thing /api/summary's `index` block can hold.
import test from 'node:test'
import assert from 'node:assert/strict'
import { indexProgress, indexHeaderLabel } from './indexProgress.js'

const fmt = n => String(n)
const clock = t => `@${t}`
const header = (index, now = 100) => indexHeaderLabel(indexProgress(index), now, fmt, clock)
const on = { enabled: true, running: true, outcome: 'indexed', pausedUntil: 0, current: '' }

test('nothing is said when nothing was measured', () => {
  assert.equal(indexProgress(undefined), null)
  assert.equal(indexProgress(null), null)
  // The worker is on and has not read the artist list yet: no numbers, not "0 of 0".
  assert.equal(indexProgress(on), null)
  assert.equal(header(on), null)
  // An empty library, measured: still nothing to say.
  assert.equal(indexProgress({ ...on, total: 0, remaining: 0 }), null)
  // A number that is not a whole number is not a measurement.
  assert.equal(indexProgress({ ...on, total: '2475', remaining: 3 }), null)
})

test('nothing is said for a worker that is off, whatever numbers are left over', () => {
  assert.equal(indexProgress({ ...on, enabled: false, total: 10, remaining: 4 }), null)
  assert.equal(indexProgress({ ...on, running: false, total: 10, remaining: 4 }), null)
})

test('a backlog reads as up to date of total, with the artist being scanned', () => {
  const p = indexProgress({ ...on, total: 2475, remaining: 2443, current: 'Björk' })
  assert.deepEqual(p, { kind: 'indexing', done: 32, total: 2475, remaining: 2443, pct: (32 / 2475) * 100, current: 'Björk', pausedUntil: 0 })
  assert.deepEqual(header({ ...on, total: 2475, remaining: 2443 }),
    ['Indexing library · 32 of 2475 artists', 'Indexing · 32/2475'])
  // The last artist failed and the next one is due: still indexing.
  assert.equal(indexProgress({ ...on, outcome: 'failed', total: 10, remaining: 4 }).kind, 'indexing')
  // Before the first tick has ended there is no outcome yet.
  assert.equal(indexProgress({ ...on, outcome: '', total: 10, remaining: 10 }).kind, 'indexing')
})

test('nothing left is not a bar at 100 % that never leaves', () => {
  const p = indexProgress({ ...on, outcome: 'idle', total: 2475, remaining: 0 })
  assert.equal(p.kind, 'done')
  assert.equal(header({ ...on, outcome: 'idle', total: 2475, remaining: 0 }), null)
})

test('what is left only waits for a retry: the card says so, the header says nothing', () => {
  const index = { ...on, outcome: 'idle', total: 2475, remaining: 3, current: 'stale name' }
  assert.deepEqual(indexProgress(index), { kind: 'waiting', done: 2472, total: 2475, remaining: 3, pct: (2472 / 2475) * 100, current: '', pausedUntil: 0 })
  assert.equal(header(index), null)
})

test('an outage is said as one, with the time it resumes while that is ahead', () => {
  const index = { ...on, outcome: 'outage', total: 2475, remaining: 2000, pausedUntil: 500 }
  assert.equal(indexProgress(index).kind, 'outage')
  assert.deepEqual(header(index, 100), ['Indexing paused · MusicBrainz is not answering · resumes @500', 'Indexing paused · resumes @500'])
  assert.deepEqual(header(index, 600), ['Indexing paused · MusicBrainz is not answering', 'Indexing paused'])
})

test('a manual build is the build, whatever the worker last said', () => {
  const index = { ...on, outcome: 'paused', total: 2475, remaining: 2443, build: { done: 12, total: 2475, current: 'Can' } }
  assert.deepEqual(indexProgress(index), { kind: 'build', done: 12, total: 2475, remaining: 2463, pct: (12 / 2475) * 100, current: 'Can', pausedUntil: 0 })
  assert.deepEqual(header(index), ['Building index · 12 of 2475', 'Building · 12/2475'])
  // The task has not listed the artists yet: no "0 of 0".
  assert.deepEqual(header({ enabled: false, running: false, build: { done: 0, total: 0, current: '' } }), ['Building index', 'Building index'])
  // …and a build is shown with the worker off.
  assert.equal(indexProgress({ enabled: false, running: false, build: { done: 3, total: 9, current: '' } }).kind, 'build')
})

test('numbers that disagree are clamped, never negative', () => {
  assert.deepEqual(indexProgress({ ...on, total: 5, remaining: 9 }).done, 0)
  assert.deepEqual(indexProgress({ ...on, build: { done: 12, total: 9 } }).done, 9)
})
