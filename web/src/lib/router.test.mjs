// The address the apps' "Open in lb-bot" link opens (Worbus P4 S3):
// `<base>/#/needs/imports`. `node --test web/src/lib/router.test.mjs`.
import test from 'node:test'
import assert from 'node:assert/strict'

// router.js runs in a browser: just enough of one for it to load.
const store = new Map()
globalThis.location = { hash: '', pathname: '/', search: '' }
globalThis.history = { state: null, replaceState(state, _t, url) { this.state = state; if (url) location.hash = url.slice(url.indexOf('#')) }, pushState() {} }
globalThis.sessionStorage = { getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)) }
globalThis.window = { addEventListener() {} }

const { parseHash, routeHash } = await import('./router.js')
const { needsView } = await import('./needs.js')

test('#/needs/imports opens Needs you on the imports list', () => {
  const route = parseHash('#/needs/imports')
  assert.deepEqual(route, { tab: 'needs', params: ['imports'] })
  assert.deepEqual(needsView(route.params), { tab: 'fix', list: 'imports', key: '' })
  // One record by its id, as a link may name it.
  const one = parseHash('#/needs/imports/rec41')
  assert.deepEqual(needsView(one.params), { tab: 'fix', list: 'imports', key: 'imp:rec41' })
  assert.equal(routeHash('needs', 'imports'), '#/needs/imports')
})
