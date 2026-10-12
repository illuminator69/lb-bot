import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useApp, navigate } from '../App.jsx'
import { api, post } from '../lib/api.js'
import { jobs, useJobs } from '../lib/jobs.js'
import {
  NO_SLOT_NOTE, NOT_LISTED_YET, afterJobSteps, albumQuiet, carriesSelection, changeCoverLink, changedParts, lineAtRest, commitView, countsLine,
  coverQuery, coverThumbUrl, editionLine, isRunning, jobView, notSaid, parseReleaseRef, previewRows, previewSummary,
  refusalView, releaseLine, restView, startedId, undoSays, variantInLine, variantOfRelease,
} from '../lib/overrides.js'
import { nb, splitStillWorking } from '../lib/merges.js'
import { iso } from '../lib/text.js'
import Cover from '../components/Cover.jsx'
import { Fluid, Grp, Meter, Roll, St, useRevealOn } from '../components/Page.jsx'
import { Notice, Skeleton } from '../components/ui.jsx'

// The album override on lb-bot's own album page (I-029, surface A): which
// MusicBrainz release an album the library holds *is* — "My files are this
// edition" retags its files to the edition picked from the chips — the undo,
// and the cover.
//
// The routes are the ones Feishin and Navic use through the hub, and the
// round's contract binds this page as it binds them. What may be said is
// `lib/overrides.js`'s; a job is followed by `lib/jobs.js`, outside this page,
// so leaving the page stops nothing.
//
// Two halves. The components here are given what to show and say nothing they
// are not given: each state of the surface is one set of props (a scratch
// harness renders every one). `useAlbumOverride` is the page's side: what it
// has read, what it is asking, which job it shows.

const enc = encodeURIComponent
const album = e => e?.data?.album || ''
const carried = e => String(e?.data?.releaseMbid || '').toLowerCase()

// ── The line under the album's title ─────────────────────────────────────────
// What the album is now: nothing for an album lb-bot has not changed; "Tags
// changed by lb-bot · when · Undo" for one it has; the job's own line while a
// job runs — the state word, the whole job's meter, "3 of 6".
//
//   entry   { status: loading | ok | error, data: AlbumState, albumId, error }
//   job     the watch's job for its row, when it shows one
//   name    the album's title, said only when the page holds several
//   of      the album's title, for what its Undo tells a screen reader (F7)
export function ChangedLine({ entry, job, name = '', of = '', now, busy = false, onUndo, onRetry }) {
  const lead = name ? <span className="muted">{iso(name)} · </span> : null
  if (entry.status === 'loading' && !entry.data) return <Skeleton className="mt-2.5 h-3 w-2/5" />
  if (entry.status === 'error' && !entry.data) {
    return (
      <div className="mt-2 fadein">
        {lead}{entry.error?.text}{' '}
        {onRetry && <button className="link" onClick={onRetry}>Try again</button>}
      </div>
    )
  }
  const row = entry.data?.override || null
  // What the line is, decided in the lib (`lineAtRest`, with its matrix): a
  // row in a state this page does not know says nothing, and breaks nothing.
  const line = lineAtRest(row, job)
  if (!line) return null
  const { v } = line
  if (line.kind === 'running') {
    // A <div>: the meter is a block, which a <p> cannot hold. Not a live
    // region: the count moves with every poll, and the page speaks a job's
    // state once, elsewhere (`spoken`).
    return (
      <div className="mt-2 flex flex-wrap items-center gap-x-2.5 gap-y-1 fadein">
        {lead}<St tone={v.tone}>{v.word}</St>
        {v.bar && <div style={{ width: 120, maxWidth: '40%' }}><Meter value={v.percent} /></div>}
        {v.count && <span className="num muted">{v.count}</span>}
      </div>
    )
  }
  // At rest. A job that just ended says how. A row read with the album says
  // what lb-bot changed and when — with how many problems, when it has any;
  // one that did not finish says that.
  const changed = line.kind === 'changed'
  const when = changed ? changedParts(row, now) : null
  const undo = (!v || v.canUndo) && row && onUndo
    ? <button className="link" disabled={busy} aria-busy={busy} aria-label={of ? undoSays(v?.undoLabel || 'Undo', of) : undefined}
        onClick={onUndo}>{v?.undoLabel || 'Undo'}</button>
    : null
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-0.5 fadein">
      {lead}
      {/* What changed is the statement; when is secondary (the frame's `muted`). */}
      {changed
        ? <span>{when.what}{when.when ? <span className="muted"> · {when.when}</span> : null}{v?.problemsHeading ? ` · ${v.problemsHeading}` : ''}</span>
        // A job that went silent says where everything lb-bot changed is listed: the tab's name is the way there.
        : <St tone={v.tone}>{v.seeOverrides ? <JobLine line={v.line} /> : v.word}</St>}
      {/* Retagged, and Navidrome has not picked the album up yet: said of an
          applied row only — a blank id on any other row is just not known. */}
      {row?.state === 'applied' && !entry.albumId && <span className="muted">{NOT_LISTED_YET}</span>}
      {undo}
    </div>
  )
}

// A job's sentence. One of them names a place — "Still working — see System ›
// Overrides" — and there the name is the way to it (the words stay as they are).
export function JobLine({ line }) {
  const parts = splitStillWorking(line)
  if (!parts) return line
  return <>{parts.before}<button className="link" onClick={() => navigate('system', 'overrides')}>{parts.link}</button>{parts.after}</>
}

