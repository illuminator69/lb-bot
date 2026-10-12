// What the web UI may say about an artist name merge, and about the list of
// everything lb-bot changed (I-029, U3): the merge sets, the preview, the
// job, and System › Overrides' rows. `node --test web/src/lib/merges.test.mjs`
// — no dependency. Names here are invented; the shapes are the wire's.
import test from 'node:test'
import assert from 'node:assert/strict'
// A name in a sentence is isolated (lib/text.js): what an expectation writes around one.
const I = s => '\u2068' + s + '\u2069'
import {
  FIXED_SENTENCE, afterMerge, heldRows, holdRow, keepSets, keptDefault, listRowView, mergeBody, mergeFrom, mergeMachine,
  mergeLabel, mergePreviewView, mergeQuiet, mergeRest, mergeToast, mergeView, nb, overridesGroups, rowKey, rowOpens, rowsShown, setOf,
  mergeRowsNow, setRowView, splitStillWorking, toFixNote, toFixWaiting, undoEnded,
  artistsTouched, listedNoMore,
} from './merges.js'
import { setBody } from './artistMatch.js'
import { jobStart, jobStep, mergeOpOfState, opOfState, SILENT_MS } from './overrides.js'

const e = (id, name, albums) => ({ ndArtistId: id, name, albums, collab: false })
const SETS = [
  { mbid: 'm1', artists: [e('a1', 'Finch & Sparrow', 9), e('a2', 'Finch and Sparrow', 2)] },
  { mbid: 'm2', artists: [e('b1', 'Harbour Trio', 4), e('b2', '1986 Harbour Trio', 2), e('b3', 'ハーバートリオ', 1)] },
]
const mrow = (over = {}) => ({ id: 'mg1', state: 'merging', into: { ndArtistId: 'a1', name: 'Finch & Sparrow' },
  from: { ndArtistId: 'a2', name: 'Finch and Sparrow' }, mbid: 'm1', done: 0, total: 9, failures: [], setAt: 1000, ...over })

// ── the sets ──
test('the set an artist is in: found by the artist, never by the MusicBrainz id; none when lb-bot gave no list', () => {
  assert.equal(setOf({ siblings: SETS }, 'a2').mbid, 'm1')
  assert.equal(setOf({ siblings: SETS }, 'b3').artists.length, 3)
  assert.equal(setOf({ siblings: SETS }, 'zz'), null)
  assert.equal(setOf({ siblings: [] }, 'a1'), null)
  // `siblings` absent: lb-bot could not work the sets out. No block — not "no other spelling".
  assert.equal(setOf({ artists: [] }, 'a1'), null)
  assert.equal(setOf(null, 'a1'), null)
  // A set of one is not a set; an entry marked as a collaboration credit is not offered.
  assert.equal(setOf({ siblings: [{ mbid: 'm', artists: [e('x', 'X', 1)] }] }, 'x'), null)
  const credit = { siblings: [{ mbid: 'm', artists: [e('x', 'X', 3), { ...e('y', 'X & Y', 1), collab: true }] }] }
  assert.equal(setOf(credit, 'x'), null, 'nobody is left to merge with')
  assert.equal(setOf(credit, 'y'), null)
})

test('which name is kept: the one with the most albums first; which is merged into it', () => {
  const pair = setOf({ siblings: SETS }, 'a2')
  assert.equal(keptDefault(pair), 'a1')
  assert.deepEqual(mergeFrom(pair, 'a1').map(x => x.ndArtistId), ['a2'])
  assert.deepEqual(mergeFrom(pair, 'a2').map(x => x.ndArtistId), ['a1'], 'the other way round is the user’s to choose')
  const three = setOf({ siblings: SETS }, 'b2')
  assert.equal(keptDefault(three), 'b1')
  assert.deepEqual(mergeFrom(three, 'b1').map(x => x.ndArtistId), ['b2', 'b3'], 'one pair at a time: each of the others can be merged in')
  // Equal counts: the order lb-bot lists them in.
  assert.equal(keptDefault({ artists: [e('p', 'P', 2), e('q', 'Q', 2)] }), 'p')
  assert.deepEqual(mergeBody('a1', 'a2'), { nd_id: 'a1', from_nd_id: 'a2' })
})

// ── the preview ──
test('the preview: “N files in M albums will say <name>”, the sample, how many more, the fixed sentence', () => {
  const v = mergePreviewView({ into: { ndArtistId: 'a1', name: 'Finch & Sparrow' }, from: { ndArtistId: 'a2', name: 'Finch and Sparrow' },
    mbid: 'm1', albums: 12, files: 143, sample: Array.from({ length: 10 }, (_, i) => ({ album: `Album ${i + 1}`, tracks: 10 + i })) })
  assert.equal(v.line, '143 files in 12 albums will say ' + I('Finch & Sparrow'))
  assert.equal(v.sample.length, 10)
  assert.deepEqual(v.sample[0], { key: '0', title: 'Album 1', sub: '10 tracks' })
  assert.equal(v.more, '…and 2 more albums')
  assert.equal(v.fixed, 'lb-bot rewrites the artist tags of 143 files and keeps the previous ones, so this can be undone. Folders are not renamed.')
  assert.equal(FIXED_SENTENCE(1), 'lb-bot rewrites the artist tags of 1 file and keeps the previous ones, so this can be undone. Folders are not renamed.')
  assert.deepEqual([v.can, v.button], [true, 'Merge'])
  const one = mergePreviewView({ into: { name: 'A' }, from: { name: 'B' }, albums: 1, files: 1, sample: [{ album: 'Only', tracks: 1 }] })
  assert.deepEqual([one.line, one.more, one.sample[0].sub], ['1 file in 1 album will say ' + I('A'), '', '1 track'])
})

test('a preview with nothing to rewrite offers no merge, and says what lb-bot counted', () => {
  const v = mergePreviewView({ into: { name: 'A' }, from: { name: 'B' }, albums: 0, files: 0, sample: [] })
  assert.deepEqual([v.can, v.line, v.fixed], [false, 'No file says ' + I('B') + ': there is nothing to merge into ' + I('A') + '.', ''])
})

// ── the job ──
test('a merge row’s states are jobs of the one machine: merging and undoing run, merged and failed rest', () => {
  assert.deepEqual(['merging', 'undoing', 'merged', 'failed', 'applying'].map(mergeOpOfState), ['merge', 'unmerge', '', '', ''])
  // An album override's row is never taken for a merge's, nor the other way round.
  assert.deepEqual(['merging', 'undoing'].map(opOfState), ['', ''])
  // X1: the POST's own state seeds it — a merge that failed before the first poll is seen.
  let job = jobStart('merge', 0, 'merging')
  job = jobStep(job, { ok: true, row: mrow({ state: 'failed', failures: [{ album: 'A', title: 'T', reason: 'read-only' }] }) }, 2000)
  assert.equal(job.phase, 'failed')
  // merged settles it.
  assert.equal(jobStep(jobStart('merge', 0, 'merging'), { ok: true, row: mrow({ state: 'merged', done: 9 }) }, 2000).phase, 'settled')
  // An undo read before lb-bot flipped the row still says `merged`: not its end.
  let undo = jobStart('unmerge', 0, 'undoing')
  undo = jobStep(undo, { ok: true, row: mrow({ state: 'undoing', done: 4 }) }, 2000)
  assert.equal(undo.phase, 'polling')
  // The coded 404 ends a successful undo; any other 404 does not.
  assert.equal(jobStep(undo, { ok: false, status: 404, code: 'no_merge' }, 4000).phase, 'settled')
  assert.equal(jobStep(undo, { ok: false, status: 404, code: '' }, 4000).phase, 'polling')
  assert.equal(jobStep(jobStart('merge', 0, 'merging'), { ok: false, status: 404, code: 'no_merge' }, 2000).phase, 'gone')
  // X1, the other way round: followed without the POST's own state (a row found by a read), a first poll that still
  // says `merged` — lb-bot has not flipped the row yet — is not the undo's end.
  assert.equal(jobStep(jobStart('unmerge', 0, ''), { ok: true, row: mrow({ state: 'merged' }) }, 2000).phase, 'polling')
  // An undo that ends back on `merged` (it put nothing back), or on `failed`, failed.
  assert.equal(jobStep(undo, { ok: true, row: mrow({ state: 'merged' }) }, 4000).phase, 'failed')
  assert.equal(jobStep(undo, { ok: true, row: mrow({ state: 'failed' }) }, 4000).phase, 'failed')
  // X2: three minutes with no change of state or count.
  let quiet = jobStep(jobStart('merge', 0, 'merging'), { ok: true, row: mrow({ done: 3 }) }, 2000)
  quiet = jobStep(quiet, { ok: true, row: mrow({ done: 3 }) }, 2000 + SILENT_MS)
  assert.equal(quiet.phase, 'timeout')
})

