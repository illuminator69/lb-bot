// Hash routes, plus the one thing the hash alone cannot say: where you came
// from. Artist and album pages belong to no section — they open on top of
// whatever you were looking at, and their Back button names that page. So
// every history entry this app makes carries its index (`history.state.i`),
// and a small list beside it remembers each entry's page and title.

import { BAG_KEYS, bagForPush, boundBag, readStored, restores, sameTab } from './view.js'

export const SECTIONS = [
  ['needs', 'Needs you'],
  ['activity', 'Activity'],
  ['discover', 'Discover'],
  ['library', 'Library'],
  ['system', 'System'],
]
const SECTION_KEYS = SECTIONS.map(s => s[0])
// Pages that sit on top of a section.
const OVERLAYS = { artist: 'Artist', album: 'Album', search: 'Search', place: 'File a folder' }
export const ROUTES = [...SECTION_KEYS, ...Object.keys(OVERLAYS)]
export const isSection = tab => SECTION_KEYS.includes(tab)
export const sectionLabel = tab => SECTIONS.find(s => s[0] === tab)?.[1] || OVERLAYS[tab] || tab

// Hashes from before the redesign (and the restructure before that), so
// bookmarks and history entries still land somewhere sensible.
function aliasOf(segs) {
  const [head, ...rest] = segs
  switch (head) {
    case 'gaps': return ['needs', 'gaps', ...rest]
    case 'playlist': return ['needs', 'gaps']
    case 'downloads':
      if (rest[0] === 'place') return ['place', ...rest.slice(1)]
      return ['activity']
    case 'import': return rest.length ? ['place', ...rest] : ['needs', 'fix']
    case 'fresh': return ['discover', 'new']
    case 'settings': return ['system', ...rest]
    case 'system': {
      const sub = { health: 'status', prefs: 'sources' }[rest[0]]
      return sub ? ['system', sub] : null
    }
    case 'library':
      if (rest[0] === 'duplicates') return ['library', 'cleanup']
      if (rest[0] === 'artists' && rest[1]) {
        return rest[2] ? ['album', rest[1], ...rest.slice(2)] : ['artist', rest[1]]
      }
      return null
    default: return null
  }
}

const enc = s => encodeURIComponent(s)

/** Build a hash for a route plus positional params. Empty params stop the list. */
export function routeHash(tab, ...params) {
  if (!ROUTES.includes(tab)) return '#/'
  const parts = []
  for (const p of params) {
    if (p == null || p === '') break
    parts.push(enc(String(p)))
  }
  return `#/${tab}${parts.length ? '/' + parts.join('/') : ''}`
}

