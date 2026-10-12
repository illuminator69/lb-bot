// How the frame's height changes move (I-029: V3, W1, Y4). `Roll` folds open
// and shut in CSS; `Fluid` (components/Page.jsx) moves a box to its content's
// new height. What `Fluid` decides at each change is here, pure, with its
// test (motion.test.mjs) — a browser is the only thing that can show the move
// itself.

/** `Roll`'s time, and `Fluid`'s for an ordinary change. */
export const ROLL_MS = 300
/** A tall fold's (`<Roll slow>`), and the most a `Fluid` move takes. */
export const SLOW_MS = 450

/** How long a move of `px` takes: `Roll`'s own time up to a third of a screen,
 *  then longer in proportion, to `SLOW_MS`. */
export const moveMs = px => Math.round(Math.min(SLOW_MS, Math.max(ROLL_MS, Math.abs(px) * 0.9)))

/** What a `Fluid` does when its content's height has changed.
 *
 *    moving   the box is on its way somewhere (it holds a height of its own)
 *    from     where the box is now; `next` where its content is
 *    target   where the box is heading (when it is moving)
 *    left     how much of that move's time is left, in ms
 *    calm     reduced motion is asked for
 *    itself   something inside is folding by itself (a `Roll`)
 *
 *  `move` — hold the box where it is and let it go to `next` over `ms`; with
 *  `keep`, within the time its move already has (the end it was given stands);
 *  `rest` — let go of it (it is a plain box again);
 *  `leave` — do nothing: it is already heading there.
 *
 *  At rest, something inside folding by itself is simply followed. ON ITS WAY
 *  the box is never let go — that is a jump to its full height (Y4) — and never
 *  left to finish where it was heading while a `Roll` inside grows the content
 *  past that: it would be let go, at its move's end, under content that had
 *  moved on, which is the same jump a little later (R1). It is aimed again at
 *  where the content is now, for the time that is left; the `Roll` carries on
 *  by itself, and when the box's time is up the two are together. */
export function fluidMove({ moving, from, next, target, left = 0, calm = false, itself = false }) {
  if (calm) return { act: 'rest' }
  if (!moving) {
    if (itself || Math.abs(next - from) < 2) return { act: 'rest' }
    return { act: 'move', ms: moveMs(next - from) }
  }
  if (Math.abs(next - target) < 2) return { act: 'leave' }
  if (itself) return left > 0 ? { act: 'move', ms: Math.max(1, Math.round(left)), keep: true } : { act: 'rest' }
  return { act: 'move', ms: moveMs(next - from) }
}