test('the job’s sentences — a state per row', () => {
  const v = (op, phase, row) => { const x = mergeView(op, phase, row); return [x.tone, x.word, x.line, x.bar, x.ended, x.canUndo] }
  assert.deepEqual(v('merge', 'polling', null), ['run', 'Merging', 'Rewriting artist tags…', false, false, false])
  assert.deepEqual(v('merge', 'polling', mrow({ done: 3 })), ['run', 'Merging', 'Rewriting artist tags… 3 of 9', true, false, false])
  assert.equal(mergeView('merge', 'polling', mrow({ done: 3 })).percent, 33)
  // X4: the writes are over — successes and failures reach the total — and lb-bot waits for Navidrome.
  assert.deepEqual(v('merge', 'polling', mrow({ done: 8, failures: [{ album: 'A', title: 'T', reason: 'r' }] })),
                   ['run', 'Waiting for Navidrome', 'Waiting for Navidrome to read the albums again…', false, false, false])
  assert.deepEqual(v('merge', 'settled', mrow({ state: 'merged', done: 9 })), ['ok', 'Merged', 'lb-bot rewrote the artist tags of 9 files.', false, true, true])
  assert.deepEqual(v('merge', 'settled', mrow({ state: 'merged', done: 1, total: 1 })), ['ok', 'Merged', 'lb-bot rewrote the artist tags of 1 file.', false, true, true])
  assert.deepEqual(v('merge', 'settled', mrow({ state: 'merged', done: 0, total: 0 })), ['ok', 'Merged', 'Nothing was rewritten.', false, true, true])
  // Merged with problems: the old spelling still has those files.
  const partial = mergeView('merge', 'settled', mrow({ state: 'merged', done: 8, failures: [{ album: 'Debut', title: 'Opening Song', reason: 'lb-bot cannot write the tags of a .m4a file' }] }))
  assert.deepEqual([partial.tone, partial.problemsHeading, partial.problems], ['wait', '1 problem',
    [{ title: I('Debut') + ' · ' + I('Opening Song'), reason: 'lb-bot cannot write the tags of a .m4a file' }]])
  assert.deepEqual(v('merge', 'failed', mrow({ state: 'failed', done: 2 })), ['bad', 'Did not finish', 'lb-bot did not finish the merge (2 of 9 files rewritten).', false, true, true])
  assert.deepEqual(v('merge', 'failed', mrow({ state: 'failed', done: 0, total: 0 })), ['bad', 'Did not finish', 'lb-bot did not merge these names.', false, true, true])
  assert.deepEqual(v('unmerge', 'polling', mrow({ state: 'undoing', done: 2 })), ['run', 'Restoring', 'Putting the previous tags back… 2 of 9', true, false, false])
  assert.deepEqual(v('unmerge', 'settled', null), ['ok', 'Restored', 'Restored: the two names are apart again.', false, true, false])
  const undone = mergeView('unmerge', 'failed', mrow({ state: 'failed', failures: [{ album: 'A', title: 'T', reason: 'the file was not found; press Undo again to give up on it' }] }))
  assert.deepEqual([undone.tone, undone.word, undone.line, undone.canUndo, undone.undoLabel], ['bad', 'Not restored', 'lb-bot could not put everything back.', true, 'Undo again'])
  assert.deepEqual(v('merge', 'gone', null), ['', 'No longer there', 'lb-bot no longer has this merge.', false, true, false])
  const still = mergeView('merge', 'timeout', mrow({ done: 2 }))
  assert.deepEqual([still.tone, still.word, still.line, still.ended, still.seeOverrides], ['wait', 'Still working', 'Still working — see System › Overrides', true, true])
})

test('“Still working — see System › Overrides”: the words stay, the tab’s name is the way there', () => {
  assert.deepEqual(splitStillWorking('Still working — see System › Overrides'), { before: 'Still working — see ', link: 'System › Overrides', after: '' })
  assert.equal(splitStillWorking('lb-bot wrote the tags of 6 files.'), null)
  assert.equal(splitStillWorking(''), null)
})

test('a merge row at rest: merged says nothing more; one left running, failed or with problems says so', () => {
  assert.equal(mergeRest(mrow({ state: 'merged', done: 9 })), null)
  assert.equal(mergeRest(null), null)
  assert.deepEqual([mergeRest(mrow({ state: 'undoing', done: 1 })).word, mergeRest(mrow({ state: 'merging' })).ended], ['Restoring', false])
  const failed = mergeRest(mrow({ state: 'failed', done: 2 }))
  assert.deepEqual([failed.tone, failed.line, failed.canUndo], ['bad', 'A merge of these names did not finish.', true])
  assert.equal(mergeRest(mrow({ state: 'merged', failures: [{ album: 'A', title: 'T', reason: 'r' }] })).problemsHeading, '1 problem')
  assert.equal(mergeRest(mrow({ state: 'paused' })), null, 'a state this page does not know says nothing')
  assert.equal(mergeRest(mrow({ state: 'paused', failures: [{ album: 'A', title: 'T', reason: 'r' }] })), null, '…whatever else its row carries')
})

test('the merge as a job of the watch: asked about while it is polling and no longer; adopted by its own states; its end remembered', () => {
  for (const phase of ['settled', 'failed', 'gone', 'timeout']) assert.equal(mergeMachine.running({ op: 'merge', phase }), false, phase)
  assert.equal(mergeMachine.running({ op: 'merge', phase: 'polling' }), true)
  assert.deepEqual(['merging', 'undoing', 'merged', 'applying', 'removing'].map(state => mergeMachine.opOf({ state })), ['merge', 'unmerge', '', '', ''])
  // A poll that brings another count is something a surface shows.
  const sig = row => JSON.stringify(mergeMachine.sig({ op: 'merge', phase: 'polling', row }))
  assert.notEqual(sig(mrow({ done: 3 })), sig(mrow({ done: 4 })))
  // At rest it is remembered by the row it was for; a silent one is not (it may still end).
  assert.equal(mergeMachine.restKey({ op: 'merge', phase: 'settled' }, mrow({ setAt: 7 })), '7@merge')
  assert.equal(mergeMachine.restKey({ op: 'merge', phase: 'timeout' }, mrow({ setAt: 7 })), '')
  assert.equal(mergeMachine.toast({ op: 'merge', phase: 'polling', row: mrow() }, mrow()), null)
  assert.match(mergeMachine.toast({ op: 'unmerge', phase: 'settled', row: null }, mrow()).text, /restored/)
})

