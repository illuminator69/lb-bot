// Shared primitives used by every screen.

import { useEffect, useRef, useState } from 'react'
import Cover from './Cover.jsx'

// ── Status vocabulary ────────────────────────────────────────────────────────
// ONE wording per state, used by every screen. The gap state machine used to be
// described three ways — the rail said "source ready" / "choosing a source",
// the chips "Ready" / "Pick source", the Library table "Has gaps" / "Needs
// decision" — for the same five states.
// tone: '' | 'good' | 'warn' | 'bad' maps onto the .chip colour classes.
export const STATUS = {
  // gap / album-in-review statuses
  ready: { tone: 'warn', word: 'Has gaps', dot: 'var(--accent)' },
  picking: { tone: 'warn', word: 'Needs a decision', dot: 'var(--decide-fg)' },
  downloading: { tone: 'good', word: 'Working', dot: 'var(--green)' },
  failed: { tone: 'bad', word: 'Failed', dot: 'var(--danger)' },
  complete: { tone: '', word: 'Complete', dot: 'var(--dot)' },
  // transfer states
  active: { tone: 'good', word: 'Downloading', dot: 'var(--green)' },
  queued: { tone: 'warn', word: 'Queued', dot: 'var(--decide-fg)' },
  done: { tone: '', word: 'Done', dot: 'var(--dot)' },
  // track states
  present: { tone: 'good', word: 'Present', dot: 'var(--green)' },
  missing: { tone: '', word: 'Missing', dot: 'var(--dot)' },
  picked: { tone: 'warn', word: 'Picked', dot: 'var(--decide-fg)' },
  downloaded: { tone: 'good', word: 'Downloaded', dot: 'var(--green)' },
  skipped: { tone: '', word: 'Skipped', dot: 'var(--dot)' },
  // album-fill ledger states (/api/fills)
  searching: { tone: 'warn', word: 'Searching', dot: 'var(--accent)' },
  placing: { tone: 'good', word: 'Placing', dot: 'var(--green)' },
  placed: { tone: 'good', word: 'Placed', dot: 'var(--green)' },
  verified: { tone: 'good', word: 'In library', dot: 'var(--green)' },
  needs_match: { tone: 'warn', word: 'Needs a match', dot: 'var(--decide-fg)' },
  cancelled: { tone: '', word: 'Cancelled', dot: 'var(--dot)' },
  unknown: { tone: '', word: 'Not started', dot: 'var(--dot)' },
}

export function StatusChip({ status, word }) {
  const meta = STATUS[status] || { tone: '', word: status }
  return <span className={`chip ${meta.tone}`}>{word || meta.word}</span>
}

// What the rail says under an album. `sourceCount` is the number of ranked
// sources the last search left — 0 means nothing has been searched (or it found
// nothing), so the album is not "ready" in any sense a user would mean.
export function gapLine(g) {
  const miss = g.missingCount ?? (g.total - g.present)
  if (g.hidden) return { word: 'hidden', color: 'var(--faint)' }
  if (g.searching) return { word: 'searching for sources…', color: 'var(--accent)' }
  switch (g.status) {
    case 'downloading': return { word: `working on ${miss} track(s)`, color: 'var(--green)' }
    case 'failed': return { word: 'failed — needs you', color: 'var(--danger)' }
    case 'picking': return { word: 'needs a decision', color: 'var(--decide-fg)' }
    case 'complete': return { word: 'complete', color: 'var(--muted)' }
    default: return g.sourceCount
      ? { word: `${miss} missing · ${g.sourceCount} source(s) found`, color: 'var(--accent)' }
      : { word: `${miss} missing`, color: 'var(--text2)' }
  }
}

