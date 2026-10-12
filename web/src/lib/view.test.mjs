// A page comes back as it was left (B-094, N1): the state matrix of lib/view.js.
// `node --test web/src/lib/view.test.mjs` — no dependency.
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  BAG_KEYS, BAG_TEXT, FIRST_TAB, RESTORE_MS, SCROLL, bagForPush, boundBag, initialOf, isListEntry, keepForSession,
  offeredOrAny, readStored, restores, sameTab, scrollLook, scrollTargets, sessionValue, showsSelected, tabOf,
  WHEEL_QUIET_MS, keyIsUsers, wheelIsUsers, wheelScrolls, wheelStream,
} from './view.js'
import * as view from './view.js'

test('what an entry keeps is small: scalars only, and a bounded number of them (N1a)', () => {
  assert.deepEqual(boundBag({ 's.body': 900, 'lib.q': 'the', 'fix.allSets': true, zero: 0, blank: '' }),
    { 's.body': 900, 'lib.q': 'the', 'fix.allSets': true, zero: 0, blank: '' })
  // Never a list or an answer; never a number that is none.
  assert.deepEqual(boundBag({ rows: [1, 2], answer: { a: 1 }, n: NaN, inf: Infinity, f() {}, u: undefined, nul: null, ok: 1 }), { ok: 1 })
  assert.deepEqual(boundBag({ long: 'x'.repeat(BAG_TEXT + 1), fits: 'x'.repeat(BAG_TEXT) }), { fits: 'x'.repeat(BAG_TEXT) })
  const many = Object.fromEntries(Array.from({ length: BAG_KEYS + 9 }, (_, i) => [`k${i}`, i]))
  assert.equal(Object.keys(boundBag(many)).length, BAG_KEYS)
  // Nothing kept is no bag at all — what is stored stays as an older build wrote it.
  for (const none of [undefined, null, {}, [], 'x', 7, { a: [] }]) assert.equal(boundBag(none), undefined)
})

test('`lb.nav` is read whoever wrote it: a build before bags, this one, or something else (N1a)', () => {
  // An older build's entries: no `view`. Read as they are.
  const old = [{ hash: '#/library', tab: 'library', key: 'library', title: 'Library', i: 0 }, { hash: '#/artist/a1', tab: 'artist', key: 'artist/a1', title: 'A', i: 1 }]
  assert.deepEqual(readStored(old), old)
  // This build's: the bag comes back bounded; a bag that holds nothing usable is dropped.
  const mine = [{ ...old[0], view: { 's.body': 900, rows: [1] } }, { ...old[1], view: { bad: {} } }]
  assert.deepEqual(readStored(mine), [{ ...old[0], view: { 's.body': 900 } }, old[1]])
  // What is not a list of entries is nothing; an entry without its place in the history is skipped.
  for (const junk of [null, undefined, 'x', {}, 7]) assert.deepEqual(readStored(junk), [])
  assert.deepEqual(readStored([null, 5, { hash: '#/x' }, { hash: '#/y', i: -1 }, { hash: '#/z', i: 2 }]), [{ hash: '#/z', i: 2 }])
})

test('what is restored for which kind of arrival (N1b, N1c)', () => {
  // Back and Forward, a reload, and the bar's return to a section restore; a push — a link, a tab, a row — is fresh.
  assert.equal(restores('pop'), true)
  assert.equal(restores('boot'), true)
  assert.equal(restores('return'), true)
  assert.equal(restores('push'), false)
  assert.equal(restores('typed'), false)
  assert.equal(restores(undefined), false)
  // The bag a new entry starts with. A link to a page already in the history: none — it arrives at its top.
  const prev = { hash: '#/library', view: { 's.body': 900, 'lib.q': 'the' } }
  assert.equal(bagForPush({ kind: 'push', prev }), undefined)
  assert.equal(bagForPush({ kind: 'tab', prev }), undefined)
  assert.equal(bagForPush({ kind: 'section', prev }), undefined)
  // The same page under another entry (a row selected): the page stays as it is, so the new entry starts with a
  // copy of what the page keeps — a copy: the two are two entries from then on.
  const soft = bagForPush({ kind: 'soft', prev })
  assert.deepEqual(soft, prev.view)
  assert.notEqual(soft, prev.view)
  assert.equal(bagForPush({ kind: 'soft', prev: { hash: '#/x' } }), undefined)
  // The bar returning to a section opens that section's last entry as a new one: its bag goes with it.
  const from = { hash: '#/needs/gaps/g1', view: { 's.list': 2600 } }
  const back = bagForPush({ kind: 'section', prev, from })
  assert.deepEqual(back, { 's.list': 2600 })
  assert.notEqual(back, from.view)
  assert.equal(bagForPush({ kind: 'section', prev, from: { hash: '#/needs' } }), undefined)
})

