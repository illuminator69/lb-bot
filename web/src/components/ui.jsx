// Shared primitives used by every screen.

import { useEffect, useRef, useState } from 'react'
import Cover from './Cover.jsx'
import { Roll } from './Page.jsx'
import { albumBadge } from '../lib/albumSources.js'
import { albumState } from '../lib/indexStatus.js'

// ── Status vocabulary ────────────────────────────────────────────────────────
// ONE wording per state, used by every screen, and one colour per meaning:
// amber (wait) is waiting on you, green (ok) is done, red (bad) failed, the
// accent (run) is lb-bot working. tone: '' | 'wait' | 'ok' | 'bad' | 'run'.
export const STATUS = {
  // gap / album-in-review statuses
  ready: { tone: 'wait', word: 'Has gaps' },
  picking: { tone: 'wait', word: 'Needs a decision' },
  downloading: { tone: 'run', word: 'Working' },
  failed: { tone: 'bad', word: 'Failed' },
  complete: { tone: 'ok', word: 'Complete' },
  // transfer states
  active: { tone: 'run', word: 'Downloading' },
  queued: { tone: 'wait', word: 'Queued' },
  done: { tone: 'ok', word: 'Done' },
  // track states
  present: { tone: 'ok', word: 'In library' },
  missing: { tone: 'wait', word: 'Missing' },
  picked: { tone: 'wait', word: 'Picked' },
  downloaded: { tone: 'run', word: 'Downloaded' },
  skipped: { tone: '', word: 'Skipped' },
  // album-fill ledger states (/api/fills)
  searching: { tone: 'run', word: 'Searching' },
  placing: { tone: 'run', word: 'Placing' },
  placed: { tone: 'ok', word: 'Placed' },
  verified: { tone: 'ok', word: 'In library' },
  needs_match: { tone: 'wait', word: 'Needs a match' },
  cancelled: { tone: '', word: 'Cancelled' },
  unknown: { tone: '', word: 'Not started' },
}

export function StatusChip({ status, word }) {
  const meta = STATUS[status] || { tone: '', word: status }
  return <span className={`st ${meta.tone}`}>{word || meta.word}</span>
}

// What the queue says under an album. `sourceCount` is the number of ranked
// sources the last search left — 0 means nothing has been searched (or it found
// nothing), so the album is not "ready" in any sense a user would mean.
// `note` goes beside the artist; `word` + `tone` are the status at the row's end.
export function gapLine(g) {
  const miss = g.missingCount ?? (g.total - g.present)
  const missing = `${miss} missing`
  if (g.hidden) return { tone: '', word: 'Hidden', note: missing }
  if (g.searching) return { tone: 'run', word: 'Searching', note: 'searching for sources' }
  switch (g.status) {
    case 'downloading': return { tone: 'run', word: 'Working', note: `getting ${miss} track${miss === 1 ? '' : 's'}` }
    case 'failed': return { tone: 'bad', word: 'Failed', note: missing }
    case 'picking': return { tone: 'wait', word: missing, note: 'needs a decision' }
    case 'complete': return { tone: 'ok', word: 'Complete', note: 'all tracks present' }
    default: return {
      tone: 'wait', word: missing,
      note: g.sourceCount ? `${g.sourceCount} source${g.sourceCount === 1 ? '' : 's'} found` : 'not searched yet',
    }
  }
}

// Failure kinds, in words. The failure card used to print the raw kind —
// "blocked_no_source" — as its headline.
export const FAIL_REASON = {
  error: 'Something went wrong',
  download_failed: 'Download failed',
  blocked_no_source: 'No peer had these tracks',
  stalled_placement: 'Downloaded but never filed',
  no_source: 'No peer is sharing this album',
  transfer_failed: 'The transfer failed',
  placement_failed: 'Filing into the library failed',
  format_rejected: 'Only MP3 copies were found',
}
export const failWords = kind => FAIL_REASON[kind] || (kind ? kind.replace(/_/g, ' ') : 'Download failed')

// ── Small formatters ────────────────────────────────────────────────────────
// "45s" / "3m" / "2h" / "4d" for a unix timestamp, or '' if there isn't one.
export function ago(ts) {
  if (!ts) return ''
  const secs = Math.max(0, Math.round(Date.now() / 1000 - ts))
  if (secs < 60) return `${secs}s`
  if (secs < 3600) return `${Math.round(secs / 60)}m`
  if (secs < 86400) return `${Math.round(secs / 3600)}h`
  return `${Math.round(secs / 86400)}d`
}