// Failure kinds, in words. The failure card used to print the raw kind —
// "blocked_no_source" — as its headline.
export const FAIL_REASON = {
  error: 'Something went wrong',
  download_failed: 'Download failed',
  blocked_no_source: 'No peer had these tracks',
  stalled_placement: 'Downloaded but never placed',
  no_source: 'No peer is sharing this album',
  transfer_failed: 'The transfer failed',
  placement_failed: 'Placing into the library failed',
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
// The one filter/pill chip. variant="tint" (default) highlights the active
// chip with the accent tint; variant="solid" fills it. count renders " · N".
export function Chip({ active, onClick, variant = 'tint', count, children, title, disabled }) {
  const activeStyle = variant === 'solid'
    ? { background: 'var(--accent)', color: 'var(--on-accent)', borderColor: 'var(--accent)', fontWeight: 600 }
    : { background: 'var(--accent-tint)', color: 'var(--accent)', borderColor: 'var(--accent)', fontWeight: 600 }
  return (
    <button
      onClick={onClick}
      title={title}
      disabled={disabled}
      aria-pressed={!!active}
      className="!rounded-pill !border !px-[13px] !py-[7px] text-caption !leading-none"
      style={active
        ? activeStyle
        : { background: 'var(--inset-warm)', color: 'var(--muted)', borderColor: 'var(--border)', fontWeight: 400 }}
    >
      {children}{count != null ? ` · ${count.toLocaleString?.() ?? count}` : ''}
    </button>
  )
}

export function ProgressBar({ value, height = 7 }) {
  const pct = Math.max(0, Math.min(100, Number(value) || 0))
  return (
    <div className="w-full overflow-hidden rounded-pill border"
      role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}
      style={{ height, background: 'var(--inset-deep)', borderColor: 'var(--hairline)' }}>
      <div className="h-full rounded-pill transition-[width]"
        style={{ width: `${pct}%`, background: 'linear-gradient(90deg, var(--accent), var(--accent-3))' }} />
    </div>
  )
}

export function EmptyState({ title, hint, children }) {
  return (
    <div className="workspace-card text-center">
      <div className="text-title font-semibold">{title}</div>
      {hint ? <p className="mt-1.5 text-small text-muted">{hint}</p> : null}
      {children ? <div className="mt-3.5 flex flex-wrap justify-center gap-2.5">{children}</div> : null}
    </div>
  )
}

// A tinted notice: warn (amber-on-dark), danger, or quiet.
export function Notice({ tone = 'warn', title, children, actions }) {
  const styles = {
    warn: { background: 'var(--warn-tint)', borderColor: 'var(--accent-bd)', color: 'var(--text2)' },
    danger: { background: 'var(--warn-tint)', borderColor: 'var(--danger-bd)', color: 'var(--danger-text)' },
    quiet: { background: 'var(--inset-warm)', borderColor: 'var(--border)', color: 'var(--text2)' },
  }[tone]
  return (
    <div className="rounded-card border p-3.5 text-small" style={styles}>
      {title && <div className="mb-0.5 font-semibold" style={{ color: tone === 'danger' ? 'var(--danger)' : 'var(--text)' }}>{title}</div>}
      {children}
      {actions && <div className="mt-2.5 flex flex-wrap gap-2">{actions}</div>}
    </div>
  )
}

// ── Headings ────────────────────────────────────────────────────────────────
// Page title with the uppercase accent eyebrow.
export function PageTitle({ eyebrow, title, children }) {
  return (
    <div className="min-w-0">
      {eyebrow && (
        <div className="text-caption font-semibold uppercase tracking-[.12em]" style={{ color: 'var(--accent)' }}>
          {eyebrow}
        </div>
      )}
      <h1 className="mt-0.5 text-display font-bold">{title}</h1>
      {children}
    </div>
  )
}

// The one section header: caps label, a hairline rule, an optional count or
// note and an optional action on the right. There were four variants of this.
export function SectionHeader({ label, sub, action, className = '' }) {
  return (
    <div className={`mb-2.5 flex items-center gap-2.5 ${className}`}>
      <h2 className="text-caption font-semibold uppercase tracking-[.08em]" style={{ color: 'var(--text2)' }}>{label}</h2>
      <span className="h-px flex-1" style={{ background: 'var(--border)' }} />
      {sub != null && sub !== '' && <span className="text-caption text-faint">{sub}</span>}
      {action}
    </div>
  )
}

// Second-level navigation inside a section (Library → Artists/Albums/Cleanup).
// items: [[key, label, count?]].
export function SubTabs({ items, value, onChange, label = 'Sections' }) {
  return (
    <div role="tablist" aria-label={label}
      className="mb-5 flex w-fit max-w-full flex-wrap gap-1 rounded-card border border-line p-[3px]"
      style={{ background: 'var(--inset-track)' }}>
      {items.map(([k, text, count]) => {
        const active = value === k
        return (
          <button key={k} role="tab" aria-selected={active}
            className={`!rounded-ctl !border-0 !px-3.5 !py-1.5 text-small ${active ? '!bg-accent !text-accent-fg font-semibold' : '!bg-transparent !text-muted'}`}
            onClick={() => onChange(k)}>
            {text}{count ? ` · ${count}` : ''}
          </button>
        )
      })}
    </div>
  )
}

