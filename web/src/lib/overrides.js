// What lb-bot's own web UI may say about an override (I-029): the one place
// that decides. Pure, so the wording has a state matrix: overrides.test.mjs.
//
// The web UI is a third client of the routes Feishin and Navic use through the
// hub, and the round's contract binds it as it binds them (§ "Client behaviour
// both clients mirror", rulings X1–X10). Every sentence here states what a
// field of lb-bot's answer says and nothing else — in particular nothing about
// favourites, ratings or play counts, which nobody has measured. No raw state
// or code reaches the page: a state this file knows gets its sentence, a
// refusal gets lb-bot's own `error` text. A name put into a sentence is
// isolated (lib/text.js), so that a right-to-left one keeps the words beside
// it in their places.
import { iso } from './text.js'

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`

// ── A pasted id or address ───────────────────────────────────────────────────
const UUID = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
const BARE_UUID = new RegExp(`^${UUID}$`, 'i')

/** A bare MusicBrainz id, or the id out of a `musicbrainz.org/<kind>/<uuid>`
 *  address (any subdomain, with or without the scheme) — lower-cased, else ''.
 *  Only that path: a release-group id is not a release, and a look-alike host
 *  is not MusicBrainz. */
export function parseMbRef(text, kind) {
  const value = String(text ?? '').trim()
  if (BARE_UUID.test(value)) return value.toLowerCase()
  const address = new RegExp(`^(?:https?://)?(?:[a-z0-9-]+\\.)?musicbrainz\\.org/${kind}/(${UUID})(?:[/?#].*)?$`, 'i')
  const m = address.exec(value)
  return m ? m[1].toLowerCase() : ''
}
export const parseReleaseRef = text => parseMbRef(text, 'release')

// ── The job, as lb-bot's row says it ─────────────────────────────────────────
export const POLL_MS = 2000
/** X2: the watch gives up after this long with no change of `state` or `done`. */
export const SILENT_MS = 180_000

// What a poll of a job's row ends on. `failedNeedsActive`: a remove can be read
// before lb-bot has flipped the row, and `applied` is then the old state.
// `goneIsDone`: a remove deletes its row, so the coded 404 is how it ends well;
// an apply's row vanishing is an ending of its own.
// An artist name merge and its undo are the same two jobs under their own
// state names and their own coded 404 (`no_merge`): the X-rules hold as they
// stand (lib/merges.js words them).
const JOBS = {
  apply: { active: ['applying'], settled: ['applied'], failed: ['failed'], failedNeedsActive: false, goneCode: 'no_override', goneIsDone: false },
  remove: { active: ['removing'], settled: [], failed: ['applied', 'failed'], failedNeedsActive: true, goneCode: 'no_override', goneIsDone: true },
  merge: { active: ['merging'], settled: ['merged'], failed: ['failed'], failedNeedsActive: false, goneCode: 'no_merge', goneIsDone: false },
  unmerge: { active: ['undoing'], settled: [], failed: ['merged', 'failed'], failedNeedsActive: true, goneCode: 'no_merge', goneIsDone: true },
}
const RUNNING = { applying: 'apply', removing: 'remove' }
const MERGE_RUNNING = { merging: 'merge', undoing: 'unmerge' }

/** Which job an album override's row says is running, by its state, or ''. */
export const opOfState = state => RUNNING[state] || ''
export const isRunning = state => !!RUNNING[state]
/** The same of a merge's row. */
export const mergeOpOfState = state => MERGE_RUNNING[state] || ''

/** X4: the writes are over — successes plus failures reach the total — while
 *  lb-bot still holds the row active, waiting for Navidrome. `done` counts
 *  successes only, so one failed file would keep "done == total" false for ever. */
export function writesOver(row) {
  if (!row || !(isRunning(row.state) || mergeOpOfState(row.state))) return false
  const total = Number(row.total) || 0
  return total > 0 && (Number(row.done) || 0) + (row.failures?.length || 0) >= total
}

