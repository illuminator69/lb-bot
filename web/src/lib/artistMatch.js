// What the web UI may say about a library artist's MusicBrainz match (I-029,
// U2): which MusicBrainz artist lb-bot uses for it and where that comes from,
// what a search for another one answered, and what a set or a remove did.
// One place, so the pane under Needs you › To fix and the artist page — one
// picker, two homes — cannot come to say different things.
//
// Pure, with its matrix: artistMatch.test.mjs. The rules behind it are in
// CLAUDE.md § Artist MBID overrides.
//
// What lb-bot answers, and what each field can carry:
//   GET /api/overrides   artists [{ndArtistId, mbid, name, tagMbid, setAt}] — every
//                        override; `name` is the LIBRARY's name for the artist.
//                        toCheck [{ndArtistId, name, kind, mbid, albums}] — artists
//                        with no tag and no override whose own match holds nothing:
//                        `unmatched` (a guess, `mbid`) or `unresolved` (none). Absent
//                        when lb-bot could not work the list out — not "none".
//   GET /api/artists     rows {id, name, mbid?, tagMbid?}: `mbid` is the files' tag,
//                        or the override — and then `tagMbid` is there too.
//   GET /api/meta/artist the MusicBrainz artist's own name and disambiguation.
//                        Neither answer above carries them.
//   GET /api/artist/lookup  candidates [{mbid, name, disambiguation, type, country,
//                        area, score}], eight at the most; 502 when MusicBrainz
//                        did not answer — never an empty list for that.

import { inLibrary } from './indexStatus.js'
import { SILENT_MS, parseMbRef, refusalView } from './overrides.js'
import { iso } from './text.js'

/** Typing pauses this long before the field is searched. */
export const SEARCH_PAUSE_MS = 600

/** What the artist page says of a discography that is another artist's row.
 *  Said only of an answer that carries `covered` (the page's Rescan, a set, a
 *  remove): a blank `task_id` alone says only that no scan was started. */
export const SHARED_NOTE = 'This artist shares its discography with another artist in your library; there is nothing to rescan from here.'

export const mbArtistUrl = mbid => `https://musicbrainz.org/artist/${mbid}`