// ── A job, as the one card it is ─────────────────────────────────────────────
// The sentence, the state word, the whole job's meter while files are being
// written, the problems lb-bot lists (the count, then "title — reason" each),
// and what can be done once it has ended. Every part folds in and out.
//   wrap   the title is two names ("From → Into", a merge's): it wraps, where
//          an album's is cut to one line
//   of     the album its Undo is for, where a page can hold several such
//          buttons: what a screen reader is told (F7)
export function JobCard({ title = '', v, busy = false, onUndo, onDone, onOpen, notListed = false, wrap = false, of = '' }) {
  // A job that went silent has nothing to press (it is no end): no empty row of actions.
  const acts = v.ended && (!!onOpen || (v.canUndo && !!onUndo) || !!onDone)
  return (
    <div className="rows">
      <div className="row col">
        <div className="flex min-w-0 items-center gap-3">
          <div className="grow">
            {!title ? null : wrap ? <div className="s" style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{nb(title)}</div>
              : <div className="s clip">{title}</div>}
            <div className="t" style={{ whiteSpace: 'normal' }}><JobLine line={v.line} /></div>
            {notListed && v.ended ? <div className="s" style={{ whiteSpace: 'normal' }}>{NOT_LISTED_YET}</div> : null}
          </div>
          <St tone={v.tone}>{v.word}</St>
        </div>
        <Roll open={v.bar}>
          <div className="pt-2.5"><Meter lg value={v.percent} /></div>
        </Roll>
        <Roll open={v.problems.length > 0}>
          <div className="pt-2.5">
            <div className="s font-semibold">{v.problemsHeading}</div>
            {v.problems.map((p, i) => (
              <div key={i} className="s" style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>
                {p.title ? `${p.title} — ${p.reason}` : p.reason}
              </div>
            ))}
          </div>
        </Roll>
        <Roll open={acts}>
          <div className="acts pt-2.5">
            {/* The files are another album's now: this page is no longer theirs. */}
            {onOpen && <button className="sm" onClick={onOpen}>Open the album it is now</button>}
            {v.canUndo && onUndo && (
              <button className="sm" disabled={busy} aria-busy={busy} aria-label={of ? undoSays(v.undoLabel, of) : undefined}
                onClick={onUndo}>{v.undoLabel}</button>
            )}
            {onDone && <button className="sm quiet" onClick={onDone}>Done</button>}
          </div>
        </Roll>
      </div>
    </div>
  )
}

// ── Which of the library's albums ────────────────────────────────────────────
// A release group the library holds twice (two pressings, say): the block is
// about one of them, and the user says which before anything is asked. Plain
// buttons in a group, one pressed — not a listbox, which would owe arrow keys.
//
//   rows   [{ key, albumId, title, sub, same }] — `same`: it already is the
//          edition picked, so there is nothing to make of it
export function AlbumChooser({ rows, value, onChange, question = 'Which of your albums is this edition?' }) {
  return (
    <>
      <p className="mb-1.5">{question}</p>
      <div className="rows" role="group" aria-label={question}>
        {rows.map(r => (
          <button key={r.key} className="row tap" aria-pressed={r.key === value} disabled={r.same}
            onClick={() => onChange(r.key)}>
            <Cover albumId={r.albumId} name={r.title} size={36} />
            <span className="grow">
              <span className="t clip block">{r.title}</span>
              <span className="s clip block">{r.sub}</span>
            </span>
            {r.same ? <span className="st ok">This edition</span> : null}
          </button>
        ))}
      </div>
    </>
  )
}

// ── Covers to choose from ────────────────────────────────────────────────────
// The Cover Art Archive's fronts for a release and its group, after the cover
// the album has now — "Current cover", ringed until another is chosen: the
// ring on it is what keeping means. Skeleton tiles while lb-bot asks; one
// sentence when the Archive has none; a note with Try again when it did not
// answer. A tile's label wraps and is never cut; two tiles of one kind are
// told apart by their number.
//
//   state    null | { status: loading | ok | error, list: [Cover], refusal }
//   value    the chosen image's `full` address, '' for the current cover
//   current  { albumId, full } — the cover the album has now (`full` when
//            lb-bot wrote it, so the same image in the list is not shown twice)
export function CoverChoices({ state, value, onChange, onRetry, current, keepLabel = 'Current cover', disabled = false }) {
  const status = state?.status || 'loading'
  const all = state?.list || []
  // The image lb-bot wrote is the "current" tile: not offered a second time.
  const list = all.filter(c => c.full !== current?.full)
  const kind = c => (c.source === 'release' ? 'This release' : 'Release group')
  const tile = (key, pressed, onClick, art, label, spoken = label) => (
    <div className="tile" key={key}>
      <button className="art" aria-pressed={pressed} disabled={disabled} onClick={onClick} aria-label={spoken}>{art}</button>
      {/* `.s` here is the cover grid's own (it wraps and centres); the colour and size are the frame's secondary pair. */}
      <div className="s muted text-caption">{label}</div>
    </div>
  )
  return (
    <>
      <div className={`grid covers${status === 'ok' ? ' arrive' : ''}`}>
        {tile('keep', value === '', () => onChange(''),
          <Cover albumId={current?.albumId} url={current?.full ? coverThumbUrl(current.full) : undefined} name={keepLabel} fluid />,
          keepLabel)}
        {status === 'loading' && [0, 1, 2].map(i => (
          <div className="tile" key={`sk${i}`} aria-hidden="true">
            <Skeleton style={{ width: '100%', aspectRatio: '1 / 1' }} />
            <Skeleton className="h-3 w-3/5 self-center" />
          </div>
        ))}
        {status === 'ok' && list.map(c => {
          const same = list.filter(x => kind(x) === kind(c))
          const spoken = same.length > 1 ? `${kind(c)}, cover ${same.indexOf(c) + 1} of ${same.length}` : kind(c)
          return tile(c.full, value === c.full, () => onChange(c.full),
            <Cover url={c.thumb} fallbackUrl={c.full} name={c.full} fluid />, kind(c), spoken)
        })}
      </div>
      {status === 'ok' && !list.length && (
        <p className="muted mt-2 fadein">
          {all.length ? 'The Cover Art Archive has no other cover for this release.'
            : 'The Cover Art Archive has no cover for this release.'}
        </p>
      )}
      {status === 'error' && (
        <div className="mt-2.5 fadein">
          <Notice tone="quiet" title="Couldn’t reach the Cover Art Archive"
            actions={onRetry && <button className="sm" onClick={onRetry}>Try again</button>}>
            {state.refusal?.text}
          </Notice>
        </div>
      )}
    </>
  )
}

const PreviewRow = ({ r }) => (
  <div className="row wrap" style={{ columnGap: 10 }}>
    <span className="num" style={{ flex: '1 1 210px', minWidth: 0, overflowWrap: 'anywhere' }}>{r.from}</span>
    {r.to ? (
      <>
        <span className="muted" aria-label="becomes">→</span>
        <span className="num" style={{ flex: '1 1 210px', minWidth: 0, overflowWrap: 'anywhere' }}>{r.to}</span>
      </>
    ) : null}
  </div>
)