export function fmtBytes(n) {
  if (!n) return ''
  const mb = n / 1024 / 1024
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb.toFixed(1)} MB`
}

export function fmtDuration(seconds) {
  if (!seconds && seconds !== 0) return ''
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(seconds % 3600 ? 1 : 0)} h`
  if (seconds >= 60) return `${Math.round(seconds / 60)} min`
  return `${Math.round(seconds)} s`
}

// ── Chips, bars, empty states ───────────────────────────────────────────────
// The one choice chip (genres, origins, source filters). count renders " N".
export function Chip({ active, onClick, count, children, title, disabled }) {
  return (
    <button className="chip" onClick={onClick} title={title} disabled={disabled} aria-pressed={!!active}>
      {children}{count != null ? ` ${count.toLocaleString?.() ?? count}` : ''}
    </button>
  )
}

export function ProgressBar({ value, height }) {
  const pct = Math.max(0, Math.min(100, Number(value) || 0))
  return (
    <div className={`meter${height && height <= 5 ? '' : ' lg'}`}
      role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}>
      <i style={{ width: `${pct}%` }} />
    </div>
  )
}

export function EmptyState({ title, hint, children }) {
  return (
    <div className="empty">
      <div className="text-lead font-semibold text-ink">{title}</div>
      {hint ? <p className="mt-1">{hint}</p> : null}
      {children ? <div className="mt-3.5 flex flex-wrap justify-center gap-2.5">{children}</div> : null}
    </div>
  )
}

// A note with a coloured edge: warn (waiting on you), danger, or quiet.
//   role   what it is to a screen reader, when it appears in answer to a
//          press: `alert` for a refusal (the button came back to life and
//          nothing else says why), `status` for an answer that could not be
//          had. None for a note that is simply part of the page — the "why"
//          notes, a read that failed on arrival: they are no interruptions.
export function Notice({ tone = 'warn', title, children, actions, role }) {
  const cls = { warn: '', danger: ' bad', quiet: ' quiet' }[tone] ?? ''
  return (
    <div className={`why${cls} !my-0`} role={role}>
      {title && <div className="mb-0.5 font-semibold">{title}</div>}
      {children}
      {actions && <div className="acts mt-2.5">{actions}</div>}
    </div>
  )
}

// ── Headings ────────────────────────────────────────────────────────────────
// A heading inside a page body. The page's own title is the frame's (Page.jsx).
export function PageTitle({ title, children }) {
  return (
    <div className="min-w-0">
      <h2 className="text-display font-semibold">{title}</h2>
      {children}
    </div>
  )
}

// The one section header: caps label, an optional count or note and an
// optional action on the right.
export function SectionHeader({ label, sub, action, className = '' }) {
  return (
    <div className={`grp ${className}`}>
      <h2>{label}</h2>
      {sub != null && sub !== '' && <span className="num">{sub}</span>}
      {action ? <span className="x">{action}</span> : null}
    </div>
  )
}

// A switch between views inside a page body. Section tabs are the frame's.
// items: [[key, label, count?]].
export function SubTabs({ items, value, onChange, label = 'Sections' }) {
  return (
    <div className="seg mb-4" role="tablist" aria-label={label}>
      {items.map(([k, text, count]) => (
        <button key={k} role="tab" aria-selected={value === k} aria-pressed={value === k} onClick={() => onChange(k)}>
          {text}{count ? ` ${count}` : ''}
        </button>
      ))}
    </div>
  )
}

