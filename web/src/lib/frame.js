// What the frame holds in place (B-094). Pure: the numbers and the decisions a
// page's layout goes by, each with its case in frame.test.mjs. The pages and
// the stylesheet do the drawing; nothing here measures anything.

// ── A hero's text beside something else (N4) ────────────────────────────────
// `.hero` is a wrapping row: the cover, the text (`.grow`), and on a gap's
// detail a third thing, the pager. `.grow` is `flex: 1; min-width: 0` — a
// basis of nothing — so wherever the pager just fitted beside it the text got
// what was left: 18 px on a 412 px phone, a title of fourteen lines. The text
// column has a floor: that is its basis, and a wrapping row then puts the
// pager on a row of its own wherever it cannot give the text its floor beside
// it. By the pane's width, not the window's (a media query would go by the
// window, and a two-pane page squeezes it at 1,100 px just as well).
export const HERO_GAP = 16          // `.hero { gap }` in index.css (the test holds the two together)
export const HERO_TEXT_FLOOR = 160  // px

/** The text column's `flex-basis`, beside a cover `cover` px wide: the floor,
 *  but never more than the row has beside the cover (less one pixel, against
 *  rounding) — the cover and the text stay side by side however narrow. */
export const heroTextBasis = cover => `min(${HERO_TEXT_FLOOR}px, calc(100% - ${cover + HERO_GAP + 1}px))`

/** How the wrapping row lays a hero out at `width`: the text column's width,
 *  whether what is beside it (`aside` px wide, 0 for none) went below, and
 *  whether the text went under the cover (it must not). The stylesheet's
 *  arithmetic, written out so that the floor's consequences can be checked. */
export function heroRow({ width, cover, aside = 0 }) {
  const basis = Math.min(HERO_TEXT_FLOOR, width - (cover + HERO_GAP + 1))
  const textBelow = cover + HERO_GAP + Math.max(0, basis) > width
  const beside = aside > 0 && cover + HERO_GAP + basis + HERO_GAP + aside <= width
  return { text: width - cover - HERO_GAP - (beside ? HERO_GAP + aside : 0), asideBelow: aside > 0 && !beside, textBelow }
}

// ── Nothing above a page's body changes height after its first frame (N3) ───
// A line that is drawn only once its text is in makes the head taller when
// the text arrives, and the body under it moves.

export const NBSP = '\u00a0'

/** The frame's second title line. `null`: the page gives none (`sub` is not
 *  given). Given and empty — the page has nothing to say yet, or just now —
 *  it is a line all the same, one line high, so that the head is as tall in
 *  its first frame as it will be. */
export function subLine(sub) {
  if (sub === undefined || sub === null) return null
  return typeof sub === 'string' && !sub.trim() ? NBSP : sub
}

/** Library › Artists' "N of M indexed" (`GET /api/library-index/status`).
 *  `known`: lb-bot's answer is in hand — this visit's, or the session's last,
 *  which a later visit draws at once while it reads again behind it. Until
 *  then the line's place is held by a stand-in its own size: the line as it
 *  will most likely read, counted from what the page already has (`guess`,
 *  its artist list's length — as many digits, in a font whose digits are one
 *  width). Which count the line shows is lb-bot's, as before. */
export function indexLineView(status, guess) {
  const indexed = status?.artistsIndexed, total = status?.artistsTotal
  if (Number.isFinite(indexed) && Number.isFinite(total)) {
    return {
      known: true, counts: `${indexed.toLocaleString()} of ${total.toLocaleString()}`,
      tail: `${status.artistsStale ? ` · ${status.artistsStale} stale` : ''}${status.building ? ' · building…' : ''}`,
    }
  }
  const n = (Number.isFinite(guess) && guess > 0 ? guess : 1000).toLocaleString()
  return { known: false, counts: `${n} of ${n}`, tail: '' }
}

/** Whether the line's place stands. It folds away only when the read failed
 *  with nothing kept: a place held for a line that will not come is a hole. */
export const indexLineStands = ({ status, failed }) => !!status || !failed