/** A job about to be followed. `seedState` is the state lb-bot already gave the
 *  row: the POST's own answer, or the row's when a running job is picked up
 *  (X1) — lb-bot can reach `failed` before the first poll. */
export function jobStart(op, now, seedState = '') {
  const spec = JOBS[op]
  return {
    op, phase: 'polling', quietSince: now, row: null, album: null,
    sawActive: spec.active.includes(seedState) || seedState === 'failed',
  }
}

/** One poll. `answer` is `{ok: true, row, album}` or `{ok: false, status, code}`.
 *  An answer is judged before the clock. A poll that failed is no answer: only
 *  the clock moves, and the last row stays so that progress does not blink.
 *  Phases: polling · settled · failed · gone · timeout (final: X3). */
export function jobStep(job, answer, now) {
  if (job.phase !== 'polling') return job
  const spec = JOBS[job.op]
  if (answer?.ok && answer.row) {
    const state = answer.row.state
    const sawActive = job.sawActive || spec.active.includes(state)
    const next = { ...job, row: answer.row, album: answer.album ?? job.album, sawActive }
    if (spec.settled.includes(state)) return { ...next, phase: 'settled' }
    if (spec.failed.includes(state) && (sawActive || !spec.failedNeedsActive)) return { ...next, phase: 'failed' }
    const moved = job.row !== null && (state !== job.row.state || answer.row.done !== job.row.done)
    const quietSince = moved ? now : job.quietSince
    return now - quietSince >= SILENT_MS ? { ...next, quietSince, phase: 'timeout' } : { ...next, quietSince }
  }
  if (answer && !answer.ok && answer.status === 404 && answer.code === spec.goneCode) {
    return { ...job, phase: spec.goneIsDone ? 'settled' : 'gone' }
  }
  return now - job.quietSince >= SILENT_MS ? { ...job, phase: 'timeout' } : job
}

/** After a job ended: where the album is read again from. `albumId` is the id
 *  the page had for it. `use`: the poll's own last answer is the album (an
 *  apply Navidrome has listed again — `dead` is then the id it had, when that
 *  moved). Otherwise `steps`, tried in order: `album` — by the id the page has,
 *  which may still be the album's (a cover-only undo, a job that wrote nothing,
 *  an id Navidrome kept); `row` — by the override's id, while it has a row.
 *  `steps: null` for a job lb-bot has not said ended. A row's own `albumId` is
 *  blank from the moment an apply starts, so it is believed only once the
 *  apply has settled with one. */
export function afterJobSteps(albumId, job) {
  const none = { use: null, dead: '', steps: null }
  if (!job || job.phase === 'polling' || job.phase === 'timeout') return none
  const listed = job.op === 'apply' && job.phase === 'settled' && job.album?.albumId ? job.album : null
  if (listed) return { use: listed, dead: albumId && albumId !== listed.albumId ? albumId : '', steps: [] }
  const rowLeft = job.phase !== 'gone' && !(job.op === 'remove' && job.phase === 'settled')
  return { ...none, steps: [...(albumId ? ['album'] : []), ...(rowLeft ? ['row'] : [])] }
}

// ── What a job says ──────────────────────────────────────────────────────────
const WAITING = 'Waiting for Navidrome to read the album again…'
/** The tab every change lb-bot made is listed on: named in the sentence a
 *  silent job ends on, where a surface makes the name the way there. */
export const OVERRIDES_TAB = 'System › Overrides'
export const STILL_WORKING = `Still working — see ${OVERRIDES_TAB}`
export const NOT_LISTED_YET = 'Navidrome has not listed the album again yet.'

// (A problem is drawn "title — reason": the title shares its line.)
const problemsOf = row => (Array.isArray(row?.failures) ? row.failures : [])
  .map(f => ({ title: iso(String(f?.title || '')), reason: String(f?.reason || '') }))

function view(over) {
  return {
    tone: '', word: '', line: '', bar: false, percent: 0, count: '',
    problems: [], problemsHeading: '', ended: false, canUndo: false, undoLabel: 'Undo', seeOverrides: false,
    ...over,
  }
}

