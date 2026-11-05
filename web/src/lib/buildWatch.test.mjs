// node --test web/src/lib/buildWatch.test.mjs
import test from 'node:test'
import assert from 'node:assert/strict'
import { BUILD_CHECK_GAP_MS, UPDATED_TEXT, buildStep, checkDue, createBuildWatch, readBuild } from './buildWatch.js'

test('the first read is the baseline, a different later one is an upgrade', () => {
  let s = { baseline: null, updated: false, lastAt: null }
  s = buildStep(s, 'a+1', 0)
  assert.equal(s.baseline, 'a+1'); assert.equal(s.updated, false)
  s = buildStep(s, 'a+1', 1); assert.equal(s.updated, false)
  s = buildStep(s, 'b+2', 2); assert.equal(s.updated, true)
  // It stays updated, whatever comes after.
  s = buildStep(s, 'a+1', 3); assert.equal(s.updated, true)
})

test('no answer is no answer, never an upgrade', () => {
  let s = buildStep({ baseline: null, updated: false, lastAt: null }, null, 0)
  assert.equal(s.baseline, null)
  s = buildStep(buildStep(s, 'a', 1), null, 2)
  assert.equal(s.updated, false)
  assert.equal(readBuild({}), null)            // an older lb-bot
  assert.equal(readBuild({ build: '  ' }), null)
  assert.equal(readBuild({ build: 7 }), null)
  assert.equal(readBuild(null), null)
  assert.equal(readBuild({ build: 'e29390a-dev+0123456789ab' }), 'e29390a-dev+0123456789ab')
})

test('at most once a minute', () => {
  assert.equal(checkDue(null, 0), true)
  assert.equal(checkDue(0, BUILD_CHECK_GAP_MS - 1), false)
  assert.equal(checkDue(0, BUILD_CHECK_GAP_MS), true)
})

test('the watch reads once a minute at most, says it once, and never reloads', async () => {
  let t = 0, reads = 0, said = 0
  const answers = ['a', 'a', 'b', 'c']
  const w = createBuildWatch({ read: async () => answers[reads++], now: () => t, onUpdated: () => said++ })
  await w.check()                       // load: baseline
  t = 10_000; await w.check()           // focus inside the minute: nothing asked
  assert.equal(reads, 1)
  t = 61_000; await w.check()           // same build
  t = 125_000; await w.check()          // upgraded
  assert.equal(w.state.updated, true); assert.equal(said, 1)
  t = 999_000; await w.check()          // updated: nothing more is asked
  assert.equal(reads, 3); assert.equal(said, 1)
})

test('a read that throws is no answer', async () => {
  let t = 0
  const w = createBuildWatch({ read: async () => { throw new Error('down') }, now: () => t })
  await w.check()
  assert.deepEqual([w.state.baseline, w.state.updated], [null, false])
})

test('the sentence', () => {
  assert.equal(UPDATED_TEXT, 'lb-bot was updated. Reload to use the new version.')
})

test('the build the page was served with is the baseline (review 12)', async () => {
  // A tab that loaded the old page just before a restart: its first read
  // already answers the new build, and that is an upgrade, not the baseline.
  let said = 0
  const w = createBuildWatch({ baseline: 'old+1', read: async () => 'new+2', now: () => 0, onUpdated: () => said++ })
  assert.equal(w.state.baseline, 'old+1')
  await w.check()
  assert.equal(w.state.updated, true); assert.equal(said, 1)
  // No served build (an older lb-bot, the dev server): the first read is.
  const v = createBuildWatch({ baseline: readBuild({ build: '' }), read: async () => 'a', now: () => 0 })
  await v.check()
  assert.deepEqual([v.state.baseline, v.state.updated], ['a', false])
})

test('a read that hangs is given up on, and the next check runs (review 12)', { timeout: 3000 }, async () => {
  let t = 0, reads = 0, aborted = 0
  const w = createBuildWatch({
    read: signal => {
      reads++
      if (reads > 1) return Promise.resolve('a')
      signal?.addEventListener('abort', () => aborted++)
      return new Promise(() => {})            // never answers
    },
    now: () => t, timeoutMs: 20,
  })
  await w.check()
  assert.equal(aborted, 1)
  assert.equal(w.state.baseline, null)
  t = 61_000; await w.check()
  assert.equal(reads, 2); assert.equal(w.state.baseline, 'a')
})
