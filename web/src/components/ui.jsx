// Shared primitives used by every screen, including the Advanced panels.

import { useState } from 'react'
import Cover from './Cover.jsx'

// One status vocabulary across gap albums, transfers, and track rows.
// tone: '' | 'good' | 'warn' | 'bad' maps onto the .chip color classes.
export const STATUS = {
  // gap / album statuses
  ready: { tone: 'warn', word: 'Ready', dot: 'var(--accent)' },
  picking: { tone: 'warn', word: 'Pick source', dot: 'var(--decide-fg)' },
  downloading: { tone: 'good', word: 'Downloading', dot: 'var(--green)' },
  failed: { tone: 'bad', word: 'Failed', dot: 'var(--danger)' },
  complete: { tone: '', word: 'Done', dot: 'var(--dot)' },
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
}

export function StatusChip({ status, word }) {
  const meta = STATUS[status] || { tone: '', word: status }
  return <span className={`chip ${meta.tone}`}>{word || meta.word}</span>
}

// The one filter/pill chip. variant="tint" (default) highlights the active
// chip with the accent tint; variant="solid" fills it solid accent (the
// Fill-gaps rail look). count renders a trailing " · N".
export function Chip({ active, onClick, variant = 'tint', count, children }) {
  const activeStyle = variant === 'solid'
    ? { background: 'var(--accent)', color: '#17130f', borderColor: 'var(--accent)', fontWeight: 600 }
    : { background: 'var(--accent-tint)', color: 'var(--accent)', borderColor: 'var(--accent)', fontWeight: 600 }
  return (
    <button
      onClick={onClick}
      className="!rounded-pill !border !px-[13px] !py-[7px] text-[12.5px] leading-none"
      style={active
        ? activeStyle
        : { background: 'var(--inset-warm)', color: 'var(--muted)', borderColor: 'var(--border)', fontWeight: 400 }}
    >
      {children}{count != null ? ` · ${count}` : ''}
    </button>
  )
}

// The one progress bar: inset track, accent gradient fill.
export function ProgressBar({ value, height = 7 }) {
  const pct = Math.max(0, Math.min(100, Number(value) || 0))
  return (
    <div className="w-full overflow-hidden rounded-pill border"
      style={{ height, background: 'var(--inset-deep)', borderColor: 'var(--hairline)' }}>
      <div className="h-full rounded-pill transition-[width]"
        style={{ width: `${pct}%`, background: 'linear-gradient(90deg, var(--accent), var(--accent-3))' }} />
    </div>
  )
}

// Shared empty/complete state: workspace-card styling, quiet copy, optional
// action buttons as children.
export function EmptyState({ title, hint, children }) {
  return (
    <div className="workspace-card text-center">
      <div className="text-[18px] font-semibold">{title}</div>
      {hint ? <p className="muted mt-1.5 text-[13px]">{hint}</p> : null}
      {children ? <div className="mt-3.5 flex justify-center gap-2.5">{children}</div> : null}
    </div>
  )
}

// Format badge tinting per the mock's badge(): FLAC reads green (the target
// format), everything else amber.
export function badgeColors(format) {
  return String(format).toUpperCase() === 'FLAC'
    ? { background: 'var(--green-tint)', color: 'var(--green)' }
    : { background: 'var(--accent-chip)', color: 'var(--accent-2)' }
}

// size="lg" is the 44px square used in SourceRow; default is the inline pill.
export function Badge({ format, size }) {
  const label = (format || '?').toUpperCase()
  const colors = badgeColors(label)
  if (size === 'lg') {
    return (
      <span
        className="flex h-11 w-11 shrink-0 items-center justify-center rounded-[9px] font-mono text-[12px] font-semibold"
        style={colors}
      >
        {label}
      </span>
    )
  }
  return (
    <span className="inline-flex items-center rounded-[7px] px-2 py-0.5 font-mono text-[11px] font-semibold" style={colors}>
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
      <span
        className="absolute top-[2px] h-5 w-5 rounded-pill transition-[left]"
        style={{
          left: on ? 20 : 2,
          background: on ? '#17130f' : 'var(--muted)',
        }}
      />
    </button>
  )
}

