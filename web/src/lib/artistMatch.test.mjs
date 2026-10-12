// What the web UI may say about a library artist's MusicBrainz match (I-029,
// U2): where the match comes from, what a search answered, what a set or a
// remove did. `node --test web/src/lib/artistMatch.test.mjs` — no dependency.
import test from 'node:test'
import assert from 'node:assert/strict'
// A name in a sentence is isolated (lib/text.js): what an expectation writes around one.
const I = s => '\u2068' + s + '\u2069'
import {
  NOT_KNOWN, SHARED_NOTE, candidateView, dateWords, fixRowView, keepListed, linedUp, listGiven, lookupView, matchNow, mbArtistUrl,
  nowView, pageRead, pastedCandidate, quietLine, scanMachine, scanResume, scanStart, scanStep, scanToast, scanView, searchRef, setBody,
} from './artistMatch.js'
import { SILENT_MS } from './overrides.js'

const A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const C = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
const SET_AT = new Date(2026, 9, 11, 12).getTime() / 1000
const overrides = (over = {}) => ({ artists: [], toCheck: [], ...over })
const yours = (over = {}) => ({ ndArtistId: 'nd-1', mbid: A, name: 'The Band', tagMbid: '', setAt: SET_AT, ...over })
const check = (kind, over = {}) => ({ ndArtistId: 'nd-1', name: 'The Band', kind, mbid: kind === 'unmatched' ? B : '', albums: 5, ...over })
const pick = (now, ...keys) => keys.map(k => now[k])

// ── where the match comes from ──
test('your choice: the override, when it was set, and what removing it goes back to', () => {
  const now = matchNow({ id: 'nd-1', overrides: overrides({ artists: [yours({ tagMbid: C })] }), artist: { id: 'nd-1', mbid: A, tagMbid: C } })
  assert.deepEqual(pick(now, 'source', 'mbid', 'setAt', 'backTo'), ['yours', A, SET_AT, 'tags'])
  const untagged = matchNow({ id: 'nd-1', overrides: overrides({ artists: [yours()] }), indexMbid: null })
  assert.deepEqual(pick(untagged, 'source', 'backTo'), ['yours', 'search'])
  // Another artist's override is not this one's.
  assert.equal(matchNow({ id: 'nd-2', overrides: overrides({ artists: [yours()] }), indexMbid: null }).source, 'search')
})

test('the files’ tags: the artist list’s own id, when nothing overrides it', () => {
  const now = matchNow({ id: 'nd-1', overrides: overrides(), artist: { id: 'nd-1', mbid: C } })
  assert.deepEqual(pick(now, 'source', 'mbid', 'backTo', 'holdsNone'), ['tags', C, '', false])
  // Before the overrides are read the artist list already says which it is.
  assert.equal(matchNow({ id: 'nd-1', artist: { id: 'nd-1', mbid: C } }).source, 'tags')
  assert.deepEqual(pick(matchNow({ id: 'nd-1', artist: { id: 'nd-1', mbid: A, tagMbid: C } }), 'source', 'mbid', 'setAt', 'backTo'),
                   ['yours', A, 0, 'tags'])
})

test('the artist list is kept for a minute: after a remove the overrides answer is believed, and the list’s tag', () => {
  // The list still carries the override (mbid A, tagMbid C); lb-bot's own answer no longer has it.
  const tagged = matchNow({ id: 'nd-1', overrides: overrides(), artist: { id: 'nd-1', mbid: A, tagMbid: C } })
  assert.deepEqual(pick(tagged, 'source', 'mbid'), ['tags', C])
  const untagged = matchNow({ id: 'nd-1', overrides: overrides({ toCheck: [check('unresolved')] }), artist: { id: 'nd-1', mbid: A, tagMbid: '' } })
  assert.equal(untagged.source, 'none')
})

test('lb-bot’s own guess: one that holds none of the albums is the list’s `unmatched`', () => {
  const now = matchNow({ id: 'nd-1', overrides: overrides({ toCheck: [check('unmatched')] }) })
  assert.deepEqual(pick(now, 'source', 'mbid', 'holdsNone', 'albums'), ['guess', B, true, 5])
  // Listed as a guess with no id to it: there is nothing to keep, and it reads as no match.
  assert.equal(matchNow({ id: 'nd-1', overrides: overrides({ toCheck: [check('unmatched', { mbid: '' })] }) }).source, 'none')
  // A guess that holds albums is not listed: the page's own read names it.
  const held = matchNow({ id: 'nd-1', overrides: overrides(), indexMbid: B })
  assert.deepEqual(pick(held, 'source', 'mbid', 'holdsNone'), ['guess', B, false])
  // The list wins over a page that is behind it.
  assert.equal(matchNow({ id: 'nd-1', overrides: overrides({ toCheck: [check('unmatched')] }), indexMbid: C }).mbid, B)
})

test('no match: the list’s `unresolved`, or a page whose own read names nobody', () => {
  const none = matchNow({ id: 'nd-1', overrides: overrides({ toCheck: [check('unresolved', { albums: 1 })] }), indexMbid: null })
  assert.deepEqual(pick(none, 'source', 'mbid', 'albums'), ['none', '', 1])
  // The artist page has read its discography and it names nobody — whatever the list says or does not.
  assert.deepEqual(pick(matchNow({ id: 'nd-1', artist: { id: 'nd-1' }, indexMbid: '' }), 'source', 'albums'), ['none', null])
  assert.equal(matchNow({ id: 'nd-1', overrides: overrides(), artist: { id: 'nd-1' }, indexMbid: '' }).source, 'none')
  assert.equal(matchNow({ id: 'nd-1', overrides: { artists: [] }, indexMbid: '' }).source, 'none')
})