test('the one toast for a merge that ended with nobody showing it', () => {
  const name = I('Finch and Sparrow') + ' → ' + I('Finch & Sparrow')
  assert.equal(mergeToast('merge', 'polling', mrow()), null)
  assert.deepEqual(mergeToast('merge', 'settled', mrow({ state: 'merged', done: 9 })), { text: `${name}: lb-bot rewrote the artist tags of 9 files.`, level: 'info' })
  assert.deepEqual(mergeToast('unmerge', 'settled', mrow({ state: 'merged' })), { text: `${name}: restored — the two names are apart again.`, level: 'info' })
  assert.equal(mergeToast('merge', 'failed', mrow({ state: 'failed', done: 2 })).level, 'error')
  assert.deepEqual(mergeToast('merge', 'timeout', mrow()), { text: `${name}: still working — see System › Overrides`, level: 'info' })
  assert.deepEqual(mergeToast('merge', 'settled', mrow({ state: 'merged', done: 8, failures: [{ album: 'A', title: 'T', reason: 'r' }] })),
                   { text: `${name}: lb-bot rewrote the artist tags of 8 files. 1 problem — see System › Overrides.`, level: 'error' })
  assert.equal(mergeToast('merge', 'gone', null).text, 'The merge: lb-bot no longer has this merge.')
  // The watch's machine for it.
  assert.equal(mergeMachine.opOf(mrow({ state: 'undoing' })), 'unmerge')
  assert.equal(mergeMachine.running(jobStart('merge', 0, 'merging')), true)
  assert.equal(mergeMachine.toast({ op: 'merge', phase: 'settled', row: mrow({ state: 'merged', done: 9 }) }, null).level, 'info')
})

test('after a merge: the page of the spelling that is gone moves to the kept artist’s; any other stays', () => {
  const row = mrow({ state: 'merged' })
  assert.deepEqual(afterMerge('a2', { op: 'merge', phase: 'settled', row }), { go: 'a1', toast: I('Finch and Sparrow') + ' is now ' + I('Finch & Sparrow') + '.' })
  assert.deepEqual(afterMerge('a1', { op: 'merge', phase: 'settled', row }), { go: '', toast: '' })
  assert.deepEqual(afterMerge('zz', { op: 'merge', phase: 'settled', row }), { go: '', toast: '' })
  // Not for a merge that did not finish, is still running, or left files behind under the old name; not for an undo.
  assert.equal(afterMerge('a2', { op: 'merge', phase: 'failed', row: mrow({ state: 'failed' }) }).go, '')
  assert.equal(afterMerge('a2', { op: 'merge', phase: 'polling', row: mrow() }).go, '')
  assert.equal(afterMerge('a2', { op: 'merge', phase: 'settled', row: mrow({ state: 'merged', failures: [{ album: 'A', title: 'T', reason: 'r' }] }) }).go, '')
  assert.equal(afterMerge('a2', { op: 'unmerge', phase: 'settled', row: null }).go, '')
  assert.equal(afterMerge('a2', { op: 'unmerge', phase: 'settled', row: mrow({ state: 'undoing' }) }).go, '', 'an undo that is through: the spelling is back')
  assert.equal(afterMerge('a2', null).go, '')
})

// ── Needs you › To fix: "One artist, two names" ──
test('the sets listed on a visit stay, “Merged” once lb-bot no longer lists them — and only by a list it gave', () => {
  const first = keepSets([], { siblings: SETS })
  assert.deepEqual(first.map(i => [i.key, i.kind, i.moved, i.set.artists.length]), [['two:a1', 'two', false, 2], ['two:b1', 'two', false, 3]])
  // a2 is merged into a1: the pair is gone from lb-bot's list and stays in the page's.
  const merged = keepSets(first, { siblings: [SETS[1]] })
  assert.deepEqual(merged.map(i => [i.key, i.moved]), [['two:a1', true], ['two:b1', false]])
  // One of three merged: the set is still a set, with what is left.
  const two = keepSets(first, { siblings: [SETS[0], { mbid: 'm2', artists: [e('b1', 'Harbour Trio', 6), e('b3', 'ハーバートリオ', 1)] }] })
  assert.deepEqual(two.map(i => [i.key, i.moved, i.set.artists.length]), [['two:a1', false, 2], ['two:b1', false, 2]])
  // `siblings` absent: no list. Nothing is called merged by that.
  assert.deepEqual(keepSets(first, { artists: [] }).map(i => i.moved), [false, false])
  assert.equal(keepSets(first, null), first)
  // The undo: listed again as before.
  assert.deepEqual(keepSets(merged, { siblings: SETS }).map(i => [i.key, i.moved]), [['two:a1', false], ['two:b1', false]])
})

test('a set’s row in the list', () => {
  assert.deepEqual(setRowView(SETS[0], false), { title: 'Finch & Sparrow', sub: 'also ' + I('Finch and Sparrow') + ' · 11 albums', tone: 'wait', word: 'Two names' })
  assert.deepEqual(setRowView(SETS[1], false), { title: 'Harbour Trio', sub: 'also ' + I('1986 Harbour Trio') + ', ' + I('ハーバートリオ') + ' · 7 albums', tone: 'wait', word: 'Three names' })
  assert.deepEqual(setRowView(SETS[0], true), { title: 'Finch & Sparrow', sub: 'Merged', tone: 'ok', word: 'Merged' })
})

// ── System › Overrides ──
const arow = (over = {}) => ({ id: 'ov1', state: 'applied', releaseMbid: 'r-new', prevReleaseMbid: 'r-old', rgid: 'rg-1', artist: 'The Lanterns',
  album: 'Currents', albumId: 'al-2', coverUrl: '', setAt: 1000, done: 6, total: 6, failures: [], ...over })

test('four groups, each from its own list — one lb-bot left out could not be read, an empty one has nothing', () => {
  const g = overridesGroups({
    artists: [{ ndArtistId: 'n1', mbid: 'mb-1', name: 'The Quartet', tagMbid: '', setAt: 500 }],
    albums: [arow(), arow({ id: 'ov2', prevReleaseMbid: 'r-new', coverUrl: 'https://coverartarchive.org/release/r-new/1.jpg' }), arow({ id: 'ov3', state: 'failed' })],
    merges: [mrow({ state: 'merged', done: 9 })],
  })
  assert.deepEqual(g.map(x => [x.key, x.title, x.rows.length, x.missing]),
                   [['editions', 'Album editions', 2, false], ['covers', 'Covers', 1, false], ['artists', 'Artist matches', 1, false], ['merges', 'Name merges', 1, false]])
  // A list lb-bot left out: "could not be read" — never "none".
  const partial = overridesGroups({ artists: [] })
  assert.deepEqual(partial.map(x => [x.key, x.rows.length, x.missing]), [['editions', 0, true], ['covers', 0, true], ['artists', 0, false], ['merges', 0, true]])
  assert.equal(overridesGroups({ artists: [], albums: [], merges: [] }).every(x => !x.missing && !x.rows.length), true)
  assert.deepEqual(overridesGroups(null), [])
})