/** A job in words, from its row: the state word for a status dot, the one
 *  sentence, whether there is a bar and how full, the problems lb-bot lists.
 *  The bar is the whole job's (`done` of `total`), never one file's. */
export function jobView(op, phase, row) {
  const done = Number(row?.done) || 0
  const total = Number(row?.total) || 0
  const problems = problemsOf(row)
  // X9: problems are counted as problems — a cover that could not be saved is
  // one, and is not a file.
  const problemsHeading = problems.length ? plural(problems.length, 'problem', 'problems') : ''
  if (phase === 'timeout') return view({ tone: 'wait', word: 'Still working', line: STILL_WORKING, ended: true, seeOverrides: true })
  // The row is gone. A 404 says that and no more — not who undid it, or how.
  if (phase === 'gone') return view({ word: 'No longer there', line: 'lb-bot no longer has this change.', ended: true })
  if (phase === 'polling') {
    const running = op === 'remove'
      ? { word: 'Restoring', text: 'Putting the previous tags back…' }
      : { word: 'Retagging', text: 'Writing tags…' }
    if (writesOver(row)) return view({ tone: 'run', word: 'Waiting for Navidrome', line: WAITING })
    if (row && total > 0) {
      const count = `${done} of ${total}`
      return view({ tone: 'run', word: running.word, line: `${running.text} ${count}`, bar: true,
                    percent: Math.max(0, Math.min(100, Math.round((done / total) * 100))), count })
    }
    // A row with nothing to write says neither "0 of 0" nor that it waits.
    return view({ tone: 'run', word: running.word, line: row ? 'Working…' : running.text })
  }
  if (op === 'remove') {
    if (phase === 'settled') return view({ tone: 'ok', word: 'Restored', line: 'Restored.', ended: true })
    return view({ tone: 'bad', word: 'Not restored', line: 'lb-bot could not put everything back.',
                  problems, problemsHeading, ended: true, canUndo: true, undoLabel: 'Undo again' })
  }
  if (phase === 'settled') {
    // 0 of 0 says that nothing was written, and not why.
    const line = done > 0 ? `lb-bot wrote the tags of ${plural(done, 'file', 'files')}.`
      : total > 0 ? 'lb-bot wrote no file’s tags.'
      : 'Nothing was rewritten.'
    return view({ tone: problems.length ? 'wait' : 'ok', word: 'Retagged', line, problems, problemsHeading,
                  ended: true, canUndo: true })
  }
  return view({
    tone: 'bad', word: 'Did not finish', problems, problemsHeading, ended: true, canUndo: true,
    line: total > 0 ? `lb-bot did not finish writing the tags (${done} of ${plural(total, 'file', 'files')} written).`
      : 'lb-bot did not retag this album.',
  })
}

/** A row nobody is following, read with its album: null when it is simply
 *  applied (the line under the title says that), else what there is to say. */
export function restView(row) {
  if (!row) return null
  const running = opOfState(row.state)
  if (running) return jobView(running, 'polling', row)
  if (row.state === 'failed') {
    // The row does not say whether it was an apply or an undo that failed.
    return { ...jobView('apply', 'failed', row), line: 'A change lb-bot made to this album did not finish.' }
  }
  return problemsOf(row).length ? jobView('apply', 'settled', row) : null
}

/** The line under an album's title, as a decision: null — nothing is said (an
 *  album lb-bot has not changed, and a row in a state this page does not
 *  know: another build's, which must say nothing and break nothing); else
 *  `running` (the job's own line), `changed` ("Tags changed by lb-bot · when",
 *  with `v` when the row lists problems) or `ended` (how a job, or a row left
 *  failed, ended). `job` is the watch's job for the row, when one is shown. */
export function lineAtRest(row, job) {
  const v = job ? jobView(job.op, job.phase, job.row) : restView(row)
  if (v && !v.ended) return { kind: 'running', v }
  const changed = !job && row?.state === 'applied'
  if (!changed && !v) return null
  return { kind: changed ? 'changed' : 'ended', v }
}