test('lb-bot’s own search: where no page read exists, an artist the list does not name', () => {
  // Under To fix (`indexMbid: null`): read, not listed, no tag, no override.
  assert.equal(matchNow({ id: 'nd-1', overrides: overrides(), indexMbid: null }).source, 'search')
})

test('a failed read never takes the way to the picker away (R3a): the line says “not known”, the picker keeps its search', () => {
  const failed = matchNow({ id: 'nd-1', overrides: overrides(), artist: { id: 'nd-1' }, indexMbid: false })
  // By the hero: said as not known, so that "Fix match" has a line to stand beside.
  assert.deepEqual(quietLine(failed, undefined), { text: 'MusicBrainz match: not known', naming: false })
  // Still nothing while the read is out — there is nothing to say yet, and it is about to be said.
  assert.equal(quietLine(matchNow({ id: 'nd-1', overrides: overrides(), artist: { id: 'nd-1' }, indexMbid: undefined }), undefined), null)
  // In the picker: the sentence, and "Choose another" — choosing an artist is the one thing that mends a name
  // MusicBrainz's search does not find. Nothing is kept or gone back to: nothing is known.
  const v = nowView(failed)
  assert.deepEqual([v.state, v.line, v.choose, v.keep, v.back, v.title, v.note], ['noread', NOT_KNOWN.noread, true, false, '', '', ''])
  // A list lb-bot left out is still only said (the pane under To fix has its own Try again, and its own search state).
  assert.equal(nowView(matchNow({ id: 'nd-1', overrides: { artists: [] }, indexMbid: null })).state, 'unknown')
})

test('the page’s own lookup answering with nobody is an answer, not a failure (R3a): “No MusicBrainz match”, as it is', () => {
  // `pageRead(disco, error, nobody)`: the page looked the artist up by name and MusicBrainz named no candidate.
  // The page shows that as an error of its scan — but it is known, and true, that there is no match.
  assert.equal(pageRead(null, 'No MusicBrainz match for “16teen”', true), '')
  assert.equal(pageRead({ artist_mbid: '' }, 'No MusicBrainz match for “16teen”', true), '')
  // Only a request that did not answer, or answered an error, is a failed read.
  assert.equal(pageRead(null, 'lb-bot could not read its index just now', false), false)
  assert.equal(pageRead(null, 'MusicBrainz did not answer'), false)
  // An id that was read is an id.
  assert.equal(pageRead({ artist_mbid: B }, 'x', true), B)
  const none = matchNow({ id: 'nd-1', overrides: overrides(), artist: { id: 'nd-1' }, indexMbid: pageRead(null, 'No MusicBrainz match for “16teen”', true) })
  assert.equal(none.source, 'none')
  assert.deepEqual(quietLine(none, undefined), { text: 'No MusicBrainz match', naming: false })
  assert.equal(nowView(none).state, 'none')
})

test('a page read that FAILED is not known either (R3): never “no match”, and not a read that is still out', () => {
  // What the artist page's own discography read says, as the picker takes it: an id; '' — it answered and names
  // nobody; undefined — it is still out; false — it failed, and nothing in a failure says there is no match.
  assert.equal(pageRead({ artist_mbid: B }, null), B)
  assert.equal(pageRead({ artist_mbid: '' }, null), '')
  assert.equal(pageRead(null, null), undefined)
  assert.equal(pageRead(null, 'MusicBrainz did not answer'), false)
  // A stored stub that names nobody, and the scan that would have said failed: still not known.
  assert.equal(pageRead({ artist_mbid: '' }, 'The scan was lost'), false)
  // An id that was read is an id, whatever came after.
  assert.equal(pageRead({ artist_mbid: B }, 'The scan was lost'), B)

  const failed = matchNow({ id: 'nd-1', overrides: overrides(), artist: { id: 'nd-1' }, indexMbid: false })
  assert.deepEqual(pick(failed, 'source', 'why'), ['unknown', 'noread'])
  assert.notEqual(quietLine(failed, undefined)?.text, 'No MusicBrainz match')
  assert.notEqual(nowView(failed).state, 'none', 'the picker says lb-bot could not say — not “has not matched this artist”')
  assert.match(NOT_KNOWN.noread, /could not read this artist’s discography/)
  assert.match(NOT_KNOWN.nolist, /could not work out which artists need a look/)
  // Before lb-bot's own list is read it is the same: a failed read is known to have failed.
  assert.equal(matchNow({ id: 'nd-1', overrides: null, artist: { id: 'nd-1' }, indexMbid: false }).why, 'noread')
  // What IS known needs no page: a choice, a tag, the list's own word.
  assert.equal(matchNow({ id: 'nd-1', overrides: overrides({ artists: [yours()] }), indexMbid: false }).source, 'yours')
  assert.equal(matchNow({ id: 'nd-1', overrides: overrides(), artist: { id: 'nd-1', mbid: C }, indexMbid: false }).source, 'tags')
  assert.equal(matchNow({ id: 'nd-1', overrides: overrides({ toCheck: [check('unmatched')] }), indexMbid: false }).source, 'guess')
  assert.equal(matchNow({ id: 'nd-1', overrides: overrides({ toCheck: [check('unresolved')] }), indexMbid: false }).source, 'none')
})

