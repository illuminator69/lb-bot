// Worbus P1 (S6): who sees what, and the Find-it rows.
// `node --test web/src/lib/access.test.mjs` — no dependency.
import test from 'node:test'
import assert from 'node:assert/strict'
import { downloadsKnown, downloadsOn, findItRows, routeDownloads, routeRedirect, sectionsFor } from './access.js'

const SECTIONS = [['needs', 'Needs you'], ['activity', 'Activity'], ['discover', 'Discover'],
  ['library', 'Library'], ['system', 'System']]

test('downloads: off only when the server says so; absent is an older lb-bot, on', () => {
  assert.equal(downloadsOn({ downloads: 'off' }), false)
  assert.equal(downloadsOn({ downloads: 'on' }), true)
  assert.equal(downloadsOn({}), true)
})

test('downloads before the summary has loaded: unknown, so hidden, and no redirect for it', () => {
  // Review S#8: the first paint flashed download surfaces on a server
  // that does not download.
  for (const unloaded of [null, undefined]) {
    assert.equal(downloadsKnown(unloaded), false)
    assert.equal(downloadsOn(unloaded), false)
    assert.equal(routeDownloads(unloaded), true)
    assert.deepEqual(sectionsFor(SECTIONS, { admin: true, downloads: downloadsOn(unloaded) })
      .map(s => s[0]), ['needs', 'discover', 'library', 'system'])
    assert.equal(routeRedirect('activity', '', { admin: true, downloads: routeDownloads(unloaded) }), null)
  }
  assert.equal(downloadsKnown({}), true)
  assert.equal(routeDownloads({ downloads: 'off' }), false)
  assert.equal(routeDownloads({ downloads: 'on' }), true)
})

test('the sidebar: a non-admin has Discover and Library; off drops Activity', () => {
  const keys = o => sectionsFor(SECTIONS, o).map(s => s[0])
  assert.deepEqual(keys({ admin: true, downloads: true }), ['needs', 'activity', 'discover', 'library', 'system'])
  assert.deepEqual(keys({ admin: true, downloads: false }), ['needs', 'discover', 'library', 'system'])
  assert.deepEqual(keys({ admin: false, downloads: true }), ['discover', 'library'])
  assert.deepEqual(keys({ admin: false, downloads: false }), ['discover', 'library'])
})

test('routes a user or server cannot show go somewhere that can', () => {
  const user = { admin: false, downloads: true }
  const off = { admin: true, downloads: false }
  assert.deepEqual(routeRedirect('needs', 'gaps', user), ['library'])
  assert.deepEqual(routeRedirect('place', '', user), ['library'])
  assert.deepEqual(routeRedirect('system', 'logs', user), ['system', 'appearance'])
  assert.equal(routeRedirect('system', 'appearance', user), null)
  assert.equal(routeRedirect('album', 'x', user), null)
  assert.deepEqual(routeRedirect('activity', '', off), ['needs'])
  assert.deepEqual(routeRedirect('place', '', off), ['needs'])
  assert.deepEqual(routeRedirect('system', 'sources', off), ['system', 'status'])
  assert.equal(routeRedirect('needs', 'fix', off), null)
  assert.equal(routeRedirect('activity', '', { admin: true, downloads: true }), null)
})

test('Find it: confirmed first, searches named "Search <service>", nothing that is not a link', () => {
  const rows = findItRows([
    { service: 'qobuz', kind: 'search', url: 'https://www.qobuz.com/search?q=a' },
    { service: 'spotify', kind: 'confirmed', url: 'https://open.spotify.com/album/1' },
    { service: 'other', kind: 'confirmed', url: 'https://www.7digital.com/x' },
    { service: 'bandcamp', kind: 'search', url: 'javascript:alert(1)' },
  ])
  assert.deepEqual(rows.map(r => [r.kind, r.label]), [
    ['confirmed', 'Spotify'], ['confirmed', '7digital.com'], ['search', 'Search Qobuz']])
  assert.equal(findItRows(undefined), null)
  assert.deepEqual(findItRows([]), [])
})
