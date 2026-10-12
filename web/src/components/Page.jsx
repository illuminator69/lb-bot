import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { navigate } from '../App.jsx'
import { backTarget, goBack, setEntryTitle } from '../lib/router.js'
import { fluidMove } from '../lib/motion.js'
import { reveal } from '../lib/reveal.js'
import { subLine } from '../lib/frame.js'
import { useKeptScroll } from '../lib/useView.js'

// The one page frame: a top bar with Back and the title, tabs, one filter
// row, then the body. Every screen renders exactly one of these.
//
//   tabs      [[key, label, count?], …] with `tab` and `onTab`
//   bar       the filter row (one row: a switch, a text filter, menus)
//   flush     the body is two panes that scroll on their own (.two)
//   paneBack  { label, onBack } — on a narrow screen with a detail pane open,
//             Back closes the pane instead of leaving the page
//   fallbackBack { label, onBack } — where Back goes when this page was
//             opened directly and there is no page to return to
//   crumb     shown in the bar instead of the title, for a page that carries
//             its own title in its body; `title` still names it for Back
export default function Page({
  title, sub, crumb, action, tabs, tab, onTab, bar, flush, paneBack, fallbackBack, children,
}) {
  useEffect(() => { if (title) setEntryTitle(title) }, [title])
  const target = backTarget()
  // A page that gives a second line holds its height from its first frame
  // (N3): '' is "nothing to say yet", not given is "no such line".
  const second = subLine(sub)
  // The page's scrollers keep their place per history entry (N1).
  const body = useRef(null)
  useKeptScroll(body, tab || '')
  const back = paneBack
    ? <button className="back" onClick={paneBack.onBack}>← {paneBack.label}</button>
    : target
      ? <button className="back" onClick={goBack}>← {target.title}</button>
      : fallbackBack
        ? <button className="back" onClick={fallbackBack.onBack}>← {fallbackBack.label}</button>
        : null
  return (
    <>
      <div className="top">
        {back}
        {crumb ? (
          // The title is in the page itself (an album beside its cover); the
          // bar only says what kind of page this is.
          <div className="grow sub clip">{crumb}</div>
        ) : (
          <div className="grow">
            <h1 className="clip">{title}</h1>
            {second !== null ? <div className="sub clip">{second}</div> : null}
          </div>
        )}
        {/* With a detail pane open on a narrow screen the bar is the pane's:
            Back and the title, nothing else. */}
        {!paneBack && <button className="phone-only" aria-label="Search" onClick={() => navigate('search')}>Search</button>}
        {!paneBack && action}
      </div>
      {tabs && (
        <div className="tabs" role="tablist">
          {tabs.map(([k, label, count]) => (
            <button key={k} role="tab" aria-selected={tab === k} onClick={() => tab !== k && onTab(k)}>
              {label}{count != null && count !== '' ? <span className="n">{typeof count === 'number' ? count.toLocaleString() : count}</span> : null}
            </button>
          ))}
        </div>
      )}
      {bar && !paneBack ? <div className="bar" key={`bar-${tab || ''}`}>{bar}</div> : null}
      <div ref={body} className={`body${flush ? ' flush' : ''}`} key={`body-${tab || ''}`}>{children}</div>
    </>
  )
}

/** Segmented switch for the one main split of a list. */
export function Seg({ value, onChange, options, label }) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map(([k, l]) => (
        <button key={k} aria-pressed={value === k} onClick={() => value !== k && onChange(k)}>{l}</button>
      ))}
    </div>
  )
}

/** A compact menu in the filter row. options: [[value, label], …] */
export function Pick({ value, onChange, options, label }) {
  return (
    <select aria-label={label} title={label} value={value} onChange={e => onChange(e.target.value)}>
      {options.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
    </select>
  )
}

/** Dot + word. tone: '' | 'wait' | 'ok' | 'bad' | 'run' */
export function St({ tone = '', children, className = '' }) {
  return <span className={`st ${tone} ${className}`}>{children}</span>
}

export function Meter({ value, lg }) {
  const pct = Math.max(0, Math.min(100, Number(value) || 0))
  return (
    <div className={`meter${lg ? ' lg' : ''}`} role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}>
      <i style={{ width: `${pct}%` }} />
    </div>
  )
}

/** Section label inside a body: uppercase, with a count and a right-hand slot. */
export function Grp({ children, n, right }) {
  return (
    <div className="grp">
      {children}
      {n != null && n !== '' ? <span className="num">{typeof n === 'number' ? n.toLocaleString() : n}</span> : null}
      {right ? <span className="x">{right}</span> : null}
    </div>
  )
}

// Properties whose own transition, somewhere inside, already moves a height.
const MOVES_HEIGHT = new Set(['grid-template-rows', 'height', 'max-height'])

/** Content whose height changes while it is on the page — a skeleton replaced
 *  by its answer, a grid that gains a row, a note that appears — moves to its
 *  new height instead of jumping there, in `Roll`'s curve (and its time, or a
 *  little longer for a tall change: `moveMs` in lib/motion.js). `Roll` folds open and shut;
 *  this is for what changes inside something open.
 *
 *  The content's own height is watched (a `ResizeObserver`, which reports
 *  after layout and before the frame is painted): on a change the box is held
 *  at the height it had and let go to the new one. When the change is a `Roll`
 *  (or anything else) inside it moving by itself, that motion is the
 *  transition and the box simply follows — unless the box is itself on its
 *  way somewhere: then it is aimed again at where the content is now, within
 *  the time its move has left (letting go mid-way is a jump, and so is being
 *  let go at the move's end under content that grew past it). A second change
 *  of the content itself while it moves starts again from where the box is. Under
 *  `prefers-reduced-motion`, and where there is no `ResizeObserver`, it is a
 *  plain box. */