const lower = v => String(v || '').toLowerCase()
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`
const count = v => (Number.isFinite(Number(v)) && v !== null && v !== '' ? Number(v) : null)

// ── where the match comes from ───────────────────────────────────────────────

/** Whether lb-bot gave its list of artists worth a look. It leaves `toCheck`
 *  out when it could not work the list out (Navidrome's artist list busy —
 *  likely right after a set): that is "no list", never "nothing to check". */
export const listGiven = overrides => !!overrides && Array.isArray(overrides.toCheck)

/** What the artist page's own discography read says of the match, for
 *  `matchNow`'s `indexMbid`: the id it names; '' — it answered and names
 *  nobody; `undefined` — it is still out; `false` — it failed (the read, or
 *  the scan that would have said: a stored stub that names nobody is then no
 *  answer either).
 *
 *  `nobody`: the page looked the artist up by name and MusicBrainz answered
 *  with no candidate. The page shows that as an error of its scan, but it is
 *  an ANSWER — "no match" is true then, and is said (R3a). Only a request that
 *  did not answer, or answered an error, is a read that failed. */
export function pageRead(disco, error, nobody = false) {
  if (disco?.artist_mbid) return disco.artist_mbid
  if (nobody) return ''
  if (error) return false
  return disco ? '' : undefined
}

/** What is said when lb-bot could not say (`matchNow`'s `why`). */
export const NOT_KNOWN = {
  nolist: 'lb-bot could not work out which artists need a look just now.',
  noread: 'lb-bot could not read this artist’s discography just now.',
  // The merge sets alone left out of an answer that gave the artists to match (lib/merges.js, `toFixNote`).
  nosets: 'lb-bot could not work out which artists are under two names just now.',
}

/** Which MusicBrainz artist lb-bot uses for a library artist, and why.
 *
 *    id         the artist's Navidrome id
 *    overrides  `GET /api/overrides`'s answer, null until it is read
 *    artist     the artist's row of `GET /api/artists`, when the caller has it
 *    indexMbid  what the artist page's own discography read names (`pageRead`):
 *               an id, '' for nobody, `undefined` while that read is still
 *               out, `false` when it failed — and `null` where there is no
 *               such read (the pane under To fix)
 *
 *  `source`: `yours` (an override), `tags` (the files' tag), `guess` (lb-bot's
 *  own name search; `holdsNone` when it is the list's `unmatched`), `none` (the
 *  list's `unresolved`, or a page whose own read names nobody), `search`
 *  (lb-bot's own search, and nothing here says what it found — only where no
 *  page read exists), `unknown`: not known, and said as that — nothing read
 *  yet, the page's read still out, or lb-bot could not say: it left its list
 *  out (`why: 'nolist'`), or the page's read failed (`why: 'noread'` — nothing
 *  in a failure says there is no match). `backTo` is what removing an override
 *  goes back to: `tags` or `search`. */
export function matchNow({ id, overrides = null, artist = null, indexMbid } = {}) {
  const base = { source: 'unknown', why: '', mbid: '', setAt: 0, backTo: '', holdsNone: false, albums: null }
  const read = !!overrides && Array.isArray(overrides.artists)
  const own = read ? overrides.artists.find(a => a.ndArtistId === id) : null
  if (own) {
    return { ...base, source: 'yours', mbid: lower(own.mbid), setAt: Number(own.setAt) || 0, backTo: own.tagMbid ? 'tags' : 'search' }
  }
  // The artist list says "overridden" by carrying `tagMbid`. It is kept for a
  // minute, so after a remove it can still say so: lb-bot's own answer, once
  // read, is the one believed, and the list's `tagMbid` is then the tag.
  const listedAsYours = !!artist && artist.tagMbid !== undefined
  if (!read && listedAsYours) {
    return { ...base, source: 'yours', mbid: lower(artist.mbid), backTo: artist.tagMbid ? 'tags' : 'search' }
  }
  const tag = listedAsYours ? artist.tagMbid : artist?.mbid
  if (tag) return { ...base, source: 'tags', mbid: lower(tag) }
  const listed = read && listGiven(overrides) ? overrides.toCheck.find(a => a.ndArtistId === id) : null
  if (listed) {
    const albums = count(listed.albums)
    if (listed.kind === 'unmatched' && listed.mbid) return { ...base, source: 'guess', mbid: lower(listed.mbid), holdsNone: true, albums }
    return { ...base, source: 'none', albums }
  }
  if (indexMbid) return { ...base, source: 'guess', mbid: lower(indexMbid) }
  // The page's own read failed: not known — and known to be so (R3).
  if (indexMbid === false) return { ...base, why: 'noread' }
  // The page's own read has answered and names nobody: no match.
  if (indexMbid === '') return { ...base, source: 'none' }
  // That read is still out, or nothing was read at all: not known yet.
  if (indexMbid === undefined || !read) return base
  // No page read exists here. Without lb-bot's list nothing says which it is.
  if (!listGiven(overrides)) return { ...base, why: 'nolist' }
  return { ...base, source: 'search' }
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** "11 Oct 2026", in the browser's own zone; '' for no time. */
export function dateWords(sec) {
  const n = Number(sec)
  if (!(n > 0)) return ''
  const d = new Date(n * 1000)
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`
}

// (What MusicBrainz says in its own words — the disambiguation — is isolated,
// lib/text.js: it shares its line with the rest.)
const facts = mb => [iso(mb?.disambiguation), mb?.type, mb?.area || mb?.country].filter(Boolean).join(' · ')

const NOTE = { tags: 'from your files’ tags', guess: 'lb-bot’s own guess' }
const BACK = { tags: 'Go back to the files’ tag', search: 'Go back to lb-bot’s own search' }