test('not known is not known (Y1, Y2): a read on its way, and a list lb-bot left out', () => {
  // Nothing read yet.
  assert.equal(matchNow({ id: 'nd-1' }).source, 'unknown')
  assert.equal(matchNow({ id: 'nd-1', indexMbid: null }).source, 'unknown')
  assert.equal(matchNow({ id: 'nd-1', overrides: null, artist: { id: 'nd-1' } }).source, 'unknown')
  // The artist page's discography is still being read: neither "its own search" nor "no match".
  const loading = matchNow({ id: 'nd-1', overrides: overrides(), artist: { id: 'nd-1' }, indexMbid: undefined })
  assert.deepEqual(pick(loading, 'source', 'why'), ['unknown', ''])
  assert.equal(quietLine(loading, undefined), null)
  assert.equal(nowView(loading).state, 'unknown')
  // …unless the list already says: that needs no page.
  assert.equal(matchNow({ id: 'nd-1', overrides: overrides({ toCheck: [check('unmatched')] }), indexMbid: undefined }).source, 'guess')
  // `toCheck` absent — lb-bot could not work the list out (Navidrome busy, right after a set) — is "no list",
  // never "nothing to check": the picker does not know, and says so.
  const nolist = matchNow({ id: 'nd-1', overrides: { artists: [] }, indexMbid: null })
  assert.deepEqual(pick(nolist, 'source', 'why'), ['unknown', 'nolist'])
  assert.equal(nowView(nolist).state, 'unknown')
  // What the answer does carry is still believed without the list.
  assert.equal(matchNow({ id: 'nd-1', overrides: { artists: [yours()] }, indexMbid: null }).source, 'yours')
  assert.equal(matchNow({ id: 'nd-1', overrides: { artists: [] }, artist: { id: 'nd-1', mbid: C }, indexMbid: null }).source, 'tags')
  assert.equal(matchNow({ id: 'nd-1', overrides: { artists: [] }, indexMbid: B }).source, 'guess')
})

test('the To fix list keeps its rows (Y1): matched is said of an artist that left a list lb-bot gave, or that has an override', () => {
  const a = (id, over = {}) => ({ ndArtistId: id, name: id.toUpperCase(), kind: 'unmatched', mbid: B, albums: 2, ...over })
  const first = keepListed([], { artists: [], toCheck: [a('x'), a('y'), a('z')] })
  assert.deepEqual(first.map(i => [i.key, i.kind, i.moved]), [['art:x', 'art', false], ['art:y', 'art', false], ['art:z', 'art', false]])
  // y is matched from the pane: it leaves lb-bot's list, and stays in the page's, in its place.
  const set = keepListed(first, { artists: [{ ndArtistId: 'y', mbid: A }], toCheck: [a('x'), a('z', { albums: 3 })] })
  assert.deepEqual(set.map(i => [i.key, i.moved, i.a.albums]), [['art:x', false, 2], ['art:y', true, 2], ['art:z', false, 3]])
  // The same re-read WITHOUT `toCheck`: no list. Every row stays as it was — only the artist the answer itself
  // says has an override is "matched"; about a hundred rows did flip.
  const nolist = keepListed(first, { artists: [{ ndArtistId: 'y', mbid: A }] })
  assert.deepEqual(nolist.map(i => [i.key, i.moved]), [['art:x', false], ['art:y', true], ['art:z', false]])
  assert.deepEqual(keepListed(first, { artists: [] }).map(i => i.moved), [false, false, false])
  // A list that comes back after that is believed again; one listed again is listed as before.
  const again = keepListed(nolist, { artists: [], toCheck: [a('x'), a('y'), a('w')] })
  assert.deepEqual(again.map(i => [i.key, i.moved]), [['art:x', false], ['art:y', false], ['art:z', true], ['art:w', false]])
  // No answer at all: what the page had.
  assert.equal(keepListed(first, null), first)
  assert.equal(listGiven({ artists: [] }), false)
  assert.equal(listGiven({ artists: [], toCheck: [] }), true)
  assert.equal(listGiven(null), false)
})

// ── what the picker says of it ──
const MB = { name: 'The Band', disambiguation: 'Canadian-American rock group', type: 'Group', country: 'CA', area: 'Canada' }

test('the match as a row: name, what MusicBrainz says of it, a link out, where it comes from', () => {
  const v = nowView(matchNow({ id: 'nd-1', overrides: overrides(), artist: { id: 'nd-1', mbid: C } }), MB)
  assert.deepEqual([v.state, v.title, v.sub, v.href, v.note, v.line], ['match', 'The Band',
    I('Canadian-American rock group') + ' · Group · Canada', `https://musicbrainz.org/artist/${C}`, 'from your files’ tags', ''])
  assert.deepEqual([v.keep, v.choose, v.back], [false, true, ''])
  assert.equal(mbArtistUrl(C), `https://musicbrainz.org/artist/${C}`)
})

