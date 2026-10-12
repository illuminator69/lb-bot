// What the frame holds in place (B-094): the state matrix of lib/frame.js.
// `node --test web/src/lib/frame.test.mjs` — no dependency.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { HERO_GAP, HERO_TEXT_FLOOR, NBSP, heroRow, heroTextBasis, indexLineStands, indexLineView, subLine } from './frame.js'

const css = readFileSync(new URL('../index.css', import.meta.url), 'utf8')

test('a hero’s text beside something else has a floor: where the row cannot give it, the other thing takes a row of its own (N4)', () => {
  // The gap's detail: a 96 px cover, the text, the pager (219 px). Header widths as measured at each viewport.
  const at = width => heroRow({ width, cover: 96, aside: 219 })
  // 412 px: the pager fitted beside and left the text 18 px. It goes below; the text has the row.
  assert.deepEqual(at(365), { text: 253, asideBelow: true, textBelow: false })
  // 1,100 px, two panes: 74 px.
  assert.deepEqual(at(421), { text: 309, asideBelow: true, textBelow: false })
  // Where the pager went below already, nothing changes: 360 px, 1,010 px.
  assert.deepEqual(at(313), { text: 201, asideBelow: true, textBelow: false })
  assert.deepEqual(at(331), { text: 219, asideBelow: true, textBelow: false })
  // Where there is room for both, both: 1,320 px as before; and the text is never under its floor beside the pager.
  assert.deepEqual(at(641), { text: 294, asideBelow: false, textBelow: false })
  assert.deepEqual(at(507), { text: HERO_TEXT_FLOOR, asideBelow: false, textBelow: false })
  assert.equal(at(506).asideBelow, true)
  for (let width = 273; width <= 1400; width += 1) {
    const row = at(width)
    assert.ok(row.text >= HERO_TEXT_FLOOR, `${width}: ${row.text}`)
    assert.equal(row.textBelow, false, `${width}: the cover and the text stay side by side`)
  }
  // A 320 px viewport (the header is 273): the cover and the text side by side — and narrower still, where the
  // floor itself no longer fits beside the cover, the text gives way rather than going under the cover.
  assert.deepEqual(at(273), { text: 161, asideBelow: true, textBelow: false })
  assert.deepEqual(at(240), { text: 128, asideBelow: true, textBelow: false })
  // A hero with nothing beside its text is not this rule's.
  assert.deepEqual(heroRow({ width: 365, cover: 96, aside: 0 }), { text: 253, asideBelow: false, textBelow: false })
})

test('the floor as the stylesheet takes it: never more than the row has beside the cover (N4)', () => {
  assert.equal(HERO_TEXT_FLOOR, 160)
  assert.equal(heroTextBasis(96), 'min(160px, calc(100% - 113px))')
  assert.equal(heroTextBasis(132), 'min(160px, calc(100% - 149px))')
  // The gap the model counts with is the stylesheet's own.
  assert.match(css, new RegExp(`\\.hero \\{[^}]*\\bgap: ${HERO_GAP}px;`))
  // …and the text column can still shrink (`min-width: 0`) and grow (`flex: 1`): the floor is its basis only.
  assert.match(css, /\.grow \{ flex: 1; min-width: 0; \}/)
})

test('a page that gives a second title line holds its height from the first frame; a page that gives none has none (N3)', () => {
  // Not given: no line (an album's page says what it is in the bar's crumb).
  assert.equal(subLine(undefined), null)
  assert.equal(subLine(null), null)
  // Given and empty — the page has nothing to say yet, or just now: the line is there, one line high.
  assert.equal(subLine(''), NBSP)
  assert.equal(subLine('   '), NBSP)
  assert.equal(NBSP, String.fromCharCode(0xa0))
  // Given: itself.
  assert.equal(subLine('38 need a decision'), '38 need a decision')
  assert.equal(subLine(0), 0, 'a number is something to say')
})

test('Library’s index line: its place is held from the first frame by a stand-in its own size, then the line (N3)', () => {
  const n = v => v.toLocaleString()
  // lb-bot's answer in hand — this visit's, or the session's last, drawn at once on a later visit.
  assert.deepEqual(indexLineView({ artistsIndexed: 2426, artistsTotal: 2474, artistsStale: 0, building: false }, 2474),
    { known: true, counts: `${n(2426)} of ${n(2474)}`, tail: '' })
  assert.deepEqual(indexLineView({ artistsIndexed: 2400, artistsTotal: 2474, artistsStale: 12, building: true }, 0),
    { known: true, counts: `${n(2400)} of ${n(2474)}`, tail: ' · 12 stale · building…' })
  assert.deepEqual(indexLineView({ artistsIndexed: 0, artistsTotal: 0 }, 5), { known: true, counts: '0 of 0', tail: '' })
  // Not yet: the stand-in is the line as it will most likely be — as many digits as the page's own artist list has.
  assert.deepEqual(indexLineView(null, 2474), { known: false, counts: `${n(2474)} of ${n(2474)}`, tail: '' })
  // …and with no list either, a library of thousands.
  for (const guess of [undefined, null, 0, -1, NaN, 'many']) {
    assert.deepEqual(indexLineView(null, guess), { known: false, counts: `${n(1000)} of ${n(1000)}`, tail: '' }, String(guess))
  }
  // An answer that is not one is not shown as counts.
  assert.equal(indexLineView({}, 7).known, false)
  assert.equal(indexLineView({ artistsIndexed: 'x', artistsTotal: 3 }, 7).known, false)
})

test('Library’s index line: its place folds away only when the read failed and nothing is kept (N3)', () => {
  const kept = { artistsIndexed: 1, artistsTotal: 2 }
  assert.equal(indexLineStands({ status: null, failed: false }), true, 'reading: the place is held')
  assert.equal(indexLineStands({ status: kept, failed: false }), true)
  assert.equal(indexLineStands({ status: kept, failed: true }), true, 'the session’s last answer stays')
  assert.equal(indexLineStands({ status: null, failed: true }), false, 'neither a hole nor a pop: it folds')
})
