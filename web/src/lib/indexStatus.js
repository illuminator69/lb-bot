// What lb-bot's index says of a release — the `status` of a discography row —
// as the web UI reads it. One place, because the index has FIVE words and the
// pages knew four (B-091).
//
// A scan produces `complete`, `incomplete`, `untagged` and `missing`. The
// fifth, `present`, no scan ever produces: lb-bot filled or placed the album
// and has not scanned its artist since (`_index_mark_release_present`). It is
// in the library. Read as its own word it had no chip, was in no group of its
// artist's page, and was not "in the library" to its own page — on exactly
// the albums a wrong edition shows up on, a fresh fill.
//
// Pure, with its matrix: indexStatus.test.mjs.

const SCANNED = new Set(['complete', 'incomplete', 'untagged', 'missing'])

/** The state a page shows for a row — its chip, its group: one of the four a
 *  scan produces, '' for a word lb-bot did not say. `present` reads `complete`. */
export function albumState(status) {
  if (status === 'present') return 'complete'
  return SCANNED.has(status) ? status : ''
}

/** The library holds the album: everything but `missing`. */
export const inLibrary = status => status === 'present' || (SCANNED.has(status) && status !== 'missing')

/** Whether the row's own `present` / `total` were measured for the status it
 *  has. Not for `present`: its numbers are the last scan's, from before the
 *  fill ("nothing here knows the real numbers"), so "1/9" beside "In library"
 *  would be said of an album that has nine. */
export const countsMeasured = status => status !== 'present' && inLibrary(status)