test('your choice says its date, and names what “Go back” goes back to', () => {
  const tagged = nowView(matchNow({ id: 'nd-1', overrides: overrides({ artists: [yours({ tagMbid: C })] }) }), MB)
  assert.deepEqual([tagged.note, tagged.back, tagged.keep, tagged.choose],
                   ['your choice on 11 Oct 2026', 'Go back to the files’ tag', false, true])
  const untagged = nowView(matchNow({ id: 'nd-1', overrides: overrides({ artists: [yours()] }), indexMbid: null }), MB)
  assert.equal(untagged.back, 'Go back to lb-bot’s own search')
  // No date on record: none is said.
  assert.equal(nowView(matchNow({ id: 'nd-1', artist: { id: 'nd-1', mbid: A, tagMbid: '' } }), MB).note, 'your choice')
  assert.equal(dateWords(SET_AT), '11 Oct 2026')
  assert.equal(dateWords(0), '')
  assert.equal(dateWords('x'), '')
})

test('a guess that holds none of the albums: Keep this match beside Choose another, and why it is here', () => {
  const v = nowView(matchNow({ id: 'nd-1', overrides: overrides({ toCheck: [check('unmatched')] }) }), MB)
  assert.deepEqual([v.state, v.note, v.keep, v.choose, v.back], ['match', 'lb-bot’s own guess', true, true, ''])
  assert.equal(v.line, 'None of the 5 albums in your library line up with this artist’s discography.')
  const one = nowView(matchNow({ id: 'nd-1', overrides: overrides({ toCheck: [check('unmatched', { albums: 1 })] }) }), MB)
  assert.equal(one.line, 'The one album in your library does not line up with this artist’s discography.')
  // A guess that holds albums needs no keeping.
  const held = nowView(matchNow({ id: 'nd-1', overrides: overrides(), indexMbid: B }), MB)
  assert.deepEqual([held.note, held.keep, held.choose, held.line], ['lb-bot’s own guess', false, true, ''])
})

test('the name is asked for, and may not come: a skeleton first, then a row that still links out', () => {
  const now = matchNow({ id: 'nd-1', overrides: overrides({ toCheck: [check('unmatched')] }) })
  const asking = nowView(now, null)
  assert.deepEqual([asking.state, asking.naming, asking.title, asking.keep], ['match', true, '', true])
  const unnamed = nowView(now, { name: '', disambiguation: '' })
  assert.deepEqual([unnamed.naming, unnamed.title, unnamed.sub, unnamed.href], [false, 'MusicBrainz artist', '', mbArtistUrl(B)])
})

test('no match, lb-bot’s own search, and not known yet', () => {
  const none = nowView(matchNow({ id: 'nd-1', overrides: overrides({ toCheck: [check('unresolved')] }) }), undefined)
  assert.deepEqual([none.state, none.keep, none.choose, none.back, none.title], ['none', false, false, '', ''])
  assert.equal(none.line, 'lb-bot has not matched this artist on MusicBrainz, so its 5 albums cannot be checked for gaps.')
  assert.equal(nowView(matchNow({ id: 'nd-1', overrides: overrides({ toCheck: [check('unresolved', { albums: 1 })] }) })).line,
               'lb-bot has not matched this artist on MusicBrainz, so its 1 album cannot be checked for gaps.')
  const search = nowView(matchNow({ id: 'nd-1', overrides: overrides(), indexMbid: null }))
  assert.deepEqual([search.state, search.line, search.choose, search.keep], ['search', 'lb-bot goes by its own search for this artist.', true, false])
  const unknown = nowView(matchNow({ id: 'nd-1' }))
  assert.deepEqual([unknown.state, unknown.line, unknown.choose, unknown.keep, unknown.back], ['unknown', '', false, false, ''])
})

test('the quiet line by the artist page’s hero', () => {
  const tags = matchNow({ id: 'nd-1', artist: { id: 'nd-1', mbid: C } })
  assert.deepEqual(quietLine(tags, MB), { text: 'MusicBrainz match: ' + I('The Band') + ' (' + I('Canadian-American rock group') + ')', naming: false })
  assert.deepEqual(quietLine(tags, { name: 'The Band', disambiguation: '' }), { text: 'MusicBrainz match: ' + I('The Band'), naming: false })
  assert.deepEqual(quietLine(tags, null), { text: 'MusicBrainz match:', naming: true })
  assert.deepEqual(quietLine(tags, { name: '' }), { text: 'Matched on MusicBrainz', naming: false })
  assert.deepEqual(quietLine(matchNow({ id: 'nd-1', artist: { id: 'nd-1' }, indexMbid: '' }), undefined), { text: 'No MusicBrainz match', naming: false })
  assert.equal(quietLine(matchNow({ id: 'nd-1' }), undefined), null)
  // The line and the picker say one thing in every state (Y2): what the picker calls lb-bot's own search is not "no match".
  const own = matchNow({ id: 'nd-1', overrides: overrides(), indexMbid: null })
  assert.equal(nowView(own).state, 'search')
  assert.deepEqual(quietLine(own, undefined), { text: 'MusicBrainz match: lb-bot’s own search', naming: false })
  for (const now of [matchNow({ id: 'nd-1', artist: { id: 'nd-1' }, indexMbid: '' }),
                     matchNow({ id: 'nd-1', overrides: overrides({ toCheck: [check('unresolved')] }), indexMbid: '' })]) {
    assert.equal(nowView(now).state, 'none')
    assert.equal(quietLine(now, undefined).text, 'No MusicBrainz match')
  }
})