/** The match as the picker shows it. `mb` is what MusicBrainz says of the
 *  artist (`{name, disambiguation, type, country, area}`): null while it is
 *  being asked, anything without a name when it could not be had.
 *
 *    state   unknown | noread | none | search | match
 *            (`noread`: the home's own read failed — `line` says so, the home
 *            offers its Try again, and the search stays usable: choosing an
 *            artist is the one thing that mends a name the search cannot find)
 *    note    where the match comes from, beside the heading
 *    title, sub, href   the MusicBrainz artist's row (`naming`: a skeleton stands for the name)
 *    line    one sentence under it, '' for none
 *    keep    "Keep this match" is offered
 *    choose  "Choose another" is offered (with no match the search is simply there)
 *    back    the label of "Go back to …", '' when there is no override */
export function nowView(now, mb) {
  const view = { state: 'unknown', note: '', title: '', naming: false, sub: '', href: '', line: '', keep: false, choose: false, back: '' }
  if (now?.source === 'unknown' && now.why === 'noread') return { ...view, state: 'noread', line: NOT_KNOWN.noread, choose: true }
  if (!now || now.source === 'unknown') return view
  if (now.source === 'none') {
    return { ...view, state: 'none', line: now.albums > 0
      ? `lb-bot has not matched this artist on MusicBrainz, so its ${plural(now.albums, 'album', 'albums')} cannot be checked for gaps.`
      : 'lb-bot has not matched this artist on MusicBrainz.' }
  }
  if (now.source === 'search' || !now.mbid) {
    return { ...view, state: 'search', line: 'lb-bot goes by its own search for this artist.', choose: true }
  }
  const date = now.source === 'yours' ? dateWords(now.setAt) : ''
  const note = now.source === 'yours' ? `your choice${date ? ` on ${date}` : ''}` : NOTE[now.source] || ''
  let line = ''
  if (now.holdsNone) {
    line = now.albums === 1 ? 'The one album in your library does not line up with this artist’s discography.'
      : now.albums > 1 ? `None of the ${now.albums} albums in your library line up with this artist’s discography.`
      : 'None of this artist’s albums in your library line up with this discography.'
  }
  return {
    ...view, state: 'match', note, line,
    naming: mb === null,
    title: mb === null ? '' : (mb?.name || 'MusicBrainz artist'),
    sub: facts(mb), href: mbArtistUrl(now.mbid),
    keep: now.source === 'guess' && now.holdsNone,
    choose: true,
    back: now.source === 'yours' ? BACK[now.backTo] || BACK.search : '',
  }
}

/** The quiet line by the artist page's hero, before "Fix match": null while
 *  nothing is known; `naming` while the name is being asked for. */
export function quietLine(now, mb) {
  // A read that failed is said as not known — and keeps the line, so that
  // "Fix match" has somewhere to stand (R3a). While the read is out: nothing.
  if (now?.source === 'unknown' && now.why === 'noread') return { text: 'MusicBrainz match: not known', naming: false }
  if (!now || now.source === 'unknown') return null
  // One thing with the picker in every state: its "own search" is not "no match".
  if (now.source === 'search') return { text: 'MusicBrainz match: lb-bot’s own search', naming: false }
  if (!now.mbid) return { text: 'No MusicBrainz match', naming: false }
  if (mb === null) return { text: 'MusicBrainz match:', naming: true }
  if (!mb?.name) return { text: 'Matched on MusicBrainz', naming: false }
  return { text: `MusicBrainz match: ${iso(mb.name)}${mb.disambiguation ? ` (${iso(mb.disambiguation)})` : ''}`, naming: false }
}

// ── the search ───────────────────────────────────────────────────────────────

const OTHER_MB_ADDRESS = /^(?:https?:\/\/)?(?:[a-z0-9-]+\.)?musicbrainz\.org\/(?!artist\/)[a-z-]+\/[0-9a-f]{8}-/i
const ARTIST_MB_ADDRESS = /^(?:https?:\/\/)?(?:[a-z0-9-]+\.)?musicbrainz\.org\/artist(?:\/|$)/i