// ── Menus ───────────────────────────────────────────────────────────────────
// Close-on-outside-click and close-on-Escape, for every popover.
export function useDismiss(open, setOpen) {
  const ref = useRef(null)
  useEffect(() => {
    if (!open) return
    function onDoc(e) { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    function onKey(e) { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, setOpen])
  return ref
}

// A button that opens a panel. `children` is either nodes or a function of
// `close` so an item can close the menu after acting.
export function Menu({ label, buttonClass = '', buttonStyle, align = 'left', width = 220, title, ariaLabel, children }) {
  const [open, setOpen] = useState(false)
  const ref = useDismiss(open, setOpen)
  return (
    <div className="relative" ref={ref}>
      <button className={buttonClass} style={buttonStyle} title={title} aria-label={ariaLabel}
        aria-haspopup="true" aria-expanded={open} onClick={() => setOpen(o => !o)}>
        {label}
      </button>
      {open && (
        <div className={`inup absolute top-[calc(100%+6px)] z-30 flex flex-col rounded-card border border-line bg-panel p-1 ${align === 'right' ? 'right-0' : 'left-0'}`}
          style={{ minWidth: width, maxWidth: 'calc(100vw - 32px)', boxShadow: '0 12px 32px -12px rgba(0,0,0,.45)' }}>
          {typeof children === 'function' ? children(() => setOpen(false)) : children}
        </div>
      )}
    </div>
  )
}

// One row inside a Menu.
export function MenuItem({ onClick, disabled, hint, children, active }) {
  return (
    <button disabled={disabled} onClick={onClick}
      className="!block w-full !border-0 !px-3 !py-2 text-left"
      style={{ background: active ? 'var(--accent-tint)' : 'transparent' }}>
      <div className="font-medium">{children}</div>
      {hint && <div className="mt-0.5 text-caption text-muted" style={{ whiteSpace: 'normal' }}>{hint}</div>}
    </button>
  )
}

// ── Format badges, toggles, pagers ──────────────────────────────────────────
export function badgeColors(format) {
  return String(format).toUpperCase() === 'FLAC'
    ? { background: 'var(--ok-tint)', color: 'var(--ok)' }
    : { background: 'var(--sunk)', color: 'var(--fg2)' }
}

export function Badge({ format }) {
  const label = (format || '?').toUpperCase()
  return (
    <span className="inline-flex items-center rounded-ctl px-1.5 py-px font-mono text-micro font-medium"
      style={badgeColors(label)}>
      {label}
    </span>
  )
}

export function Toggle({ on, onChange, disabled, label }) {
  return (
    <button
      role="switch"
      aria-checked={!!on}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!on)}
      className="relative h-[22px] w-[38px] shrink-0 !rounded-pill !border-0 !p-0"
      style={{ background: on ? 'var(--accent)' : 'var(--line)' }}
    >
      <span className="absolute top-[2px] h-[18px] w-[18px] rounded-pill transition-[left]"
        style={{ left: on ? 18 : 2, background: on ? 'var(--on-accent)' : 'var(--surface)' }} />
    </button>
  )
}

// "1–10 of N", ← numbered pages →.
export function Pager({ page, pages, onPage, total, pageSize }) {
  if (pages <= 1) return null
  const from = total != null ? page * pageSize + 1 : null
  const to = total != null ? Math.min((page + 1) * pageSize, total) : null
  // A long result set gets a window of page numbers rather than all of them.
  const nums = []
  for (let i = 0; i < pages; i++) {
    if (i === 0 || i === pages - 1 || Math.abs(i - page) <= 2) nums.push(i)
    else if (nums[nums.length - 1] !== '…') nums.push('…')
  }
  return (
    <div className="flex w-full flex-wrap items-center gap-1.5">
      {total != null && <span className="num muted">{from.toLocaleString()}–{to.toLocaleString()} of {total.toLocaleString()}</span>}
      <span className="spacer" />
      <button className="sm" disabled={page <= 0} onClick={() => onPage(page - 1)} aria-label="Previous page">←</button>
      {nums.map((n, i) => n === '…'
        ? <span key={`e${i}`} className="muted px-1">…</span>
        : <button key={n} className={`sm num${n === page ? ' active' : ''}`} aria-current={n === page ? 'page' : undefined}
            onClick={() => onPage(n)}>{n + 1}</button>)}
      <button className="sm" disabled={page + 1 >= pages} onClick={() => onPage(page + 1)} aria-label="Next page">→</button>
    </div>
  )
}

// ── Sources ─────────────────────────────────────────────────────────────────
// The "show files" disclosure, shared by SourceRow and the Fill-gaps chosen
// source card. Opening it asks the peer for its *real* folder listing.
export function useSourceFiles(src, onExpand) {
  const [open, setOpen] = useState(false)
  const [expanded, setExpanded] = useState(null)
  const [expanding, setExpanding] = useState(false)
  const [expandError, setExpandError] = useState('')

  async function toggle() {
    const next = !open
    setOpen(next)
    if (!next || expanded || expanding || !onExpand) return
    setExpanding(true)
    setExpandError('')
    try {
      setExpanded(await onExpand(src.id))
    } catch (e) {
      setExpandError(e.message || 'Could not read the peer’s folder')
    } finally {
      setExpanding(false)
    }
  }

  return {
    open, toggle, expanding, expandError,
    view: expanded?.files?.length ? { ...src, ...expanded } : src,
    isFullFolder: !!expanded?.expanded,
  }
}