test('a kept state’s first value: what the entry kept on a restore, the page’s own otherwise (N1d)', () => {
  const bag = { 'lib.q': 'the', 'lib.tiles': 360, 'fix.allSets': true, odd: 'yes' }
  assert.equal(initialOf({ restore: true, bag }, 'lib.q', ''), 'the')
  assert.equal(initialOf({ restore: true, bag }, 'lib.tiles', 120), 360)
  assert.equal(initialOf({ restore: true, bag }, 'fix.allSets', false), true)
  // A fresh arrival is the page's own, whatever a bag says.
  assert.equal(initialOf({ restore: false, bag }, 'lib.q', ''), '')
  // Nothing kept under that name; no bag; a value of another kind than the page's own (another build's).
  assert.equal(initialOf({ restore: true, bag }, 'lib.sort', 'az'), 'az')
  assert.equal(initialOf({ restore: true, bag: undefined }, 'lib.q', ''), '')
  assert.equal(initialOf({ restore: true, bag }, 'odd', false), false)
  assert.equal(initialOf({ restore: true, bag: { 'lib.tiles': '360' } }, 'lib.tiles', 120), 120)
})

test('the scrollers’ saved places: the body, the list, and the detail only for the item it was saved for (N1b)', () => {
  const bag = { [SCROLL.body]: 900, [SCROLL.list]: 2600, [SCROLL.detail]: 199, [SCROLL.detailFor]: '#/needs/gaps/g1' }
  assert.deepEqual(scrollTargets(bag, '#/needs/gaps/g1'), { body: 900, list: 2600, detail: 199 })
  assert.deepEqual(scrollTargets(bag, '#/needs/gaps/g2'), { body: 900, list: 2600, detail: null }, 'another item: its detail starts at its top')
  // A place of 0 is a place (the list was left at its top); nothing saved is none.
  assert.deepEqual(scrollTargets({ [SCROLL.list]: 0 }, '#/x'), { body: null, list: 0, detail: null })
  assert.deepEqual(scrollTargets(undefined, '#/x'), { body: null, list: null, detail: null })
  assert.deepEqual(scrollTargets({ [SCROLL.body]: 'far', [SCROLL.list]: -5 }, '#/x'), { body: null, list: null, detail: null })
})

test('a scroller waiting for its place: at once where the content is in hand, when it arrives where it is fetched — never against the user, and not for ever (N1e)', () => {
  // The content is tall enough: go there, and that is all.
  assert.deepEqual(scrollLook({ target: 900, max: 4000, shown: true, touched: false, waited: 0 }), { act: 'set', top: 900 })
  assert.deepEqual(scrollLook({ target: 900, max: 900, shown: true, touched: false, waited: 0 }), { act: 'set', top: 900 })
  // Not tall enough yet (the rows are being fetched): wait — and take the place when they arrive.
  assert.deepEqual(scrollLook({ target: 900, max: 120, shown: true, touched: false, waited: 300 }), { act: 'wait' })
  assert.deepEqual(scrollLook({ target: 900, max: 1500, shown: true, touched: false, waited: 700 }), { act: 'set', top: 900 })
  // The user has scrolled, touched or typed since: it is theirs, whatever has arrived.
  assert.deepEqual(scrollLook({ target: 900, max: 4000, shown: true, touched: true, waited: 10 }), { act: 'stop' })
  assert.deepEqual(scrollLook({ target: 900, max: 120, shown: true, touched: true, waited: 10 }), { act: 'stop' })
  // Time is up: nothing moves late (X3). The scroller is left where it is — not put at the bottom of what there
  // is, four seconds after arrival, under a user who touched nothing.
  assert.deepEqual(scrollLook({ target: 900, max: 120, shown: true, touched: false, waited: RESTORE_MS }), { act: 'stop' })
  assert.deepEqual(scrollLook({ target: 900, max: -3, shown: true, touched: false, waited: RESTORE_MS + 1 }), { act: 'stop' })
  assert.deepEqual(scrollLook({ target: 900, max: 899, shown: true, touched: false, waited: RESTORE_MS - 1 }), { act: 'wait' })
  // Not laid out (a list hidden behind its detail on a narrow screen): it is waited for until it is shown, however
  // long — nothing can be put anywhere in a box that has no size.
  assert.deepEqual(scrollLook({ target: 2600, max: 0, shown: false, touched: false, waited: 0 }), { act: 'wait' })
  assert.deepEqual(scrollLook({ target: 2600, max: 0, shown: false, touched: false, waited: RESTORE_MS * 50 }), { act: 'wait' })
  // …but a scroller that is not on the page at all is given up on in time.
  assert.deepEqual(scrollLook({ target: 2600, max: 0, shown: false, missing: true, touched: false, waited: RESTORE_MS }), { act: 'stop' })
  assert.deepEqual(scrollLook({ target: 2600, max: 0, shown: false, missing: true, touched: false, waited: 10 }), { act: 'wait' })
  // The top is where a page is anyway.
  assert.deepEqual(scrollLook({ target: 0, max: 0, shown: true, touched: false, waited: 0 }), { act: 'set', top: 0 })
})