/** X7: the release is the one it was — only the cover was changed. A row that
 *  does not carry the two ids is "not known", never "unchanged". Both present
 *  and both blank is unchanged too (F2): lb-bot's cover route wrote that row
 *  for an album with no release tag — an apply always names its release — and
 *  no tag was touched. (lb-bot always sends both keys, as strings.) A blank
 *  id before beside a named one now is an untagged album that was given a
 *  release: its tags changed. */
export const releaseUnchanged = row => typeof row?.releaseMbid === 'string' && typeof row?.prevReleaseMbid === 'string'
  && row.releaseMbid === row.prevReleaseMbid

/** lb-bot is — as far as this page knows — still changing this album's files
 *  (F6; Z3 on the album page): by the album's own state as last read, or by
 *  the job followed here, running or gone silent while it ran — the
 *  three-minute give-up is no end, and lb-bot would answer another job
 *  `in_progress`. While it is so, nothing that starts another job for the
 *  album is offered. `job`: the watch's, when the page shows one. */
export function albumQuiet(row, job) {
  if (job && (job.running || job.phase === 'polling' || job.phase === 'timeout')) return true
  return isRunning(row?.state)
}

/** "Change cover", under the album's cover (G1). The link keeps its place
 *  whatever lb-bot is doing: it is the last line of the cover's column, that
 *  column sets the hero's height, and a line taken out of it moves everything
 *  under the hero in one frame — when a job starts, and again when it ends.
 *  While lb-bot is changing the album (`albumQuiet`) it cannot be pressed, and
 *  says why. `canCover`: the page holds an album lb-bot is not changing now. */
export const COVER_HELD = 'Not while lb-bot is changing this album’s files.'
export const changeCoverLink = canCover => (canCover ? { pressable: true, why: '' } : { pressable: false, why: COVER_HELD })

/** What an Undo button on the album page says to a screen reader (F7): its
 *  word and the album it is for — a page that holds the album more than once
 *  has several buttons that all say "Undo". */
export const undoSays = (label, name) => (name ? `${label} ${iso(name)}` : label)

/** Which list of System › Overrides a row belongs to. */
export const rowGroup = row => (releaseUnchanged(row) ? 'covers' : 'editions')

export function agoText(setAt, nowSec) {
  if (!setAt) return ''
  const secs = Math.max(0, Math.round(nowSec - setAt))
  if (secs < 60) return 'just now'
  if (secs < 3600) return `${Math.floor(secs / 60)} min ago`
  if (secs < 86400) return `${Math.floor(secs / 3600)} h ago`
  return `${Math.floor(secs / 86400)} d ago`
}

/** The quiet line under an album's title, in its two parts: what lb-bot
 *  changed (the statement) and when (secondary — the page sets it apart). */
export function changedParts(row, nowSec) {
  if (!row) return { what: '', when: '' }
  const what = releaseUnchanged(row) ? 'Cover changed by lb-bot'
    : row.coverUrl ? 'Tags and cover changed by lb-bot' : 'Tags changed by lb-bot'
  return { what, when: agoText(row.setAt, nowSec) }
}

/** The name a toast calls an album by. */
export const albumLabel = row => row?.album || row?.artist || 'The album'

/** The one toast for a job that ended with nobody looking at it, or null. */
export function endToast(op, phase, row) {
  if (phase === 'polling') return null
  const v = jobView(op, phase, row)
  const name = row?.album || row?.artist ? iso(albumLabel(row)) : albumLabel(row)
  if (phase === 'timeout') return { text: `${name}: still working — see System › Overrides`, level: 'info' }
  if (phase === 'settled' && op === 'remove') return { text: `${name}: restored.`, level: 'info' }
  const more = phase === 'settled' && v.problemsHeading ? ` ${v.problemsHeading} — open the album to see it.` : ''
  return { text: `${name}: ${v.line}${more}`, level: v.tone === 'ok' || phase === 'gone' ? 'info' : 'error' }
}

// ── The preview ──────────────────────────────────────────────────────────────
/** The preview's numbers. `changing`: files an apply would rewrite — they have
 *  a place and are not already right. `noSlot`: files with no place, which an
 *  apply leaves untouched. `missing`: tracks of the release no file fills. */
