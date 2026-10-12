// Run: node --test web/src/lib/reveal.test.mjs   (no dependency, no network)
// How far the page scrolls to show what a press opened (I-029, V2): the least
// that does it, never when it is already wholly in view, and never so far
// that the thing pressed leaves the top of the view.
import test from 'node:test'
import assert from 'node:assert/strict'
import { revealDelta, revealStep } from './reveal.js'

const view = { viewTop: 100, viewBottom: 900, pad: 12 }

test('wholly in view: nothing moves', () => {
  assert.equal(revealDelta({ ...view, top: 300, bottom: 700 }), 0)
  assert.equal(revealDelta({ ...view, top: 112, bottom: 888 }), 0)
})

test('cut by the fold: its end comes into view, and no further', () => {
  assert.equal(revealDelta({ ...view, top: 500, bottom: 1100 }), 212)
  assert.equal(revealDelta({ ...view, top: 500, bottom: 889 }), 1)
})

test('taller than what is left of the view: the thing pressed comes to the top part, not past it', () => {
  // The block would need 800 px of scroll for its end; the action's row may
  // come up 388 px (to the top of the view, less the margin).
  assert.equal(revealDelta({ ...view, top: 400, bottom: 1700, anchorTop: 500 }), 388)
  // Already at the top: nothing more.
  assert.equal(revealDelta({ ...view, top: 0, bottom: 1700, anchorTop: 112 }), 0)
  // With no anchor named, the block's own top is what must stay.
  assert.equal(revealDelta({ ...view, top: 500, bottom: 1700 }), 388)
})

test('above the view: back down to its top, by the least that shows it', () => {
  assert.equal(revealDelta({ ...view, top: 40, bottom: 300 }), -72)
})

test('one frame of the way: never a jump, always an end', () => {
  // A long way: a capped step. Close: a quarter of what is left, then the rest.
  assert.equal(revealStep(600), 32)
  assert.equal(revealStep(-600), -32)
  assert.equal(revealStep(60), 15)
  assert.equal(revealStep(6), 2)
  assert.equal(revealStep(1.5), 1.5)
  assert.equal(revealStep(0), 0)
  // The whole of it is walked in steps no larger than the cap.
  let left = 600, frames = 0
  while (Math.abs(left) >= 1) { const s = revealStep(left); assert.ok(Math.abs(s) <= 32); left -= s; frames += 1 }
  assert.ok(frames > 18 && frames < 60, `${frames} frames`)
})

// ── the follow itself, on a pane that is three numbers and a clock ──────────
// No browser: a "pane" with a scroll offset, a block inside it, and frames
// stepped by hand. What it shows is when the follow moves the pane and when it
// lets go — not how it looks.
function page({ view = 400, blockTop = 300, blockHeight = 600 } = {}) {
  const frames = []
  const listeners = new Map()
  const on = (map, type, fn) => { if (!map.has(type)) map.set(type, new Set()); map.get(type).add(fn) }
  const pane = {
    scrollTop: 0,
    parentElement: null,
    listeners: new Map(),
    addEventListener(type, fn) { on(pane.listeners, type, fn) },
    removeEventListener(type, fn) { pane.listeners.get(type)?.delete(fn) },
    getBoundingClientRect: () => ({ top: 0, bottom: view }),
    emit(type) { for (const fn of [...(pane.listeners.get(type) || [])]) fn({ type }) },
  }
  const el = {
    isConnected: true,
    parentElement: pane,
    getBoundingClientRect: () => ({ top: blockTop - pane.scrollTop, bottom: blockTop + blockHeight - pane.scrollTop }),
  }
  let clock = 0
  globalThis.window = {
    getComputedStyle: n => ({ overflowY: n === pane ? 'auto' : 'visible' }),
    requestAnimationFrame: fn => { frames.push(fn); return frames.length },
    cancelAnimationFrame: id => { frames[id - 1] = null },
    matchMedia: () => ({ matches: false }),
    addEventListener(type, fn) { on(listeners, type, fn) },
    removeEventListener(type, fn) { listeners.get(type)?.delete(fn) },
  }
  globalThis.performance = { now: () => clock }
  const frame = (n = 1) => { for (let i = 0; i < n; i++) { clock += 16; const run = frames.splice(0); run.forEach(fn => fn && fn(clock)) } }
  return { pane, el, frame, pending: () => frames.filter(Boolean).length }
}

test('the follow scrolls the pane a step a frame, and stops once the block is in view', async () => {
  const { reveal } = await import('./reveal.js')
  const p = page({ blockTop: 100, blockHeight: 500 })      // its end is 212 px below the view's
  reveal(p.el)
  p.frame()
  assert.ok(p.pane.scrollTop > 0 && p.pane.scrollTop <= 32, `one capped step, not a jump: ${p.pane.scrollTop}`)
  p.frame(40)
  assert.ok(p.pane.scrollTop > 86.9 && p.pane.scrollTop <= 88, `the least that shows the thing pressed at the top of the view: ${p.pane.scrollTop}`)
  assert.equal(p.pending(), 0, 'and it has let go')
})

test('any scroll the follow did not make ends it (Y3): the scrollbar dragged, not only the wheel', async () => {
  const { reveal } = await import('./reveal.js')
  const p = page()
  reveal(p.el)
  p.frame(3)
  const at = p.pane.scrollTop
  assert.ok(at > 0)
  // The follow's own writes raise scroll events too: those are not the user.
  p.pane.emit('scroll')
  p.frame()
  assert.ok(p.pane.scrollTop > at, 'its own scroll does not end it')
  // The user drags the scrollbar back up: no wheel, no key, no touch — only the offset moves.
  p.pane.scrollTop = 5
  p.pane.emit('scroll')
  p.frame(10)
  assert.equal(p.pane.scrollTop, 5, 'the pane is theirs')
  assert.equal(p.pending(), 0)
  // And a press of the pointer in the pane ends it before anything moves.
  const q = page()
  reveal(q.el)
  q.frame(2)
  const there = q.pane.scrollTop
  q.pane.emit('pointerdown')
  q.frame(10)
  assert.equal(q.pane.scrollTop, there)
})

test('an answer that comes late is not revealed when the user has scrolled since the press (Y3)', async () => {
  const { reveal } = await import('./reveal.js')
  // The press: revealed, and the follow runs to its end.
  const p = page({ blockTop: 100, blockHeight: 500 })
  reveal(p.el)
  p.frame(60)
  const left = p.pane.scrollTop
  assert.ok(left > 0)
  // The answer arrives a second later, the pane where the follow left it: revealed like any change.
  reveal(p.el, { late: true })
  assert.equal(p.pending(), 1)
  p.frame(20)
  // The user scrolls away to read; then another answer arrives: they are not pulled back.
  p.pane.scrollTop = 0
  reveal(p.el, { late: true })
  assert.equal(p.pending(), 0, 'no follow was started')
  p.frame(10)
  assert.equal(p.pane.scrollTop, 0)
  // A press is a press: it is revealed wherever the pane is.
  reveal(p.el)
  p.frame(60)
  assert.ok(Math.abs(p.pane.scrollTop - left) < 1.5, `${p.pane.scrollTop} / ${left}`)
  // A late answer in a pane no follow has touched yet has nothing to compare with, and is revealed.
  const q = page({ blockTop: 100, blockHeight: 500 })
  reveal(q.el, { late: true })
  assert.equal(q.pending(), 1)
})
