// A page comes back as it was left (B-094, N1): the pure decisions. The app
// mounts one page and unmounts it when left, and the router kept `{hash, tab,
// key, title}` per history entry — so every scroller's offset and every page's
// own state was gone after a visit elsewhere. View state belongs to the
// history entry: each entry keeps a small bag (scroll offsets, small scalars —
// never a list or an answer), stored with the entries in `lb.nav`. Arriving at
// an entry by Back, Forward, a reload or the bar's return restores it; a push
// is a new entry, a fresh page at its top. lib/router.js holds the bags,
// lib/useView.js is the hooks; what they decide is here, with view.test.mjs.

export const BAG_KEYS = 24      // how many things an entry keeps
export const BAG_TEXT = 200     // how long a kept string may be
export const RESTORE_MS = 4000  // how long a fetched page is waited for before its place is given up on

const keeps = v => (typeof v === 'number' ? Number.isFinite(v) : typeof v === 'boolean' || (typeof v === 'string' && v.length <= BAG_TEXT))

/** A bag as it may be kept and stored, or undefined when it holds nothing. */
export function boundBag(bag) {
  if (!bag || typeof bag !== 'object' || Array.isArray(bag)) return undefined
  const out = {}
  let n = 0
  for (const [k, v] of Object.entries(bag)) {
    if (n >= BAG_KEYS) break
    if (k.length <= 40 && keeps(v)) { out[k] = v; n += 1 }
  }
  return n ? out : undefined
}

/** `lb.nav` as read back: the entries that say where in the history they are,
 *  each with its bag bounded. An entry written by a build before bags has
 *  none and is read as it is; this build's `view` is one more key to that
 *  build, which keeps it and ignores it (a rollback loses nothing). */
export function readStored(stored) {
  const out = []
  for (const e of Array.isArray(stored) ? stored : []) {
    if (!e || typeof e !== 'object' || !Number.isInteger(e.i) || e.i < 0) continue
    const { view, ...entry } = e
    const bag = boundBag(view)
    out.push(bag ? { ...entry, view: bag } : entry)
  }
  return out
}

/** How an entry was arrived at decides whether what it kept is restored:
 *  `pop` (Back or Forward — the page's, the pane's, the browser's), `boot` (a
 *  reload inside a session) and `return` (the bar going back to where a
 *  section was left) restore; `push` (a link, a tab, a row) and `typed` (an
 *  address the app did not make) are a fresh page. */
export const restores = how => how === 'pop' || how === 'boot' || how === 'return'

/** The bag a pushed entry starts with. `from`: the entry the bar is returning
 *  to — the new entry is that page again, so its bag goes with it. `kind`
 *  'soft': the same page under another entry (a row selected) — the page
 *  stays as it is, so the entry starts with a copy of what it keeps. Else
 *  none: a fresh page. */
export function bagForPush({ kind, prev, from }) {
  if (from) return from.view ? { ...from.view } : undefined
  if (kind === 'soft' && prev?.view) return { ...prev.view }
  return undefined
}

/** A kept state's first value: what the entry kept, on a restore, when it is
 *  of the kind the page's own value is; the page's own otherwise. */
export function initialOf({ restore, bag }, name, initial) {
  if (!restore || !bag || !Object.prototype.hasOwnProperty.call(bag, name)) return initial
  return typeof bag[name] === typeof initial ? bag[name] : initial
}

/** Where the frame keeps each scroller's place: the page's body, and on a
 *  two-pane page the list and the detail — the detail's with the address it
 *  was saved under, since it is only that item's. */
export const SCROLL = { body: 's.body', list: 's.list', detail: 's.detail', detailFor: 's.for' }

/** The places to restore at `hash`: a number (0 is a place: the top), or null
 *  for none saved. */
export function scrollTargets(bag, hash) {
  const at = k => (Number.isFinite(bag?.[k]) && bag[k] >= 0 ? bag[k] : null)
  return { body: at(SCROLL.body), list: at(SCROLL.list), detail: bag?.[SCROLL.detailFor] === hash ? at(SCROLL.detail) : null }
}

/** One look at a scroller that is waiting for its place.
 *  `set`: go to `top`, and that is all. `wait`: not yet. `stop`: leave it.
 *  Content in hand at mount is tall enough at the first look, before the
 *  first paint. Fetched content is waited for, `limit` at most, and then the
 *  place is as far as there is. The user's own scroll, touch or key ends it
 *  at once (`touched`). A scroller that is not laid out (`shown` false: a list
 *  hidden behind its detail on a narrow screen) is waited for until it is,
 *  however long; one that is not on the page (`missing`) only for `limit`. */
export function scrollLook({ target, max, shown, missing = false, touched, waited, limit = RESTORE_MS }) {
  if (touched) return { act: 'stop' }
  if (!shown) return missing && waited >= limit ? { act: 'stop' } : { act: 'wait' }
  if (max >= target) return { act: 'set', top: target }
  // Time is up: nothing moves late (X3). The scroller is left where it is.
  if (waited >= limit) return { act: 'stop' }
  return { act: 'wait' }
}