// The size of the common answer — the release, the counts, one sentence, one
// row of cover tiles, the fixed sentence, the button — so that the answer
// takes the skeleton's place and little moves (V3). It drew rows for every
// file, which the usual answer folds into one sentence: 230 px gone in a frame.
function PreviewSkeleton() {
  return (
    <div className="fadein" aria-busy="true" aria-label="Asking lb-bot what would change">
      <Skeleton className="mb-1.5 h-4 w-3/5" />
      <Skeleton className="mb-3 h-3 w-2/5" />
      <Skeleton className="h-3 w-4/5" />
      <Skeleton className="mb-2 mt-4 h-3 w-14" />
      <div className="grid covers">
        {[0, 1, 2].map(i => (
          <div className="tile" key={i}>
            <Skeleton style={{ width: '100%', aspectRatio: '1 / 1' }} />
            <Skeleton className="h-3 w-3/5 self-center" />
          </div>
        ))}
      </div>
      <Skeleton className="mt-4 h-3 w-4/5" />
      <Skeleton className="mt-2.5 h-8 w-36" />
    </div>
  )
}

// ── What changes ─────────────────────────────────────────────────────────────
// The answer of POST /api/album/override/preview, under the action that asked
// for it: the release, the counts, the from → to rows, the cover, the one
// fixed sentence, and the button that says what it will do.
//
//   pv        { key, status: loading | ok | error, data: Preview, refusal }
//   covers    as CoverChoices' `state`; `cover` the chosen one ('' = keep)
//   refusal   lb-bot's answer to the commit, when it refused
//   tint      another filled button is on this page (an album with gaps has its
//             own next step): the commit is then in the accent's family, not filled
export function PreviewPanel({
  pv, covers, cover, onCover, onRetryCovers, current,
  committing = false, refusal = null, onCommit, onCancel, onRetry, tint = false,
}) {
  const [showSame, setShowSame] = useState(false)
  const [showRight, setShowRight] = useState(false)
  if (!pv || pv.status === 'loading') return <PreviewSkeleton />
  if (pv.status === 'error') {
    return (
      <div className="fadein">
        <Notice tone="danger" role="status" actions={<>
          {pv.refusal?.retry && onRetry && <button className="sm" onClick={onRetry}>Try again</button>}
          <button className="sm quiet" onClick={onCancel}>Cancel</button>
        </>}>{pv.refusal?.text}</Notice>
      </div>
    )
  }
  const summary = previewSummary(pv.data)
  const rows = previewRows(pv.data)
  const commit = commitView(summary, !!cover)
  const counts = countsLine(summary)
  return (
    <div className="fadein">
      <div className="t" style={{ overflowWrap: 'anywhere' }}>{releaseLine(pv.data.release)}</div>
      {counts ? <div className="muted">{counts}</div> : null}

      {rows.differ.length > 0 && <div className="rows mt-2.5">{rows.differ.map(r => <PreviewRow key={r.key} r={r} />)}</div>}
      {rows.sameLabel.length > 0 && (
        <div className="mt-2">
          <span className="muted">{rows.sameLabelText}. </span>
          <button className="link plain underline" aria-expanded={showSame} onClick={() => setShowSame(x => !x)}>
            {showSame ? 'Hide them' : 'Show them'}
          </button>
          <Roll open={showSame}>
            <div className="rows mt-2">{rows.sameLabel.map(r => <PreviewRow key={r.key} r={{ ...r, to: '' }} />)}</div>
          </Roll>
        </div>
      )}
      {rows.unchanged.length > 0 && (
        <div className="mt-2">
          <button className="link plain underline" aria-expanded={showRight} onClick={() => setShowRight(x => !x)}>
            {rows.unchangedLabel}
          </button>
          <Roll open={showRight}>
            <div className="rows mt-2">{rows.unchanged.map(r => <PreviewRow key={r.key} r={{ ...r, to: '' }} />)}</div>
          </Roll>
        </div>
      )}
      {rows.noSlot.length > 0 && (
        <div className="mt-3">
          <div className="mb-1.5">
            <span className="warn">{rows.noSlot.length === 1 ? '1 file has no place on this release.' : `${rows.noSlot.length} files have no place on this release.`}</span>
            {' '}{NO_SLOT_NOTE}
          </div>
          <div className="rows">{rows.noSlot.map(r => <PreviewRow key={r.key} r={r} />)}</div>
        </div>
      )}

      <div className="grp" style={{ margin: '16px 0 8px' }}>Cover</div>
      <CoverChoices state={covers} value={cover} onChange={onCover} onRetry={onRetryCovers} current={current} disabled={committing} />

      <p className="mt-4">{commit.line}</p>
      <Roll open={!!refusal}>
        <div className="pt-2.5">
          {/* lb-bot's own words. When they are of the kind worth sending again
              (a dependency that did not answer), Try again is the same press. */}
          <Notice tone="danger" role="alert"
            actions={refusal?.retry && <button className="sm" disabled={committing} onClick={onCommit}>Try again</button>}>
            {refusal?.text}
          </Notice>
        </div>
      </Roll>
      <div className="acts mt-2.5">
        <button className={commit.disabled ? '' : tint ? 'tint' : 'primary'} disabled={commit.disabled || committing}
          aria-busy={committing} onClick={onCommit}>
          {committing && commit.kind === 'cover' ? 'Saving the cover…' : commit.label}
        </button>
        <button className="quiet" disabled={committing} onClick={onCancel}>Cancel</button>
        {committing && commit.kind === 'cover' && <span className="muted">A large image can take most of a minute.</span>}
      </div>
    </div>
  )
}