/** What the field holds: nothing, a MusicBrainz artist id or address (that
 *  artist is offered), a MusicBrainz address that names no artist — another
 *  kind's (`why: 'kind'`) or an artist's with no id in it (`why: 'artist'`):
 *  said, never searched for as a name — or a name to search for. */
export function searchRef(text) {
  const q = String(text ?? '').trim()
  if (!q) return { kind: 'empty' }
  const mbid = parseMbRef(q, 'artist')
  if (mbid) return { kind: 'id', mbid }
  if (ARTIST_MB_ADDRESS.test(q)) return { kind: 'wrong', why: 'artist' }
  if (OTHER_MB_ADDRESS.test(q)) return { kind: 'wrong', why: 'kind' }
  return { kind: 'name', q }
}

/** One candidate's row. The one that is the match in use is not offered
 *  again — except a guess, which "Keep this match" makes the user's own. */
export function candidateView(c, now) {
  const mbid = lower(c?.mbid)
  const current = !!mbid && mbid === now?.mbid
  const guess = now?.source === 'guess'
  return {
    key: mbid,
    title: c?.name || 'MusicBrainz artist',
    sub: facts(c),
    href: mbArtistUrl(mbid),
    label: current ? (guess ? 'Keep this match' : '') : 'Use this artist',
    word: current && !guess ? 'In use' : '',
  }
}

/** The artist a pasted id names, as a candidate. `meta` is `GET
 *  /api/meta/artist`'s answer, or null when it could not be had: the artist is
 *  offered all the same — setting it is what asks MusicBrainz whether it is one. */
export function pastedCandidate(mbid, meta) {
  return { mbid: lower(mbid), name: meta?.name || '', disambiguation: meta?.disambiguation || '',
           type: meta?.type || '', country: meta?.country || '', area: '' }
}

/** How many rows the lookup answers at the most — and, for most names, does. */
const LOOKUP_ROWS = 8

/** What the search shows.
 *
 *    lookup   null | { status: loading | ok | error | wrong, kind: name | id | wrong, list, error }
 *             `list` is the last answer while another is being asked for.
 *
 *  `skeleton` rows stand for an answer that is on its way with nothing on the
 *  page; with candidates on the page they stay (`busy`) until the next answer
 *  replaces them. `empty` is only ever said of a search that ran. */
export function lookupView(lookup, now) {
  const view = { skeleton: 0, busy: false, rows: [], empty: '', error: null }
  if (!lookup) return view
  const rows = (lookup.list || []).map(c => candidateView(c, now))
  if (lookup.status === 'loading') {
    return { ...view, busy: true, rows, skeleton: rows.length ? 0 : lookup.kind === 'id' ? 1 : LOOKUP_ROWS }
  }
  if (lookup.status === 'wrong') {
    return { ...view, empty: lookup.why === 'artist' ? 'That address holds no MusicBrainz artist id.'
      : 'That is a MusicBrainz address, but not an artist’s.' }
  }
  if (lookup.status === 'error') {
    const said = refusalView(lookup.error).text
    return { ...view, error: { title: 'Couldn’t search MusicBrainz', text: lookup.error?.status ? said : (lookup.error?.message || said) } }
  }
  if (!rows.length) return { ...view, empty: 'No MusicBrainz artist by that name.' }
  return { ...view, rows }
}

/** What `POST /api/artist/override` is sent: the library artist's own id and
 *  name (lb-bot scans under Navidrome's name for it), the candidate's id. */
export const setBody = (artist, candidate) => ({ nd_id: artist.id, mbid: lower(candidate.mbid), name: artist.name || '' })

// ── the job: a set or a remove, and the scan it starts ───────────────────────

/** Of the albums the library holds for this artist — every entry that is the
 *  artist, as the scan bucketed them (`result.library.album_ids`) — how many a
 *  release of the discography holds. null when the result does not say what
 *  it was matched against. */