// ── Menus ───────────────────────────────────────────────────────────────────
// Close-on-outside-click and close-on-Escape, for every popover. Two of the
// three dropdowns had neither Escape nor aria-expanded.
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
        <div className={`absolute top-[calc(100%+6px)] z-30 flex flex-col gap-1 rounded-panel border border-line bg-panel p-1.5 ${align === 'right' ? 'right-0' : 'left-0'}`}
          style={{ minWidth: width, boxShadow: '0 20px 48px -14px rgba(0,0,0,.6)' }}>
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
      className="!block w-full !border-0 !px-3 !py-2 text-left !text-small"
      style={{ background: active ? 'var(--accent-tint)' : 'transparent', color: active ? 'var(--accent)' : 'var(--text)' }}>
      <div className="font-medium">{children}</div>
      {hint && <div className="mt-0.5 text-caption text-muted">{hint}</div>}
    </button>
  )
}

// ── Format badges, toggles, pagers ──────────────────────────────────────────
export function badgeColors(format) {
  return String(format).toUpperCase() === 'FLAC'
    ? { background: 'var(--green-tint)', color: 'var(--green)' }
    : { background: 'var(--accent-chip)', color: 'var(--accent-2)' }
}

export function Badge({ format, size }) {
  const label = (format || '?').toUpperCase()
  const colors = badgeColors(label)
  if (size === 'lg') {
    return (
      <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-ctl font-mono text-caption font-semibold"
        style={colors}>
        {label}
      </span>
    )
  }
  return (
    <span className="inline-flex items-center rounded-ctl px-2 py-0.5 font-mono text-micro font-semibold" style={colors}>
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
      className="relative h-[24px] w-[42px] shrink-0 !rounded-pill !border-0 !p-0 transition-colors"
      style={{ background: on ? 'var(--accent)' : 'var(--border-warm)' }}
    >
      <span className="absolute top-[2px] h-5 w-5 rounded-pill transition-[left]"
        style={{ left: on ? 20 : 2, background: on ? 'var(--on-accent)' : 'var(--muted)' }} />
    </button>
  )
}