export function previewSummary(preview) {
  const files = Array.isArray(preview?.files) ? preview.files : []
  const placed = files.filter(f => f.to)
  const changing = placed.filter(f => f.changed).length
  return {
    changing,
    unchanged: placed.length - changing,
    noSlot: files.length - placed.length,
    missing: Array.isArray(preview?.missing) ? preview.missing.length : 0,
    matched: placed.length,
    total: files.length,
  }
}

/** The preview's counts in one line. A count of nothing is not said: "8 files
 *  change · 2 tracks still missing afterwards", and '' when there is none. */
export function countsLine(summary) {
  return [
    summary.changing > 0 && `${plural(summary.changing, 'file', 'files')} ${summary.changing === 1 ? 'changes' : 'change'}`,
    summary.noSlot > 0 && `${plural(summary.noSlot, 'file', 'files')} with no place on this release`,
    summary.missing > 0 && `${plural(summary.missing, 'track', 'tracks')} still missing afterwards`,
  ].filter(Boolean).join(' · ')
}

export function releaseLine(release) {
  if (!release) return ''
  const name = [iso(release.title), release.disambiguation ? `(${iso(release.disambiguation)})` : ''].filter(Boolean).join(' ')
  return [
    name, release.year, release.country, release.format,
    release.trackCount > 0 ? plural(release.trackCount, 'track', 'tracks') : '',
    release.discCount > 1 ? `${release.discCount} discs` : '',
  ].filter(Boolean).join(' · ')
}

/** "03 Title" — the disc before it only on a release of several, and no
 *  number for a file that has none. */
export function slotLabel(slot, multiDisc) {
  if (!slot) return ''
  if (!(slot.track > 0)) return slot.title || ''
  const disc = multiDisc && slot.disc > 0 ? `${slot.disc}-` : ''
  return `${disc}${String(slot.track).padStart(2, '0')} ${slot.title || ''}`.trim()
}

/** The from → to rows: the changed ones, the ones that are already right (shown
 *  behind their count), and the files with no place on the release. */
export function previewRows(preview) {
  const files = Array.isArray(preview?.files) ? preview.files : []
  const multiDisc = (preview?.release?.discCount || 0) > 1 || files.some(f => (f.from?.disc || 0) > 1 || (f.to?.disc || 0) > 1)
  const rows = files.map((f, i) => ({
    key: `${i}-${f.from?.disc || 0}-${f.from?.track || 0}-${f.from?.title || ''}`,
    from: slotLabel(f.from, multiDisc), to: f.to ? slotLabel(f.to, multiDisc) : '',
    placed: !!f.to, changed: !!f.to && !!f.changed,
  }))
  const unchanged = rows.filter(r => r.placed && !r.changed)
  // A changed file whose number and title stay — another pressing of the same
  // tracklist changes every file that way — would read "01 X → 01 X". Those
  // are counted in one sentence and listed behind it.
  const sameLabel = rows.filter(r => r.changed && r.from === r.to)
  const one = sameLabel.length === 1
  return {
    multiDisc,
    differ: rows.filter(r => r.changed && r.from !== r.to),
    sameLabel,
    sameLabelText: `${plural(sameLabel.length, 'file', 'files')} ${one ? 'keeps its' : 'keep their'} number and title; `
      + `only the release ${one ? 'it is' : 'they are'} tagged with changes`,
    unchanged,
    noSlot: rows.filter(r => !r.placed),
    unchangedLabel: `${plural(unchanged.length, 'file', 'files')} ${unchanged.length === 1 ? 'is' : 'are'} already right`,
  }
}

export const NO_SLOT_NOTE = 'Left untouched. Navidrome will list them as an album of their own.'

/** The one fixed sentence before a retag (contract, "Client behaviour" 6). */
export const commitSentence = n =>
  `lb-bot rewrites the tags of ${plural(n, 'file', 'files')} and keeps the previous ones, so this can be undone.`

