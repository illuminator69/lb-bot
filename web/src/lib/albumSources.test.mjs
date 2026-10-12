// Run: node --test web/src/lib/albumSources.test.mjs   (no dependency, no network)
// What the album page may say about a click on a source, about where a fill is
// coming from, and about the list the click was made in (ruling A1).
import test from 'node:test'
import assert from 'node:assert/strict'
import { askedText, fillSourceText, fillSourceBeyond, sourcesAgeText, sourceKey, albumBadge, SAMPLED_NOTE,
         extraFilesText, downloadAllLabel, leftOutText } from './albumSources.js'

test('a click says what was asked for, not that anything is queued', () => {
  assert.equal(askedText({ ok: true, existing: false }, 'peer-a'), 'Asked @peer-a for this album')
  assert.ok(!/queued/i.test(askedText({ ok: true }, 'peer-a')))
  // A fill already in flight is the answer to the click, and the click changed nothing.
  const existing = askedText({ ok: true, existing: true }, 'peer-a')
  assert.ok(/already/.test(existing))
  assert.ok(!existing.includes('peer-a'))
})

test('the status names the source the fill has', () => {
  assert.equal(fillSourceText(null), '')
  assert.equal(fillSourceText({ state: 'searching' }), '')
  // An automatic fill: where it is coming from.
  assert.equal(fillSourceText({ state: 'downloading', source: 'peer-b', pickedSource: '' }), 'from @peer-b')
  // A click, still on the source that was clicked.
  assert.equal(fillSourceText({ state: 'queued', source: 'peer-a', pickedSource: 'peer-a' }), 'from @peer-a')
  // A click whose row has no source yet (one search for a list that is gone),
  // or that failed before anything was queued.
  assert.equal(fillSourceText({ state: 'searching', pickedSource: 'peer-a' }), 'asked for from @peer-a')
  assert.equal(fillSourceText({ state: 'failed', source: '', pickedSource: 'peer-a' }), 'asked for from @peer-a')
})

test('it says when lb-bot moved to another source than the one clicked', () => {
  const text = fillSourceText({ state: 'downloading', source: 'peer-b', pickedSource: 'peer-a' })
  assert.ok(text.startsWith('from @peer-b'), text)
  assert.ok(text.includes('moved on from @peer-a'), text)
  // Names differing only in case are different peers on Soulseek.
  assert.ok(fillSourceText({ source: 'Peer-A', pickedSource: 'peer-a' }).includes('moved on'))
})

test('the list says how old it is', () => {
  assert.equal(sourcesAgeText(null, 0), '')
  assert.equal(sourcesAgeText({ searchedAt: 0, serverTime: 100, receivedMs: 0 }, 0), '')
  const fresh = { searchedAt: 1000, serverTime: 1002, receivedMs: 50_000 }
  assert.equal(sourcesAgeText(fresh, 50_000), 'Searched just now')
  // The age is the server's own two clocks, plus the time the page has had it
  // — asked again with a later `now`, it has moved on.
  assert.equal(sourcesAgeText(fresh, 50_000 + 4 * 60_000), 'Searched 4 min ago')
  assert.equal(sourcesAgeText(fresh, 50_000 + 16 * 60_000), 'Searched 16 min ago')
  // A server that sent no clock: only what the page itself measured.
  assert.equal(sourcesAgeText({ searchedAt: 1000, receivedMs: 0 }, 61_000), 'Searched 1 min ago')
  // A clock that went backwards is not a negative age.
  assert.equal(sourcesAgeText({ searchedAt: 1000, serverTime: 900, receivedMs: 0 }, 0), 'Searched just now')
})

