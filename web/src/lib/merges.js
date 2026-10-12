// What lb-bot's own web UI may say about an artist name merge, and about the
// list of everything lb-bot changed (I-029, U3): the one place that decides.
// Pure, with its matrix: merges.test.mjs. The rules behind it are in CLAUDE.md
// § Artist name merge and § `GET /api/overrides` — `merges` and `siblings`.
//
// What lb-bot answers:
//   GET /api/overrides            siblings [{mbid, artists:[{ndArtistId, name, albums, collab}]}] — the MERGE
//                                 SETS: two or more library artists that can be merged into one another, the
//                                 one with the most albums first. An artist id is in at most one set, and two
//                                 sets may carry one `mbid`: a set is found by the artist, never by the id.
//                                 merges [MergeRow], albums [OverrideRow], artists [{ndArtistId, mbid, name,
//                                 tagMbid, setAt}]. Each list is ABSENT when lb-bot could not work it out —
//                                 "no list", never "none".
//   GET /api/artist/merge/preview {into, from, mbid, albums, files, sample:[{album, tracks}]} (≤ 10 albums)
//   POST /api/artist/merge        {nd_id (the name that is KEPT), from_nd_id} → {ok, id, state: "merging"}
//   GET /api/artist/merge?id=     MergeRow {id, state: merging|merged|failed|undoing, into, from, mbid, done,
//                                 total, failures:[{album, title, reason}], setAt}; 404 `no_merge`
//   POST /api/artist/merge/undo   {id} → {ok, id, state: "undoing"}