export function Fluid({ children, className = '' }) {
  const outer = useRef(null)
  const inner = useRef(null)
  useLayoutEffect(() => {
    const box = outer.current
    const content = inner.current
    if (!box || !content || typeof ResizeObserver === 'undefined') return undefined
    let last = content.offsetHeight
    let timer = 0
    let target = 0        // where the box is heading, while it holds a height of its own
    let endAt = 0         // when the move it is on was to end
    const rest = () => {
      clearTimeout(timer)
      target = 0
      endAt = 0
      box.style.height = ''
      box.style.transitionDuration = ''
      box.classList.remove('moving')
    }
    const seen = new ResizeObserver(() => {
      const next = content.offsetHeight
      if (next === last) return
      // Where it is now: on its way somewhere (its own transition), or at rest.
      const moving = !!box.style.height
      const from = moving ? box.getBoundingClientRect().height : last
      last = next
      const calm = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
      const itself = (content.getAnimations?.({ subtree: true }) || [])
        .some(a => MOVES_HEIGHT.has(a.transitionProperty) && a.playState !== 'finished')
      // Decided in lib/motion.js (`fluidMove`, with its test): something inside
      // folding by itself is followed at rest; a box that is on its way is
      // aimed again at where the content is now, for the time it has left; a
      // tall change takes a little longer.
      const plan = fluidMove({ moving, from, next, target, left: endAt - performance.now(), calm, itself })
      if (plan.act === 'leave') return
      if (plan.act === 'rest') { rest(); return }
      const { ms } = plan
      // (`keep`: the end the move was given stands — it is not a new move.)
      if (!plan.keep) endAt = performance.now() + ms
      target = next
      box.classList.add('moving')
      box.style.transitionDuration = `${ms}ms`
      box.style.height = `${from}px`
      void box.offsetHeight            // the browser has to see where it starts from
      box.style.height = `${next}px`
      // A transition that never runs (the page is hidden) ends nothing: the
      // box is let go all the same, so it is never left at a height it was given.
      clearTimeout(timer)
      timer = setTimeout(rest, ms + 120)
    })
    seen.observe(content)
    // Only a move that ran to its end lets the box go. A move that was
    // replaced by the next one ends with `transitioncancel` — acting on that
    // let go of the box in the middle of the new move: a jump.
    const ended = e => { if (e.target === box && e.propertyName === 'height') rest() }
    box.addEventListener('transitionend', ended)
    return () => {
      clearTimeout(timer)
      seen.disconnect()
      box.removeEventListener('transitionend', ended)
    }
  }, [])
  return (
    <div ref={outer} className={`fluid${className ? ` ${className}` : ''}`}>
      <div ref={inner}>{children}</div>
    </div>
  )
}

/** "words · action" in running text. A separator starts no line and ends
 *  none: the last word before the dot, the dot and what follows wrap as one
 *  piece (V9, W4). `last` stands in for that last word where it is not text
 *  (a skeleton for a name still on its way). */
export function Dot({ after = '', last = null, children }) {
  const text = String(after)
  const at = last ? text.length : text.lastIndexOf(' ')
  return (
    <>
      {last ? `${text} ` : text.slice(0, at + 1)}
      <span style={{ whiteSpace: 'nowrap' }}>{last || text.slice(at + 1)}{' · '}{children}</span>
    </>
  )
}

/** What a press opens comes into view (lib/reveal.js). `tokens` names what is
 *  open; when one appears that was not there, `box` is brought into view, with
 *  `anchor` — the thing pressed — kept in the top part of it. Never on
 *  arrival, and never for something that only shut. A token that begins with
 *  `~` is an ANSWER (a preview that arrived, a refusal, a job's card): it
 *  comes some time after the press, and is not revealed when the user has
 *  scrolled the pane since — they went somewhere, and are not pulled back. */
export function useRevealOn(box, tokens, anchor) {
  const had = useRef(null)
  const key = tokens.join('|')
  useEffect(() => {
    const before = had.current
    had.current = tokens
    if (before === null || !box.current) return undefined
    const fresh = tokens.filter(t => !before.includes(t))
    if (!fresh.length) return undefined
    return reveal(box.current, { anchor: anchor?.current || undefined, late: fresh.every(t => t.startsWith('~')) })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])
}

/** Content that folds open and shut instead of popping in and out of the page.
 *  It must already be on the page, shut, for opening to animate: mount it
 *  where the content will appear and flip `open`. While it folds shut it keeps
 *  showing what it last held. `slow`: for a fold taller than about a third of
 *  a screen (the search with its eight rows, 530 px) — 450 ms instead of 300,
 *  so that its fastest frame is 55 px and not 83. */
export function Roll({ open, slow = false, children }) {
  const kept = useRef(children)
  if (open) kept.current = children
  const [linger, setLinger] = useState(open)
  useEffect(() => {
    if (open) { setLinger(true); return }
    const t = setTimeout(() => setLinger(false), slow ? 470 : 320)
    return () => clearTimeout(t)
  }, [open, slow])
  return (
    <div className={`roll${slow ? ' slow' : ''}${open ? '' : ' shut'}`}>
      <div inert={!open}>{open ? children : linger ? kept.current : null}</div>
    </div>
  )
}
