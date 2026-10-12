// A page comes back as it was left (B-094, N1): the hooks. The decisions are
// lib/view.js, the bags are the router's (lib/router.js).
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { currentArrival, onRoute, saveView, saveViewIn, viewBag } from './router.js'
import {
  SCROLL, initialOf, keepForSession, keyIsUsers, scrollLook, scrollTargets, sessionValue, wheelIsUsers, wheelScrolls,
  wheelStream,
} from './view.js'
import { forgetPane } from './reveal.js'

/** How the entry on show was arrived at: `{ n, restore }`, `n` one more for
 *  every arrival that is not the same page under another entry. */
export function useArrival() {
  const [arrival, setArrival] = useState(currentArrival)
  useEffect(() => {
    setArrival(currentArrival())
    return onRoute(() => setArrival(currentArrival()))
  }, [])
  return arrival
}

/** `useState` that belongs to the history entry: on a restore it starts with
 *  what the entry kept under `name`, and every value it takes is kept there.
 *  For view choices only — a filter, a sort, how much of a list is drawn, a
 *  "Show all". Never for an answer, and never for something whose restoring
 *  would ask lb-bot for anything or write anything. */
export function useViewState(name, initial) {
  const arrival = useArrival()
  const [value, setValue] = useState(() => initialOf({ restore: currentArrival().restore, bag: viewBag() }, name, initial))
  // The component outlived its entry (a tab of the same section, the same kind
  // of page for another artist, the bar pressed again): for the entry it is
  // now on, it starts as a page that has just arrived there would.
  const seen = useRef(arrival.n)
  if (seen.current !== arrival.n) {
    seen.current = arrival.n
    const next = initialOf({ restore: arrival.restore, bag: viewBag() }, name, initial)
    if (!Object.is(next, value)) setValue(next)
  }
  const live = useRef(value)
  live.current = value
  useEffect(() => { saveView(name, value) }, [name, value])
  // The same page under another entry (a row selected, or deselected by Back):
  // that entry keeps what the page shows now.
  useEffect(() => onRoute(kind => { if (kind === 'soft') saveView(name, live.current) }), [name])
  const set = useCallback(next => setValue(next), [])
  return [value, set]
}

/** `useState` kept for the session (X2): the same on every arrival. For a
 *  queue's filter, which is the queue's and not a history entry's. */
export function useSessionState(name, initial) {
  const [value, setValue] = useState(() => sessionValue(name, initial))
  const set = useCallback(next => { keepForSession(name, next); setValue(next) }, [name])
  return [value, set]
}

// Scrollers the frame is putting, or has put, at a saved place.
const placed = new WeakSet()
// …and how to end the wait of one that is still waiting.
const waits = new WeakMap()
/** A list whose kept place no longer applies (X5: the selection is another
 *  than on the arrival the place was kept for): it is not put there any more,
 *  and no longer counts as having one. */
export function letGoOfPlace(el) {
  if (!el) return
  waits.get(el)?.()
  placed.delete(el)
}
/** Has this scroller a saved place on this arrival? A list that has one is
 *  not moved to its selected row (N1g). */
export const hasKeptPlace = el => !!el && placed.has(el)

const scrollersOf = body => [
  ['body', () => body],
  ['list', () => body.querySelector?.('.two .list') || null],
  ['detail', () => body.querySelector?.('.two .detail') || null],
]
const TOUCHES = ['wheel', 'touchstart', 'pointerdown']

// The wheel streams, heard for the whole app (W1): a page that has just
// arrived cannot know by itself that a stream was running before it was
// there. Heard at the window, before the event reaches a scroller.
let wheel = { began: -Infinity, last: -Infinity }
if (typeof window !== 'undefined') {
  window.addEventListener?.('wheel', () => { wheel = wheelStream(wheel, performance.now()) }, { capture: true, passive: true })
}