// ── The block between the edition chips and the tracklist ────────────────────
// There from the start, shut; it opens when the chip selected is not the
// edition the files carry, and for as long as there is a job, a preview or a
// refusal to show. Everything in it folds or fades, a height that changes
// while it is open moves there (`Fluid`), and what a press opens is brought
// into view (`useRevealOn`): nothing pops, and nothing opens off the screen.
//
//   other      the chip selected is not the edition the files carry: its line
//              and "My files are this edition" are on show
//   line       the selected edition, in words; `pastedLine` a pasted release's
//   note       what the files are tagged as instead (not said while a job runs)
//   chooser    AlbumChooser's props, when the library holds several albums here
//   canAct     an album is chosen and lb-bot has an id for it
//   pasting    { open, text, error, active }; a pasted release is previewed like a chip
//   preview    PreviewPanel's props while the preview is open, else null
//   cards      [{ key, title, v, busy, onUndo, onDone, notListed, of, silent }] — the jobs
//              (`silent`: lb-bot stopped answering about it — no end, so it
//              still holds the block: F6)
//   refusal    { text, retry, onRetry, onDismiss } — lb-bot refused an undo
//   focusChip  () → puts the keyboard on the selected chip: where it goes when
//              what a press opened has shut and the block with it
export function EditionBlock({
  open, other = true, line, note, lineKey = '', chooser = null, canAct = true, waiting = '',
  onPick, pasting, onPasteToggle, onPasteText, onPasteGo,
  preview = null, previewKey = '', cards = [], refusal = null, pastedLine = '', focusChip,
}) {
  // While a job runs — or has gone silent, which is no end (F6) — its card is
  // the block: nothing else is offered for the album.
  const running = cards.some(c => !c.v.ended || c.silent)
  const flowing = !running && (other || !!preview || !!pasting?.open)
  const previewing = !!preview && !pasting?.active
  const box = useRef(null)
  const actions = useRef(null)
  const action = useRef(null)
  const paste = useRef(null)
  // What a press opened, and — marked `~` — what arrived for it afterwards:
  // an answer is not brought into view once the user has scrolled away.
  useRevealOn(box, !open ? [] : [
    'open',
    preview && `preview:${previewKey}`,
    preview && `~answer:${previewKey}:${preview.pv?.status || ''}:${preview.covers?.status || ''}`,
    preview?.refusal && '~commit refused',
    pasting?.open && 'paste', pasting?.error && 'paste refused',
    ...cards.map(c => `~card:${c.key}:${c.v.ended ? 'ended' : 'running'}`),
    refusal && '~refused',
  ].filter(Boolean), preview ? actions : undefined)
  // The keyboard goes back to the control that opened what has just shut: the
  // action while it is on show, the paste link while its field is, else the
  // chip — the block itself is folding away.
  // `preventScroll`: a browser jumps to what it focuses when that is out of
  // view (the action, above a long preview) — a step in one frame, where
  // everything else here folds.
  const back = () => {
    if (other && action.current) action.current.focus({ preventScroll: true })
    else if (pasting?.open && paste.current) paste.current.focus({ preventScroll: true })
    else focusChip?.()
  }
  const then = fn => (fn ? () => { fn(); back() } : undefined)
  return (
    <Roll open={open}>
      <div className="next" style={{ margin: '0 0 14px' }} ref={box}>
        <Grp>Edition<span className="note">{running ? '' : note}</span></Grp>

        <Roll open={cards.length > 0}>
          <div className="flex flex-col gap-2 pt-2.5">
            {cards.map(({ key, onDone, silent, ...card }) => <JobCard key={key} {...card} onDone={then(onDone)} />)}
          </div>
        </Roll>

        <Roll open={!!refusal}>
          <div className="pt-2.5">
            <Notice tone="danger" role="alert" actions={<>
              {refusal?.retry && <button className="sm" onClick={refusal.onRetry}>Try again</button>}
              <button className="sm quiet" onClick={then(refusal?.onDismiss)}>Dismiss</button>
            </>}>{refusal?.text}</Notice>
          </div>
        </Roll>

        <Roll open={flowing}>
          <div className="pt-2.5">
            {chooser ? <div className="mb-3"><AlbumChooser {...chooser} /></div> : null}
            <Roll open={other && !!line}>
              {/* Another chip's line fades in over the last one's. */}
              <div key={lineKey} className="fadein t" style={{ overflowWrap: 'anywhere', paddingBottom: 10 }}>{line}</div>
            </Roll>
            {/* The action, and at the end of its own row the way to a release
                the chips do not list — so that "What changes" follows the
                action that asked for it. */}
            <div className="acts" ref={actions}>
              {other && (
                // It stays where it is once pressed, and keeps the keyboard:
                // `aria-disabled`, not `disabled`, while what it opened is open.
                <button ref={action} className="fadein" disabled={!canAct} aria-disabled={previewing || undefined}
                  aria-expanded={previewing} onClick={previewing ? undefined : onPick}>
                  My files are this edition
                </button>
              )}
              <button ref={paste} className="link plain underline" aria-expanded={!!pasting?.open} onClick={onPasteToggle}>
                Not listed? Paste a MusicBrainz release link
              </button>
            </div>
            {waiting ? <p className="muted mt-1.5">{waiting}</p> : null}
            <Roll open={!!pasting?.open}>
              <form className="acts pt-2.5" onSubmit={e => { e.preventDefault(); onPasteGo() }}>
                <input className="grow" style={{ minWidth: 200 }} value={pasting?.text || ''} onChange={e => onPasteText(e.target.value)}
                  placeholder="https://musicbrainz.org/release/…" aria-label="A MusicBrainz release id or address"
                  aria-invalid={!!pasting?.error} />
                <button type="submit" disabled={!canAct || !(pasting?.text || '').trim()}>Show what changes</button>
              </form>
              {pasting?.error ? <p className="bad mt-1.5">{pasting.error}</p> : null}
            </Roll>
          </div>
        </Roll>

        <Roll open={flowing && !!preview}>
          <div className="pt-3">
            <div className="grp" style={{ margin: '0 0 8px' }}>
              What changes{pastedLine ? <span className="note">for the release you pasted</span> : null}
            </div>
            {/* The skeleton, its answer, a refusal, another edition's preview:
                each takes the last one's place at its own height, and the box
                moves there. */}
            <Fluid>
              <div key={previewKey}>{preview ? <PreviewPanel {...preview} onCancel={then(preview.onCancel)} /> : null}</div>
            </Fluid>
          </div>
        </Roll>
      </div>
    </Roll>
  )
}

// ── "Change cover" ───────────────────────────────────────────────────────────
// Under the hero: the cover grid, the current one marked, and one button. A
// large original takes lb-bot most of a minute to fetch and write, and the
// button says so while it does.
//   held   lb-bot is still changing the chosen album's files: nothing is saved
//          onto it meanwhile (F6)
export function ChangeCover({ open, chooser = null, covers, value, onChange, onRetry, current, saving = false, held = false, refusal = null, onSave, onClose }) {
  const box = useRef(null)
  useRevealOn(box, !open ? [] : [
    'open', `~covers:${covers?.status || ''}`, chooser?.value && `album:${chooser.value}`, refusal && '~refused',
  ].filter(Boolean))
  return (
    <Roll open={open}>
      <div className="next" style={{ margin: '16px 0 0' }} ref={box}>
        <Grp>Cover<span className="note">from the Cover Art Archive</span></Grp>
        <div className="pt-2.5">
          {chooser ? <div className="mb-3"><AlbumChooser {...chooser} question="Which of your albums gets the cover?" /></div> : null}
          {/* Skeleton tiles become tiles, a sentence, or a note: the box moves
              to whichever height that is. */}
          <Fluid>
            {chooser && !chooser.value ? null : (
              <CoverChoices state={covers} value={value} onChange={onChange} onRetry={onRetry} current={current} disabled={saving} />
            )}
            <Roll open={!!refusal}>
              <div className="pt-2.5">
                <Notice tone="danger" role="alert"
                  actions={refusal?.retry && <button className="sm" disabled={saving || held} onClick={onSave}>Try again</button>}>
                  {refusal?.text}
                </Notice>
              </div>
            </Roll>
          </Fluid>
          <div className="acts mt-3">
            <button className="primary" disabled={!value || saving || held} aria-busy={saving} onClick={onSave}>
              {saving ? 'Saving the cover…' : 'Use this cover'}
            </button>
            <button className="quiet" disabled={saving} onClick={onClose}>Cancel</button>
            {saving && <span className="muted">A large image can take most of a minute.</span>}
          </div>
        </div>
      </div>
    </Roll>
  )
}