test('a row opens its page only by an id that is live now', () => {
  const live = { artists: new Set(['n1', 'a1']), albums: { ov1: { albumId: 'al-2', rgid: 'rg-1' }, ov3: null } }
  // An album: by the album's own answer (its release group as it is now) — not by the row carrying an id.
  assert.deepEqual(rowOpens('album', arow(), live), { tab: 'album', params: ['-', 'rg-1'] })
  assert.equal(rowOpens('album', arow({ id: 'ov3' }), live), null, 'Navidrome no longer lists that id')
  assert.equal(rowOpens('album', arow({ id: 'ov9' }), live), null, 'not checked yet: a plain row')
  assert.equal(rowOpens('album', arow({ albumId: '' }), { ...live, albums: {} }), null)
  // An artist match and a merge: by the artist list.
  assert.deepEqual(rowOpens('artist', { ndArtistId: 'n1' }, live), { tab: 'artist', params: ['n1'] })
  assert.equal(rowOpens('artist', { ndArtistId: 'gone' }, live), null)
  assert.deepEqual(rowOpens('merge', mrow({ state: 'merged' }), live), { tab: 'artist', params: ['a1'] })
  assert.equal(rowOpens('merge', mrow({ into: { ndArtistId: 'dead', name: 'X' } }), live), null)
  // The artist list not read: nothing is live.
  assert.equal(rowOpens('artist', { ndArtistId: 'n1' }, { artists: null, albums: {} }), null)
})

// ── System › Overrides: a row in words ──
const lrow = (over = {}) => ({ id: 'ov1', state: 'applied', album: 'Tide', artist: 'The Lanterns', albumId: 'al1', rgid: 'rg1',
  releaseMbid: 'rel-2', prevReleaseMbid: 'rel-1', coverUrl: '', done: 8, total: 8, failures: [], setAt: 1000, ...over })
const NOW = 1000 + 2 * 86400

test('an album row at rest: “Album · Artist”, when, the release on MusicBrainz, the state word, Undo', () => {
  const v = listRowView('album', lrow(), null, NOW)
  assert.equal(v.title, I('Tide') + ' · ' + I('The Lanterns'))
  assert.equal(v.sub, '2 d ago')
  assert.deepEqual(v.link, { href: 'https://musicbrainz.org/release/rel-2', text: 'Release on MusicBrainz' })
  assert.equal(v.v.word, 'Applied')
  assert.equal(v.v.tone, 'ok')
  assert.equal(v.v.line, '', 'the group says what it is: no sentence beside the word')
  assert.equal(v.v.bar, false)
  assert.equal(v.undo.label, 'Undo')
  // Half a name is the half there is; a row with no release names none.
  assert.equal(listRowView('album', lrow({ artist: '' }), null, NOW).title, I('Tide'))
  assert.equal(listRowView('album', lrow({ album: '', artist: '' }), null, NOW).title, 'An album')
  assert.equal(listRowView('album', lrow({ releaseMbid: '' }), null, NOW).link, null)
})

test('an album row at rest with lb-bot’s lines: applied with problems waits for a look; failed says so — the reasons fold open', () => {
  const partial = listRowView('album', lrow({ failures: [{ title: 'First Song', reason: 'read-only file' }] }), null, NOW)
  assert.equal(partial.v.word, 'Applied')
  assert.equal(partial.v.tone, 'wait')
  assert.equal(partial.v.problemsHeading, '1 problem')
  assert.deepEqual(partial.v.problems, [{ title: I('First Song'), reason: 'read-only file' }])
  assert.equal(partial.undo.label, 'Undo')
  const failed = listRowView('album', lrow({ state: 'failed', done: 0, failures: [{ title: '', reason: 'lb-bot was restarted' }] }), null, NOW)
  assert.equal(failed.v.word, 'Did not finish')
  assert.equal(failed.v.tone, 'bad')
  assert.equal(failed.v.line, '')
  assert.equal(failed.v.problems.length, 1)
  assert.equal(failed.undo.label, 'Undo')
})

test('an album row found running, before the watch has it: the job’s own words, the whole job’s bar, no Undo', () => {
  const applying = listRowView('album', lrow({ state: 'applying', done: 2 }), null, NOW)
  assert.equal(applying.v.word, 'Retagging')
  assert.equal(applying.v.bar, true)
  assert.equal(applying.v.percent, 25)
  assert.equal(applying.undo, null)
  const removing = listRowView('album', lrow({ state: 'removing', done: 8 }), null, NOW)
  assert.equal(removing.v.word, 'Waiting for Navidrome')
  assert.equal(removing.undo, null)
})

test('a state this page does not know says nothing and offers nothing', () => {
  const v = listRowView('album', lrow({ state: 'reticulating' }), null, NOW)
  assert.equal(v.v, null)
  assert.equal(v.undo, null)
  assert.equal(v.title, I('Tide') + ' · ' + I('The Lanterns'))
  const m = listRowView('merge', mrow({ state: 'reticulating' }), null, NOW)
  assert.equal(m.v, null)
  assert.equal(m.undo, null)
})

test('an undo in the row: restoring with its bar, waiting for Navidrome, restored; a failed one stays with “Undo again”', () => {
  const job = (phase, row) => ({ op: 'remove', phase, row })
  const run = listRowView('album', lrow(), job('polling', lrow({ state: 'removing', done: 3 })), NOW)
  assert.equal(run.v.word, 'Restoring')
  assert.equal(run.v.line, 'Putting the previous tags back… 3 of 8')
  assert.equal(run.v.percent, 38)
  assert.equal(run.undo, null)
  assert.equal(listRowView('album', lrow(), job('polling', lrow({ state: 'removing', done: 8 })), NOW).v.word, 'Waiting for Navidrome')
  const done = listRowView('album', lrow(), job('settled', null), NOW)
  assert.equal(done.v.word, 'Restored')
  assert.equal(done.undo, null)
  const failed = listRowView('album', lrow(), job('failed', lrow({ state: 'failed', failures: [{ title: 'Last Song', reason: 'not found' }] })), NOW)
  assert.equal(failed.v.word, 'Not restored')
  assert.equal(failed.v.problems.length, 1)
  assert.equal(failed.undo.label, 'Undo again')
})

test('a job that went silent, on the very tab its sentence names: the word, and no way “there”', () => {
  const v = listRowView('album', lrow(), { op: 'remove', phase: 'timeout', row: lrow({ state: 'removing' }) }, NOW)
  assert.equal(v.v.word, 'Still working')
  assert.equal(v.v.line, '')
  assert.equal(v.undo, null)
  const m = listRowView('merge', mrow({ state: 'merged' }), { op: 'unmerge', phase: 'timeout', row: mrow({ state: 'undoing' }) }, NOW)
  assert.equal(m.v.word, 'Still working')
  assert.equal(m.v.line, '')
})

test('a merge row: “From → Into”, files, when, the state, Undo', () => {
  const v = listRowView('merge', mrow({ state: 'merged', done: 9 }), null, NOW)
  assert.equal(v.title, I('Finch and Sparrow') + ' → ' + I('Finch & Sparrow'))
  assert.equal(v.sub, '9 files · 2 d ago')
  assert.equal(v.link, null)
  assert.equal(v.v.word, 'Merged')
  assert.equal(v.v.tone, 'ok')
  assert.equal(v.undo.label, 'Undo')
  assert.equal(listRowView('merge', mrow({ state: 'merged', done: 1 }), null, NOW).sub, '1 file · 2 d ago')
  const failed = listRowView('merge', mrow({ state: 'failed', failures: [{ album: 'Debut', title: 'Opening Song', reason: 'read-only' }] }), null, NOW)
  assert.equal(failed.v.word, 'Did not finish')
  assert.deepEqual(failed.v.problems, [{ title: I('Debut') + ' · ' + I('Opening Song'), reason: 'read-only' }])
  const undoing = listRowView('merge', mrow({ state: 'merged' }), { op: 'unmerge', phase: 'polling', row: mrow({ state: 'undoing', done: 4 }) }, NOW)
  assert.equal(undoing.v.word, 'Restoring')
  assert.equal(undoing.v.bar, true)
  assert.equal(undoing.undo, null)
})