// Pager per the mock's source pagination: "showing 1–4 of N" range label,
// ← numbered pages →. Falls back to the compact arrows-only form when no
// total/pageSize is given.
export function Pager({ page, pages, onPage, total, pageSize }) {
  if (pages <= 1) return null
  const pageBtn = (active, dis) => ({
    minWidth: 32, height: 32,
    borderRadius: 8,
    border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
    background: active ? 'var(--accent-tint)' : 'var(--inset-warm)',
    color: dis ? 'var(--dot)' : active ? 'var(--accent)' : 'var(--text2)',
    fontWeight: active ? 600 : 400,
    padding: '0 6px',
  })
  const from = total != null ? page * pageSize + 1 : null
  const to = total != null ? Math.min((page + 1) * pageSize, total) : null
  return (
    <div className="flex w-full items-center gap-1.5">
      {total != null && (
        <span className="text-[12px] text-faint">showing {from}–{to} of {total}</span>
      )}
      <span className="spacer" />
      <button style={pageBtn(false, page <= 0)} disabled={page <= 0} onClick={() => onPage(page - 1)}>←</button>
      {Array.from({ length: pages }, (_, i) => (
        <button key={i} style={pageBtn(i === page, false)} onClick={() => onPage(i)}>{i + 1}</button>
      ))}
      <button style={pageBtn(false, page + 1 >= pages)} disabled={page + 1 >= pages} onClick={() => onPage(page + 1)}>→</button>
    </div>
  )
}

// The "show files" disclosure, shared by SourceRow and the Fill-gaps hero's
// chosen-source card. Opening it asks the peer for its *real* folder listing:
// the collapsed row only ever knew the search hits, so a source that genuinely
// has the track could read as missing it. The listing is fetched once and kept.
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
    // The expanded listing supersedes the search hits wherever it arrived.
    view: expanded?.files?.length ? { ...src, ...expanded } : src,
    isFullFolder: !!expanded?.expanded,
  }
}

// The one-line provenance note under an open disclosure: search hits are a
// subset of the folder, and saying which of the two you're looking at is the
// difference between "this source lacks the track" and "the search did".
export function SourceFilesNote({ expanding, expandError, isFullFolder }) {
  return (
    <div className="mt-1 text-[11px] text-faint">
      {expanding ? 'Reading the peer’s folder…'
        : expandError ? `Search hits only — ${expandError}`
        : isFullFolder ? 'Showing the peer’s full folder'
        : 'Search hits only'}
    </div>
  )
}