export function SourceFilesNote({ expanding, expandError, isFullFolder }) {
  return (
    <div className="mt-1 text-micro text-faint">
      {expanding ? 'Reading the peer’s folder…'
        : expandError ? `Search hits only — ${expandError}`
        : isFullFolder ? 'Showing the peer’s full folder'
        : 'Search hits only'}
    </div>
  )
}

// One source in a ranked list — the gaps queue and the album page. The peer,
// what it has and how fast it is on the left; whether the artist is confirmed
// and the one action on the right. "Show files" asks the peer for its real
// folder listing, which is how you check it is the right album before
// committing.
//
// Whether it is the album is said by what lb-bot takes for evidence of it
// (`albumBadge`), not by how the folder's name scores. `note` is one quiet
// fact about the folder and `secondaryLabel` / `onSecondary` a second, quieter
// action (the album page's "N more files in this folder" and "Download all M
// files"): both in the row's line of small things, so the row's own action
// stays where it is.
export function SourceRow({
  src, onUse, busy,
  actionLabel = 'Use this source',
  done = false, doneLabel = 'Selected',
  selected = false, primary = false,
  onExpand, onPick,
  note = '', secondaryLabel = '', onSecondary,
}) {
  const { open, toggle, expanding, expandError, view, isFullFolder } = useSourceFiles(src, onExpand)
  const coverage = src.coverage ?? ''
  const full = src.coverageFull ?? (typeof coverage === 'string' && (coverage.includes('all') || coverage.startsWith('full')))
  const peer = src.peer ? `${String(src.peer).startsWith('@') ? '' : '@'}${src.peer}` : '—'
  const what = [
    [String(src.format || '').toUpperCase(), src.bitrate].filter(Boolean).join(' '),
    src.size || null,
    coverage !== '' ? `${coverage}${typeof coverage === 'number' ? ' tracks' : ''}` : null,
  ].filter(Boolean)
  const speed = [
    src.speedMbps != null ? `${src.speedMbps} MB/s` : null,
    src.freeSlot === true ? 'free slot' : src.queueLength != null ? `queue ${src.queueLength}` : null,
  ].filter(Boolean).join(' · ')
  // Strict tri-state: an old server never sends artistVerified and nothing is claimed.
  const verified = src.artistVerified === false ? ['wait', 'Unverified']
    : src.artistVerified === true ? ['ok', 'Artist verified'] : null
  const canShowFiles = src.files?.length || src.missingTracks?.length || onExpand
  const badge = albumBadge(src)
  const second = secondaryLabel && onSecondary && !done
  return (
    <div className="row col src" aria-selected={selected || undefined}
      style={selected ? { background: 'var(--accent-tint)' } : undefined}>
      <div className="flex min-w-0 items-center gap-3">
        <div className="grow">
          <div className="t num clip">
            {peer}
            {src.recommended && <span className="st ok ml-2 !font-sans">recommended</span>}
          </div>
          <div className="s">
            <span style={full ? undefined : { color: 'var(--wait)' }}>{what.join(' · ')}</span>
            {speed ? ` · ${speed}` : ''}
          </div>
          {(badge || src.flags?.length || canShowFiles || note || second) ? (
            <div className="s mt-0.5 flex flex-wrap items-center gap-x-2.5 gap-y-0.5">
              {badge && (
                <span style={{ color: badge.ok ? 'var(--ok)' : 'var(--wait)' }} title={badge.title}>
                  {badge.text}{src.yearInPath ? ' · year matches' : ''}
                </span>
              )}
              {(src.flags || []).map(f => <span key={f} style={{ color: 'var(--wait)' }}>{f}</span>)}
              {canShowFiles ? (
                <button type="button" className="link plain underline" aria-expanded={open} aria-busy={expanding} onClick={toggle}>
                  {open ? 'Hide files' : `Show files${src.files?.length ? ` (${src.files.length})` : ''}`}
                </button>
              ) : null}
              {note ? <span>{note}</span> : null}
              {second ? (
                <button type="button" className="link plain underline" disabled={busy} onClick={() => onSecondary(src.id)}>
                  {secondaryLabel}
                </button>
              ) : null}
            </div>
          ) : null}
        </div>
        {verified && (
          <span className={`st ${verified[0]} hide-sm`}
            title={src.artistVerified === false
              ? 'Neither the folder path, the filenames nor the tracklist mention this artist'
              : 'The folder path, the filenames or the tracklist name this artist'}>
            {verified[1]}
          </span>
        )}
        {done
          ? <span className="st ok whitespace-nowrap">{doneLabel}</span>
          : onUse && <button className={`sm whitespace-nowrap ${primary ? 'primary' : ''}`} disabled={busy} onClick={() => onUse(src.id)}>{actionLabel}</button>}
      </div>
      <Roll open={open}>
        <SourceFilesNote expanding={expanding} expandError={expandError} isFullFolder={isFullFolder} />
        <SourceFileList src={view} onPick={onPick} />
      </Roll>
    </div>
  )
}