test('the pane’s Back on a narrow screen: back to the list’s own entry when that is the entry before, asked of the router (N1f)', () => {
  const before = (tab, ...params) => ({ tab, params })
  // The list of this tab, no row selected: that is the entry to go back to.
  assert.equal(isListEntry(before('needs', 'gaps'), 'needs', 'gaps'), true)
  assert.equal(isListEntry(before('needs', 'fix'), 'needs', 'fix'), true)
  assert.equal(isListEntry(before('needs'), 'needs', 'gaps'), true, 'the section’s front page is its first tab’s list')
  // Another row's detail, another tab's list, another page, or nothing before (a deep link): replace, as before.
  assert.equal(isListEntry(before('needs', 'gaps', 'g7'), 'needs', 'gaps'), false)
  assert.equal(isListEntry(before('needs', 'fix'), 'needs', 'gaps'), false)
  assert.equal(isListEntry(before('needs'), 'needs', 'fix'), false)
  assert.equal(isListEntry(before('artist', 'a1'), 'needs', 'gaps'), false)
  assert.equal(isListEntry(null, 'needs', 'gaps'), false)
})

test('a section’s front address and its first tab are one page: "the same tab" is asked in one place (X1)', () => {
  // The bottom bar lands on `#/needs`; a row opened from there is `#/needs/gaps/<id>`. One tab — the same page,
  // not an arrival: what is kept with the entry does not start over while the detail is open.
  assert.equal(tabOf('needs', []), 'gaps')
  assert.equal(tabOf('needs', ['gaps', 'g1']), 'gaps')
  assert.equal(sameTab('needs', [], ['gaps', 'g1']), true)
  assert.equal(sameTab('needs', ['gaps'], ['gaps', 'g1']), true)
  assert.equal(sameTab('needs', ['gaps', 'g1'], []), true, 'and back again: the pane’s Back')
  // Another tab is another tab, from either address.
  assert.equal(sameTab('needs', [], ['fix']), false)
  assert.equal(sameTab('needs', ['gaps'], ['fix', 'art:a1']), false)
  assert.equal(sameTab('needs', ['fix'], ['fix', 'two:k']), true)
  // Every section has a first tab, the one its page falls back to.
  assert.deepEqual(FIRST_TAB, { needs: 'gaps', activity: 'now', discover: 'new', library: 'artists', system: 'status' })
  assert.equal(sameTab('library', [], ['artists']), true)
  assert.equal(sameTab('system', [], ['overrides']), false)
  assert.equal(sameTab('system', ['status'], []), true)
  // A page that is no section has no tabs.
  assert.equal(tabOf('artist', ['a1']), 'a1')
  assert.equal(tabOf('search', []), '')
})

test('the bar pressed for the section you are in resets nothing: the front address, the page as it is (Y1)', () => {
  // Library › Artists with a filter, scrolled; "Library" pressed in the bar. The front address is the first
  // tab's own, so that is the same page under another entry: no arrival, and the entry starts with what the page
  // keeps — the filter and the place are there, and Back and Forward between the two addresses find them.
  const prev = { hash: '#/library/artists', view: { 'lib.q': 'the', 's.body': 900 } }
  assert.equal(sameTab('library', ['artists'], []), true)
  const front = bagForPush({ kind: 'soft', prev })
  assert.deepEqual(front, { 'lib.q': 'the', 's.body': 900 })
  // Nothing can ask for that press to be a fresh page: there is no such way in any more.
  assert.deepEqual(bagForPush({ kind: 'soft', prev, fresh: true }), { 'lib.q': 'the', 's.body': 900 })
  assert.equal(view.freshens, undefined, 'the rule that made the press on the front address itself a fresh page is gone')
  // From another tab of the section the front address is another tab: a tab pressed, a fresh page — as it was.
  assert.equal(sameTab('library', ['albums'], []), false)
  assert.equal(bagForPush({ kind: 'tab', prev: { hash: '#/library/albums', view: { 's.body': 40 } } }), undefined)
  // From another section it is the return, with what the section's entry kept (N1c stands).
  assert.deepEqual(bagForPush({ kind: 'section', prev: { hash: '#/needs' }, from: prev }), { 'lib.q': 'the', 's.body': 900 })
})