test('a kept list says it was kept, not that anybody was shown it', () => {
  // `cached` is true for a list this page never showed: one the automatic
  // download's own search left, or another client asked for.
  const kept = { cached: true, searchedAt: 1000, serverTime: 1000 + 9 * 60 + 59, receivedMs: 0 }
  assert.equal(sourcesAgeText(kept, 0), 'Kept from a search 9 min ago')
  assert.equal(sourcesAgeText({ ...kept, serverTime: 1010 }, 0), 'Kept from a search just now')
  assert.ok(!/shown/.test(sourcesAgeText(kept, 0)))
})

test('a row is badged by what the server takes for album evidence', () => {
  const named = { albumMatch: 100, albumMatchOk: true }
  // The name is evidence.
  assert.equal(albumBadge({ ...named, albumEvidence: 'name' }).text, '✓ matches album title')
  assert.equal(albumBadge({ ...named, albumEvidence: 'name' }).ok, true)
  // The files are — whatever the name scores, also nothing.
  for (const src of [{ ...named, albumEvidence: 'tracks' },
                     { albumMatch: 21, albumMatchOk: false, albumEvidence: 'tracks' },
                     { albumMatch: 0, albumMatchOk: false, albumEvidence: 'tracks' }]) {
    const badge = albumBadge(src)
    assert.equal(badge.ok, true)
    assert.ok(/tracks/.test(badge.text), badge.text)
    assert.ok(!/different album/.test(badge.text))
  }
  // The name scores as a match and the server takes no name for evidence
  // ("2018 - EP - After 4AM" for "After EP"): not "matches album title".
  const unconfirmed = albumBadge({ albumMatch: 80, albumMatchOk: true, albumEvidence: '' })
  assert.equal(unconfirmed.ok, false)
  assert.ok(!/matches album title/.test(unconfirmed.text), unconfirmed.text)
  assert.ok(/80%/.test(unconfirmed.title))
  // Nothing says it is the album.
  assert.equal(albumBadge({ albumMatch: 31, albumMatchOk: false, albumEvidence: '' }).text, 'different album?')
  assert.equal(albumBadge({ albumMatch: 0, albumMatchOk: false, albumEvidence: '' }), null)
  // No word from the server (an older one, a source nobody judged): by the
  // name's score, as before.
  assert.equal(albumBadge(named).text, '✓ matches album title')
  assert.equal(albumBadge({ albumMatch: 31, albumMatchOk: false }).text, 'different album?')
  assert.equal(albumBadge({ albumMatch: 0 }), null)
  assert.equal(albumBadge({}), null)
  assert.equal(albumBadge(null), null)
})

test('a row is the same row by its peer and folder, whatever its rank', () => {
  assert.equal(sourceKey({ id: 0, peer: 'a', folder: 'x/y' }), sourceKey({ id: 7, peer: 'a', folder: 'x/y' }))
  assert.notEqual(sourceKey({ peer: 'a', folder: 'x/y' }), sourceKey({ peer: 'a', folder: 'x/z' }))
  assert.notEqual(sourceKey({ peer: 'a', folder: 'x' }), sourceKey({ peer: 'b', folder: 'x' }))
})

test('the sample note is one line', () => {
  assert.ok(SAMPLED_NOTE.length < 200 && !SAMPLED_NOTE.includes('\n'))
  assert.ok(/sample/.test(SAMPLED_NOTE))
})

// ── "Download all" (B-084 H5e; the wire is contract-b084.md) ────────────────

test('a row says when its folder has more files than the album', () => {
  // Absent: an older lb-bot, or no tracklist to count against — nothing to
  // say, and never "0 more files".
  for (const src of [null, {}, { fileCount: 11 }, { extraFiles: null, fileCount: 11 },
                     { extraFiles: 0, fileCount: 5 }]) {
    assert.equal(extraFilesText(src), '')
    assert.equal(downloadAllLabel(src), '', 'no second action without the line')
  }
  assert.equal(extraFilesText({ extraFiles: 6, fileCount: 11 }), '6 more files in this folder')
  assert.equal(extraFilesText({ extraFiles: 1, fileCount: 13 }), '1 more file in this folder')
  // The second action names how many files it will ask for: the row's own count.
  assert.equal(downloadAllLabel({ extraFiles: 6, fileCount: 11 }), 'Download all 11 files')
  assert.equal(downloadAllLabel({ extraFiles: 1, fileCount: 13 }), 'Download all 13 files')
  // Never computed here from the files shown: only the server's field counts.
  assert.equal(extraFilesText({ fileCount: 11, files: new Array(11).fill({ matchedTo: null }) }), '')
})