// One source result row — used by both the Fill-gaps picker and the Library
// slskd search results. Rendered as an inset card per the mock; `selected`
// gives the chosen-source highlight, `done` swaps the action button for its
// confirmation state ("Selected" / "✓ Requested — see Downloads").
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
  // Every meaningful stat is a discrete token that wraps, never truncates —
  // bitrate/size/peer/speed/queue/coverage stay legible in a narrow rail, and
  // the action drops to its own full-width row once the metadata no longer fits
  // beside it (flex-wrap on the outer row).
  const metaTokens = [
    src.bitrate || null,
    src.size || null,
    ...stats,
  ].filter(Boolean)
  return (
    <div
      className="mb-2 flex flex-wrap items-start gap-x-3.5 gap-y-2.5 rounded-[12px] border p-3.5"
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
                  {i > 0 && <span className="text-[12px] text-faint">·</span>}
                  <span className="whitespace-nowrap font-mono text-[12.5px] font-semibold">{tok}</span>
                </span>
              ))
            : <span className="font-mono text-[12.5px] font-semibold">—</span>}
          {src.recommended && <span className="chip good !my-0">✓ recommended</span>}
        </div>
        {coverage !== '' && (
          <div className="mt-1 text-[12.5px]">
            <span className="whitespace-nowrap" style={{ color: full ? 'var(--green)' : 'var(--accent-2)' }}>
              {coverage}{typeof coverage === 'number' ? ' tracks' : ''}
            </span>
          </div>
        )}
        {/* Whether the folder's own name is this album. For an ambiguous query
            — a self-titled album is the worst case — the peer's whole
            discography comes back, and this is the difference between the
            album you asked for and one that merely uploads fast. */}
        {src.albumMatch != null && src.albumMatch > 0 && (
          <div className="mt-1 text-[11.5px]">
            <span style={{ color: src.albumMatchOk ? 'var(--green)' : 'var(--decide-fg)' }}
              title={`The folder name scores ${src.albumMatch}% against the album title`}>
              {src.albumMatchOk ? '✓ matches album title' : 'different album?'}
            </span>
            {src.yearInPath && (
              <span className="muted"> · year matches</span>
            )}
          </div>
        )}
        {src.flags?.length ? (
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {src.flags.map(f => (
              <span key={f}
                className="inline-block rounded-pill border px-2 py-px text-[11px]"
                style={{ background: 'var(--accent-tint)', borderColor: 'var(--accent-bd)', color: 'var(--accent-2)' }}>
                {f}
              </span>
            ))}
          </div>
        ) : null}
        {(src.files?.length || src.missingTracks?.length || onExpand) ? (
          <button type="button" className="link-inline mt-1.5 !text-[12px] !text-muted"
            aria-expanded={open} aria-busy={expanding} onClick={toggle}>
            {open ? 'Hide files' : `Show files (${src.files?.length ?? 0})`}
          </button>
        ) : null}
        {open && (
          <>
            <SourceFilesNote expanding={expanding} expandError={expandError}
              isFullFolder={isFullFolder} />
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

// How a file was paired with a track. The tiers are not equally trustworthy and
// the UI should not pretend otherwise: a title that agreed is evidence, a
// duration that merely failed to disagree is a judgement call. Only the weaker
// bases are labelled — chipping every exact match would be noise.
const MATCH_BASIS_LABEL = {
  fuzzy: 'close title',
  duration: 'by duration',
  position: 'by track no.',
}
const MATCH_BASIS_TONE = {
  fuzzy: 'var(--accent-2)',
  duration: 'var(--decide-fg)',
  position: 'var(--decide-fg)',
}
const MATCH_BASIS_HINT = {
  fuzzy: 'The filename closely resembles the track title, but does not match it exactly.',
  duration: 'No title matched. This file is the only one whose length fits this track, and no other track fits it.',
  position: 'No title matched. The folder holds the whole album, so the track number was used.',
}

export function MatchBasisChip({ basis }) {
  const label = MATCH_BASIS_LABEL[basis]
  if (!label) return null
  return (
    <span
      title={MATCH_BASIS_HINT[basis]}
      className="inline-block rounded-pill border px-1.5 py-px text-[10.5px] leading-[1.5]"
      style={{ borderColor: MATCH_BASIS_TONE[basis], color: MATCH_BASIS_TONE[basis] }}
    >
      {label}
    </span>
  )
}

// What the peer's folder actually holds, and which track each file would fill.
// The point is to answer "is this the right album?" before committing: a source
// whose files match no track on the tracklist is visibly wrong here, where the
// collapsed row can only say how many files it has.
export function SourceFileList({ src, onPick }) {
  const files = src.files || []
  const missing = src.missingTracks || []
  return (
    <div className="mt-2 rounded-[9px] border border-line p-2" style={{ background: 'var(--inset-deep)' }}>
      {files.map((f, i) => (
        <div key={`${f.filename}-${i}`}
          className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 py-[3px]">
          <span className="min-w-0 flex-1 truncate font-mono text-[11.5px]"
            style={{ color: f.accepted ? 'var(--text2)' : 'var(--faint)' }}
            title={f.filename}>
            {f.filename}
          </span>
          {f.sizeMb ? <span className="font-mono text-[11px] text-faint">{f.sizeMb} MB</span> : null}
          {f.matchedTo
            ? <span className="inline-flex items-center gap-1.5 text-[11.5px]">
                <span style={{ color: MATCH_BASIS_TONE[f.matchedTo.basis] || 'var(--green)' }}>
                  → {f.matchedTo.position ? `${f.matchedTo.position}. ` : ''}{f.matchedTo.title}
                </span>
                <MatchBasisChip basis={f.matchedTo.basis} />
              </span>
            : <span className="text-[11.5px] text-faint">
                {f.accepted ? 'not on the tracklist' : `${f.ext} — wrong format`}
              </span>}
          {onPick && (
            <button type="button" className="!py-0.5 !text-[11.5px]"
              onClick={() => onPick(f)}>Use for this track</button>
          )}
        </div>
      ))}
      {src.filesTruncated && (
        <div className="mt-1 text-[11px] text-faint">…more files not shown</div>
      )}
      {missing.length > 0 && (
        <div className="mt-1.5 border-t border-line pt-1.5">
          {missing.map((t, i) => (
            <div key={`${t.title}-${i}`} className="py-[3px] text-[11.5px]"
              style={{ color: 'var(--accent-2)' }}>
              no file for: {t.position ? `${t.position}. ` : ''}{t.title}
            </div>
          ))}
        </div>
      )}
      {!files.length && !missing.length && (
        <div className="text-[11.5px] text-faint">This source reported no files.</div>
      )}
    </div>
  )
}

// Inline source-picker filters, shared by the Fill-gaps picker and the Library
// slskd results. Kept as a plain predicate + a chip row so both call sites store
// their own `{ flacOnly, freeOnly, fast }` state and reuse the same logic.
export const EMPTY_SOURCE_FILTERS = { flacOnly: false, freeOnly: false, fast: false }

export function filterSources(sources, f) {
  if (!f) return sources
  return (sources || []).filter(s => {
    if (f.flacOnly && String(s.format).toUpperCase() !== 'FLAC') return false
    if (f.freeOnly && s.freeSlot === false) return false
    // speedMbps may be a string ("4.2") from the search response.
    if (f.fast && s.speedMbps != null && Number(s.speedMbps) < 1.0) return false
    return true
  })
}

export function SourceFilters({ value, onChange }) {
  const opts = [
    ['flacOnly', 'FLAC only'],
    ['freeOnly', 'Free slots'],
    ['fast', 'Hide slow'],
  ]
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {opts.map(([k, label]) => (
        <Chip key={k} active={!!value[k]} onClick={() => onChange({ ...value, [k]: !value[k] })}>
          {label}
        </Chip>
      ))}
    </div>
  )
}

// ── Artist-screen primitives ─────────────────────────────────────────────────

// Album-state vocabulary for release-groups measured against the library.
// These are library states, not transfer states, so they get dedicated tones
// instead of borrowing STATUS: complete reads green with a check; gaps reads
// gold carrying the missing count; missing reads faint with a dimmed cover
// and a hollow +; untagged reads accent-2 with a dashed border and a ?.
export const ALBUM_STATE = {
  complete:   { word: 'Complete', icon: '✓', fg: 'var(--green)',     tint: 'var(--green-tint)',  bd: 'var(--green-bd)' },
  incomplete: { word: 'Has gaps', icon: '·', fg: 'var(--decide-fg)', tint: 'var(--decide-tint)', bd: 'var(--decide-fg)' },
  missing:    { word: 'Missing',  icon: '+', fg: 'var(--faint)',     tint: 'transparent',        bd: 'var(--border-warm)', dimCover: true },
  untagged:   { word: 'Untagged', icon: '?', fg: 'var(--accent-2)',  tint: 'var(--accent-chip)', bd: 'var(--accent-bd)', dashed: true },
}

// Corner badge for release tiles. Gaps shows the missing count instead of an
// icon; missing stays hollow (transparent fill).
export function AlbumStateBadge({ state, count }) {
  const meta = ALBUM_STATE[state]
  if (!meta) return null
  const label = state === 'incomplete' && count != null ? count : meta.icon
  return (
    <span
      title={meta.word}
      className="flex h-[22px] min-w-[22px] items-center justify-center rounded-pill border px-1.5 text-[12px] font-bold leading-none"
      style={{
        color: meta.fg,
        background: meta.tint,
        borderColor: meta.bd,
        borderStyle: meta.dashed ? 'dashed' : 'solid',
      }}
    >
      {label}
    </span>
  )
}

// Worded chip for the album hero / filter contexts, same tones as the badge.
export function AlbumStateChip({ state, count }) {
  const meta = ALBUM_STATE[state]
  if (!meta) return null
  return (
    <span
      className="chip !my-0"
      style={{
        color: meta.fg,
        background: meta.tint,
        borderColor: meta.bd,
        borderStyle: meta.dashed ? 'dashed' : 'solid',
      }}
    >
      {meta.word}{state === 'incomplete' && count != null ? ` · ${count} missing` : ''}
    </span>
  )
}

// Shimmer placeholder. Size it with utility classes or an inline style.
export function Skeleton({ className = '', style }) {
  return <div className={`skeleton ${className}`} style={style} aria-hidden="true" />
}

// Artist portrait tile for the ArtistIndex grid: round cover, name, releases
// and (when the server sends play data) listen count.
export function ArtistTile({ name, releaseCount, plays, coverUrl, onClick }) {
  return (
    <button
      onClick={onClick}
      className="!flex flex-col items-center gap-2.5 !rounded-[14px] !border-line !bg-panel !p-4 text-center"
    >
      <div className="w-full max-w-[140px]">
        <Cover url={coverUrl} name={name} fluid round />
      </div>
      <div className="w-full min-w-0">
        <div className="truncate text-[13.5px] font-semibold">{name}</div>
        <div className="muted truncate text-[11.5px]">
          {releaseCount} release{releaseCount === 1 ? '' : 's'}
          {plays != null ? ` · ${plays.toLocaleString()} play${plays === 1 ? '' : 's'}` : ''}
        </div>
      </div>
    </button>
  )
}

// Cover-forward release tile with a corner state badge. Missing albums dim
// their cover; untagged tiles carry the dashed border on the tile itself.
export function ReleaseTile({ title, year, state, count, coverUrl, onClick }) {
  const meta = ALBUM_STATE[state] || {}
  return (
    <button
      onClick={onClick}
      className="!block w-full !rounded-[12px] !border !bg-panel !p-2.5 text-left"
      style={{
        borderColor: meta.dashed ? meta.bd : 'var(--border)',
        borderStyle: meta.dashed ? 'dashed' : 'solid',
      }}
    >
      <div className="relative mb-2">
        <div style={meta.dimCover ? { opacity: 0.45 } : undefined}>
          <Cover url={coverUrl} name={title} fluid />
        </div>
        <span className="absolute right-1.5 top-1.5">
          <AlbumStateBadge state={state} count={count} />
        </span>
      </div>
      <div className="truncate text-[13px] font-semibold">{title}</div>
      <div className="muted truncate text-[11.5px]">{year || '—'}</div>
    </button>
  )
}

function fmtDuration(seconds) {
  if (!seconds && seconds !== 0) return ''
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

// A real tracklist: numbered zebra rows in the rounded table container, mono
// durations. `loading` renders shimmer rows in the same silhouette.
//
// When the server knows per-track presence (`presenceKnown`), each row says
// whether *that* track is in the library. Without it no row is marked at all —
// marking the trailing N as absent because the counts say N are missing is a
// guess, and on a real album the gaps are rarely at the end.
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
              <div
                key={t.mbid || `${t.position}-${t.title}`}
                className="flex items-center gap-3 px-3.5 py-2"
                style={{
                  background: i % 2 ? 'var(--surface)' : 'var(--surface-alt)',
                  borderBottom: i === tracks.length - 1 ? 0 : '1px solid var(--hairline)',
                }}
              >
                <span className="w-7 shrink-0 text-right font-mono text-[12px] text-faint">{t.position || ''}</span>
                <span className="min-w-0 flex-1 truncate text-[13px]"
                  style={absent ? { color: 'var(--muted)' } : undefined}>{t.title}</span>
                {presenceKnown && (
                  <span className="shrink-0 text-[11.5px]"
                    style={{ color: absent ? 'var(--accent)' : 'var(--green)' }}
                    title={absent ? 'Not in your library' : 'In your library'}>
                    {absent ? 'missing' : '✓'}
                  </span>
                )}
                <span className="shrink-0 font-mono text-[11.5px] text-muted">{fmtDuration(t.duration)}</span>
              </div>
            )
          })}
    </div>
  )
}

// Segmented pill group for mutually-exclusive sorts/views (the header's
// segmented-pill look, as one reusable control).
export function SortToggle({ value, options, onChange }) {
  return (
    <div
      className="flex overflow-hidden rounded-pill border border-line"
      style={{ background: 'var(--inset-warm)' }}
      role="group"
    >
      {options.map(([k, label]) => (
        <button
          key={k}
          onClick={() => onChange(k)}
          className="!rounded-none !border-0 !px-3.5 !py-[7px] text-[12.5px] leading-none"
          style={k === value
            ? { background: 'var(--accent-tint)', color: 'var(--accent)', fontWeight: 600 }
            : { background: 'transparent', color: 'var(--muted)', fontWeight: 400 }}
        >
          {label}
        </button>
      ))}
    </div>
  )
}

// 28px page title with uppercase accent eyebrow — shared header pattern for
// the Downloads / Library / System screens.
export function PageTitle({ eyebrow, title }) {
  return (
    <div>
      <div className="text-[12px] font-semibold uppercase tracking-[.12em]" style={{ color: 'var(--accent)' }}>
        {eyebrow}
      </div>
      <div className="mt-0.5 text-[28px] font-bold leading-[1.1]">{title}</div>
    </div>
  )
}