test('a filter kept for the session that the queue no longer offers is "any" (Y2)', () => {
  const origins = { library: 2900, playlist: 29, spotify: 118 }
  // Offered: it stands. "Any" is always "any".
  assert.equal(offeredOrAny('playlist', origins), 'playlist')
  assert.equal(offeredOrAny('', origins), '')
  // Its last album of that origin is gone: the queue would be filtered to nothing, with no row in the menu to
  // clear it by.
  assert.equal(offeredOrAny('playlist', { library: 2900, spotify: 118 }), '')
  assert.equal(offeredOrAny('playlist', { library: 2900, playlist: 0, spotify: 118 }), '')
  // One origin left: the menu is not shown at all — whichever origin that is.
  assert.equal(offeredOrAny('playlist', { playlist: 29 }), '')
  assert.equal(offeredOrAny('library', { library: 2900 }), '')
  assert.equal(offeredOrAny('playlist', {}), '')
  // The queue is not read yet: what it offers is not known, and the choice is not thrown away over that.
  assert.equal(offeredOrAny('playlist', undefined), 'playlist')
  assert.equal(offeredOrAny('playlist', null), 'playlist')
})

test('a list shown again: its kept place is for the arrival it was kept for — after the selection changed it shows the selected row (X5)', () => {
  // Shown with a kept place, the selection as it was on arrival: the place.
  assert.equal(showsSelected({ kept: true, selAtArrival: 'g1', sel: 'g1' }), false)
  // Next pressed in the pane a few times, then the pane's Back: the album now selected.
  assert.equal(showsSelected({ kept: true, selAtArrival: 'g1', sel: 'g4' }), true)
  // No kept place (a deep link, the replace path): the selected row, as before.
  assert.equal(showsSelected({ kept: false, selAtArrival: 'g1', sel: 'g1' }), true)
  assert.equal(showsSelected({ kept: false, selAtArrival: '', sel: 'g1' }), true)
})

test('what is kept for the session is not the entry’s: it is there on every arrival, a fresh one too (X2)', () => {
  assert.equal(sessionValue('test.origin', ''), '', 'the page’s own until something is chosen')
  keepForSession('test.origin', 'playlist')
  assert.equal(sessionValue('test.origin', ''), 'playlist')
  // No arrival is asked: a tab pressed and pressed back, a link, Back — the same.
  assert.equal(initialOf({ restore: false, bag: { 'test.origin': 'library' } }, 'test.origin', ''), '', 'an entry’s bag would have started over')
  assert.equal(sessionValue('test.origin', ''), 'playlist')
  keepForSession('test.origin', '')
  assert.equal(sessionValue('test.origin', 'x'), '', 'back to "any" is a choice too')
})

test('input that was already under way when the page arrived is not the user using the page (W1)', () => {
  // A two-finger swipe back is a stream of wheel events, and its inertia goes on arriving over the page that has
  // just mounted. Streams, as the app hears them: a wheel that comes after a quiet gap begins a new one.
  const none = { began: -Infinity, last: -Infinity }
  let stream = wheelStream(none, 1000)
  assert.deepEqual(stream, { began: 1000, last: 1000 }, 'the first wheel begins a stream')
  for (const at of [1008, 1016, 1024, 1032]) stream = wheelStream(stream, at)
  assert.deepEqual(stream, { began: 1000, last: 1032 })
  // The browser goes back; the page arrives at 1040, in the middle of it.
  const arrived = 1040
  // A stream from before the arrival: its events over the new page are not the user's — whichever way they point
  // (the rule never asks), so a restore that is still on its way is not ended by them.
  stream = wheelStream(stream, 1048)
  assert.equal(wheelIsUsers(stream, arrived), false)
  // The same stream still trickling: inertia's events come further and further apart, and stay the same stream
  // for as long as each comes within the quiet gap of the one before.
  for (const at of [1070, 1110, 1180, 1300, 1300 + WHEEL_QUIET_MS - 1]) {
    stream = wheelStream(stream, at)
    assert.equal(wheelIsUsers(stream, arrived), false, `still the swipe at ${at}`)
  }
  assert.equal(stream.began, 1000)
  // A new one after the quiet: the user, scrolling the page that is there now.
  const later = stream.last + WHEEL_QUIET_MS
  stream = wheelStream(stream, later)
  assert.deepEqual(stream, { began: later, last: later })
  assert.equal(wheelIsUsers(stream, arrived), true)
  assert.equal(wheelIsUsers(wheelStream(stream, later + 8), arrived), true, 'and so is the rest of it')
  // No stream was running at arrival: the first wheel on the page is the user's, as it always was.
  assert.equal(wheelIsUsers(wheelStream(none, 5000), 4000), true)
  assert.equal(wheelIsUsers(wheelStream({ began: 100, last: 180 }, 5000), 4000), true, 'one long before the arrival is over')
  // The gap: longer than inertia's widest steps, shorter than lifting the fingers and starting again.
  assert.ok(WHEEL_QUIET_MS >= 150 && WHEEL_QUIET_MS <= 400, String(WHEEL_QUIET_MS))
})