// Put one scroller at its place: at once when its content is tall enough, when
// it arrives otherwise (lib/view.js `scrollLook`). Returns a function that
// ends the wait.
function place(get, target, waiting) {
  let over = false, touched = false, el = null, timer = 0, watch = null, shownAt = 0
  const began = performance.now()
  const taken = () => { touched = true; look() }
  // The user using the page ends the wait — not input that was already under
  // way when the page arrived (W1): the wheel stream of the swipe that brought
  // it, a key held down, a modifier by itself. A press and a touch cannot have
  // begun before the page was there.
  const touch = event => {
    // (A wheel counts only when both hold: its stream began after the page
    // arrived, and it is mainly vertical — it could scroll this — W1b.)
    if (event?.type === 'wheel' && !(wheelIsUsers(wheel, began) && wheelScrolls(event))) return
    if (event?.type === 'keydown' && !keyIsUsers(event)) return
    taken()
  }
  const hold = now => {
    if (el) { waiting.delete(el); waits.delete(el); for (const t of TOUCHES) el.removeEventListener?.(t, touch) }
    el = now
    if (el) {
      waiting.add(el)
      placed.add(el)
      waits.set(el, stop)
      for (const t of TOUCHES) el.addEventListener?.(t, touch, { passive: true })
      watch?.observe(el)
    }
  }
  const stop = () => {
    if (over) return
    over = true
    clearInterval(timer)
    watch?.disconnect()
    window.removeEventListener?.('keydown', touch)
    hold(null)
  }
  function look() {
    if (over) return
    const now = get()
    if (now !== el) hold(now)
    const shown = !!el && el.clientHeight > 0
    if (shown && !shownAt) shownAt = performance.now()
    const step = scrollLook({
      target, shown, missing: !el, touched,
      max: shown ? el.scrollHeight - el.clientHeight : 0,
      waited: performance.now() - (shown ? shownAt : began),
    })
    if (step.act === 'wait') {
      // A box that is not laid out is told of by the observer; content that is
      // being fetched is looked at again a few times a second, for as long as
      // `scrollLook` allows.
      const poll = !(el && !shown && watch)
      if (poll && !timer) timer = setInterval(look, watch ? 80 : 250)
      if (!poll && timer) { clearInterval(timer); timer = 0 }
      return
    }
    if (step.act === 'set') {
      if (!(Math.abs(el.scrollTop - step.top) <= 1)) el.scrollTop = step.top
      // For what a press opens later (lib/reveal.js) this was not the user scrolling.
      forgetPane(el)
    }
    stop()
  }
  watch = typeof ResizeObserver === 'function' ? new ResizeObserver(() => look()) : null
  look()
  if (!over) window.addEventListener?.('keydown', touch)
  return stop
}

/** The page's scrollers keep their place per history entry: `.body`, and on a
 *  two-pane page `.two .list` and `.two .detail`. Saved as they are scrolled;
 *  restored on an arrival that restores (`useArrival`), before the first paint
 *  where the content is in hand. `tabKey`: the body is another element per tab. */
export function useKeptScroll(bodyRef, tabKey) {
  const arrival = useArrival()
  const waiting = useRef(new Set())
  // The bag of the entry this frame was last drawn for. A scroll event that
  // lands after the route has changed, before the page for it is drawn, is
  // the page before's (X6): it is written there, not into the new entry.
  const drawnFor = useRef(null)
  useLayoutEffect(() => { drawnFor.current = viewBag() })
  useEffect(() => {
    const body = bodyRef.current
    if (!body?.addEventListener) return undefined
    const which = el => scrollersOf(body).find(([, get]) => get() === el)?.[0] || ''
    const onScroll = e => {
      const el = e.target, name = which(el)
      // (Not while it waits for its place: a list that is not tall enough yet reads 0.)
      if (!name || waiting.current.has(el)) return
      const bag = drawnFor.current || viewBag()
      saveViewIn(bag, SCROLL[name], Math.round(el.scrollTop))
      if (name === 'detail' && bag === viewBag()) saveViewIn(bag, SCROLL.detailFor, location.hash)
    }
    body.addEventListener('scroll', onScroll, { capture: true, passive: true })
    // The same page under another entry: that entry's places are where the
    // page is now (a list that is hidden keeps the place it was given).
    const off = onRoute(kind => {
      if (kind !== 'soft') return
      for (const [name, get] of scrollersOf(body)) {
        const el = get()
        if (el && el.clientHeight > 0 && !waiting.current.has(el) && name !== 'detail') saveView(SCROLL[name], Math.round(el.scrollTop))
      }
    })
    return () => { body.removeEventListener('scroll', onScroll, { capture: true }); off() }
  }, [bodyRef, tabKey])
  const mounted = useRef(0)
  useLayoutEffect(() => {
    const body = bodyRef.current
    const first = mounted.current !== arrival.n && mounted.current === 0
    mounted.current = arrival.n
    if (!body) return undefined
    if (!arrival.restore) {
      // A fresh page is at its top — also where the frame outlived the entry
      // and its scrollers are the ones the page before was scrolled in.
      if (!first) for (const [, get] of scrollersOf(body)) { const el = get(); if (el && el.scrollTop > 0) el.scrollTop = 0 }
      return undefined
    }
    const targets = scrollTargets(viewBag(), location.hash)
    const stops = []
    for (const [name, get] of scrollersOf(body)) {
      if (targets[name] !== null) stops.push(place(get, targets[name], waiting.current))
    }
    return () => { for (const stop of stops) stop() }
  }, [bodyRef, tabKey, arrival.n, arrival.restore])
}