import { NOT_KNOWN, mbArtistUrl, scanToast, scanView } from './artistMatch.js'
import { iso } from './text.js'
import {
  OVERRIDES_TAB, STILL_WORKING, agoText, endToast, jobStart, jobStep, jobView, mergeOpOfState, opOfState, rowGroup, writesOver,
} from './overrides.js'

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`
const NUMBER_WORDS = ['No', 'One', 'Two', 'Three', 'Four', 'Five', 'Six']

// ── the merge sets ───────────────────────────────────────────────────────────

/** The set a library artist can be merged within, or null: no set holds it,
 *  lb-bot gave no list of sets, or nobody is left in it to merge with (an
 *  entry lb-bot marks as a collaboration credit is never offered). */
export function setOf(overrides, artistId) {
  if (!overrides || !Array.isArray(overrides.siblings)) return null
  for (const set of overrides.siblings) {
    const artists = (set.artists || []).filter(a => !a.collab)
    if (artists.length > 1 && artists.some(a => a.ndArtistId === artistId)) return { mbid: set.mbid || '', artists }
  }
  return null
}

/** The name that is kept unless the user says otherwise: the one with the
 *  most albums — lb-bot lists it first. */
export const keptDefault = set => set?.artists?.[0]?.ndArtistId || ''

/** The spellings that can be merged into the kept one: the route takes one
 *  pair at a time, so in a set of three the user says which. */
export const mergeFrom = (set, keptId) => (set?.artists || []).filter(a => a.ndArtistId !== keptId)

export const mergeBody = (keptId, fromId) => ({ nd_id: keptId, from_nd_id: fromId })

// ── the preview ──────────────────────────────────────────────────────────────

/** The one fixed sentence above "Merge": what lb-bot is about to do. */
export const FIXED_SENTENCE = files =>
  `lb-bot rewrites the artist tags of ${plural(files, 'file', 'files')} and keeps the previous ones, so this can be undone. Folders are not renamed.`

/** A merge preview as the page shows it. `can`: there is something to merge. */
export function mergePreviewView(preview) {
  const files = Number(preview?.files) || 0
  const albums = Number(preview?.albums) || 0
  // A name in a sentence is isolated (lib/text.js): a right-to-left one must not reorder the words beside it.
  const into = preview?.into?.name ? iso(preview.into.name) : 'the kept name'
  const from = preview?.from?.name ? iso(preview.from.name) : 'that spelling'
  const sample = (Array.isArray(preview?.sample) ? preview.sample : [])
    .map((x, i) => ({ key: String(i), title: String(x?.album || ''), sub: plural(Number(x?.tracks) || 0, 'track', 'tracks') }))
  if (!files) {
    return { can: false, line: `No file says ${from}: there is nothing to merge into ${into}.`, sample: [], more: '', fixed: '', button: 'Merge' }
  }
  const rest = albums - sample.length
  return {
    can: true,
    line: `${plural(files, 'file', 'files')} in ${plural(albums, 'album', 'albums')} will say ${into}`,
    sample,
    more: rest > 0 ? `…and ${plural(rest, 'more album', 'more albums')}` : '',
    fixed: FIXED_SENTENCE(files),
    button: 'Merge',
  }
}

// ── the job ──────────────────────────────────────────────────────────────────
const WAITING = 'Waiting for Navidrome to read the albums again…'

const problemsOf = row => (Array.isArray(row?.failures) ? row.failures : [])
  .map(f => ({ title: [f?.album, f?.title].filter(Boolean).map(iso).join(' · '), reason: String(f?.reason || '') }))

function view(over) {
  return {
    tone: '', word: '', line: '', bar: false, percent: 0, count: '',
    problems: [], problemsHeading: '', ended: false, canUndo: false, undoLabel: 'Undo', seeOverrides: false,
    ...over,
  }
}

/** A merge job in words, from its row — the same shape as an album job's
 *  (`jobView`), so one card draws both. The bar is the whole job's. */
export function mergeView(op, phase, row) {
  const done = Number(row?.done) || 0
  const total = Number(row?.total) || 0
  const problems = problemsOf(row)
  const problemsHeading = problems.length ? plural(problems.length, 'problem', 'problems') : ''
  const undo = op === 'unmerge'
  if (phase === 'timeout') return view({ tone: 'wait', word: 'Still working', line: STILL_WORKING, ended: true, seeOverrides: true })
  if (phase === 'gone') return view({ word: 'No longer there', line: 'lb-bot no longer has this merge.', ended: true })
  if (phase === 'polling') {
    const running = undo ? { word: 'Restoring', text: 'Putting the previous tags back…' }
      : { word: 'Merging', text: 'Rewriting artist tags…' }
    if (writesOver(row)) return view({ tone: 'run', word: 'Waiting for Navidrome', line: WAITING })
    if (row && total > 0) {
      const count = `${done} of ${total}`
      return view({ tone: 'run', word: running.word, line: `${running.text} ${count}`, bar: true,
                    percent: Math.max(0, Math.min(100, Math.round((done / total) * 100))), count })
    }
    return view({ tone: 'run', word: running.word, line: row ? 'Working…' : running.text })
  }
  if (undo) {
    if (phase === 'settled') return view({ tone: 'ok', word: 'Restored', line: 'Restored: the two names are apart again.', ended: true })
    return view({ tone: 'bad', word: 'Not restored', line: 'lb-bot could not put everything back.',
                  problems, problemsHeading, ended: true, canUndo: true, undoLabel: 'Undo again' })
  }
  if (phase === 'settled') {
    const line = done > 0 ? `lb-bot rewrote the artist tags of ${plural(done, 'file', 'files')}.`
      : total > 0 ? 'lb-bot rewrote no file.'
      : 'Nothing was rewritten.'
    return view({ tone: problems.length ? 'wait' : 'ok', word: 'Merged', line, problems, problemsHeading, ended: true, canUndo: true })
  }
  return view({
    tone: 'bad', word: 'Did not finish', problems, problemsHeading, ended: true, canUndo: true,
    line: total > 0 ? `lb-bot did not finish the merge (${done} of ${plural(total, 'file', 'files')} rewritten).`
      : 'lb-bot did not merge these names.',
  })
}

/** A merge row nobody is following: null when it is simply merged, else what
 *  there is to say (and null for a state this page does not know). */
export function mergeRest(row) {
  if (!row) return null
  const running = mergeOpOfState(row.state)
  if (running) return mergeView(running, 'polling', row)
  if (row.state === 'failed') {
    // The row does not say whether it was a merge or an undo that failed.
    return { ...mergeView('merge', 'failed', row), line: 'A merge of these names did not finish.' }
  }
  if (row.state !== 'merged') return null
  return problemsOf(row).length ? mergeView('merge', 'settled', row) : null
}

/** lb-bot is — as far as this page knows — still merging these names (or
 *  taking a merge of them back): by the list last read (`rows`: its merges of
 *  these names, one of them in a running state), or by the job followed here,
 *  running or gone silent while it ran (the three-minute give-up is no end).
 *  While it is so, nothing is offered for these names — after the give-up as
 *  on arrival: lb-bot would refuse it (`in_progress`). */
export function mergeQuiet(rows, job) {
  if (job && (job.running || job.phase === 'timeout')) return true
  return (rows || []).some(r => !!mergeOpOfState(r?.state))
}

/** lb-bot's merges of these names, as far as they can be believed NOW: a row
 *  that says a merge is running, for a job this page has itself seen end, is
 *  out of a list older than that end — kept through a re-read that failed —
 *  and is left out: it neither holds the block quiet nor draws a running card.
 *  `seenEnd`: the ids of the jobs seen to end since the list was read. */
export function mergeRowsNow(rows, seenEnd) {
  return (rows || []).filter(r => !(seenEnd?.has?.(r?.id) && mergeOpOfState(r?.state)))
}

/** How a merge names itself where two names fit: "From → Into". */
export const mergeLabel = row => (row?.from?.name && row?.into?.name ? `${iso(row.from.name)} → ${iso(row.into.name)}` : 'The merge')

/** The one toast for a merge that ended with nobody showing it, or null. */
export function mergeToast(op, phase, row) {
  if (phase === 'polling') return null
  const v = mergeView(op, phase, row)
  const name = mergeLabel(row)
  if (phase === 'timeout') return { text: `${name}: still working — see ${OVERRIDES_TAB}`, level: 'info' }
  if (phase === 'settled' && op === 'unmerge') return { text: `${name}: restored — the two names are apart again.`, level: 'info' }
  const more = phase === 'settled' && v.problemsHeading ? ` ${v.problemsHeading} — see ${OVERRIDES_TAB}.` : ''
  return { text: `${name}: ${v.line}${more}`, level: (v.tone === 'ok' || phase === 'gone') && !more ? 'info' : 'error' }
}

/** A merge as a job of the one watch (lib/jobWatch.js, kind `merge`): by the
 *  merge row's id, the X-rules as for an album job (`jobStep`). */
export const mergeMachine = {
  start: (op, now, seed) => jobStart(op, now, seed),
  step: jobStep,
  running: state => state.phase === 'polling',
  sig: state => [state.op, state.phase, state.row],
  opOf: row => mergeOpOfState(row?.state),
  restKey: (state, row) => ((state.phase === 'settled' || state.phase === 'failed') && row ? `${row.setAt}@${state.op}` : ''),
  toast: (state, row) => mergeToast(state.op, state.phase, state.row || row),
}

/** "Still working — see System › Overrides" in three parts, so that a surface
 *  can make the tab's name the way there; null for any other sentence. */
export function splitStillWorking(line) {
  const text = String(line || '')
  const at = text.indexOf(OVERRIDES_TAB)
  if (at < 0) return null
  return { before: text.slice(0, at), link: OVERRIDES_TAB, after: text.slice(at + OVERRIDES_TAB.length) }
}

/** Where the artist page goes once a merge has ended. The page of the
 *  spelling that is gone moves to the kept artist's (`go`, with a toast);
 *  every other stays and reads again. Only for a merge that settled with every
 *  file rewritten: with problems the old spelling is still an artist. */
export function afterMerge(pageArtistId, job) {
  const stay = { go: '', toast: '' }
  const row = job?.row
  if (!row || job.op !== 'merge' || job.phase !== 'settled' || problemsOf(row).length) return stay
  if (!pageArtistId || pageArtistId !== row.from?.ndArtistId || !row.into?.ndArtistId) return stay
  return { go: row.into.ndArtistId, toast: `${iso(row.from.name)} is now ${iso(row.into.name)}.` }
}

// ── Needs you › To fix: "One artist, two names" ──────────────────────────────

/** The sets listed under To fix on this visit, after another answer of `GET
 *  /api/overrides`: each stays in its place until the tab is left, `moved` —
 *  merged — once a list lb-bot GAVE no longer holds it. An answer without
 *  `siblings` is no list: nothing is called merged by it. A set is known by
 *  the artist lb-bot lists first in it (the one with the most albums). */
export function keepSets(prev, overrides) {
  if (!overrides || !Array.isArray(overrides.siblings)) return prev
  const now = new Map()
  for (const raw of overrides.siblings) {
    const set = setOf({ siblings: [raw] }, raw.artists?.[0]?.ndArtistId)
    if (set) now.set(`two:${set.artists[0].ndArtistId}`, set)
  }
  const kept = prev.map(i => (now.has(i.key) ? { ...i, set: now.get(i.key), moved: false } : { ...i, moved: true }))
  const had = new Set(kept.map(i => i.key))
  return [...kept, ...[...now].filter(([key]) => !had.has(key)).map(([key, set]) => ({ key, kind: 'two', set, moved: false }))]
}

/** What the To fix list says when lb-bot did not give its lists — the artists
 *  to match (`toCheck`) or the merge sets (`siblings`) left out of the answer,
 *  or no answer at all: that it could not be worked out, never that nothing
 *  needs a decision (which is a claim about a list nobody gave). It says what
 *  is missing (M2): the artists to match, or both, are `NOT_KNOWN.nolist`; the
 *  sets alone, `NOT_KNOWN.nosets`. '' when both were given — also by an answer
 *  read earlier, kept through a re-read that failed — and where the skeleton
 *  stands (`toFixWaiting`). `drawn`: the page draws one of the two groups —
 *  the note then stays as it is through a read, and is replaced only by that
 *  read's answer. `state`: the read's own, { status, data, reading }. */
export function toFixNote(state, drawn = false) {
  // Where the skeleton stands, nothing is said beside it. Everywhere else the
  // note is the last answer's, and stays as it is through a read (F3).
  if (!state || toFixWaiting(state, drawn)) return ''
  const data = state.data
  if (!data) return state.status === 'error' ? NOT_KNOWN.nolist : ''
  // …and it says what is missing: the artists to match (or both lists), or the sets alone.
  if (!Array.isArray(data.toCheck)) return NOT_KNOWN.nolist
  return Array.isArray(data.siblings) ? '' : NOT_KNOWN.nosets
}

/** A skeleton stands where the two groups ("Artists to match", "One artist,
 *  two names") will be — so that what is under them does not jump when the
 *  answer comes — only while a read of lb-bot's lists is out, the lists are
 *  not both held, AND the page draws neither group (`drawn`): the first read,
 *  and a Try again from a page that has neither. In every other case a read
 *  that is out moves nothing (F3): the page keeps the rows it has through an
 *  answer that left a list out, and a skeleton under groups that are on the
 *  page — with the note taken away — moved "Left on disk" down and back for
 *  the length of a read nobody asked for. A function of what is drawn and
 *  whether a read is out: nobody has to remember who asked.
 *  `state`: the read's own, { status, data, reading }. */
export function toFixWaiting(state, drawn = false) {
  if (!state || drawn) return false
  const data = state.data
  if (data && Array.isArray(data.toCheck) && Array.isArray(data.siblings)) return false
  return state.reading === true || state.status === 'loading' || state.status === 'idle'
}

/** A set's row in the list. */
export function setRowView(set, moved) {
  const artists = set?.artists || []
  const title = artists[0]?.name || ''
  if (moved) return { title, sub: 'Merged', tone: 'ok', word: 'Merged' }
  const albums = artists.reduce((n, a) => n + (Number(a.albums) || 0), 0)
  return {
    title,
    // The title is an element of its own; the other names share a line with a count.
    sub: `also ${artists.slice(1).map(a => iso(a.name)).join(', ')} · ${plural(albums, 'album', 'albums')}`,
    tone: 'wait',
    word: `${NUMBER_WORDS[artists.length] || artists.length} names`,
  }
}

// ── System › Overrides ───────────────────────────────────────────────────────

/** Everything lb-bot changed, in four groups, each from its own list of `GET
 *  /api/overrides`. `missing`: lb-bot left that list out — it "could not be
 *  read", which is not "there is none". [] for no answer at all. */
export function overridesGroups(overrides) {
  if (!overrides) return []
  const albums = Array.isArray(overrides.albums) ? overrides.albums : null
  const of = (key, title, kind, list) => ({ key, title, kind, rows: list || [], missing: list === null })
  return [
    of('editions', 'Album editions', 'album', albums && albums.filter(r => rowGroup(r) === 'editions')),
    of('covers', 'Covers', 'album', albums && albums.filter(r => rowGroup(r) === 'covers')),
    of('artists', 'Artist matches', 'artist', Array.isArray(overrides.artists) ? overrides.artists : null),
    of('merges', 'Name merges', 'merge', Array.isArray(overrides.merges) ? overrides.merges : null),
  ]
}

/** The page a row opens, or null for a plain row. Only by an id that is live
 *  NOW: an album by its own answer (`live.albums[row.id]` — `{albumId, rgid}`
 *  as lb-bot describes the album at this moment, null when Navidrome no
 *  longer lists the id the row carries, absent while not asked), an artist
 *  and a merge by the artist list (`live.artists`, a Set of Navidrome ids).
 *  A row carrying an id says nothing: a retag gave the album another one. */
export function rowOpens(kind, row, live) {
  if (kind === 'album') {
    const album = live?.albums?.[row?.id]
    return album && album.rgid ? { tab: 'album', params: ['-', album.rgid] } : null
  }
  const id = kind === 'merge' ? row?.into?.ndArtistId : row?.ndArtistId
  return id && live?.artists?.has?.(id) ? { tab: 'artist', params: [id] } : null
}

/** A separator neither starts a line nor ends one: " · " and " → " hold on to
 *  the words on both sides, so a long title wraps between its other words. */
export const nb = text => String(text || '').replace(/ ([·→]) /g, '\u00a0$1\u00a0')

/** What a row of the list is known by: an album change and a merge by the
 *  row's own id, an artist match by the artist's. */
export const rowKey = (kind, row) => `${kind}:${kind === 'artist' ? row?.ndArtistId : row?.id}`

// A row nobody is following, as the list says it: the state word, and lb-bot's
// own lines behind it. No sentence — the group's heading says what the row is.
const rested = (tone, word, problems) => view({
  tone: problems.length && tone === 'ok' ? 'wait' : tone, word, problems,
  problemsHeading: problems.length ? plural(problems.length, 'problem', 'problems') : '', ended: true,
})
// "Still working — see System › Overrides" is said elsewhere; on that very tab
// there is no "there" to point at, and the word says the rest.
const here = v => (v.seeOverrides ? { ...v, line: '' } : v)

/** A row lb-bot lists no more — said only by an answer that GAVE the row's
 *  own list (`albums` for an album, `merges` for a merge, `artists` for an
 *  artist match) and does not hold the row (F4). An answer that left that
 *  list out says nothing of the row: absent is not gone. (A held row whose
 *  undo went silent is let go of as restored by this, and by nothing less.) */
export function listedNoMore(overrides, kind, row) {
  const list = kind === 'album' ? overrides?.albums : kind === 'merge' ? overrides?.merges : kind === 'artist' ? overrides?.artists : null
  if (!Array.isArray(list)) return false
  const key = rowKey(kind, row)
  return !list.some(r => rowKey(kind, r) === key)
}

/** The library artists a change touches, by Navidrome id: whose discography,
 *  kept for the session by the artist page, is let go of when the change is
 *  answered or seen to end somewhere else (F1) — an artist match's own
 *  artist, both names of a merge. null: the row does not say (an album's row
 *  names its artist in words) — then everything kept is let go of: a page
 *  that reads once more is right, one that shows what it kept is not. */
export function artistsTouched(kind, row) {
  if (kind === 'artist') return row?.ndArtistId ? [row.ndArtistId] : null
  if (kind === 'merge') {
    const ids = [row?.into?.ndArtistId, row?.from?.ndArtistId]
    return ids.every(Boolean) ? ids : null
  }
  return null
}

/** A row of System › Overrides in words.
 *    kind   'album' | 'merge' | 'artist'
 *    job    the watch's job for the row when one is followed: an album job, a
 *           merge, or — for an artist match being removed — the scan after it
 *    mb     for an artist match, what MusicBrainz calls the artist: { name }
 *           once read ('' — it could not be had), null while it is asked for,
 *           undefined when nobody asks
 *  → { title, to, sub, link, v, undo }: `to` — an artist match's other half,
 *    { text, naming, href }; `link` — the release on MusicBrainz; `v` — the
 *    state (word, tone, the job's sentence and its bar, lb-bot's lines), null
 *    when there is none to speak of; `undo` — { label, says } when it can be
 *    asked: the button's word, and what a screen reader is told it is for
 *    (every row's button says "Undo"). */
export function listRowView(kind, row, job, nowSec, mb) {
  const when = agoText(row?.setAt, nowSec)
  if (kind === 'artist') {
    const name = row?.name || ''
    const sv = job ? scanView(job.state, { name }) : null
    return {
      // The two names share a line, an arrow between them.
      title: name ? iso(name) : 'An artist',
      to: { text: mb?.name ? iso(mb.name) : 'its MusicBrainz artist', naming: mb === null, href: mbArtistUrl(row?.mbid || '') },
      sub: when,
      link: null,
      // The scan's bar is unmeasured until lb-bot has said how many there are: it says "some".
      v: sv && view({ tone: sv.tone, word: sv.word, line: sv.line, bar: sv.bar, percent: sv.percent == null ? 4 : sv.percent, ended: sv.ended }),
      // Removed already: the row is only waiting for the scan that follows.
      undo: job ? null : { label: 'Remove', says: `Remove ${name ? iso(name) : 'An artist'}` },
    }
  }
  const merge = kind === 'merge'
  const say = merge ? mergeView : jobView
  let v
  if (job) {
    v = here(say(job.op, job.phase, job.row))
  } else {
    const running = (merge ? mergeOpOfState : opOfState)(row?.state)
    const problems = say(merge ? 'merge' : 'apply', 'settled', row).problems
    v = running ? say(running, 'polling', row)
      : row?.state === (merge ? 'merged' : 'applied') ? rested('ok', merge ? 'Merged' : 'Applied', problems)
      : row?.state === 'failed' ? rested('bad', 'Did not finish', problems)
      : null                                       // another build's state: nothing is said, nothing offered
  }
  const title = merge ? mergeLabel(row) : [row?.album, row?.artist].filter(Boolean).map(iso).join(' · ') || 'An album'
  const word = !v || !v.ended ? '' : job ? (v.canUndo ? v.undoLabel : '') : 'Undo'
  const undo = word ? { label: word, says: `${word} ${title}` } : null
  if (merge) {
    return { title, to: null, sub: [plural(Number(row?.done) || 0, 'file', 'files'), when].filter(Boolean).join(' · '), link: null, v, undo }
  }
  return {
    title,
    to: null,
    sub: when,
    link: row?.releaseMbid ? { href: `https://musicbrainz.org/release/${row.releaseMbid}`, text: 'Release on MusicBrainz' } : null,
    v, undo,
  }
}