test('an artist match: the library’s name → what MusicBrainz calls the artist, when, Remove', () => {
  const row = { ndArtistId: 'a9', mbid: 'mb-9', name: 'Tide', tagMbid: '', setAt: 1000 }
  const named = listRowView('artist', row, null, NOW, { name: 'The Tide' })
  assert.equal(named.title, I('Tide'))
  assert.deepEqual(named.to, { text: I('The Tide'), naming: false, href: 'https://musicbrainz.org/artist/mb-9' })
  assert.equal(named.sub, '2 d ago')
  assert.equal(named.v, null, 'a match in force has no state to speak of')
  assert.equal(named.undo.label, 'Remove')
  // Still being asked: a skeleton stands for the name. Could not be had: said as what it is, and still a way out.
  assert.equal(listRowView('artist', row, null, NOW, null).to.naming, true)
  assert.equal(listRowView('artist', row, null, NOW, { name: '' }).to.text, 'its MusicBrainz artist')
  assert.equal(listRowView('artist', row, null, NOW, undefined).to.text, 'its MusicBrainz artist')
  assert.equal(listRowView('artist', row, null, NOW, undefined).to.naming, false)
})

test('an artist match being removed: the scan that follows is the row’s job, and nothing more is offered', () => {
  const row = { ndArtistId: 'a9', mbid: 'mb-9', name: 'Tide', setAt: 1000 }
  const scan = { state: { op: 'remove', phase: 'scanning', done: 3, total: 12, current: 'Second Wave' } }
  const v = listRowView('artist', row, scan, NOW, { name: 'The Tide' })
  assert.equal(v.v.word, 'Reading')
  assert.equal(v.v.line, 'Reading ' + I('Tide') + '’s discography again…')
  assert.equal(v.v.bar, true)
  assert.equal(v.v.percent, 25)
  assert.equal(v.undo, null)
  // A scan with no total yet has a bar that says "some".
  const early = listRowView('artist', row, { state: { op: 'remove', phase: 'scanning', done: 0, total: 0 } }, NOW)
  assert.equal(early.v.bar, true)
  assert.equal(early.v.percent, 4)
})

// ── System › Overrides: which rows an undo holds, and how it ended ──
test('a row’s key: an album and a merge by the row’s id, an artist match by the artist’s', () => {
  assert.equal(rowKey('album', lrow()), 'album:ov1')
  assert.equal(rowKey('merge', mrow()), 'merge:mg1')
  assert.equal(rowKey('artist', { ndArtistId: 'a9' }), 'artist:a9')
})

test('rows an undo is running for are held — whoever started it — so that they are still there when lb-bot lists them no more', () => {
  const albums = [lrow(), lrow({ id: 'ov2' })]
  const merges = [mrow({ state: 'merged' })]
  const jobsOf = { 'album:ov2': { op: 'remove', phase: 'polling', running: true }, 'merge:mg1': { op: 'merge', phase: 'polling', running: true } }
  const editions = { key: 'editions', kind: 'album', rows: albums }, names = { key: 'merges', kind: 'merge', rows: merges }
  const held = heldRows({}, [editions, names], key => jobsOf[key])
  assert.deepEqual(Object.keys(held), ['album:ov2'], 'an apply or a merge that is running takes nothing away')
  assert.equal(held['album:ov2'].row.id, 'ov2')
  assert.equal(held['album:ov2'].leaving, false)
  // Nothing new to hold: the same object, so nothing re-renders.
  assert.equal(heldRows(held, [editions], key => jobsOf[key]), held)
  // A merge's undo.
  const un = heldRows({}, [names], () => ({ op: 'unmerge', phase: 'polling', running: true }))
  assert.deepEqual(Object.keys(un), ['merge:mg1'])
  // An undo that is over holds nothing: a failed one is lb-bot's to list.
  assert.deepEqual(heldRows({}, [editions], () => ({ op: 'remove', phase: 'failed', running: false })), {})
  assert.deepEqual(heldRows({}, [editions], () => null), {})
})

test('how an undo ended: restored — the row goes, with one toast; not restored — it stays; still running — nothing yet', () => {
  assert.deepEqual(undoEnded('album', lrow(), { op: 'remove', phase: 'settled', running: false, row: null }),
    { gone: true, toast: { text: I('Tide') + ': restored.', level: 'info' } })
  assert.deepEqual(undoEnded('merge', mrow(), { op: 'unmerge', phase: 'settled', running: false, row: null }),
    { gone: true, toast: { text: I('Finch and Sparrow') + ' → ' + I('Finch & Sparrow') + ': restored — the two names are apart again.', level: 'info' } })
  // lb-bot no longer has the row: there is nothing left to list.
  assert.equal(undoEnded('album', lrow(), { op: 'remove', phase: 'gone', running: false, row: null }).gone, true)
  assert.equal(undoEnded('album', lrow(), { op: 'remove', phase: 'failed', running: false, row: lrow({ state: 'failed' }) }).gone, false)
  // A job that went silent has not ended: nothing is known of how it will (R2 — a later answer of the list says).
  assert.equal(undoEnded('album', lrow(), { op: 'remove', phase: 'timeout', running: false, row: lrow({ state: 'removing' }) }), null)
  assert.equal(undoEnded('album', lrow(), { op: 'remove', phase: 'polling', running: true, row: lrow({ state: 'removing' }) }), null)
  // An apply that ended takes no row away.
  assert.equal(undoEnded('album', lrow(), { op: 'apply', phase: 'settled', running: false, row: lrow() }).gone, false)
  // An artist match: the remove is stored at once; the row goes when the scan that follows is over, whatever it says.
  const scan = phase => ({ op: 'remove', phase, running: phase === 'scanning', state: { op: 'remove', phase, lined: { lined: 4, of: 5 } } })
  assert.equal(undoEnded('artist', { ndArtistId: 'a9', name: 'Tide' }, scan('scanning')), null)
  const over = undoEnded('artist', { ndArtistId: 'a9', name: 'Tide' }, scan('done'))
  assert.equal(over.gone, true)
  assert.ok(over.toast.text.startsWith(I('Tide') + ': '), over.toast.text)
  assert.equal(undoEnded('artist', { ndArtistId: 'a9', name: 'Tide' }, scan('failed')).gone, true)
})

test('a separator neither starts a line nor ends one: it holds on to the words beside it', () => {
  assert.equal(nb('Tide · The Lanterns'), 'Tide\u00a0·\u00a0The Lanterns')
  assert.equal(nb('9 files · 2 d ago'), '9 files\u00a0·\u00a02 d ago')
  assert.equal(nb('Finch and Sparrow → Finch & Sparrow'), 'Finch and Sparrow\u00a0→\u00a0Finch & Sparrow')
  assert.equal(nb('No separator here'), 'No separator here')
  assert.equal(nb(''), '')
  assert.equal(nb(undefined), '')
})

// ── Z1: a right-to-left name in a sentence ──
const FSI = '\u2068', PDI = '\u2069'
const HEBREW = "להקת הדוגמה"

test('a set’s line with a Hebrew second name: the name is isolated, so “· N albums” stays after it', () => {
  const set = { mbid: 'm', artists: [e('c1', 'The Example Band', 5), e('c2', HEBREW, 2)] }
  const v = setRowView(set, false)
  assert.equal(v.sub, `also ${FSI}${HEBREW}${PDI} · 7 albums`)
  // In the string itself the count comes after the name's closing mark: nothing of the line is inside the isolate.
  assert.ok(v.sub.indexOf(PDI) < v.sub.indexOf(' · 7 albums'))
  assert.equal(v.title, 'The Example Band', 'a name that is an element of its own needs nothing')
  // Several other names: each on its own.
  const three = setRowView({ mbid: 'm', artists: [e('c1', 'The Example Band', 5), e('c2', HEBREW, 2), e('c3', 'Example', 1)] }, false)
  assert.equal(three.sub, `also ${FSI}${HEBREW}${PDI}, ${FSI}Example${PDI} · 8 albums`)
})