// ── Whose input it is (W1) ───────────────────────────────────────────────────
// A restore that is still on its way is ended by the user using the page. But
// input that was already under way when the page arrived is not that: going
// back by a two-finger swipe IS a stream of wheel events, and its inertia goes
// on arriving over the page that has just mounted — one of them, in the frame
// or two a page waits for its place, ended the restore for good.

/** The quiet gap between two wheel events that makes the second the start of
 *  a new stream. Inertia is sent frame by frame (8–17 ms apart) and its last
 *  events come further apart as it dies; this is a dozen frames and more, wider
 *  than any step of a decaying stream is expected to be, and still shorter
 *  than lifting the fingers and starting to scroll again. Too short ends a
 *  restore under a swipe's tail; too long only means a scroll begun that soon
 *  after a swipe carries on from the restored place instead of holding it. */
export const WHEEL_QUIET_MS = 250

/** The wheel streams as the app hears them: `{ began, last }` after a wheel
 *  event at `at`. One that comes a quiet gap after the one before begins a
 *  new stream. */
export function wheelStream(stream, at) {
  return at - stream.last >= WHEEL_QUIET_MS ? { began: at, last: at } : { began: stream.began, last: at }
}

/** Is a wheel event the user's, on a page that arrived at `arrived`? Only
 *  when its stream began after the page was there. The stream that was
 *  running at arrival — and its inertia, whichever way it points and however
 *  long it trickles — is the gesture that brought the page. */
export const wheelIsUsers = (stream, arrived) => stream.began > arrived

/** Could a wheel event scroll one of the frame's scrollers (W1b)? They go up
 *  and down and nowhere else, so only one that is mainly vertical: a
 *  sideways one, a zero one, and the tie are not the user scrolling the
 *  page, whatever stream they belong to. A swipe may pause before the fingers
 *  lift, and what the browser sends at the release is then a new stream begun
 *  after the page arrived — sideways, like the swipe. Cost: someone scrolling
 *  a sideways strip while the page's content is still on its way is moved
 *  when the page takes its place. */
export const wheelScrolls = event => Math.abs(event?.deltaY) > Math.abs(event?.deltaX)

const MODIFIERS = new Set(['Alt', 'AltGraph', 'Control', 'Meta', 'Shift', 'OS', 'Fn', 'CapsLock', 'NumLock', 'ScrollLock'])
/** Is a key press the user's? Not a key that was already down (`repeat`), and
 *  not a modifier by itself (Alt, held for Alt+←): a key pressed anew is. */
export const keyIsUsers = event => !event?.repeat && !MODIFIERS.has(event?.key)

// ── One page, one tab (X1) ───────────────────────────────────────────────────
/** Each section's first tab: the one its page shows on the section's front
 *  address (`#/needs` is the gaps queue). The pages fall back to these. */
export const FIRST_TAB = { needs: 'gaps', activity: 'now', discover: 'new', library: 'artists', system: 'status' }

/** The tab an address is on: its own first parameter, else its section's
 *  first tab. **The one place that says which tab an address is** — the
 *  router's "the same page under another entry" and the pane's Back both ask
 *  it. Comparing the raw parameter made `#/needs` and `#/needs/gaps` two
 *  tabs: a row opened from the bottom bar's address was an arrival, and what
 *  the entry kept started over behind the open detail. */
export const tabOf = (section, params) => params?.[0] || FIRST_TAB[section] || ''
export const sameTab = (section, a, b) => tabOf(section, a) === tabOf(section, b)

/** A list that is shown again (X5): its kept place is for the arrival it was
 *  kept for. Once the selection is another than it was then — Next pressed in
 *  the pane — the list shows the selected row; with no kept place it always
 *  did. */
export const showsSelected = ({ kept, selAtArrival, sel }) => !kept || sel !== selAtArrival

// ── Kept for the session, not with an entry (X2) ─────────────────────────────
// A filter of a queue is the queue's for the session — like its search — and
// not a history entry's: it is there on every arrival, a tab pressed included.
const session = new Map()
export const sessionValue = (name, initial) => (session.has(name) ? session.get(name) : initial)
export const keepForSession = (name, value) => { session.set(name, value) }

/** A kept choice among what a menu offers (Y2): one the menu no longer offers
 *  is "any" — the queue would otherwise be filtered to nothing, with no row
 *  in the menu to clear it by. `counts`: what the queue has of each, by name;
 *  the menu is shown for two or more of them. Not known yet (the queue is not
 *  read): the choice stands. The kept value itself is not touched — it is in
 *  force again if the queue offers it again. */
export function offeredOrAny(value, counts) {
  if (!value || counts === undefined || counts === null) return value || ''
  const offered = Object.keys(counts).filter(k => counts[k] > 0)
  return offered.length > 1 && offered.includes(value) ? value : ''
}

/** Needs you on a narrow screen: is the entry before this one the list this
 *  detail was opened from — this tab's list, no row selected? Then the pane's
 *  Back goes back to it; otherwise it replaces, as it always did. `before`:
 *  the router's entry before the current one, parsed, or null. */
export function isListEntry(before, section, tab) {
  if (!before || before.tab !== section) return false
  return tabOf(section, before.params) === tab && !before.params[1]
}