const UNDOES = { album: 'remove', merge: 'unmerge' }

/** One more row held while its undo runs: with the group it is drawn in and
 *  the place it has among that group's rows as they are SHOWN now — its index
 *  (`at`) and the row it stands before (`before`, a key) — because lb-bot may
 *  list it no more a moment later (an artist match is removed at once), and
 *  acting on a row must not move it. `group`: { key, kind, rows } as the tab
 *  has it from lb-bot. The same object when the row is held already (the
 *  place it had is kept). */
export function holdRow(prev, group, row) {
  const key = rowKey(group.kind, row)
  if (prev[key]) return prev
  const shown = rowsShown(group, prev).map(r => rowKey(group.kind, r))
  const found = shown.indexOf(key)
  const at = found < 0 ? shown.length : found
  return { ...prev, [key]: { kind: group.kind, group: group.key, row, at, before: shown[at + 1] || '', leaving: false } }
}

/** The rows an undo is running for, kept by the list while it runs — whoever
 *  started it (this tab, another client, a visit before this one): lb-bot
 *  lists such a row no more once the undo is through, and the row must still
 *  be there to say so. `groups`: the tab's own, [{ key, kind, rows }];
 *  `jobOf(key)`: the watch's job for a row. The same object when there is
 *  nothing new to hold. */