test('every sentence of a merge that carries a name isolates it', () => {
  const row = mrow({ state: 'merged', done: 9, from: { ndArtistId: 'c2', name: HEBREW }, into: { ndArtistId: 'c1', name: 'The Example Band' } })
  assert.equal(mergeLabel(row), `${FSI}${HEBREW}${PDI} → ${FSI}The Example Band${PDI}`)
  assert.equal(afterMerge('c2', { op: 'merge', phase: 'settled', row }).toast, `${FSI}${HEBREW}${PDI} is now ${FSI}The Example Band${PDI}.`)
  assert.ok(mergeToast('merge', 'settled', row).text.startsWith(`${FSI}${HEBREW}${PDI} → ${FSI}The Example Band${PDI}: `))
  const pv = mergePreviewView({ into: row.into, from: row.from, albums: 2, files: 9, sample: [{ album: HEBREW, tracks: 9 }] })
  assert.equal(pv.line, `9 files in 2 albums will say ${FSI}The Example Band${PDI}`)
  assert.equal(pv.sample[0].title, HEBREW, 'an album in a row of its own needs nothing')
  assert.equal(mergePreviewView({ into: row.into, from: row.from, albums: 0, files: 0, sample: [] }).line,
    `No file says ${FSI}${HEBREW}${PDI}: there is nothing to merge into ${FSI}The Example Band${PDI}.`)
  // A word that stands in for a missing name is not a name.
  assert.equal(mergePreviewView({ albums: 1, files: 1, sample: [] }).line, '1 file in 1 album will say the kept name')
  // lb-bot's own lines: album · title, each a name.
  const failed = mergeView('merge', 'failed', mrow({ state: 'failed', failures: [{ album: HEBREW, title: 'Opening Song', reason: 'read-only' }] }))
  assert.equal(failed.problems[0].title, `${FSI}${HEBREW}${PDI} · ${FSI}Opening Song${PDI}`)
})

test('a row of System › Overrides: “Album · Artist” and “Name → MusicBrainz’s name” isolate each name', () => {
  const album = listRowView('album', lrow({ album: HEBREW, artist: 'The Example Band' }), null, NOW)
  assert.equal(album.title, `${FSI}${HEBREW}${PDI} · ${FSI}The Example Band${PDI}`)
  assert.equal(nb(album.title), `${FSI}${HEBREW}${PDI}\u00a0·\u00a0${FSI}The Example Band${PDI}`, 'the dot still holds on to both')
  const match = listRowView('artist', { ndArtistId: 'a9', mbid: 'mb-9', name: HEBREW, setAt: 1000 }, null, NOW, { name: 'The Example Band' })
  assert.equal(match.title, `${FSI}${HEBREW}${PDI}`)
  assert.equal(match.to.text, `${FSI}The Example Band${PDI}`)
  assert.equal(listRowView('artist', { ndArtistId: 'a9', mbid: 'mb-9', name: HEBREW, setAt: 1000 }, null, NOW, { name: '' }).to.text, 'its MusicBrainz artist')
  assert.equal(listRowView('artist', { ndArtistId: 'a9', mbid: 'mb-9', name: '', setAt: 1000 }, null, NOW).title, 'An artist')
})

test('what is sent to lb-bot, and what a row is known by, carries no such mark', () => {
  const marks = /[\u2066-\u2069\u202a-\u202e\u200e\u200f]/
  assert.ok(!marks.test(JSON.stringify(mergeBody('c1', 'c2'))))
  assert.ok(!marks.test(JSON.stringify(setBody({ id: 'c2', name: HEBREW }, { mbid: 'MB-1', name: HEBREW }))))
  assert.equal(setBody({ id: 'c2', name: HEBREW }, { mbid: 'mb-1' }).name, HEBREW)
  assert.ok(!marks.test(rowKey('artist', { ndArtistId: 'c2', name: HEBREW })))
  const kept = keepSets([], { siblings: [{ mbid: 'm', artists: [e('c1', 'The Example Band', 5), e('c2', HEBREW, 2)] }] })
  assert.ok(!marks.test(kept[0].key) && kept[0].set.artists[1].name === HEBREW, 'the set itself is as lb-bot gave it')
})

// ── Z2: a held row keeps its place ──
const match = (id, name) => ({ ndArtistId: id, mbid: `mb-${id}`, name, setAt: 1000 })
const grp = (key, kind, rows) => ({ key, kind, rows })

test('a row an undo holds stays where it was among its group’s rows when lb-bot lists it no more', () => {
  const A = match('a1', 'First'), B = match('a2', 'Second'), C = match('a3', 'Third')
  // Remove on the first of two artist matches: lb-bot's list no longer holds it (the match is removed at once).
  let held = holdRow({}, grp('artists', 'artist', [A, B]), A)
  assert.deepEqual(held['artist:a1'], { kind: 'artist', group: 'artists', row: A, at: 0, before: 'artist:a2', leaving: false })
  assert.deepEqual(rowsShown(grp('artists', 'artist', [B]), held).map(r => r.ndArtistId), ['a1', 'a2'], 'not after the other one')
  // The middle of three, and then the first as well: each at the index it had.
  held = holdRow({}, grp('artists', 'artist', [A, B, C]), B)
  assert.deepEqual(rowsShown(grp('artists', 'artist', [A, C]), held).map(r => r.ndArtistId), ['a1', 'a2', 'a3'])
  held = holdRow(held, grp('artists', 'artist', [A, C]), A)
  assert.equal(held['artist:a1'].at, 0)
  assert.deepEqual(rowsShown(grp('artists', 'artist', [C]), held).map(r => r.ndArtistId), ['a1', 'a2', 'a3'])
  // The last row, with nothing left in lb-bot's list: still shown.
  held = holdRow({}, grp('artists', 'artist', [A, B]), B)
  assert.deepEqual(rowsShown(grp('artists', 'artist', []), held).map(r => r.ndArtistId), ['a2'])
  // A row lb-bot still lists is drawn once, where lb-bot has it.
  assert.deepEqual(rowsShown(grp('artists', 'artist', [A, B]), held).map(r => r.ndArtistId), ['a1', 'a2'])
  // Another group's held row is not this group's.
  assert.deepEqual(rowsShown(grp('merges', 'merge', [mrow({ state: 'merged' })]), held).map(r => r.id), ['mg1'])
  // Holding again changes nothing (the place it had is kept, also once it is gone from the list).
  assert.equal(holdRow(held, grp('artists', 'artist', [A]), B), held)
})

test('a held row keeps its place among its neighbours too: a second row held while the first is, and a row above it that leaves', () => {
  const A = match('a1', 'First'), B = match('a2', 'Second'), C = match('a3', 'Third'), D = match('a4', 'Fourth')
  const ids = rows => rows.map(r => r.ndArtistId)
  // Remove on the first, then — while its scan runs and lb-bot lists it no more — on the second: the second's
  // place is counted among the rows SHOWN, not among the two lb-bot still lists.
  let held = holdRow({}, grp('artists', 'artist', [A, B, C]), A)
  held = holdRow(held, grp('artists', 'artist', [B, C]), B)
  assert.equal(held['artist:a2'].at, 1)
  assert.deepEqual(ids(rowsShown(grp('artists', 'artist', [C]), held)), ['a1', 'a2', 'a3'])
  // The third of four is held; then the first leaves the list for good. The held row stays before the row it
  // stood before — by its index alone it would hop below it.
  held = holdRow({}, grp('artists', 'artist', [A, B, C, D]), C)
  assert.deepEqual(ids(rowsShown(grp('artists', 'artist', [B, D]), held)), ['a2', 'a3', 'a4'])
  // …and when that neighbour is gone as well, the index it had is what is left to go by.
  assert.deepEqual(ids(rowsShown(grp('artists', 'artist', [A, B]), held)), ['a1', 'a2', 'a3'])
  assert.deepEqual(ids(rowsShown(grp('artists', 'artist', [B]), held)), ['a2', 'a3'])
})