// "showing 1–10 of N", ← numbered pages →.
export function Pager({ page, pages, onPage, total, pageSize }) {
  if (pages <= 1) return null
  const pageBtn = (active, dis) => ({
    minWidth: 32, height: 32,
    borderColor: active ? 'var(--accent)' : 'var(--border)',
    background: active ? 'var(--accent-tint)' : 'var(--inset-warm)',
    color: dis ? 'var(--dot)' : active ? 'var(--accent)' : 'var(--text2)',
    fontWeight: active ? 600 : 400,
    padding: '0 6px',
  })
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
      {total != null && <span className="text-caption text-faint">showing {from}–{to} of {total}</span>}
      <span className="spacer" />
      <button style={pageBtn(false, page <= 0)} disabled={page <= 0} onClick={() => onPage(page - 1)} aria-label="Previous page">←</button>
      {nums.map((n, i) => n === '…'
        ? <span key={`e${i}`} className="px-1 text-faint">…</span>
        : <button key={n} style={pageBtn(n === page, false)} aria-current={n === page ? 'page' : undefined}
            onClick={() => onPage(n)}>{n + 1}</button>)}
      <button style={pageBtn(false, page + 1 >= pages)} disabled={page + 1 >= pages} onClick={() => onPage(page + 1)} aria-label="Next page">→</button>
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

// One source result row — the Fill-gaps picker and the album page's picker.
export function SourceRow({
  src, onUse, busy,
  actionLabel = 'Use this →',
  done = false, doneLabel = 'Selected',
  selected = false,
  onExpand, onPick,
}) {
  const { open, toggle, expanding, expandError, view, isFullFolder } = useSourceFiles(src, onExpand)
  const coverage = src.coverage ?? ''
  const full = src.coverageFull ?? (typeof coverage === 'string' && (coverage.includes('all') || coverage.startsWith('full')))
  const stats = [
    src.peer ? `${String(src.peer).startsWith('@') ? '' : '@'}${src.peer}` : null,
    src.speedMbps != null ? `${src.speedMbps} MB/s` : null,
    src.queueLength != null ? `queue ${src.queueLength}` : null,
  ].filter(Boolean)
  const metaTokens = [src.bitrate || null, src.size || null, ...stats].filter(Boolean)
  return (
    <div
      className="mb-2 flex flex-wrap items-start gap-x-3.5 gap-y-2.5 rounded-card border p-3.5"
      style={{
        background: selected ? 'var(--sel-row)' : 'var(--inset-warm)',
        borderColor: selected || done ? (done ? 'var(--green-bd)' : 'var(--accent-bd-sel)') : 'var(--border)',
      }}
    >
      <Badge format={src.format} size="lg" />
      <div className="min-w-[140px] flex-1">
        <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1">
          {metaTokens.length
            ? metaTokens.map((tok, i) => (
                <span key={i} className="inline-flex items-center gap-1.5">
                  {i > 0 && <span className="text-caption text-faint">·</span>}
                  <span className="whitespace-nowrap font-mono text-caption font-semibold">{tok}</span>
                </span>
              ))
            : <span className="font-mono text-caption font-semibold">—</span>}
          {src.recommended && <span className="chip good !my-0">✓ recommended</span>}
        </div>
        {coverage !== '' && (
          <div className="mt-1 text-caption">
            <span className="whitespace-nowrap" style={{ color: full ? 'var(--green)' : 'var(--accent-2)' }}>
              {coverage}{typeof coverage === 'number' ? ' tracks' : ''}
            </span>
          </div>
        )}
        {src.albumMatch != null && src.albumMatch > 0 && (
          <div className="mt-1 text-micro">
            <span style={{ color: src.albumMatchOk ? 'var(--green)' : 'var(--decide-fg)' }}
              title={`The folder name scores ${src.albumMatch}% against the album title`}>
              {src.albumMatchOk ? '✓ matches album title' : 'different album?'}
            </span>
            {src.yearInPath && <span className="text-muted"> · year matches</span>}
            {/* strict tri-state: an old server never sends the field and renders nothing */}
            {src.artistVerified === false && (
              <span style={{ color: 'var(--decide-fg)' }}
                title="Neither the folder path, the filenames nor the tracklist mention this artist">
                {' '}· artist unverified
              </span>
            )}
          </div>
        )}
        {src.flags?.length ? (
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {src.flags.map(f => (
              <span key={f}
                className="inline-block rounded-pill border px-2 py-px text-micro"
                style={{ background: 'var(--accent-tint)', borderColor: 'var(--accent-bd)', color: 'var(--accent-2)' }}>
                {f}
              </span>
            ))}
          </div>
        ) : null}
        {(src.files?.length || src.missingTracks?.length || onExpand) ? (
          <button type="button" className="link-inline mt-1.5 !text-caption !text-muted"
            aria-expanded={open} aria-busy={expanding} onClick={toggle}>
            {open ? 'Hide files' : `Show files (${src.files?.length ?? 0})`}
          </button>
        ) : null}
        {open && (
          <>
            <SourceFilesNote expanding={expanding} expandError={expandError} isFullFolder={isFullFolder} />
            <SourceFileList src={view} onPick={onPick} />
          </>
        )}
      </div>
      {done
        ? <span className="chip good self-center whitespace-nowrap">{doneLabel}</span>
        : <button className="primary self-center whitespace-nowrap" disabled={busy} onClick={() => onUse(src.id)}>{actionLabel}</button>}
    </div>
  )
}

// How a file was paired with a track. Only the weaker bases are labelled.
const MATCH_BASIS_LABEL = { fuzzy: 'close title', duration: 'by duration', position: 'by track no.' }
const MATCH_BASIS_TONE = { fuzzy: 'var(--accent-2)', duration: 'var(--decide-fg)', position: 'var(--decide-fg)' }
const MATCH_BASIS_HINT = {
  fuzzy: 'The filename closely resembles the track title, but does not match it exactly.',
  duration: 'No title matched. This file is the only one whose length fits this track, and no other track fits it.',
  position: 'No title matched. The folder holds the whole album, so the track number was used.',
}

export function MatchBasisChip({ basis }) {
  const label = MATCH_BASIS_LABEL[basis]
  if (!label) return null
  return (
    <span title={MATCH_BASIS_HINT[basis]}
      className="inline-block rounded-pill border px-1.5 py-px text-micro"
      style={{ borderColor: MATCH_BASIS_TONE[basis], color: MATCH_BASIS_TONE[basis] }}>
      {label}
    </span>
  )
}

// What the peer's folder actually holds, and which track each file would fill.
export function SourceFileList({ src, onPick }) {
  const files = src.files || []
  const missing = src.missingTracks || []
  return (
    <div className="mt-2 rounded-ctl border border-line p-2" style={{ background: 'var(--inset-deep)' }}>
      {files.map((f, i) => (
        <div key={`${f.filename}-${i}`} className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 py-[3px]">
          <span className="min-w-0 flex-1 truncate font-mono text-micro"
            style={{ color: f.accepted ? 'var(--text2)' : 'var(--faint)' }} title={f.filename}>
            {f.filename}
          </span>
          {f.sizeMb ? <span className="font-mono text-micro text-faint">{f.sizeMb} MB</span> : null}
          {f.matchedTo
            ? <span className="inline-flex items-center gap-1.5 text-micro">
                <span style={{ color: MATCH_BASIS_TONE[f.matchedTo.basis] || 'var(--green)' }}>
                  → {f.matchedTo.position ? `${f.matchedTo.position}. ` : ''}{f.matchedTo.title}
                </span>
                <MatchBasisChip basis={f.matchedTo.basis} />
              </span>
            : <span className="text-micro text-faint">
                {f.accepted ? 'not on the tracklist' : `${f.ext} — wrong format`}
              </span>}
          {onPick && (
            <button type="button" className="sm !py-0.5" onClick={() => onPick(f)}>Use for this track</button>
          )}
        </div>
      ))}
      {src.filesTruncated && <div className="mt-1 text-micro text-faint">…more files not shown</div>}
      {missing.length > 0 && (
        <div className="mt-1.5 border-t border-line pt-1.5">
          {missing.map((t, i) => (
            <div key={`${t.title}-${i}`} className="py-[3px] text-micro" style={{ color: 'var(--accent-2)' }}>
              no file for: {t.position ? `${t.position}. ` : ''}{t.title}
            </div>
          ))}
        </div>
      )}
      {!files.length && !missing.length && (
        <div className="text-micro text-faint">This source reported no files.</div>
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
    <div className="flex flex-wrap items-center gap-1.5">
      {opts.map(([k, label]) => (
        <Chip key={k} active={!!value[k]} onClick={() => onChange({ ...value, [k]: !value[k] })}>{label}</Chip>
      ))}
    </div>
  )
}

// ── Library-state primitives ────────────────────────────────────────────────
// Release-groups measured against the library. These are library states, not
// transfer states, so they have their own tones.
export const ALBUM_STATE = {
  complete:   { word: 'Complete', icon: '✓', fg: 'var(--green)',     tint: 'var(--green-tint)',  bd: 'var(--green-bd)' },
  incomplete: { word: 'Has gaps', icon: '·', fg: 'var(--decide-fg)', tint: 'var(--decide-tint)', bd: 'var(--decide-fg)' },
  missing:    { word: 'Missing',  icon: '+', fg: 'var(--faint)',     tint: 'transparent',        bd: 'var(--border-warm)', dimCover: true },
  untagged:   { word: 'Untagged', icon: '?', fg: 'var(--accent-2)',  tint: 'var(--accent-chip)', bd: 'var(--accent-bd)', dashed: true },
}

export function AlbumStateBadge({ state, count }) {
  const meta = ALBUM_STATE[state]
  if (!meta) return null
  const label = state === 'incomplete' && count != null ? count : meta.icon
  return (
    <span title={meta.word}
      className="flex h-[22px] min-w-[22px] items-center justify-center rounded-pill border px-1.5 text-caption font-bold leading-none"
      style={{ color: meta.fg, background: meta.tint, borderColor: meta.bd, borderStyle: meta.dashed ? 'dashed' : 'solid' }}>
      {label}
    </span>
  )
}

export function AlbumStateChip({ state, count }) {
  const meta = ALBUM_STATE[state]
  if (!meta) return null
  return (
    <span className="chip !my-0"
      style={{ color: meta.fg, background: meta.tint, borderColor: meta.bd, borderStyle: meta.dashed ? 'dashed' : 'solid' }}>
      {meta.word}{state === 'incomplete' && count != null ? ` · ${count} missing` : ''}
    </span>
  )
}

export function Skeleton({ className = '', style }) {
  return <div className={`skeleton ${className}`} style={style} aria-hidden="true" />
}

export function ArtistTile({ name, sub, coverUrl, onClick, badge }) {
  return (
    <button onClick={onClick}
      className="!flex flex-col items-center gap-2.5 !rounded-panel !border-line !bg-panel !p-4 text-center">
      <div className="relative w-full max-w-[140px]">
        <Cover url={coverUrl} name={name} fluid round />
        {badge && <span className="absolute right-0 top-0">{badge}</span>}
      </div>
      <div className="w-full min-w-0">
        <div className="truncate text-small font-semibold">{name}</div>
        {sub && <div className="truncate text-micro text-muted">{sub}</div>}
      </div>
    </button>
  )
}

export function ReleaseTile({ title, year, sub, state, count, coverUrl, onClick }) {
  const meta = ALBUM_STATE[state] || {}
  return (
    <button onClick={onClick}
      className="!block w-full !rounded-card !border !bg-panel !p-2.5 text-left"
      style={{ borderColor: meta.dashed ? meta.bd : 'var(--border)', borderStyle: meta.dashed ? 'dashed' : 'solid' }}>
      <div className="relative mb-2">
        <div style={meta.dimCover ? { opacity: 0.45 } : undefined}>
          <Cover url={coverUrl} name={title} fluid />
        </div>
        <span className="absolute right-1.5 top-1.5">
          <AlbumStateBadge state={state} count={count} />
        </span>
      </div>
      <div className="truncate text-small font-semibold">{title}</div>
      <div className="truncate text-micro text-muted">{sub || year || '—'}</div>
    </button>
  )
}

function fmtClock(seconds) {
  if (!seconds && seconds !== 0) return ''
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

// A real tracklist. With `presenceKnown` each row says whether *that* track is
// in the library; without it no row is marked — guessing is worse than blank.
export function TrackList({ tracks, loading, rows = 8, presenceKnown = false }) {
  return (
    <div className="overflow-hidden rounded-card border border-line">
      {loading
        ? Array.from({ length: rows }, (_, i) => (
            <div key={i} className="flex items-center gap-3 px-3.5 py-2.5"
              style={{ borderBottom: i === rows - 1 ? 0 : '1px solid var(--hairline)' }}>
              <Skeleton className="h-3 w-6" />
              <Skeleton className="h-3 flex-1" style={{ maxWidth: `${55 - (i % 4) * 8}%` }} />
              <span className="spacer" />
              <Skeleton className="h-3 w-9" />
            </div>
          ))
        : (tracks || []).map((t, i) => {
            const absent = presenceKnown && t.present === false
            return (
              // Index too: a recording can appear twice on one release.
              <div key={`${t.mbid || t.title}-${t.position}-${i}`}
                className="flex items-center gap-3 px-3.5 py-2"
                style={{
                  background: i % 2 ? 'var(--surface)' : 'var(--surface-alt)',
                  borderBottom: i === tracks.length - 1 ? 0 : '1px solid var(--hairline)',
                }}>
                <span className="w-7 shrink-0 text-right font-mono text-caption text-faint">{t.position || ''}</span>
                <span className="min-w-0 flex-1 truncate text-small"
                  style={absent ? { color: 'var(--muted)' } : undefined}>{t.title}</span>
                {presenceKnown && (
                  <span className="shrink-0 text-micro"
                    style={{ color: absent ? 'var(--accent)' : 'var(--green)' }}
                    title={absent ? 'Not in your library' : 'In your library'}>
                    {absent ? 'missing' : '✓'}
                  </span>
                )}
                <span className="shrink-0 font-mono text-micro text-muted">{fmtClock(t.duration)}</span>
              </div>
            )
          })}
    </div>
  )
}

// Segmented pill group for mutually-exclusive sorts/views.
export function SortToggle({ value, options, onChange, label = 'Sort' }) {
  return (
    <div className="flex overflow-hidden rounded-pill border border-line"
      style={{ background: 'var(--inset-warm)' }} role="group" aria-label={label}>
      {options.map(([k, text]) => (
        <button key={k} onClick={() => onChange(k)} aria-pressed={k === value}
          className="!rounded-none !border-0 !px-3.5 !py-[7px] text-caption !leading-none"
          style={k === value
            ? { background: 'var(--accent-tint)', color: 'var(--accent)', fontWeight: 600 }
            : { background: 'transparent', color: 'var(--muted)', fontWeight: 400 }}>
          {text}
        </button>
      ))}
    </div>
  )
}

// A labelled <select>, for sorts with more than three options.
export function SelectField({ label, value, options, onChange }) {
  return (
    <label className="flex items-center gap-2 text-caption text-muted">
      {label}
      <select value={value} onChange={e => onChange(e.target.value)} className="!py-1.5 text-caption">
        {options.map(([k, text]) => <option key={k} value={k}>{text}</option>)}
      </select>
    </label>
  )
}