test('a key that was already down, and a modifier by itself, are not the user using the page either; a key pressed anew is (W1)', () => {
  // Alt+← : Alt is held across the arrival, and ← may still be repeating.
  assert.equal(keyIsUsers({ key: 'ArrowLeft', repeat: true }), false, 'a held key')
  assert.equal(keyIsUsers({ key: 'Alt', repeat: false }), false, 'a modifier alone')
  for (const key of ['Alt', 'AltGraph', 'Control', 'Meta', 'Shift', 'OS']) assert.equal(keyIsUsers({ key, repeat: false }), false, key)
  assert.equal(keyIsUsers({ key: 'Shift', repeat: true }), false)
  // A fresh key: the user.
  assert.equal(keyIsUsers({ key: 'ArrowDown', repeat: false }), true)
  assert.equal(keyIsUsers({ key: 'a', repeat: false }), true)
  assert.equal(keyIsUsers({ key: ' ' }), true, 'repeat not said is not a repeat')
  assert.equal(keyIsUsers({}), true)
  assert.equal(keyIsUsers(undefined), true, 'an event that says nothing of itself is taken for the user’s, as before')
})

test('a wheel that is not mainly vertical never ends a restore: these scrollers go up and down and nowhere else (W1b)', () => {
  // A swipe may pause before the fingers lift; what the browser sends at the release is a new stream, begun at or
  // after the arrival. It is sideways — it could not scroll the page — so it is not the user scrolling it.
  assert.equal(wheelScrolls({ deltaX: 40, deltaY: 0 }), false, 'a sideways event, of whatever stream')
  assert.equal(wheelScrolls({ deltaX: -40, deltaY: 0 }), false)
  // The zero event (a gesture's end, as some touchpads send it).
  assert.equal(wheelScrolls({ deltaX: 0, deltaY: 0 }), false)
  assert.equal(wheelScrolls({ deltaX: -0, deltaY: -0 }), false)
  // A diagonal one that is mainly vertical: the user, scrolling.
  assert.equal(wheelScrolls({ deltaX: 3, deltaY: 40 }), true)
  assert.equal(wheelScrolls({ deltaX: -12, deltaY: -13 }), true)
  assert.equal(wheelScrolls({ deltaX: 0, deltaY: 1 }), true)
  // Mainly sideways, and the tie: not.
  assert.equal(wheelScrolls({ deltaX: 40, deltaY: 3 }), false)
  assert.equal(wheelScrolls({ deltaX: 13, deltaY: -12 }), false)
  assert.equal(wheelScrolls({ deltaX: 20, deltaY: 20 }), false)
  // An event that says nothing of where it points cannot be said to scroll.
  assert.equal(wheelScrolls({}), false)
  assert.equal(wheelScrolls(undefined), false)
  // Both must hold for a wheel to end a restore: its stream began after the page arrived (W1), and it is mainly
  // vertical (W1b).
  const takes = (event, stream, arrived) => wheelIsUsers(stream, arrived) && wheelScrolls(event)
  const fresh = { began: 5000, last: 5000 }, carried = { began: 900, last: 5000 }
  assert.equal(takes({ deltaX: 0, deltaY: 40 }, fresh, 4000), true)
  assert.equal(takes({ deltaX: 40, deltaY: 0 }, fresh, 4000), false, 'a sideways event of a new stream')
  assert.equal(takes({ deltaX: 0, deltaY: 40 }, carried, 4000), false, 'a vertical event of the stream that brought the page')
})