// ── the search ──
test('the field takes a name, or a MusicBrainz artist id or address', () => {
  assert.deepEqual(searchRef('  King Crimson '), { kind: 'name', q: 'King Crimson' })
  assert.deepEqual(searchRef(''), { kind: 'empty' })
  assert.deepEqual(searchRef('   '), { kind: 'empty' })
  assert.deepEqual(searchRef(A.toUpperCase()), { kind: 'id', mbid: A })
  assert.deepEqual(searchRef(`https://musicbrainz.org/artist/${A}/releases`), { kind: 'id', mbid: A })
  assert.deepEqual(searchRef(`beta.musicbrainz.org/artist/${A}`), { kind: 'id', mbid: A })
  // Another kind of MusicBrainz address is not searched for as a name.
  assert.deepEqual(searchRef(`https://musicbrainz.org/release/${A}`), { kind: 'wrong', why: 'kind' })
  assert.deepEqual(searchRef(`https://musicbrainz.org/release-group/${A}`), { kind: 'wrong', why: 'kind' })
  assert.deepEqual(searchRef(`https://musicbrainz.org/release/${A}`).why, 'kind')
  // An artist address that holds no id is a wrong address too (Y5), not a name to search for.
  for (const bad of ['musicbrainz.org/artist/not-a-uuid', 'https://musicbrainz.org/artist/', `https://beta.musicbrainz.org/artist/${A.slice(0, 20)}`]) {
    assert.deepEqual(searchRef(bad), { kind: 'wrong', why: 'artist' }, bad)
  }
  // A name that only looks like an address is a name.
  assert.deepEqual(searchRef('musicbrainz.org'), { kind: 'name', q: 'musicbrainz.org' })
})

const cand = (mbid, over = {}) => ({ mbid, name: 'King Crimson', disambiguation: '', type: 'Group', country: 'GB', area: 'United Kingdom', score: 100, ...over })

test('a candidate: name, disambiguation and what else the route says, a link out, Use this artist', () => {
  const now = matchNow({ id: 'nd-1', overrides: overrides({ toCheck: [check('unresolved')] }) })
  assert.deepEqual(candidateView(cand(A), now), { key: A, title: 'King Crimson', sub: 'Group · United Kingdom', href: mbArtistUrl(A),
                                                   label: 'Use this artist', word: '' })
  assert.equal(candidateView(cand(A, { disambiguation: 'tribute band', type: '', area: '', country: 'US' }), now).sub, I('tribute band') + ' · US')
  assert.equal(candidateView(cand(A, { name: '', type: '', area: '', country: '' }), now).title, 'MusicBrainz artist')
  assert.equal(candidateView(cand(A, { name: '', type: '', area: '', country: '' }), now).sub, '')
})

test('the candidate that is the match in use: a guess can be kept, a tag or a choice is only marked', () => {
  const guess = matchNow({ id: 'nd-1', overrides: overrides({ toCheck: [check('unmatched')] }) })
  assert.deepEqual(pick(candidateView(cand(B), guess), 'label', 'word'), ['Keep this match', ''])
  assert.deepEqual(pick(candidateView(cand(A), guess), 'label', 'word'), ['Use this artist', ''])
  const tags = matchNow({ id: 'nd-1', artist: { id: 'nd-1', mbid: C } })
  assert.deepEqual(pick(candidateView(cand(C), tags), 'label', 'word'), ['', 'In use'])
  const chosen = matchNow({ id: 'nd-1', overrides: overrides({ artists: [yours()] }), indexMbid: null })
  assert.deepEqual(pick(candidateView(cand(A.toUpperCase()), chosen), 'label', 'word'), ['', 'In use'])
})

test('what the search shows: a skeleton the size of the answer, then the candidates', () => {
  const now = matchNow({ id: 'nd-1', overrides: overrides({ toCheck: [check('unresolved')] }) })
  assert.deepEqual(lookupView(null, now), { skeleton: 0, busy: false, rows: [], empty: '', error: null })
  // The route answers eight at the most, and for most names it answers eight.
  assert.deepEqual(pick(lookupView({ status: 'loading', kind: 'name', list: [] }, now), 'skeleton', 'busy', 'rows'), [8, true, []])
  // A pasted id is one artist.
  assert.equal(lookupView({ status: 'loading', kind: 'id', list: [] }, now).skeleton, 1)
  const ok = lookupView({ status: 'ok', kind: 'name', list: [cand(A), cand(B, { name: 'King Crimson Jr.' })] }, now)
  assert.deepEqual([ok.skeleton, ok.busy, ok.empty, ok.error, ok.rows.map(r => r.title)], [0, false, '', null, ['King Crimson', 'King Crimson Jr.']])
})

test('another search while candidates are on the page: they stay, dimmed, until the answer is in', () => {
  const now = matchNow({ id: 'nd-1', overrides: overrides() })
  const v = lookupView({ status: 'loading', kind: 'name', list: [cand(A)] }, now)
  assert.deepEqual([v.skeleton, v.busy, v.rows.length], [0, true, 1])
})