/** What the commit button does and says. `kind`: apply (the retag), cover (the
 *  cover alone: nothing to rewrite, and one was picked), none. */
export function commitView(summary, coverPicked) {
  if (!summary) return { kind: 'none', label: 'Retag', disabled: true, line: '' }
  if (summary.changing > 0) {
    return { kind: 'apply', label: `Retag ${plural(summary.changing, 'file', 'files')}`, disabled: false,
             line: commitSentence(summary.changing) }
  }
  // Nothing to rewrite. That is "already carry it" only when the files have
  // their places on the release.
  const line = summary.matched > 0 ? 'The files already carry this release.'
    : 'None of this album’s files has a place on this release.'
  return coverPicked
    ? { kind: 'cover', label: 'Save the cover', disabled: false, line }
    : { kind: 'none', label: 'Retag 0 files', disabled: true, line }
}

// ── Refusals ─────────────────────────────────────────────────────────────────
export const poorMatchQuestion = (matched, total) =>
  `Only ${matched} of this album’s ${total} files have a place on this release. Retag them anyway?`

/** A refusal in words, from the error `lib/api.js` throws (`status`, `payload`).
 *
 *  - `confirm`: 409 `poor_match` with its two numbers — ask, naming both, then
 *    resend with `force`.
 *  - `retry`: a dependency did not answer (502 and its kin, `retryable`, or no
 *    answer at all) — Try again is offered.
 *  - `say`: anything else, in lb-bot's own `error` text. `always` offers Try
 *    again there too (X6: a preview's refusal may tell the user to change a
 *    setting "then try again").
 *
 *  A code is never the text. */
export function refusalView(err, { always = false } = {}) {
  const payload = err?.payload && typeof err.payload === 'object' ? err.payload : {}
  const status = Number(err?.status) || 0
  const said = String(payload.error || payload.reason || payload.message || '').trim()
  if (status === 409 && payload.code === 'poor_match'
      && Number.isFinite(payload.matched) && Number.isFinite(payload.total)) {
    return { kind: 'confirm', text: poorMatchQuestion(payload.matched, payload.total), retry: false,
             matched: payload.matched, total: payload.total }
  }
  if (!status || status === 502 || status === 503 || status === 504 || payload.retryable === true) {
    return { kind: 'retry', text: said || 'lb-bot did not answer.', retry: true, matched: 0, total: 0 }
  }
  return { kind: 'say', text: said || `lb-bot refused that (${status}).`, retry: !!always, matched: 0, total: 0 }
}

/** The job a write's answer names: its id, '' when it names none — and then
 *  it is NOT a job (F5). Followed all the same, the watch asked lb-bot about
 *  "undefined", read the 404 as the job's end and said "restored." of an undo
 *  nobody had watched. `fallback`: the row's own id, only where the contract
 *  says the job's id is that one (the album page's undo). */
export function startedId(answer, fallback = '') {
  for (const id of [answer && typeof answer === 'object' ? answer.id : undefined, fallback]) {
    if (typeof id === 'string' && id.trim()) return id.trim()
    if (typeof id === 'number' && Number.isFinite(id)) return String(id)
  }
  return ''
}

/** What a surface says, where its refusals go, of an answer that named no
 *  job: in words, with nothing to send again — the write itself was taken.
 *  `here`: on System › Overrides itself there is no "there" to point at. */
export const NOT_SAID = 'lb-bot did not say what it started.'
export const notSaid = ({ here = false } = {}) => ({
  kind: 'say', text: here ? NOT_SAID : `${NOT_SAID} See ${OVERRIDES_TAB}.`, retry: false, matched: 0, total: 0,
})

// ── Covers ───────────────────────────────────────────────────────────────────
/** What to ask `/api/album/covers` by, or null when there is nothing to ask by.
 *  X8: a release that is not one of the listed editions (a pasted id) belongs
 *  to a group this page does not know — asked by the release alone, lb-bot
 *  derives its own group; the page's `rgid` would list another group's covers. */
