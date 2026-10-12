// Run: node --test web/src/lib/motion.test.mjs   (no dependency, no network)
// What the frame's `Fluid` does when its content changes height (I-029: V3,
// W1, Y4) — the decision, without a browser: when the box moves, how long
// that takes, and when it is left alone.
import test from 'node:test'
import assert from 'node:assert/strict'
import { ROLL_MS, fluidMove, moveMs } from './motion.js'

test('a change of height is a move from where the box is to where its content is now', () => {
  assert.deepEqual(fluidMove({ moving: false, from: 370, next: 420 }), { act: 'move', ms: ROLL_MS })
  // A second change of the content while the box moves starts again from where the box is, with a time of its own.
  assert.deepEqual(fluidMove({ moving: true, from: 212, target: 370, next: 420, left: 140 }), { act: 'move', ms: ROLL_MS })
  // Less than two pixels is nothing to move for.
  assert.deepEqual(fluidMove({ moving: false, from: 370, next: 371 }), { act: 'rest' })
})

test('reduced motion: the box is a plain box', () => {
  assert.deepEqual(fluidMove({ moving: false, from: 0, next: 480, calm: true }), { act: 'rest' })
  assert.deepEqual(fluidMove({ moving: true, from: 100, target: 300, next: 480, left: 100, calm: true }), { act: 'rest' })
  assert.deepEqual(fluidMove({ moving: true, from: 100, target: 300, next: 480, left: 100, calm: true, itself: true }), { act: 'rest' })
})

test('something inside folding by itself: at rest the box follows it — on its way it is aimed at where the content is now, for the time it has left (Y4, R1)', () => {
  assert.deepEqual(fluidMove({ moving: false, from: 370, next: 380, itself: true }), { act: 'rest' })
  // The preview has just arrived and the box is 120 ms into its 300 ms move to 410; "Show them" is pressed and a
  // Roll inside grows the content. Letting go here was a jump from mid-move to the full height (Y4); leaving the
  // box to finish at 410 ended in one too — it was let go under content that had grown past it (R1). It is aimed
  // again instead: the new height, the time that is left. The Roll inside carries on by itself.
  assert.deepEqual(fluidMove({ moving: true, from: 300, target: 410, next: 436, left: 180, itself: true }), { act: 'move', ms: 180, keep: true })
  // …and again as the Roll goes on: less time each frame, so the box is where the content is when its own time is up.
  assert.deepEqual(fluidMove({ moving: true, from: 331, target: 436, next: 470, left: 163.4, itself: true }), { act: 'move', ms: 163, keep: true })
  assert.deepEqual(fluidMove({ moving: true, from: 466, target: 470, next: 478, left: 0.4, itself: true }), { act: 'move', ms: 1, keep: true })
  // Its time is up while the Roll still runs: it is let go where it has got to, and follows the Roll from there.
  assert.deepEqual(fluidMove({ moving: true, from: 476, target: 478, next: 484, left: 0, itself: true }), { act: 'rest' })
  assert.deepEqual(fluidMove({ moving: true, from: 476, target: 478, next: 484, left: -16, itself: true }), { act: 'rest' })
  // A box that is already heading to where its content is, is left alone — whoever changed it.
  assert.deepEqual(fluidMove({ moving: true, from: 250, target: 410, next: 411, left: 200, itself: true }), { act: 'leave' })
  assert.deepEqual(fluidMove({ moving: true, from: 250, target: 410, next: 411, left: 200 }), { act: 'leave' })
  // It is where its content is, but was heading elsewhere: aimed there, not left to overshoot.
  assert.deepEqual(fluidMove({ moving: true, from: 250, target: 410, next: 251, left: 200 }), { act: 'move', ms: ROLL_MS })
})

test('a tall move takes longer, so that its fastest frame stays small (W1)', () => {
  assert.equal(moveMs(0), ROLL_MS)
  assert.equal(moveMs(229), ROLL_MS)
  assert.equal(moveMs(-300), ROLL_MS)
  assert.equal(moveMs(482), 434)
  assert.equal(moveMs(-482), 434)
  assert.equal(moveMs(2000), 450, 'and never longer than that')
})