test('an empty answer is “none by that name”; a search that did not run never is', () => {
  const now = matchNow({ id: 'nd-1', overrides: overrides() })
  assert.equal(lookupView({ status: 'ok', kind: 'name', list: [] }, now).empty, 'No MusicBrainz artist by that name.')
  const down = lookupView({ status: 'error', kind: 'name', list: [], error: Object.assign(new Error('x'), { status: 502, payload: { error: 'MusicBrainz did not answer (HTTP 503)', retryable: true } }) }, now)
  assert.deepEqual([down.empty, down.rows, down.skeleton], ['', [], 0])
  assert.deepEqual(down.error, { title: 'Couldn’t search MusicBrainz', text: 'MusicBrainz did not answer (HTTP 503)' })
  // Whatever stopped it — a timeout, no connection, a 500 — it did not run.
  for (const error of [Object.assign(new Error('Timed out after 15s'), { timeout: true }), new Error('Failed to fetch'),
                       Object.assign(new Error('boom'), { status: 500, payload: { error: 'boom' } })]) {
    const v = lookupView({ status: 'error', kind: 'name', list: [cand(A)], error }, now)
    assert.equal(v.error.title, 'Couldn’t search MusicBrainz')
    assert.equal(v.empty, '')
    assert.deepEqual(v.rows, [], 'the last search’s candidates are not shown as this one’s')
  }
  assert.equal(lookupView({ status: 'error', kind: 'name', list: [], error: new Error('Failed to fetch') }, now).error.text, 'Failed to fetch')
  // A MusicBrainz address that is not an artist's is said, and nothing is asked.
  assert.equal(lookupView({ status: 'wrong', kind: 'wrong', list: [] }, now).empty, 'That is a MusicBrainz address, but not an artist’s.')
  assert.equal(lookupView({ status: 'wrong', kind: 'wrong', why: 'kind', list: [] }, now).empty, 'That is a MusicBrainz address, but not an artist’s.')
  assert.equal(lookupView({ status: 'wrong', kind: 'wrong', why: 'artist', list: [] }, now).empty, 'That address holds no MusicBrainz artist id.')
})

test('a pasted id offers that artist, named when MusicBrainz names it', () => {
  assert.deepEqual(pastedCandidate(A, { mbid: A, name: 'King Crimson', disambiguation: '', type: 'Group', country: 'GB', found: true, summary: 'long' }),
                   { mbid: A, name: 'King Crimson', disambiguation: '', type: 'Group', country: 'GB', area: '' })
  // Not named — MusicBrainz did not answer, or the read failed: still offered; setting it is what checks it.
  assert.deepEqual(pastedCandidate(A, null), { mbid: A, name: '', disambiguation: '', type: '', country: '', area: '' })
  assert.deepEqual(pastedCandidate(A, { found: false }), { mbid: A, name: '', disambiguation: '', type: '', country: '', area: '' })
})

test('what a set sends: the library artist’s own id and name, and the candidate’s id', () => {
  assert.deepEqual(setBody({ id: 'nd-1', name: 'The Band' }, cand(A)), { nd_id: 'nd-1', mbid: A, name: 'The Band' })
  assert.deepEqual(setBody({ id: 'nd-1', name: '' }, { mbid: A }), { nd_id: 'nd-1', mbid: A, name: '' })
})

// ── the job ──
test('a set or a remove starts a scan, or says why it did not', () => {
  assert.deepEqual(scanStart('set', { ok: true, mbid: A, name: 'The Band', task_id: 't1' }, 5000),
                   { op: 'set', phase: 'scanning', taskId: 't1', done: 0, total: 0, current: '', lined: null, error: '', heardAt: 5000 })
  assert.deepEqual(pick(scanStart('set', { ok: true, task_id: '', covered: true }), 'phase', 'taskId'), ['covered', ''])
  assert.deepEqual(pick(scanStart('remove', { ok: true, task_id: '' }), 'op', 'phase'), ['remove', 'unstarted'])
  // `covered` is believed only beside a blank task id.
  assert.equal(scanStart('set', { ok: true, task_id: 't1', covered: true }).phase, 'scanning')
})

const RESULT = {
  artist_mbid: A, artist_name: 'The Band',
  library: { album_ids: ['al-1', 'al-2', 'al-3', 'al-4', 'al-5'], artist_ids: ['nd-1'], artist_names: ['The Band'] },
  releases: [
    { rgid: 'rg-1', status: 'complete', navidrome_album_ids: ['al-1'] },
    { rgid: 'rg-2', status: 'incomplete', navidrome_album_ids: ['al-2', 'al-3'] },
    { rgid: 'rg-3', status: 'untagged', navidrome_album_ids: ['al-4'] },
    { rgid: 'rg-4', status: 'missing', navidrome_album_ids: [] },
    { rgid: 'rg-5', status: 'present' },
  ],
}

test('“N of M albums line up”: the library’s albums of this artist that a release of the discography holds', () => {
  assert.deepEqual(linedUp(RESULT), { held: 4, total: 5 })
  // An id a `missing` row names is not held; an id outside the artist's own albums is not counted.
  assert.deepEqual(linedUp({ library: { album_ids: ['al-1', 'al-2'] }, releases: [
    { status: 'missing', navidrome_album_ids: ['al-1'] }, { status: 'complete', navidrome_album_ids: ['al-9', 'al-2'] }] }), { held: 1, total: 2 })
  assert.deepEqual(linedUp({ library: { album_ids: [] }, releases: [] }), { held: 0, total: 0 })
  // A result that does not say what it was matched against says nothing.
  assert.equal(linedUp({ releases: RESULT.releases }), null)
  assert.equal(linedUp(null), null)
})