test('an album’s held row keeps its place in ITS group — editions or covers — and an undo found running is held with its place', () => {
  const E1 = lrow({ id: 'e1' }), E2 = lrow({ id: 'e2' }), E3 = lrow({ id: 'e3' })
  const C1 = lrow({ id: 'c1', prevReleaseMbid: 'rel-2' })              // the release is the one it was: a cover
  const groups = [grp('editions', 'album', [E1, E2, E3]), grp('covers', 'album', [C1])]
  const jobsOf = { 'album:e2': { op: 'remove', phase: 'polling', running: true } }
  const held = heldRows({}, groups, key => jobsOf[key])
  assert.deepEqual(held['album:e2'], { kind: 'album', group: 'editions', row: E2, at: 1, before: 'album:e3', leaving: false })
  assert.deepEqual(rowsShown(grp('editions', 'album', [E1, E3]), held).map(r => r.id), ['e1', 'e2', 'e3'])
  assert.deepEqual(rowsShown(grp('covers', 'album', [C1]), held).map(r => r.id), ['c1'])
})

// ── Z3: nothing is offered for names lb-bot is merging ──
test('while lb-bot is — as far as this page knows — merging these names, nothing is offered for them', () => {
  // By the list last read: a row of these names in a running state, whoever follows it.
  assert.equal(mergeQuiet([mrow({ state: 'merging' })], null), true)
  assert.equal(mergeQuiet([mrow({ state: 'undoing' })], null), true)
  // By the job followed here: running, or gone silent while it ran (the three-minute give-up) — lb-bot would refuse, `in_progress`.
  assert.equal(mergeQuiet([], { op: 'merge', phase: 'polling', running: true }), true)
  assert.equal(mergeQuiet([], { op: 'merge', phase: 'timeout', running: false }), true)
  assert.equal(mergeQuiet([mrow({ state: 'merging' })], { op: 'merge', phase: 'timeout', running: false }), true, 'after the give-up as on arrival')
  // A merge that is over — however it ended — leaves the names free again.
  for (const phase of ['settled', 'failed', 'gone']) assert.equal(mergeQuiet([], { op: 'merge', phase, running: false }), false, phase)
  assert.equal(mergeQuiet([mrow({ state: 'merged' }), mrow({ state: 'failed' })], null), false)
  assert.equal(mergeQuiet([], null), false)
  assert.equal(mergeQuiet(null, null), false)
})

// ── R4: a row's button says which row ──
test('Undo and Remove are labelled with their row for a screen reader, the names isolated', () => {
  assert.deepEqual(listRowView('album', lrow(), null, NOW).undo, { label: 'Undo', says: 'Undo ' + I('Tide') + ' · ' + I('The Lanterns') })
  assert.deepEqual(listRowView('merge', mrow({ state: 'merged' }), null, NOW).undo,
    { label: 'Undo', says: 'Undo ' + I('Finch and Sparrow') + ' → ' + I('Finch & Sparrow') })
  assert.deepEqual(listRowView('artist', { ndArtistId: 'a9', mbid: 'mb-9', name: HEBREW, setAt: 1000 }, null, NOW, { name: 'The Example Band' }).undo,
    { label: 'Remove', says: 'Remove ' + I(HEBREW) })
  const again = listRowView('album', lrow(), { op: 'remove', phase: 'failed', row: lrow({ state: 'failed' }) }, NOW).undo
  assert.deepEqual(again, { label: 'Undo again', says: 'Undo again ' + I('Tide') + ' · ' + I('The Lanterns') })
  assert.equal(listRowView('album', lrow({ album: '', artist: '' }), null, NOW).undo.says, 'Undo An album')
})

// ── R5: To fix with no list given ──
test('To fix: a list lb-bot did not give is said as that — never as “nothing needs a decision”', () => {
  const NOTE = 'lb-bot could not work out which artists need a look just now.'
  // Both lists given: nothing to say (also when both are empty — then there really is nothing).
  assert.equal(toFixNote({ status: 'ok', data: { artists: [], toCheck: [], siblings: [] } }), '')
  // Either list left out of the answer.
  assert.equal(toFixNote({ status: 'ok', data: { artists: [] } }), NOTE)
  // M2: the note says what is missing. The sets alone: the artists to match were given, and are not said to be unknown.
  assert.equal(toFixNote({ status: 'ok', data: { artists: [], toCheck: [] } }), 'lb-bot could not work out which artists are under two names just now.')
  assert.equal(toFixNote({ status: 'ok', data: { artists: [], siblings: [] } }), NOTE)
  // No answer at all (503), with nothing read before.
  assert.equal(toFixNote({ status: 'error', data: null, error: { text: 'x' } }), NOTE)
  // A re-read that failed keeps the lists it had: they were given.
  assert.equal(toFixNote({ status: 'error', data: { artists: [], toCheck: [], siblings: [] } }), '')
  // Still reading: nothing is known yet, and nothing is claimed.
  assert.equal(toFixNote({ status: 'loading', data: null }), '')
  assert.equal(toFixNote({ status: 'idle', data: null }), '')
  assert.equal(toFixNote(null), '')
})

// ── Z5: while lb-bot's list is out ──
test('To fix, while lb-bot’s lists are out: a skeleton where the two groups will be — and the note only once a read has ended without them', () => {
  const both = { artists: [], toCheck: [], siblings: [] }
  // The first read: nothing is held yet.
  assert.equal(toFixWaiting({ status: 'idle', data: null, reading: false }), true, 'it is about to be asked')
  assert.equal(toFixWaiting({ status: 'loading', data: null, reading: true }), true)
  assert.equal(toFixNote({ status: 'loading', data: null, reading: true }), '')
  // It ended without the lists: the note, and no skeleton.
  for (const ended of [{ status: 'error', data: null, reading: false }, { status: 'ok', data: { artists: [] }, reading: false }]) {
    assert.equal(toFixWaiting(ended), false)
    assert.notEqual(toFixNote(ended), '')
  }
  // Try again — after no answer, and after an answer without the lists: the skeleton again, and the note is gone
  // while it is asked (it says how a read ENDED).
  for (const again of [{ status: 'loading', data: null, reading: true }, { status: 'ok', data: { artists: [] }, reading: true },
                       { status: 'error', data: { artists: [], toCheck: [] }, reading: true }]) {
    assert.equal(toFixWaiting(again), true)
    assert.equal(toFixNote(again), '')
  }
  // The lists are held: no skeleton, whatever is being read behind them.
  assert.equal(toFixWaiting({ status: 'ok', data: both, reading: true }), false)
  assert.equal(toFixWaiting({ status: 'ok', data: both, reading: false }), false)
  assert.equal(toFixWaiting({ status: 'error', data: both, reading: false }), false)
  assert.equal(toFixWaiting(null), false)
})