export function coverQuery({ picked, current, listed, rgid }) {
  if (picked) return listed?.has(picked) && rgid ? { release_mbid: picked, rgid } : { release_mbid: picked }
  const q = {}
  if (current) q.release_mbid = current
  if (rgid) q.rgid = rgid
  return Object.keys(q).length ? q : null
}

const CAA_IMAGE = new RegExp(`^(https://coverartarchive\\.org/(?:release|release-group)/${UUID}/\\d+)\\.(?:jpe?g|png|gif)$`, 'i')

/** A chosen cover, small: the Archive's own 500 px version of that image.
 *  Any other address is shown as it is. */
export function coverThumbUrl(full) {
  const url = String(full || '')
  const m = CAA_IMAGE.exec(url)
  return m ? `${m[1]}-500.jpg` : url
}

// ── Editions ─────────────────────────────────────────────────────────────────
const lower = v => String(v || '').toLowerCase()

/** The index of the variant a release id belongs to — its own id, the pressing
 *  the library holds, or one of its editions — or -1. */
export function variantOfRelease(variants, mbid) {
  const want = lower(mbid)
  if (!want) return -1
  return (variants || []).findIndex(v => lower(v.releaseMbid) === want || lower(v.ownedReleaseMbid) === want
    || (v.editions || []).some(e => lower(e.releaseMbid) === want))
}

/** Whether the selected chip is the edition the files carry (`carried`: the
 *  album's release id as Navidrome reports it, '' for an untagged album). A
 *  pressing folded into an edition of the same label is that edition's chip. */
export function carriesSelection(variants, sel, carried) {
  const want = lower(carried)
  const variant = variants?.[sel?.variant]
  if (!want || !variant) return false
  const edition = variant.editions?.[sel.edition]
  if (!edition) return lower(variant.releaseMbid) === want
  return lower(edition.releaseMbid) === want
    || (lower(variant.ownedReleaseMbid) === want && edition.label === variant.ownedEdition)
}

/** The chips a release id selects — `{variant, edition}` — or null when it is
 *  none of them. The edition whose id it is; for a pressing folded into an
 *  edition of the same label, that edition; else the release's first chip.
 *  What this selects, `carriesSelection` calls the files' own chip. */
export function selectionOfRelease(variants, mbid) {
  const want = lower(mbid)
  const variant = variantOfRelease(variants, want)
  if (variant < 0) return null
  const v = variants[variant]
  const editions = v.editions || []
  let edition = editions.findIndex(e => lower(e.releaseMbid) === want)
  if (edition < 0 && lower(v.ownedReleaseMbid) === want) edition = editions.findIndex(e => e.label === v.ownedEdition)
  return { variant, edition: Math.max(0, edition) }
}

/** What a release chip is called: its disambiguation, else "Original" for the
 *  first of the list, else its year. `index` is its place in the list. */
export function variantName(variant, index) {
  if (!variant) return ''
  return variant.disambiguation || (index === 0 ? 'Original' : variant.year || `Release ${index + 1}`)
}

/** A release's chip name where it is joined with other words ("your files are
 *  tagged as …", "… · 13 tracks"): what MusicBrainz says of the release in its
 *  own words is isolated (lib/text.js); the page's own words for a release
 *  with none ("Original", a year, "Release 3") are words, not a name. */
export function variantInLine(variant, index) {
  if (!variant) return ''
  return variant.disambiguation ? iso(variant.disambiguation) : variantName(variant, index)
}

/** One line for the selected chip: which release, which pressing. */
export function editionLine(variant, edition, index) {
  if (!variant) return ''
  const parts = [
    variantName(variant, index), edition?.label, edition?.year || variant.year, edition?.country,
    variant.trackCount > 0 ? plural(variant.trackCount, 'track', 'tracks') : '',
  ].filter(Boolean)
  // Said once each (compared as they are); then what MusicBrainz says of the
  // release in its own words is isolated for the line it shares.
  const own = variant.disambiguation || ''
  return parts.filter((p, i) => parts.indexOf(p) === i).map(p => (own && p === own ? iso(p) : p)).join(' · ')
}