test('the scan, poll by poll: it moves, ends, fails, or is lost', () => {
  const job = scanStart('set', { ok: true, task_id: 't1' })
  const moving = scanStep(job, { ok: true, task: { status: 'running', done: 12, total: 87, current: 'Islands' } })
  assert.deepEqual(pick(moving, 'phase', 'done', 'total', 'current'), ['scanning', 12, 87, 'Islands'])
  const done = scanStep(moving, { ok: true, task: { status: 'complete', done: 87, total: 87, result: RESULT } })
  assert.deepEqual(pick(done, 'phase', 'lined'), ['done', { held: 4, total: 5 }])
  const failed = scanStep(moving, { ok: true, task: { status: 'error', error: 'MusicBrainz did not answer — the stored discography was kept; try again in a minute' } })
  assert.deepEqual(pick(failed, 'phase', 'error'), ['failed', 'MusicBrainz did not answer — the stored discography was kept; try again in a minute'])
  assert.deepEqual(pick(scanStep(moving, { ok: true, task: { status: 'cancelled' } }), 'phase', 'error'), ['failed', 'The scan was cancelled.'])
  // lb-bot forgot the task: it restarted.
  assert.equal(scanStep(moving, { ok: false, status: 404 }).phase, 'lost')
  // No answer is no answer: asked again, and what was known stays.
  assert.equal(scanStep(moving, { ok: false, status: 0 }), moving)
  assert.equal(scanStep(moving, { ok: false, status: 502 }), moving)
  // An ended job is not moved by a late answer.
  assert.equal(scanStep(done, { ok: true, task: { status: 'running', done: 1, total: 87 } }), done)
})

test('the job’s sentences — a state per row', () => {
  const v = (job, name = 'The Band') => { const x = scanView(job, { name }); return [x.tone, x.word, x.line, x.note, x.bar, x.ended] }
  assert.deepEqual(v({ op: 'set', phase: 'sending' }), ['run', 'Matching', 'Setting the match…', '', false, false])
  assert.deepEqual(v({ op: 'remove', phase: 'sending' }), ['run', 'Going back', 'Removing your choice…', '', false, false])
  const start = scanStart('set', { ok: true, task_id: 't1' })
  assert.deepEqual(v(start), ['run', 'Reading', 'Reading ' + I('The Band') + '’s discography again…', '', true, false])
  assert.deepEqual(v(start, ''), ['run', 'Reading', 'Reading the artist’s discography again…', '', true, false])
  const moving = scanStep(start, { ok: true, task: { status: 'running', done: 12, total: 87, current: 'Islands' } })
  assert.deepEqual(v(moving), ['run', 'Reading', 'Reading ' + I('The Band') + '’s discography again…', 'Islands', true, false])
  const done = scanStep(moving, { ok: true, task: { status: 'complete', result: RESULT } })
  assert.deepEqual(v(done), ['ok', 'Matched', '4 of 5 albums line up.', '', false, true])
  assert.deepEqual(v({ ...done, op: 'remove' }), ['ok', 'Removed', '4 of 5 albums line up.', '', false, true])
  assert.deepEqual(v(scanStart('set', { ok: true, task_id: '', covered: true })), ['ok', 'Matched', 'The match is set.', SHARED_NOTE, false, true])
  assert.deepEqual(v(scanStart('remove', { ok: true, task_id: '', covered: true })), ['ok', 'Removed', 'Your choice is removed.', SHARED_NOTE, false, true])
  assert.deepEqual(v(scanStart('set', { ok: true, task_id: '' })), ['ok', 'Matched', 'The match is set.', 'lb-bot started no scan for this artist.', false, true])
  assert.deepEqual(v(scanStart('remove', { ok: true, task_id: '' })), ['ok', 'Removed', 'Your choice is removed.', 'lb-bot started no scan for this artist.', false, true])
  const failed = scanStep(moving, { ok: true, task: { status: 'error', error: 'MusicBrainz did not answer' } })
  assert.deepEqual(v(failed), ['bad', 'Scan failed', 'MusicBrainz did not answer', 'The match is set; the scan can be run again from the artist page.', false, true])
  assert.deepEqual(v({ ...failed, op: 'remove', error: '' }),
                   ['bad', 'Scan failed', 'The discography scan failed.', 'Your choice is removed; the scan can be run again from the artist page.', false, true])
  assert.deepEqual(v(scanStep(moving, { ok: false, status: 404 })),
                   ['wait', 'Not finished', 'The scan did not finish: lb-bot restarted while it ran.', 'The match is set; the scan can be run again from the artist page.', false, true])
})

test('the meter is the whole scan’s: release groups checked of all there are, and unmeasured before the first poll', () => {
  const start = scanStart('set', { ok: true, task_id: 't1' })
  assert.deepEqual(pick(scanView(start, { name: 'X' }), 'percent', 'count'), [null, ''])
  const moving = scanStep(start, { ok: true, task: { status: 'running', done: 12, total: 48 } })
  assert.deepEqual(pick(scanView(moving, { name: 'X' }), 'percent', 'count'), [25, '12 of 48'])
  const over = scanStep(start, { ok: true, task: { status: 'running', done: 60, total: 48 } })
  assert.equal(scanView(over, { name: 'X' }).percent, 100)
})

