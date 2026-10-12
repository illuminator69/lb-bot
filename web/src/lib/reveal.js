// What a press opens must be on screen (I-029, V2). In a 900 px window "My
// files are this edition" opened its preview 600 px further down and the page
// did not move: the button went grey and nothing else visibly happened.
//
// `reveal(el, { anchor })` brings `el` into view by scrolling the pane it is
// in — the least that shows its end, never when it is already wholly in view,
// and never so far that `anchor` (the thing that was pressed) leaves the top
// of the view. It does this FRAME BY FRAME for as long as the block keeps
// changing height: a block that folds open, or whose content arrives a second
// later, is followed as it grows, a capped step at a time. So it neither jumps
// to where the block will be (that place does not exist yet: the pane cannot
// scroll past its present end) nor waits for the fold to finish. Under
// `prefers-reduced-motion` it moves at once. The user's own scroll ends it —
// ANY scroll the follow did not make (the scrollbar dragged, a middle-click
// autoscroll), not only a wheel or a key. And an answer that arrives after the
// press (`late`) is not revealed at all when the user has scrolled the pane
// since: they went somewhere to read, and are not pulled back.
//
// The arithmetic is pure (`revealDelta`, `revealStep`), with its test:
// reveal.test.mjs.

/** How far to scroll (down is positive) so that `top`…`bottom` is inside the
 *  view: 0 when it is; the least that shows its end otherwise, but never so far
 *  that `anchorTop` goes above the top part of the view. */
export function revealDelta({ viewTop, viewBottom, top, bottom, anchorTop = top, pad = 12 }) {
  if (bottom > viewBottom - pad) {
    const most = anchorTop - (viewTop + pad)
    return Math.max(0, Math.min(bottom - (viewBottom - pad), most))
  }
  if (top < viewTop + pad) return top - (viewTop + pad)
  return 0
}

const MAX_STEP = 32

/** One frame of the way there: a quarter of what is left, at least 2 px and
 *  at most `MAX_STEP`, or all of it when that is less. */
export function revealStep(delta) {
  const left = Math.abs(delta)
  if (!left) return 0
  return Math.sign(delta) * Math.min(left, Math.max(2, Math.min(MAX_STEP, left * 0.25)))
}

const SETTLED_FRAMES = 10     // this long with nothing to do: the block has stopped moving
const FOLLOW_MS = 1600        // a fold is 300 ms and a height change another 300
const SCROLL_KEYS = new Set(['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '])

function paneOf(el) {
  for (let n = el.parentElement; n; n = n.parentElement) {
    const flow = window.getComputedStyle(n).overflowY
    if (flow === 'auto' || flow === 'scroll') return n
  }
  return null
}

let ending = null             // one follow at a time: a newer press takes over
// Where a follow last left each pane. A pane that is somewhere else since has
// been scrolled by the user.
const leftAt = new WeakMap()

/** A pane the frame has just put back at a saved place (B-094, N1h): that was
 *  not the user scrolling, so a reveal after it behaves as on a fresh page. */
export function forgetPane(pane) { leftAt.delete(pane) }

/** Bring `el` into view and keep it there while it settles. Returns a function
 *  that ends the follow. Does nothing where there is no page to scroll.
 *  `late`: this is for an answer, not a press — skipped when the pane is not
 *  where the last follow left it. */
export function reveal(el, { anchor, late = false } = {}) {
  if (typeof window === 'undefined' || !el?.getBoundingClientRect || !window.requestAnimationFrame) return () => {}
  const pane = paneOf(el)
  if (!pane) return () => {}
  if (ending) ending()
  if (late && leftAt.has(pane) && Math.abs(pane.scrollTop - leftAt.get(pane)) > 1) return () => {}
  const calm = !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  const began = performance.now()
  let frame = 0
  let still = 0
  let over = false
  // What the follow itself last wrote: a scroll event that finds the pane
  // anywhere else was somebody else's.
  let own = pane.scrollTop
  const onKey = e => { if (SCROLL_KEYS.has(e.key)) end() }
  const onScroll = () => { if (Math.abs(pane.scrollTop - own) > 1) end(true) }
  function end(theirs = false) {
    if (over) return
    over = true
    window.cancelAnimationFrame(frame)
    pane.removeEventListener('wheel', end)
    pane.removeEventListener('touchstart', end)
    pane.removeEventListener('pointerdown', end)
    pane.removeEventListener('scroll', onScroll)
    window.removeEventListener('keydown', onKey)
    // Where the follow left the pane — not where the user has just put it.
    leftAt.set(pane, theirs === true ? own : pane.scrollTop)
    if (ending === end) ending = null
  }
  function step() {
    if (over) return
    if (el.isConnected === false) { end(); return }
    const view = pane.getBoundingClientRect()
    const box = el.getBoundingClientRect()
    const pressed = anchor && anchor.isConnected !== false ? anchor.getBoundingClientRect() : box
    const delta = revealDelta({ viewTop: view.top, viewBottom: view.bottom, top: box.top, bottom: box.bottom, anchorTop: pressed.top })
    if (Math.abs(delta) < 1) {
      still += 1
    } else {
      still = 0
      pane.scrollTop += calm ? delta : revealStep(delta)
      own = pane.scrollTop
    }
    if (still >= SETTLED_FRAMES || performance.now() - began > FOLLOW_MS) { end(); return }
    frame = window.requestAnimationFrame(step)
  }
  // The user's own scroll wins at once.
  pane.addEventListener('wheel', end, { passive: true })
  pane.addEventListener('touchstart', end, { passive: true })
  pane.addEventListener('pointerdown', end, { passive: true })
  pane.addEventListener('scroll', onScroll, { passive: true })
  window.addEventListener('keydown', onKey)
  ending = end
  frame = window.requestAnimationFrame(step)
  return end
}