export function linedUp(result) {
  const ids = result?.library?.album_ids
  if (!Array.isArray(ids)) return null
  const held = new Set()
  for (const r of result.releases || []) {
    if (!inLibrary(r?.status)) continue
    for (const id of r.navidrome_album_ids || []) held.add(id)
  }
  return { held: ids.filter(id => held.has(id)).length, total: ids.length }
}

/** The job after lb-bot's answer to a set or a remove: a scan to follow, or
 *  none — and `covered` only when the answer says so beside a blank task id.
 *  `now` (ms) is when lb-bot was last heard from: just now. */
export function scanStart(op, answer, now = 0) {
  const job = { op, phase: 'scanning', taskId: String(answer?.task_id || ''), done: 0, total: 0, current: '', lined: null, error: '',
                heardAt: now }
  if (job.taskId) return job
  return { ...job, phase: answer?.covered === true ? 'covered' : 'unstarted' }
}

/** A job kept from an earlier visit, taken up again: nobody asked meanwhile,
 *  so the silence is counted from the return. */
export const scanResume = (job, now) => (job ? { ...job, heardAt: now } : null)

/** One poll of `GET /api/tasks/<id>`: `{ ok: true, task }`, or `{ ok: false,
 *  status }`. A 404 is a task lb-bot has forgotten (it restarted). Any other
 *  failure is no answer: the job is asked about again — until lb-bot has not
 *  answered for three minutes (`SILENT_MS`, the album jobs' own limit), which
 *  is said, and the asking stops. A scan that answers and does not move is not
 *  silent: it waits its turn at MusicBrainz. */
export function scanStep(job, answer, now = 0) {
  if (!job || job.phase !== 'scanning') return job
  if (!answer?.ok) {
    if (answer?.status === 404) return { ...job, phase: 'lost' }
    return now - job.heardAt >= SILENT_MS ? { ...job, phase: 'silent' } : job
  }
  const task = answer.task || {}
  const heard = { ...job, heardAt: now }
  if (task.status === 'complete') return { ...heard, phase: 'done', lined: linedUp(task.result) }
  if (task.status === 'error') return { ...heard, phase: 'failed', error: String(task.error || '') }
  if (task.status === 'cancelled') return { ...heard, phase: 'failed', error: 'The scan was cancelled.' }
  return { ...heard, done: Number(task.done) || 0, total: Number(task.total) || 0, current: String(task.current || '') }
}

const linedLine = lined => {
  if (!lined) return 'lb-bot has read the discography again.'
  if (!lined.total) return 'lb-bot found no album of this artist’s in your library.'
  return `${lined.held} of ${plural(lined.total, 'album', 'albums')} ${lined.total === 1 ? 'lines' : 'line'} up.`
}

/** The job as its card shows it: the state word, one sentence, a muted second
 *  one, and the whole scan's meter — release groups checked of all there are,
 *  unmeasured (`percent` null) until lb-bot has said how many. */
export function scanView(job, { name = '' } = {}) {
  const view = { tone: 'run', word: '', line: '', note: '', bar: false, percent: null, count: '', ended: false }
  if (!job) return view
  const remove = job.op === 'remove'
  const ended = { ...view, tone: 'ok', word: remove ? 'Removed' : 'Matched', ended: true }
  const stored = remove ? 'Your choice is removed' : 'The match is set'
  const again = `${stored}; the scan can be run again from the artist page.`
  switch (job.phase) {
    case 'sending':
      return { ...view, word: remove ? 'Going back' : 'Matching', line: remove ? 'Removing your choice…' : 'Setting the match…' }
    case 'scanning':
      return { ...view, word: 'Reading', line: `Reading ${name ? iso(name) : 'the artist'}’s discography again…`, note: job.current || '', bar: true,
               percent: job.total > 0 ? Math.min(100, Math.round((job.done / job.total) * 100)) : null,
               count: job.total > 0 ? `${job.done} of ${job.total}` : '' }
    case 'done':
      return { ...ended, line: linedLine(job.lined) }
    case 'covered':
      return { ...ended, line: `${stored}.`, note: SHARED_NOTE }
    case 'unstarted':
      return { ...ended, line: `${stored}.`, note: 'lb-bot started no scan for this artist.' }
    case 'failed':
      return { ...ended, tone: 'bad', word: 'Scan failed', line: job.error || 'The discography scan failed.', note: again }
    case 'lost':
      return { ...ended, tone: 'wait', word: 'Not finished', line: 'The scan did not finish: lb-bot restarted while it ran.', note: again }
    case 'silent':
      return { ...ended, tone: 'wait', word: 'No answer', line: 'lb-bot has not answered for three minutes.',
               note: `${stored}; the scan may still be running.` }
    default:
      return view
  }
}