// ── M1: a list kept through a re-read that failed ──
test('the rows of a list that is older than a job’s end do not say that job is still running', () => {
  const stale = mrow({ state: 'merging', done: 3 })
  const other = mrow({ id: 'mg2', state: 'undoing' })
  // The merge ended on this page; the read its end started failed, and the list kept from before still says `merging`.
  const seen = new Set(['mg1'])
  assert.deepEqual(mergeRowsNow([stale, other], seen).map(r => r.id), ['mg2'], 'another merge’s row is believed as ever')
  assert.equal(mergeQuiet(mergeRowsNow([stale], seen), { op: 'merge', phase: 'settled', running: false }), false)
  assert.equal(mergeQuiet(mergeRowsNow([stale], seen), null), false, 'also once its card was put away')
  assert.equal(mergeRest(mergeRowsNow([stale], seen)[0]), null, 'and no running card is drawn for it')
  // A row at rest in that list is what it is.
  assert.deepEqual(mergeRowsNow([mrow({ state: 'merged', done: 9 })], seen).map(r => r.state), ['merged'])
  // Nothing was seen to end: the list is believed.
  assert.deepEqual(mergeRowsNow([stale], new Set()).length, 1)
  assert.equal(mergeQuiet(mergeRowsNow([stale], new Set()), null), true)
  assert.deepEqual(mergeRowsNow(null, seen), [])
})

// ── F3: the skeleton, and the note through a read ──
test('To fix: the skeleton stands only while a read is out and the page draws neither group; a note that stands stays through a read (F3)', () => {
  const NOLIST = 'lb-bot could not work out which artists need a look just now.'
  const NOSETS = 'lb-bot could not work out which artists are under two names just now.'
  // lb-bot's answer left `toCheck` out and gave the sets; the page draws "One artist, two names". A read is then
  // out — the merge that just ended asked for it, or Try again did: nothing moves. No skeleton; the note as it was.
  for (const reading of [false, true]) {
    const state = { status: 'ok', data: { artists: [], siblings: [{ mbid: 'm1', artists: [] }] }, reading }
    assert.equal(toFixWaiting(state, true), false, `reading ${reading}`)
    assert.equal(toFixNote(state, true), NOLIST, `reading ${reading}`)
  }
  // Rows the page kept from an earlier answer are drawn too (`keepListed`, `keepSets`): both lists left out now.
  const none = { status: 'ok', data: { artists: [] }, reading: true }
  assert.equal(toFixWaiting(none, true), false)
  assert.equal(toFixNote(none, true), NOLIST)
  // The sets alone left out, the artists to match drawn: its own sentence, through the read as well.
  const nosets = { status: 'ok', data: { artists: [], toCheck: [{}] }, reading: true }
  assert.equal(toFixWaiting(nosets, true), false)
  assert.equal(toFixNote(nosets, true), NOSETS)
  // A re-read that failed keeps the answer it had: the note is that answer's.
  assert.equal(toFixNote({ status: 'error', data: { artists: [], toCheck: [] }, reading: false }, true), NOSETS)
  // The page draws neither group: the first read, and a Try again from a page that has neither — the skeleton,
  // and no note while it stands (as before).
  for (const state of [{ status: 'idle', data: null, reading: false }, { status: 'loading', data: null, reading: true },
                       { status: 'ok', data: { artists: [] }, reading: true }, { status: 'ok', data: { artists: [], toCheck: [] }, reading: true }]) {
    assert.equal(toFixWaiting(state, false), true, JSON.stringify(state))
    assert.equal(toFixNote(state, false), '', JSON.stringify(state))
  }
  // Both lists held: nothing either way, drawn or not.
  const both = { status: 'ok', data: { artists: [], toCheck: [], siblings: [] }, reading: true }
  for (const drawn of [false, true]) {
    assert.equal(toFixWaiting(both, drawn), false)
    assert.equal(toFixNote(both, drawn), '')
  }
})

// ── F4: absent is not gone ──
test('System › Overrides: a row is “listed no more” only by an answer that gave its own list (F4)', () => {
  const album = { id: 'ov1', album: 'Currents' }
  const merge = mrow()
  // The list was given and does not hold the row: it is gone (its undo went through).
  assert.equal(listedNoMore({ artists: [], albums: [], merges: [] }, 'album', album), true)
  assert.equal(listedNoMore({ artists: [], albums: [{ id: 'ov2' }], merges: [] }, 'album', album), true)
  assert.equal(listedNoMore({ artists: [], albums: [], merges: [] }, 'merge', merge), true)
  // The list was given and still holds it.
  assert.equal(listedNoMore({ artists: [], albums: [album] }, 'album', album), false)
  assert.equal(listedNoMore({ merges: [mrow({ state: 'undoing' })] }, 'merge', merge), false)
  // lb-bot left that list out of its answer: absent is not gone — nothing is known of the row.
  assert.equal(listedNoMore({ artists: [], merges: [] }, 'album', album), false, '`albums` left out')
  assert.equal(listedNoMore({ artists: [], albums: [] }, 'merge', merge), false, '`merges` left out')
  assert.equal(listedNoMore({ artists: [] }, 'album', album), false)
  assert.equal(listedNoMore(null, 'merge', merge), false, 'no answer at all')
  // Each row by its own list, never by another's.
  assert.equal(listedNoMore({ albums: [], merges: [merge] }, 'album', { id: 'mg1' }), true)
  assert.equal(listedNoMore({ artists: [] }, 'artist', { ndArtistId: 'a1' }), true)
  assert.equal(listedNoMore({ artists: [{ ndArtistId: 'a1' }] }, 'artist', { ndArtistId: 'a1' }), false)
  assert.equal(listedNoMore({ albums: [] }, 'artist', { ndArtistId: 'a1' }), false)
})

// ── F1: whose kept discography a change makes old ──
test('the artists a change touches — what the session’s kept discographies are let go of for (F1)', () => {
  // An artist match set or removed: that artist.
  assert.deepEqual(artistsTouched('artist', { ndArtistId: 'a1', mbid: 'm1', name: 'Finch & Sparrow' }), ['a1'])
  // A merge, and its undo: both names.
  assert.deepEqual(artistsTouched('merge', mrow()), ['a1', 'a2'])
  assert.deepEqual(artistsTouched('merge', mrow({ state: 'undoing' })), ['a1', 'a2'])
  // An album's row names its artist in words, not by id: not known — everything kept is let go of (null).
  assert.equal(artistsTouched('album', { id: 'ov1', artist: 'Tame Impala', album: 'Currents' }), null)
  // A row that does not say: not known either — never "nobody".
  assert.equal(artistsTouched('artist', {}), null)
  assert.equal(artistsTouched('merge', { id: 'mg1' }), null)
  assert.equal(artistsTouched('merge', mrow({ from: { name: 'Finch and Sparrow' } })), null, 'one name’s id missing: the other alone is not all of it')
  assert.equal(artistsTouched('something', null), null)
})

test('System › Overrides lists a cover-only change of an untagged album under Covers (F2)', () => {
  const untagged = { id: 'ov9', state: 'applied', releaseMbid: '', prevReleaseMbid: '', album: 'Demo Tape', artist: 'The Lanterns', coverUrl: 'https://coverartarchive.org/release-group/x/1.jpg', setAt: 1000 }
  const groups = overridesGroups({ artists: [], albums: [untagged], merges: [] })
  assert.deepEqual(groups.find(g => g.key === 'covers').rows.map(r => r.id), ['ov9'])
  assert.deepEqual(groups.find(g => g.key === 'editions').rows, [])
  // Its row has no release to link to, and can be undone like any other.
  const view = listRowView('album', untagged, null, 1300)
  assert.deepEqual([view.link, view.undo?.label], [null, 'Undo'])
})