export function heldRows(prev, groups, jobOf) {
  let next = prev
  for (const g of groups) {
    if (!UNDOES[g.kind]) continue
    for (const row of g.rows || []) {
      const job = jobOf(rowKey(g.kind, row))
      if (job?.running && job.op === UNDOES[g.kind]) next = holdRow(next, g, row)
    }
  }
  return next
}

/** A group's rows as the tab draws them: lb-bot's, with the held rows it no
 *  longer lists put back where each stood (`holdRow`) — before the row it
 *  stood before, when that one is still there (a row above it may have left
 *  meanwhile), else at the index it had. A row lb-bot still lists is drawn
 *  once, where lb-bot has it; another group's held row is not this group's.
 *  The heading counts these. */
export function rowsShown(group, held) {
  const rows = [...(group.rows || [])]
  const listed = new Set(rows.map(r => rowKey(group.kind, r)))
  const back = Object.values(held || {})
    .filter(h => h.kind === group.kind && h.group === group.key && !listed.has(rowKey(group.kind, h.row)))
    .sort((a, b) => a.at - b.at)
  for (const h of back) {
    const next = h.before ? rows.findIndex(r => rowKey(group.kind, r) === h.before) : -1
    rows.splice(next >= 0 ? next : Math.min(h.at, rows.length), 0, h.row)
  }
  return rows
}

/** How the job of a held row ended, or null while it has not: `gone` — the
 *  change is undone (or lb-bot no longer has it), so the row folds away, with
 *  the one `toast`; not gone — it stays, and says why itself. An artist
 *  match's remove is stored the moment lb-bot answers: its row goes when the
 *  scan that follows is over, whatever that says. */
export function undoEnded(kind, row, job) {
  // (A job that went silent has not ended: a later answer of the list says how.)
  if (!job || job.running || job.phase === 'timeout') return null
  if (kind === 'artist') return { gone: true, toast: scanToast(job.state, row?.name || '') }
  const gone = job.phase === 'gone' || (job.phase === 'settled' && job.op === UNDOES[kind])
  if (!gone) return { gone: false, toast: null }
  return { gone: true, toast: (kind === 'merge' ? mergeToast : endToast)(job.op, job.phase, job.row || row) }
}