test('how many line up, in words', () => {
  const line = lined => scanView({ op: 'set', phase: 'done', lined }, { name: 'X' }).line
  assert.equal(line({ held: 0, total: 5 }), '0 of 5 albums line up.')
  assert.equal(line({ held: 5, total: 5 }), '5 of 5 albums line up.')
  assert.equal(line({ held: 1, total: 1 }), '1 of 1 album lines up.')
  assert.equal(line({ held: 0, total: 1 }), '0 of 1 album lines up.')
  assert.equal(line({ held: 0, total: 0 }), 'lb-bot found no album of this artist’s in your library.')
  // A scan that did not say what it was matched against.
  assert.equal(line(null), 'lb-bot has read the discography again.')
})

test('lb-bot not answering: asked again for three minutes since it last answered, then said and stopped', () => {
  const T0 = 1_000_000
  const job = scanStart('set', { ok: true, task_id: 't1' }, T0)
  const none = { ok: false, status: 0 }
  assert.equal(scanStep(job, none, T0 + SILENT_MS - 1), job, 'not yet')
  assert.equal(scanStep(job, none, T0 + SILENT_MS).phase, 'silent')
  assert.equal(scanStep(job, { ok: false, status: 502 }, T0 + SILENT_MS).phase, 'silent')
  // An answer — any, moving or not — is lb-bot answering: the clock starts again.
  const heard = scanStep(job, { ok: true, task: { status: 'running', done: 0, total: 0 } }, T0 + 170_000)
  assert.equal(heard.heardAt, T0 + 170_000)
  assert.equal(scanStep(heard, none, T0 + SILENT_MS + 1000), heard)
  assert.equal(scanStep(heard, none, T0 + 170_000 + SILENT_MS).phase, 'silent')
  // A scan that is answered about but does not move is not silent: MusicBrainz allows one request a second.
  let slow = job
  for (let t = 2000; t <= SILENT_MS * 2; t += 2000) slow = scanStep(slow, { ok: true, task: { status: 'running', done: 3, total: 87 } }, T0 + t)
  assert.equal(slow.phase, 'scanning')
  // A forgotten task is still "lost", whenever it is.
  assert.equal(scanStep(job, { ok: false, status: 404 }, T0 + SILENT_MS * 2).phase, 'lost')
  const v = scanView(scanStep(job, none, T0 + SILENT_MS), { name: 'The Band' })
  assert.deepEqual([v.tone, v.word, v.line, v.note, v.bar, v.ended],
                   ['wait', 'No answer', 'lb-bot has not answered for three minutes.', 'The match is set; the scan may still be running.', false, true])
  assert.equal(scanView({ ...scanStep(job, none, T0 + SILENT_MS), op: 'remove' }, {}).note, 'Your choice is removed; the scan may still be running.')
  // Coming back to a job kept from before: the clock is the return's.
  assert.equal(scanResume(job, T0 + SILENT_MS * 5).heardAt, T0 + SILENT_MS * 5)
  assert.equal(scanResume(null, T0), null)
})

test('a scan that ends unwatched: one toast, the artist and what the card would say; nothing while it runs', () => {
  const start = scanStart('set', { ok: true, task_id: 't1' })
  assert.equal(scanToast(start, 'The Band'), null)
  assert.equal(scanToast({ op: 'set', phase: 'sending' }, 'The Band'), null)
  assert.equal(scanToast(null, 'The Band'), null)
  const done = scanStep(start, { ok: true, task: { status: 'complete', result: RESULT } })
  assert.deepEqual(scanToast(done, 'The Band'), { text: I('The Band') + ': 4 of 5 albums line up.', level: 'info' })
  assert.deepEqual(scanToast(done, ''), { text: 'The artist: 4 of 5 albums line up.', level: 'info' })
  const failed = scanStep(start, { ok: true, task: { status: 'error', error: 'MusicBrainz did not answer' } })
  assert.deepEqual(scanToast(failed, 'The Band'), { text: I('The Band') + ': MusicBrainz did not answer', level: 'error' })
  assert.equal(scanToast(scanStep(start, { ok: false, status: 404 }), 'The Band').level, 'error')
  // The watch's machine for it: running only while it scans; its state is the job itself.
  assert.equal(scanMachine.running(start), true)
  assert.equal(scanMachine.running(done), false)
  assert.equal(scanMachine.start('set', 7000, start).heardAt, 7000)
  assert.equal(scanMachine.opOf({ state: 'applying' }), '')
})

// ── the list under Needs you ──
test('the To fix row: why it is listed, and “Matched” once it is', () => {
  assert.deepEqual(fixRowView(check('unmatched'), false), { sub: 'Match holds none of its albums, 5 albums', tone: 'wait', word: 'To match' })
  assert.deepEqual(fixRowView(check('unresolved', { albums: 1 }), false), { sub: 'No MusicBrainz match, 1 album', tone: 'wait', word: 'To match' })
  assert.deepEqual(fixRowView(check('unmatched'), true), { sub: 'Matched, 5 albums', tone: 'ok', word: 'Matched' })
})