// ── The page's side ──────────────────────────────────────────────────────────
// What the album page holds of the library's albums in this release group
// (`GET /api/album/override?album_id=`, once per album), and the flow on them.
//
//   indexIds   the Navidrome album ids the library index files under this
//              release group (and the gap's own): the only ids the page has.
//              A retag is expected to change an album's id, so after a job the
//              album is read under the id its row then carries. An id is asked
//              for when the index names one that no album on the page has —
//              so an album an undo gives its old id back is found again, and
//              so is what an apply left behind under the old id (files with no
//              place on the release stay an album of their own there).
//   openAlbum  (rgid) → opens that album's page: a pasted release can belong
//              to another release group, and the album is then no longer this
//              page's.
//   tint       the page has a filled button of its own (an album with gaps)
//   onChanged  a job changed the library: the page re-reads what it shows.
export function useAlbumOverride({ rgid, indexIds, variants, sel, tint = false, onChanged, openAlbum }) {
  const { pushToast, requestConfirm } = useApp()
  // One entry per album of the library the page has read. `key` is the
  // entry's own (it outlives the album's id); `albumId` the id as it is now.
  const [entries, setEntries] = useState([])      // { key, albumId, status, data, error }
  const entriesRef = useRef(entries)
  entriesRef.current = entries
  const serial = useRef(0)
  // An id moved or died under the page: what the index said on arrival is no
  // longer where the albums are.
  const [moved, setMoved] = useState(false)
  const changedRef = useRef(onChanged)
  changedRef.current = onChanged
  // A job can end after the page is left (the watch goes on): nothing is read
  // again, and no timer set, for a page that is gone. Set in the effect, not
  // only cleared: under StrictMode the dev server unmounts and mounts once.
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const changed = useCallback(() => { if (alive.current) changedRef.current?.() }, [])
  // The jobs this page shows, each with the album it is for.
  const [shown, setShown] = useState([])          // { id, key }
  const [now, setNow] = useState(() => Date.now() / 1000)

  const patch = useCallback((key, change) => setEntries(list => list.map(e => (
    e.key === key ? { ...e, ...(typeof change === 'function' ? change(e) : change) } : e))), [])
  const show = useCallback((id, key) => setShown(list => (
    list.some(s => s.id === id) ? list.map(s => (s.id === id ? { id, key } : s)) : [...list, { id, key }])), [])

  // An album as lb-bot just described it. A row that is running is a job:
  // found here, it is followed by the watch — whoever started it.
  const shownRef = useRef(shown)
  shownRef.current = shown
  const take = useCallback((key, data) => {
    patch(key, { status: 'ok', data, albumId: data.albumId || '', error: null })
    setNow(Date.now() / 1000)
    if (data.override && isRunning(data.override.state)) {
      // (A job this page gave up on is taken up again by this: X3 allows it.)
      if (jobs.adopt('album', data.override, data)) show(data.override.id, key)
      return
    }
    // The album's own state says nothing is running: a job this page gave up
    // on (it went silent, which is no end) is let go of — the album says how
    // it ended, and what the block offers is offered again (F6).
    const silent = shownRef.current.filter(s => s.key === key && jobs.get('album', s.id)?.phase === 'timeout')
    if (!silent.length) return
    silent.forEach(s => jobs.dismiss('album', s.id))
    setShown(list => list.filter(s => !silent.some(x => x.id === s.id)))
  }, [patch, show])

  const load = useCallback(async (key, albumId) => {
    patch(key, e => ({ status: e.data ? e.status : 'loading', error: null }))
    try {
      take(key, await api(`/api/album/override?album_id=${enc(albumId)}`))
    } catch (e) {
      if (e.status === 404 && e.payload?.code === 'unknown_album') {
        // Navidrome no longer lists that id: what the page read of the index
        // is behind (a retag from another client; the artist's discography is
        // kept for the session). The index is read again; the same list of
        // ids asks for nothing a second time.
        setMoved(true)
        patch(key, { status: 'gone' })
        changed()
      } else {
        patch(key, x => ({ status: x.data ? 'ok' : 'error', error: refusalView(e, { always: true }) }))
      }
    }
  }, [patch, take])

  // Run when the index's list of ids changes, never for the same list again.
  const idsKey = indexIds.join(',')
  useEffect(() => {
    // An album that is gone holds no id: the id can be an album's again.
    const live = entriesRef.current.filter(e => e.status !== 'gone')
    const add = indexIds.filter(id => !live.some(e => e.albumId === id))
      .map(id => ({ key: `a${++serial.current}`, albumId: id, status: 'loading', data: null, error: null }))
    if (!add.length) return
    // Written here as well as on the next render: under StrictMode (the dev
    // server) this effect runs twice with no render between, and the second
    // run added every album again — one album then had a chooser.
    entriesRef.current = [...entriesRef.current, ...add]
    setEntries(list => [...list, ...add.filter(e => !list.some(x => x.status !== 'gone' && x.albumId === e.albumId))])
    add.forEach(e => load(e.key, e.albumId))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idsKey])

  const jobMap = useJobs('album', shown.map(s => s.id))

  // A job ended: the album is read again under the id its row now carries.
  const handled = useRef(new Map())
  const version = jobs.version()
  useEffect(() => {
    for (const { id, key } of shown) {
      const job = jobMap[id]
      if (!job || job.phase === 'polling' || job.phase === 'timeout') continue
      const sig = `${job.op}:${job.phase}:${job.row?.setAt ?? ''}`
      if (handled.current.get(id) === sig) continue
      handled.current.set(id, sig)
      ;(async () => {
        const entry = entriesRef.current.find(e => e.key === key)
        const plan = afterJobSteps(entry?.albumId || '', job)
        if (plan.use) {
          if (plan.dead) setMoved(true)
          take(key, plan.use)
        } else {
          let found = null
          for (const step of plan.steps || []) {
            try {
              found = await api(step === 'album'
                ? `/api/album/override?album_id=${enc(entry.albumId)}`
                : `/api/album/override?id=${enc(id)}`)
              break
            } catch (e) {
              if (e.status === 404 && e.payload?.code) {
                if (step === 'album') setMoved(true)
                continue
              }
              // Not read: what is on the page stays, and says why.
              patch(key, { error: refusalView(e, { always: true }) })
              found = undefined
              break
            }
          }
          if (found) take(key, found)
          else if (found === null) patch(key, { status: 'gone' })
        }
        changed()
      })()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version, shown])

  // An album lb-bot retagged that Navidrome had not listed again when the job
  // ended: lb-bot goes on looking, and so does the page, for two minutes.
  const awaited = e => e.status === 'ok' && !e.albumId && e.data?.override?.state === 'applied'
  const unlisted = entries.filter(awaited).map(e => e.key).join(',')
  useEffect(() => {
    if (!unlisted) return
    let tries = 0
    const timer = setInterval(async () => {
      tries += 1
      if (tries > 24) { clearInterval(timer); return }
      for (const e of entriesRef.current) {
        if (!awaited(e)) continue
        try {
          const data = await api(`/api/album/override?id=${enc(e.data.override.id)}`)
          if (data.albumId) { take(e.key, data); changed() }
        } catch (err) {
          if (err.status === 404) patch(e.key, { status: 'gone' })
        }
      }
    }, 5000)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unlisted])

  // "5 min ago" is read again every half minute while a changed album shows.
  const anyChanged = entries.some(e => e.data?.override)
  useEffect(() => {
    if (!anyChanged) return
    const t = setInterval(() => setNow(Date.now() / 1000), 30_000)
    return () => clearInterval(t)
  }, [anyChanged])

  const visible = entries.filter(e => e.status !== 'gone')
  const ready = visible.filter(e => e.data)
  const [activeKey, setActiveKey] = useState('')
  const active = ready.length === 1 ? ready[0] : ready.find(e => e.key === activeKey) || null

  const variant = variants?.[sel.variant]
  const edition = variant?.editions?.[sel.edition]
  const selected = String(edition?.releaseMbid || variant?.releaseMbid || '').toLowerCase()
  const listed = useMemo(() => {
    const ids = new Set()
    for (const v of variants || []) {
      if (v.releaseMbid) ids.add(String(v.releaseMbid).toLowerCase())
      for (const e of v.editions || []) if (e.releaseMbid) ids.add(String(e.releaseMbid).toLowerCase())
    }
    return ids
  }, [variants])
  const isSame = e => carriesSelection(variants, sel, carried(e))
  const mismatch = !!selected && ready.some(e => !isSame(e))

  // ── the flow: a preview, its covers, the commit ──
  const [flow, setFlow] = useState({ open: false, pasted: '' })
  const [pasting, setPasting] = useState({ open: false, text: '', error: '' })
  const [pv, setPv] = useState(null)
  const [pvNonce, setPvNonce] = useState(0)
  const [covers, setCovers] = useState(null)
  const [coverNonce, setCoverNonce] = useState(0)
  const [cover, setCover] = useState('')
  const [committing, setCommitting] = useState(false)
  const [refusal, setRefusal] = useState(null)
  const [undoing, setUndoing] = useState('')
  const [undoRefusal, setUndoRefusal] = useState(null)

  const target = flow.pasted || selected
  // The chip picked is the edition the files carry after all: there is
  // nothing to preview, and nothing is asked — the flow shuts.
  const asking = flow.open && !!active && (!!flow.pasted || !isSame(active))
  useEffect(() => {
    if (flow.open && !asking && active) setFlow({ open: false, pasted: '' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flow.open, asking, active?.key])

  // One request at a time: picking another edition drops the one that is out.
  const pvKey = asking && active.albumId && target ? `${active.albumId}|${target}` : ''
  useEffect(() => {
    setCover('')
    setRefusal(null)
    if (!pvKey) { setPv(null); return }
    const [albumId, release] = pvKey.split('|')
    const ctl = new AbortController()
    setPv({ key: pvKey, status: 'loading' })
    api('/api/album/override/preview', {
      method: 'POST', body: JSON.stringify({ album_id: albumId, release_mbid: release }), signal: ctl.signal,
    })
      .then(data => setPv({ key: pvKey, status: 'ok', data }))
      .catch(e => {
        if (e.name === 'AbortError') return
        setPv({ key: pvKey, status: 'error', refusal: refusalView(e, { always: true }) })
      })
    return () => ctl.abort()
  }, [pvKey, pvNonce])

  const loadCovers = useCallback((query, set, signal) => {
    if (!query) { set({ status: 'ok', list: [] }); return }
    set({ status: 'loading', list: [] })
    api('/api/album/covers?' + new URLSearchParams(query), { signal })
      .then(r => set({ status: 'ok', list: r.covers || [] }))
      .catch(e => {
        if (e.name === 'AbortError') return
        set({ status: 'error', list: [], refusal: refusalView(e) })
      })
  }, [])
  const activeCarried = carried(active)
  const flowCoverKey = pvKey ? JSON.stringify(coverQuery({ picked: target, current: activeCarried, listed, rgid })) : ''
  useEffect(() => {
    if (!flowCoverKey) { setCovers(null); return }
    const ctl = new AbortController()
    loadCovers(JSON.parse(flowCoverKey), setCovers, ctl.signal)
    return () => ctl.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flowCoverKey, coverNonce])

  // ── "Change cover" ──
  const [cc, setCc] = useState({ open: false, pick: '', saving: false, refusal: null })
  const [ccCovers, setCcCovers] = useState(null)
  const [ccNonce, setCcNonce] = useState(0)
  // The image this visit chose, shown in the hero at once: { [key]: { full, thumb } }.
  const [chosen, setChosen] = useState({})
  const ccKey = cc.open && active ? JSON.stringify(coverQuery({ picked: '', current: activeCarried, listed, rgid })) : ''
  useEffect(() => {
    if (!ccKey) return
    const ctl = new AbortController()
    loadCovers(JSON.parse(ccKey), setCcCovers, ctl.signal)
    return () => ctl.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ccKey, ccNonce])

  // The cover picked, and what lb-bot refused, are about the album they were
  // picked for: choosing another album starts over. (Kept, "Use this cover"
  // was live with no tile ringed, and saved album A's pick onto album B.)
  const activeKey2 = active?.key || ''
  useEffect(() => { setCc(c => (c.pick || c.refusal ? { ...c, pick: '', refusal: null } : c)) }, [activeKey2])

  const thumbOf = (list, full) => (list || []).find(c => c.full === full)?.thumb || coverThumbUrl(full)

  async function saveCover(entry, full, thumb) {
    const r = await post('/api/album/cover', { album_id: entry.albumId, cover_url: full })
    setChosen(c => ({ ...c, [entry.key]: { full, thumb } }))
    // The change is a row now: the line under the title, with its Undo — and
    // the toast points there only once that line has been read.
    let line = false
    try {
      const now = await api(`/api/album/override?${r?.id ? `id=${enc(r.id)}` : `album_id=${enc(entry.albumId)}`}`)
      take(entry.key, now)
      line = !!now.override
    } catch { /* the cover is saved; the line comes with the next read */ }
    pushToast(line ? 'Cover changed. Undo is under the album’s title.' : 'Cover changed.')
    changed()
  }

  const coverLink = useRef(null)
  const closeCover = () => {
    setCc({ open: false, pick: '', saving: false, refusal: null })
    coverLink.current?.focus({ preventScroll: true })
  }
  async function saveChangedCover() {
    if (!active || !cc.pick) return
    setCc(c => ({ ...c, saving: true, refusal: null }))
    try {
      await saveCover(active, cc.pick, thumbOf(ccCovers?.list, cc.pick))
      closeCover()
    } catch (e) {
      setCc(c => ({ ...c, saving: false, refusal: refusalView(e) }))
    }
  }

  // "Retag N files" — or the cover alone, when nothing is to be rewritten. A
  // `poor_match` is asked about, naming both numbers, and sent again with
  // `force` for that same release and no other.
  async function commit() {
    // The preview on screen must be this album's and this release's: for one
    // render after a chip is picked it is still the last edition's.
    if (!active || !pv || pv.status !== 'ok' || pv.key !== `${active.albumId}|${target}`) return
    const entry = active
    const release = target
    const what = commitView(previewSummary(pv.data), !!cover)
    if (what.disabled) return
    setCommitting(true)
    setRefusal(null)
    try {
      if (what.kind === 'cover') {
        await saveCover(entry, cover, thumbOf(covers?.list, cover))
        setFlow({ open: false, pasted: '' })
        return
      }
      const body = { album_id: entry.albumId, release_mbid: release, ...(cover ? { cover_url: cover } : {}) }
      let r
      try {
        r = await post('/api/album/override', body)
      } catch (e) {
        const v = refusalView(e)
        if (v.kind !== 'confirm') throw e
        if (!(await requestConfirm(v.text, { confirmLabel: 'Retag anyway' }))) return
        r = await post('/api/album/override', { ...body, force: true })
      }
      // An answer that names no job is not one (F5): nothing is followed, and
      // it is said where a refused commit is — the preview stays open for it.
      const id = startedId(r)
      if (!id) { setRefusal(notSaid()); return }
      // The job is the watch's from here: seeded with the POST's own state.
      jobs.start({ kind: 'album', id, op: 'apply', state: r.state, row: entry.data?.override || null })
      show(id, entry.key)
      setFlow({ open: false, pasted: '' })
      setPasting({ open: false, text: '', error: '' })
    } catch (e) {
      setRefusal(refusalView(e))
    } finally {
      setCommitting(false)
    }
  }

  async function undo(key, row) {
    if (!row?.id) return
    setUndoing(row.id)
    setUndoRefusal(null)
    try {
      const r = await post('/api/album/override/remove', { id: row.id })
      // (The undo's job is the row's own id by contract: that one is the fallback.)
      const id = startedId(r, row.id)
      jobs.start({ kind: 'album', id, op: 'remove', state: r.state, row })
      show(id, key)
      setChosen(c => { const next = { ...c }; delete next[key]; return next })
    } catch (e) {
      setUndoRefusal({ ...refusalView(e), key, row })
    } finally {
      setUndoing('')
    }
  }

  const dismiss = id => {
    jobs.dismiss('album', id)
    setShown(list => list.filter(s => s.id !== id))
  }

  function pasteGo() {
    const mbid = parseReleaseRef(pasting.text)
    if (!mbid) {
      setPasting(p => ({ ...p, error: 'That is not a MusicBrainz release: paste a release’s address (musicbrainz.org/release/…) or its id.' }))
      return
    }
    setPasting(p => ({ ...p, error: '' }))
    setFlow({ open: true, pasted: mbid })
  }

  // ── what each part is given ──
  const many = ready.length > 1
  const entryOf = key => entries.find(e => e.key === key)
  // The job shown for an album: the last one this page took up for it.
  const jobOf = e => {
    const mine = [...shown].reverse().find(s => s.key === e?.key && jobMap[s.id])
    return mine ? jobMap[mine.id] : null
  }
  const lines = visible.map(e => ({
    key: e.key, entry: e, job: jobOf(e), name: many ? album(e) : '', of: album(e), now,
    busy: undoing === e.data?.override?.id,
    onUndo: e.data?.override ? () => undo(e.key, e.data.override) : null,
    onRetry: () => load(e.key, e.albumId),
  })).filter(l => l.entry.status !== 'ok' || l.entry.data?.override || l.job)

  // The jobs shown, and a row at rest that has something to say (it failed, or
  // was applied with problems).
  const cards = []
  for (const { id, key } of shown) {
    const job = jobMap[id]
    if (!job) continue
    const e = entryOf(key)
    const row = job.row || job.startRow || e?.data?.override
    // The release the files now carry is another album's (a pasted link):
    // lb-bot names its release group, and the card offers the way there.
    const elsewhere = job.op === 'apply' && job.phase === 'settled' && job.row?.rgid && job.row.rgid !== rgid
    cards.push({
      key: `job-${id}`, title: many ? (row?.album || album(e)) : '', v: jobView(job.op, job.phase, job.row),
      of: row?.album || album(e),
      busy: undoing === id, notListed: job.op === 'apply' && job.phase === 'settled' && !job.album?.albumId,
      onUndo: row ? () => undo(key, { ...row, id }) : null,
      // A job that went silent is not an ended one (F6): its card stays, says
      // where to look, and holds the block — there is no putting it away.
      silent: job.phase === 'timeout',
      onDone: job.phase === 'timeout' ? undefined : () => dismiss(id),
      onOpen: elsewhere && openAlbum ? () => openAlbum(job.row.rgid) : null,
    })
  }
  for (const e of ready) {
    const row = e.data.override
    if (!row || shown.some(s => s.id === row.id && jobMap[s.id])) continue
    const v = restView(row)
    if (v) cards.push({ key: `row-${row.id}`, title: many ? album(e) : '', of: album(e), v, busy: undoing === row.id, onUndo: () => undo(e.key, row) })
  }

  const heldVariants = [...new Set(ready.map(e => variantOfRelease(variants, carried(e))).filter(i => i >= 0))]
  const known = ready.length > 0 && !visible.some(e => e.status === 'loading' && !e.data)
  // What an album's files are tagged as, in the chips' own words.
  const taggedAs = e => {
    if (!carried(e)) return 'no release tag'
    const v = variantOfRelease(variants, carried(e))
    // (Joined with other words below: what MusicBrainz says of the release is isolated.)
    return v >= 0 ? variantInLine(variants[v], v) : 'a release not listed here'
  }
  const chooserRows = ready.map(e => ({
    key: e.key, albumId: e.albumId, title: album(e) || 'Album',
    sub: [taggedAs(e), e.data.tracks?.length ? `${e.data.tracks.length} tracks` : ''].filter(Boolean).join(' · '),
    same: isSame(e),
  }))
  const chooser = many ? { rows: chooserRows, value: active?.key || '', onChange: setActiveKey } : null

  const note = !mismatch ? ''
    : !active ? 'your library holds this album more than once'
    : !carried(active) ? 'your files carry no release tag'
    : `your files are tagged as ${taggedAs(active)}`
  // lb-bot is still changing an album's files — its job runs, or has gone
  // silent — and nothing that starts another job for it is offered (F6).
  const quiet = e => albumQuiet(e?.data?.override, jobOf(e))
  const canAct = !!active?.albumId && !quiet(active)
  // "Change cover": for an album lb-bot is not changing just now.
  const canCover = ready.some(e => !quiet(e))
  // (A panel that shut because a job began does not open again by itself when it is over.)
  useEffect(() => { if (!canCover) setCc(c => (c.open ? { ...c, open: false, pick: '', refusal: null } : c)) }, [canCover])
  const waiting = active && awaited(active) ? NOT_LISTED_YET : ''
  const currentCover = e => ({ albumId: e?.albumId || '', full: chosen[e?.key]?.full || e?.data?.override?.coverUrl || '' })

  const preview = asking && pv ? {
    pv, covers, cover, onCover: setCover, onRetryCovers: () => setCoverNonce(n => n + 1), current: currentCover(active),
    tint,
    committing, refusal, onCommit: commit, onRetry: () => setPvNonce(n => n + 1),
    onCancel: () => { setFlow({ open: false, pasted: '' }); setPasting(p => ({ ...p, error: '' })) },
  } : null

  const block = {
    open: mismatch || flow.open || pasting.open || cards.length > 0 || !!undoRefusal,
    other: mismatch,
    line: editionLine(variant, edition, sel.variant), lineKey: selected, note, chooser, canAct, waiting,
    pastedLine: flow.pasted && pv?.status === 'ok' ? releaseLine(pv.data.release) : '',
    onPick: () => setFlow({ open: true, pasted: '' }),
    pasting: { ...pasting, active: !!flow.pasted },
    onPasteToggle: () => setPasting(p => ({ ...p, open: !p.open, error: '' })),
    onPasteText: text => setPasting(p => ({ ...p, text, error: '' })),
    onPasteGo: pasteGo,
    preview, previewKey: pv?.key || '', cards,
    refusal: undoRefusal ? {
      text: undoRefusal.text, retry: undoRefusal.retry,
      onRetry: () => undo(undoRefusal.key, undoRefusal.row), onDismiss: () => setUndoRefusal(null),
    } : null,
  }

  const changeCover = {
    open: cc.open && ready.length > 0 && canCover,
    held: !!active && quiet(active),
    chooser: many ? { rows: chooserRows.map(r => ({ ...r, same: false })), value: active?.key || '', onChange: setActiveKey } : null,
    covers: active ? ccCovers : { status: 'ok', list: [] },
    value: cc.pick, onChange: pick => setCc(c => ({ ...c, pick, refusal: null })),
    onRetry: () => setCcNonce(n => n + 1), current: currentCover(active), saving: cc.saving, refusal: cc.refusal,
    onSave: saveChangedCover, onClose: closeCover,
  }

  // The cover lb-bot wrote for the album whose edition is on screen.
  const coverEntry = ready.find(e => (variantOfRelease(variants, carried(e)) === sel.variant || ready.length === 1)
    && (chosen[e.key] || e.data.override?.coverUrl))
  const heroCover = !coverEntry ? ''
    : chosen[coverEntry.key]?.thumb || coverThumbUrl(coverEntry.data.override?.coverUrl || '')

  // One sentence for a screen reader, in one place, that changes when a job's
  // state does and not with its count: the word while it runs, the sentence it
  // ends on.
  const latest = cards.length ? cards[cards.length - 1] : null
  const spoken = !latest ? '' : [iso(latest.title), latest.v.ended ? latest.v.line : latest.v.word].filter(Boolean).join(': ')

  return {
    lines, block, changeCover, heldVariants, known, heroCover, spoken, coverLink,
    /** The release the one album the page holds carries, '' when it holds
     *  several or none, or is still reading one: what the chips select on
     *  arrival (V7). */
    only: ready.length === 1 && visible.length === 1 ? carried(ready[0]) : '',
    has: ready.length > 0,
    /** The ids to read presence by: the albums as they are listed now. `moved`
     *  once that is no longer what the index said when the page opened. */
    albumIds: ready.map(e => e.albumId).filter(Boolean),
    moved,
    /** The release this album's files carry inside the selected variant, or ''. */
    carriedIn: index => carried(ready.find(e => carried(e) && variantOfRelease(variants, carried(e)) === index)),
    // (Not while lb-bot is at the album: a press then opens nothing, now or when the job is over.)
    openCover: () => { if (canCover) setCc(c => ({ ...c, open: !c.open, refusal: null })) },
    coverOpen: cc.open && canCover,
    /** "Change cover" as the page shows it: `pressable` while it holds an album
     *  lb-bot is not changing just now (F6), else `why` it is not (G1). */
    coverOffer: changeCoverLink(canCover),
    openPaste: () => setPasting(p => ({ ...p, open: true, error: '' })),
  }
}