// How a file was paired with a track. Only the weaker bases are labelled.
const MATCH_BASIS_LABEL = { fuzzy: 'close title', duration: 'by duration', position: 'by track no.' }
const MATCH_BASIS_HINT = {
  fuzzy: 'The filename closely resembles the track title, but does not match it exactly.',
  duration: 'No title matched. This file is the only one whose length fits this track, and no other track fits it.',
  position: 'No title matched. The folder holds the whole album, so the track number was used.',
}

export function MatchBasisChip({ basis }) {
  const label = MATCH_BASIS_LABEL[basis]
  if (!label) return null
  return <span title={MATCH_BASIS_HINT[basis]} className="chip warn !my-0">{label}</span>
}

// What the peer's folder actually holds, and which track each file would fill.
export function SourceFileList({ src, onPick }) {
  const files = src.files || []
  const missing = src.missingTracks || []
  return (
    <div className="mt-2 rounded-ctl p-2" style={{ background: 'var(--sunk)' }}>
      {files.map((f, i) => (
        <div key={`${f.filename}-${i}`} className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 py-[3px]">
          <span className="min-w-0 flex-1 truncate font-mono text-micro"
            style={{ color: f.accepted ? 'var(--fg2)' : 'var(--muted)' }} title={f.filename}>
            {f.filename}
          </span>
          {f.sizeMb ? <span className="font-mono text-micro text-muted">{f.sizeMb} MB</span> : null}
          {f.matchedTo
            ? <span className="inline-flex items-center gap-1.5 text-micro">
                <span style={{ color: MATCH_BASIS_LABEL[f.matchedTo.basis] ? 'var(--wait)' : 'var(--ok)' }}>
                  → {f.matchedTo.position ? `${f.matchedTo.position}. ` : ''}{f.matchedTo.title}
                </span>
                <MatchBasisChip basis={f.matchedTo.basis} />
              </span>
            : <span className="text-micro text-muted">
                {f.accepted ? 'not on the tracklist' : `${f.ext} — wrong format`}
              </span>}
          {onPick && (
            <button type="button" className="sm !py-0.5" onClick={() => onPick(f)}>Use for this track</button>
          )}
        </div>
      ))}
      {src.filesTruncated && <div className="mt-1 text-micro text-muted">…more files not shown</div>}
      {missing.length > 0 && (
        <div className="mt-1.5 border-t border-line pt-1.5">
          {missing.map((t, i) => (
            <div key={`${t.title}-${i}`} className="py-[3px] text-micro" style={{ color: 'var(--wait)' }}>
              no file for: {t.position ? `${t.position}. ` : ''}{t.title}
            </div>
          ))}
        </div>
      )}
      {!files.length && !missing.length && (
        <div className="text-micro text-muted">This source reported no files.</div>
      )}
    </div>
  )
}

export const EMPTY_SOURCE_FILTERS = { flacOnly: false, freeOnly: false, fast: false }

export function filterSources(sources, f) {
  if (!f) return sources
  return (sources || []).filter(s => {
    if (f.flacOnly && String(s.format).toUpperCase() !== 'FLAC') return false
    if (f.freeOnly && s.freeSlot === false) return false
    if (f.fast && s.speedMbps != null && Number(s.speedMbps) < 1.0) return false
    return true
  })
}

export function SourceFilters({ value, onChange }) {
  const opts = [['flacOnly', 'FLAC only'], ['freeOnly', 'Free slots'], ['fast', 'Hide slow']]
  return (
    <div className="chips">
      {opts.map(([k, label]) => (
        <Chip key={k} active={!!value[k]} onClick={() => onChange({ ...value, [k]: !value[k] })}>{label}</Chip>
      ))}
    </div>
  )
}

