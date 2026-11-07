// What Needs you counts and where its addresses land (Worbus P4 S3).
// `node --test web/src/lib/needs.test.mjs` — no dependency.
import test from 'node:test'
import assert from 'node:assert/strict'
import { needsView, openImports, toFixCount } from './needs.js'

test('toFixCount counts imports', () => {
  // The summary's own number when the server sends it…
  const summary = { gaps: { failed: 1 }, requests: { failed: 2 }, transfers: { needsPlacement: 3 } }
  assert.equal(toFixCount({ summary }), 6)
  assert.equal(toFixCount({ summary: { ...summary, imports: { open: 4 } } }), 10)
  // …else the page's own imports list, by the server's rule (needs_review or failed).
  const imports = { items: [{ recid: 'rec1', status: 'failed' }, { recid: 'rec2', status: 'needs_review' },
    { recid: 'rec3', status: 'imported' }] }
  assert.equal(toFixCount({ summary, imports }), 8)
  assert.deepEqual(openImports(imports).map(i => i.recid), ['rec1', 'rec2'])
  // A server from before imports were counted: nothing added, nothing broken.
  assert.equal(toFixCount({ summary, imports: null }), 6)
  assert.equal(toFixCount({}), 0)
})

test('a dismissed import is not counted and not listed as open', () => {
  // Dismiss (ruling R20) leaves the record at `dismissed`, outside the server's rule.
  const imports = { items: [{ recid: 'rec1', status: 'dismissed' }, { recid: 'rec2', status: 'failed' }] }
  assert.deepEqual(openImports(imports).map(i => i.recid), ['rec2'])
  assert.equal(toFixCount({ imports }), 1)
})

test('#/needs/imports opens Needs you on the imports list', () => {
  assert.deepEqual(needsView(['imports']), { tab: 'fix', list: 'imports', key: '' })
  assert.deepEqual(needsView(['imports', 'rec7']), { tab: 'fix', list: 'imports', key: 'imp:rec7' })
  // The other addresses are as they were.
  assert.deepEqual(needsView([]), { tab: 'gaps', list: '', key: '' })
  assert.deepEqual(needsView(['gaps', 'g1']), { tab: 'gaps', list: '', key: 'g1' })
  assert.deepEqual(needsView(['fix', 'imp:rec7']), { tab: 'fix', list: '', key: 'imp:rec7' })
  assert.deepEqual(needsView(['fix']), { tab: 'fix', list: '', key: '' })
})