/** The one toast for a scan that ended with nobody showing it — the artist,
 *  and what its card would have said — or null while it runs. */
export function scanToast(job, name) {
  if (!job || job.phase === 'scanning' || job.phase === 'sending') return null
  const v = scanView(job, { name })
  if (!v.ended) return null
  return { text: `${name ? iso(name) : 'The artist'}: ${v.line}`, level: v.tone === 'ok' ? 'info' : 'error' }
}

/** The scan as a job of the one watch (lib/jobWatch.js, kind `scan`): followed
 *  by the artist's id — the two homes know that, not the task's — and asked
 *  about by its task. `start`'s seed is the job `scanStart` made of lb-bot's
 *  answer; the watch's `row` for it is `{ name }`, the artist's, for the toast. */
export const scanMachine = {
  start: (op, now, seed) => scanResume(seed, now),
  step: scanStep,
  running: state => state.phase === 'scanning',
  sig: state => [state.op, state.phase, state.done, state.total, state.current, state.lined, state.error],
  opOf: () => '',
  restKey: () => '',
  toast: (state, row) => scanToast(state, row?.name),
}

// ── the list under Needs you › To fix ────────────────────────────────────────

/** The artists listed under To fix on this visit, after another answer of
 *  `GET /api/overrides`: each stays in its place for as long as the tab is
 *  open. `moved` — it is matched — is said of one that has left a list lb-bot
 *  GAVE, or that the answer itself says has an override. An answer without
 *  `toCheck` is no list: the rows stay as they were, and nobody is called
 *  matched by its absence (about a hundred rows did flip, once). */
export function keepListed(prev, overrides) {
  if (!overrides) return prev
  const overridden = new Set((Array.isArray(overrides.artists) ? overrides.artists : []).map(a => a.ndArtistId))
  if (!listGiven(overrides)) {
    return prev.map(i => (overridden.has(i.a.ndArtistId) && !i.moved ? { ...i, moved: true } : i))
  }
  const now = new Map(overrides.toCheck.map(a => [a.ndArtistId, a]))
  const kept = prev.map(i => (now.has(i.a.ndArtistId) ? { ...i, a: now.get(i.a.ndArtistId), moved: false } : { ...i, moved: true }))
  const had = new Set(kept.map(i => i.a.ndArtistId))
  return [...kept, ...overrides.toCheck.filter(a => !had.has(a.ndArtistId))
    .map(a => ({ key: `art:${a.ndArtistId}`, kind: 'art', a, moved: false }))]
}

/** An artist's row in the list: why it is listed, and — once it has left
 *  lb-bot's list while the user is still on it (`moved`) — that it is matched. */
export function fixRowView(a, moved) {
  const albums = plural(Number(a?.albums) || 0, 'album', 'albums')
  if (moved) return { sub: `Matched, ${albums}`, tone: 'ok', word: 'Matched' }
  return { sub: `${a?.kind === 'unmatched' ? 'Match holds none of its albums' : 'No MusicBrainz match'}, ${albums}`, tone: 'wait', word: 'To match' }
}