// ── Library-state primitives ────────────────────────────────────────────────
// A release-group measured against the library.
export const ALBUM_STATE = {
  complete:   { word: 'In library', tone: 'ok' },
  incomplete: { word: 'Has gaps', tone: 'wait' },
  missing:    { word: 'Not in library', tone: '', dimCover: true },
  untagged:   { word: 'Untagged', tone: 'wait' },
}

// `state` is an index row's `status` as lb-bot says it: read through
// `albumState`, so the fifth word (`present`, an album lb-bot filled) has its
// chip — "In library" — like the four a scan produces.
export function AlbumStateChip({ state, count }) {
  const known = albumState(state)
  const meta = ALBUM_STATE[known]
  if (!meta) return null
  return (
    <span className={`st ${meta.tone}`}>
      {known === 'incomplete' && count != null ? `${count} missing` : meta.word}
    </span>
  )
}
export const AlbumStateBadge = AlbumStateChip

export function Skeleton({ className = '', style }) {
  return <div className={`skeleton ${className}`} style={style} aria-hidden="true" />
}

export function ArtistTile({ name, sub, coverUrl, onClick, badge }) {
  return (
    <div className="tile">
      <button className="art" onClick={onClick} aria-label={name}>
        <Cover url={coverUrl} name={name} fluid round />
      </button>
      <div className="t clip text-center font-medium">{name}</div>
      {(sub || badge) && <div className="muted clip text-center text-caption">{sub}{badge ? <> {badge}</> : null}</div>}
    </div>
  )
}

export function ReleaseTile({ title, year, sub, state, count, coverUrl, onClick, children }) {
  const meta = ALBUM_STATE[albumState(state)] || {}
  return (
    <div className="tile">
      <button className="art" onClick={onClick} aria-label={title} style={meta.dimCover ? { opacity: 0.6 } : undefined}>
        <Cover url={coverUrl} name={title} fluid />
      </button>
      <div className="t clip font-medium">{title}</div>
      <div className="muted clip text-caption">{sub || year || '—'}</div>
      <div className="flex flex-wrap items-center justify-between gap-1.5">
        <AlbumStateChip state={state} count={count} />
        {children}
      </div>
    </div>
  )
}

export function fmtClock(seconds) {
  if (!seconds && seconds !== 0) return ''
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

// A real tracklist. With `presenceKnown` each row says whether *that* track is
// in the library; without it no row is marked — guessing is worse than blank.
export function TrackList({ tracks, loading, rows = 8, presenceKnown = false, absentWord = 'Missing' }) {
  return (
    <div className="rows">
      {loading
        ? Array.from({ length: rows }, (_, i) => (
            <div key={i} className="row">
              <Skeleton className="h-3 w-5" />
              <Skeleton className="h-3 flex-1" style={{ maxWidth: `${55 - (i % 4) * 8}%` }} />
              <span className="spacer" />
              <Skeleton className="h-3 w-9" />
            </div>
          ))
        : (tracks || []).map((t, i) => {
            const absent = presenceKnown && t.present === false
            return (
              // Index too: a recording can appear twice on one release.
              <div key={`${t.mbid || t.title}-${t.position}-${i}`} className="row">
                <span className="num muted" style={{ width: 22 }}>{t.position || ''}</span>
                <span className="grow clip">{t.title}</span>
                <span className="num muted hide-sm">{fmtClock(t.duration)}</span>
                {presenceKnown && (
                  <span className={`st ${absent ? (absentWord === 'Missing' ? 'wait' : '') : 'ok'}`}>
                    {absent ? absentWord : 'In library'}
                  </span>
                )}
              </div>
            )
          })}
    </div>
  )
}

// Segmented switch for mutually-exclusive sorts/views.
export function SortToggle({ value, options, onChange, label = 'Sort' }) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map(([k, text]) => (
        <button key={k} onClick={() => onChange(k)} aria-pressed={k === value}>{text}</button>
      ))}
    </div>
  )
}

// A <select> named by its label, for sorts with more than three options.
export function SelectField({ label, value, options, onChange }) {
  return (
    <select value={value} onChange={e => onChange(e.target.value)} aria-label={label} title={label}>
      {options.map(([k, text]) => <option key={k} value={k}>{text}</option>)}
    </select>
  )
}