function splitHash(hash) {
  const raw = (hash || '').replace(/^#\/?/, '')
  return raw.split('/').filter(s => s !== '').map(s => { try { return decodeURIComponent(s) } catch { return s } })
}

/** Parse a hash into { tab, params } — params are already decoded. An
 *  old-style hash is rewritten in place (no history entry). */
export function parseHash(hash = location.hash) {
  let segs = splitHash(hash)
  if (!segs.length) return { tab: null, params: [] }
  const aliased = aliasOf(segs)
  if (aliased) {
    segs = aliased
    const next = routeHash(segs[0], ...segs.slice(1))
    if (hash === location.hash && next !== location.hash) {
      history.replaceState(history.state, '', location.pathname + location.search + next)
    }
  }
  if (!ROUTES.includes(segs[0])) return { tab: null, params: [] }
  return { tab: segs[0], params: segs.slice(1) }
}

// "The same page" for Back: every tab and selection inside a section is that
// section; an album is itself whichever artist id it was reached through.
function pageKey(tab, params) {
  if (isSection(tab)) return tab
  if (tab === 'artist') return `artist/${params[0] || ''}`
  if (tab === 'album') return `album/${params[1] || ''}`
  if (tab === 'place') return `place/${params[0] || ''}`
  return tab || ''
}

// ── The entry list ───────────────────────────────────────────────────────────
// An entry is `{hash, tab, key, title}` and — B-094, N1 — `view`: the small bag
// of view state the page kept while it was this entry (lib/view.js says what a
// bag may hold and when it is restored). Stored with the entries; additive, so
// the build before reads what this one writes and this one what that wrote.
const STORE = 'lb.nav'
let entries = []
let idx = 0
let lastKind = 'soft'
// How the entry on show was arrived at: `n` goes up with every arrival that is
// not the same page under another entry, `restore` says whether what the
// entry kept is to be put back.
let arrival = { n: 0, restore: false }
const listeners = new Set()

function persist() {
  try {
    const out = []
    for (let j = Math.max(0, entries.length - 60); j < entries.length; j++) {
      if (!entries[j]) continue
      const { view, ...entry } = entries[j]
      const bag = boundBag(view)
      out.push(bag ? { ...entry, view: bag, i: j } : { ...entry, i: j })
    }
    sessionStorage.setItem(STORE, JSON.stringify(out))
  } catch { /* optional */ }
}
// A bag is written to as a page is scrolled: stored a moment later, and when
// the page is left for good (a reload reads it back).
let storing = 0
function persistSoon() {
  if (storing) return
  storing = setTimeout(() => { storing = 0; persist() }, 250)
}

function entryFor(hash) {
  const { tab, params } = parseHash(hash)
  return { hash, tab, key: pageKey(tab, params), title: '' }
}

function boot() {
  const i = history.state?.i
  let stored = []
  try { stored = readStored(JSON.parse(sessionStorage.getItem(STORE))) } catch { /* optional */ }
  if (Number.isInteger(i) && stored.length) {
    // A reload inside a session: the browser kept the entries, so keep ours.
    for (const e of stored) entries[e.i] = e
    idx = i
  } else {
    entries = []
    idx = 0
    history.replaceState({ ...(history.state || {}), i: 0 }, '')
  }
  // The entry the reload is on, as it was left: what it kept is restored.
  const kept = !!entries[idx] && entries[idx].hash === location.hash
  if (!kept) entries[idx] = entryFor(location.hash)
  arrival = { n: 1, restore: restores(kept ? 'boot' : 'push') }
}
boot()

function kindOf(prev, next, viaSection) {
  if (!prev || prev.key === next.key) {
    if (!prev || !isSection(next.tab)) return 'soft'
    // (A section's front address and its first tab are one tab — lib/view.js `tabOf`.)
    return sameTab(next.tab, parseHash(prev.hash).params, parseHash(next.hash).params) ? 'soft' : 'tab'
  }
  return viaSection ? 'section' : 'push'
}

function emit(kind) {
  lastKind = kind
  persist()
  for (const fn of listeners) fn(kind)
}

/** Subscribe to route changes; the callback gets 'push' | 'pop' | 'section' |
 *  'tab' | 'soft' — how the page should arrive. */
export function onRoute(fn) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

// `from`: the entry the bar is returning to (navigateSection) — the new entry
// is that page again, and takes what it kept.
function push(hash, viaSection, from) {
  if (location.hash === hash) return
  const prev = entries[idx]
  idx += 1
  entries.length = idx
  entries[idx] = entryFor(hash)
  const kind = kindOf(prev, entries[idx], viaSection)
  const view = bagForPush({ kind, prev, from })
  if (view) entries[idx].view = view
  // The same page under another entry (a row selected) is no arrival: the page stays as it is.
  if (kind !== 'soft') arrival = { n: arrival.n + 1, restore: restores(from ? 'return' : 'push') }
  history.pushState({ i: idx }, '', location.pathname + location.search + hash)
  emit(kind)
}

/** Go to a route as a new history entry (the normal navigation verb). */
export function navigate(tab, ...params) { push(routeHash(tab, ...params), false) }

/** Sidebar navigation: the section arrives from below instead of sideways.
 *  With no params it returns to where you last were in that section — the
 *  tab, the selected album — unless you are already there, in which case it
 *  goes to the section's front page. */
export function navigateSection(tab, ...params) {
  const here = entries[idx]
  if (!params.length && here?.tab !== tab) {
    for (let j = idx - 1; j >= 0 && entries[j]; j--) {
      if (entries[j].tab === tab) { push(entries[j].hash, true, entries[j]); return }
    }
  }
  // The section you are in: its front address, and nothing else (Y1). From its
  // first tab that is the same page under another entry — nothing is reset,
  // and Back finds the page as it was; on the front address itself `push`
  // does nothing. (For a day this press gave a fresh page, and one stray tap
  // on the bar cost the filter and the place for good.)
  push(routeHash(tab, ...params), true)
}

/** The section an artist, album or search page was opened from, for the
 *  sidebar's highlight. null when the page was opened directly. */
export function rootSection() {
  for (let j = idx; j >= 0 && entries[j]; j--) {
    if (isSection(entries[j].tab)) return entries[j].tab
  }
  return null
}

/** Rewrite the current route without adding an entry (transient sync only). */
export function replaceRoute(tab, ...params) {
  const hash = routeHash(tab, ...params)
  if (location.hash === hash) return
  const title = entries[idx]?.title || ''
  const prev = entries[idx]
  history.replaceState({ ...(history.state || {}), i: idx }, '', location.pathname + location.search + hash)
  entries[idx] = entryFor(hash)
  if (prev && prev.key === entries[idx].key) {
    entries[idx].title = title
    // The same page, the same entry: what it keeps stays with it.
    if (prev.view) entries[idx].view = prev.view
  }
  emit('soft')
}

window.addEventListener('popstate', () => {
  const i = history.state?.i
  const prev = entries[idx]
  if (!Number.isInteger(i)) {
    // An entry this app did not make: a typed hash or a plain <a href="#/…">.
    idx += 1
    entries.length = idx
    entries[idx] = entryFor(location.hash)
    history.replaceState({ ...(history.state || {}), i: idx }, '')
    arrival = { n: arrival.n + 1, restore: restores('typed') }
    emit(kindOf(prev, entries[idx], false))
    return
  }
  const back = i < idx
  idx = i
  // Back or Forward to an entry as it was left: what it kept is restored.
  const kept = !!entries[idx] && entries[idx].hash === location.hash
  if (!kept) entries[idx] = entryFor(location.hash)
  const kind = kindOf(prev, entries[idx], false)
  if (kind !== 'soft') arrival = { n: arrival.n + 1, restore: restores(kept ? 'pop' : 'push') }
  emit(kind === 'push' && back ? 'pop' : kind)
})
window.addEventListener('pagehide', persist)

// ── What an entry keeps (B-094, N1) ──────────────────────────────────────────
/** How the entry on show was arrived at: `{ n, restore }`. */
export const currentArrival = () => arrival

/** The bag of the entry on show (made when there is none). */
export function viewBag() {
  const entry = entries[idx]
  if (!entry) return {}
  if (!entry.view) entry.view = {}
  return entry.view
}

/** Keep `value` under `name` in the entry on show. Bounded: a bag that is full
 *  takes no new name. */
export function saveView(name, value) { saveViewIn(viewBag(), name, value) }

/** The same, into a bag in hand: a page writes into the bag of the entry it
 *  was drawn for — the entry on show may be another by the time an event
 *  lands (X6). */
export function saveViewIn(bag, name, value) {
  if (!bag || bag[name] === value) return
  if (!(name in bag) && Object.keys(bag).length >= BAG_KEYS) return
  bag[name] = value
  persistSoon()
}

/** The entry before the one on show, parsed (`{ tab, params }`), or null. */
export function entryBefore() {
  const entry = entries[idx - 1]
  return entry ? parseHash(entry.hash) : null
}

/** The page Back would return to: the nearest earlier entry that is a
 *  different page. null when this page was opened directly. */
export function backTarget() {
  const cur = entries[idx]
  // A section is a root: the sidebar is how you leave it.
  if (!cur || isSection(cur.tab)) return null
  for (let j = idx - 1; j >= 0; j--) {
    const e = entries[j]
    if (!e) return null
    if (e.key !== cur.key) return { steps: idx - j, title: e.title || sectionLabel(e.tab), tab: e.tab }
  }
  return null
}

export function goBack() {
  const t = backTarget()
  if (t) history.go(-t.steps)
}

/** A page tells the router its title once it knows it, so the page opened on
 *  top of it can name it on its Back button. */
export function setEntryTitle(title) {
  if (!entries[idx] || entries[idx].title === title) return
  entries[idx].title = title
  persist()
}

export const currentKind = () => lastKind