test('after "Download all" the page says what lb-bot understood', () => {
  const all = { allFiles: true, fileCount: 11 }
  // The echo: lb-bot took it.
  assert.equal(askedText({ ok: true, existing: false, allFiles: true }, 'peer-a', all),
               'Asked @peer-a for all 11 files in the folder')
  // It was asked for and the answer does not say so (false, or no echo at
  // all): the album's tracks only, said plainly — never "all files".
  for (const answer of [{ ok: true, existing: false, allFiles: false }, { ok: true, existing: false }, {}]) {
    const text = askedText(answer, 'peer-a', all)
    assert.ok(/tracks only/.test(text), text)
    assert.ok(/did not take/.test(text), text)
    assert.ok(!/for all 11 files/.test(text), text)
  }
  // A fill already running is the answer, whatever was asked and echoed.
  for (const answer of [{ existing: true, allFiles: true }, { existing: true, allFiles: false }]) {
    assert.equal(askedText(answer, 'peer-a', all), askedText({ existing: true }, 'peer-a'))
  }
  // A plain click reads as it did, whatever the answer echoes.
  assert.equal(askedText({ ok: true, allFiles: false }, 'peer-a'), 'Asked @peer-a for this album')
  assert.equal(askedText({ ok: true, allFiles: true }, 'peer-a'), 'Asked @peer-a for this album')
  assert.equal(askedText({ ok: true }, 'peer-a', { allFiles: false, fileCount: 11 }),
               'Asked @peer-a for this album')
})

test('the status says when files of the folder were left out', () => {
  for (const fill of [null, {}, { leftOut: 0 }, { leftOut: null }, { state: 'downloading' }]) {
    assert.equal(leftOutText(fill), '')
  }
  assert.equal(leftOutText({ leftOut: 6 }), '6 other files in the folder were left out')
  assert.equal(leftOutText({ leftOut: 1 }), '1 other file in the folder was left out')
  // Asked whole: nothing was left out, so nothing is said.
  assert.equal(leftOutText({ allFiles: true, leftOut: 0 }), '')
})

test('a row titled with its peer adds only what the title does not say', () => {
  // The redesign's request row is titled "@<the peer the fill has>". Under it
  // goes what `fillSourceText` says beyond that name, and nothing when it
  // would only repeat it.
  assert.equal(fillSourceBeyond(null), '')
  assert.equal(fillSourceBeyond({ state: 'searching' }), '')
  // An automatic fill, and a click still on the peer that was clicked: the title says it.
  assert.equal(fillSourceBeyond({ state: 'downloading', source: 'peer-b', pickedSource: '' }), '')
  assert.equal(fillSourceBeyond({ state: 'downloading', source: 'peer-a', pickedSource: 'peer-a' }), '')
  // lb-bot moved on from the peer that was picked: the whole sentence.
  const moved = { state: 'downloading', source: 'peer-b', pickedSource: 'peer-a' }
  assert.equal(fillSourceBeyond(moved), fillSourceText(moved))
  assert.ok(fillSourceBeyond(moved).includes('moved on from @peer-a, which you picked'))
  // Picked, and no peer is serving yet: the title has no name to give.
  const asked = { state: 'searching', source: '', pickedSource: 'peer-a' }
  assert.equal(fillSourceBeyond(asked), fillSourceText(asked))
  assert.ok(fillSourceBeyond(asked).includes('@peer-a'))
})
