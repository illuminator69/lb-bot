import { useEffect, useState } from 'react'
import { useApp, navigate } from '../App.jsx'
import { api, post, put } from '../lib/api.js'
import { Badge, Chip, PageTitle, Toggle } from '../components/ui.jsx'

const LOG_FILTERS = [['', 'All'], ['errors', 'Errors'], ['slskd', 'slskd'], ['task', 'Tasks'], ['navidrome', 'Navidrome']]
// Tag colors per the mock: slskd amber-2, task accent, navidrome green.
const TAG_COLOR = {
  slskd: 'var(--accent-2)',
  task: 'var(--accent)',
  navidrome: 'var(--green)',
}

function Health() {
  const { state, refresh, pushToast } = useApp()
  const { health } = state
  // Configuration + recent-activity data (folded in from the old
  // Diagnostics/Home panels) — fetched once, not on the poll loop.
  const [settings, setSettings] = useState(null)
  const [activity, setActivity] = useState(null)
  useEffect(() => {
    api('/api/settings').then(setSettings).catch(() => {})
    api('/api/action-center').then(setActivity).catch(() => {})
  }, [])
  if (!health) return <p className="muted">Running checks…</p>
  const recent = activity?.summary || {}
  return (
    <>
      <div className="toolbar">
        <span className="muted">
          Last run {health.lastRun ? new Date(health.lastRun * 1000).toLocaleTimeString() : '—'}
        </span>
        <button onClick={async () => {
          await post('/api/system/recheck')
          pushToast('Diagnostics re-run')
          refresh({ silent: true })
        }}>Re-run checks</button>
      </div>
      <div className="settings-grid">
        {health.checks.map(c => (
          <div key={c.id} className="rounded-[12px] border bg-panel p-4"
            style={{ borderColor: c.ok ? 'var(--border)' : 'var(--danger-bd)' }}>
            <div className="flex items-center gap-2.5">
              <span className="inline-block h-[9px] w-[9px] rounded-pill"
                style={{ background: c.ok ? 'var(--green)' : 'var(--danger)' }} />
              <b className="text-[14.5px]">{c.label}</b>
              <span className="spacer" />
              <span className="rounded-pill px-2.5 py-px text-[11px] font-semibold"
                style={{
                  background: c.ok ? 'var(--green-tint-2)' : 'var(--danger-tint)',
                  color: c.ok ? 'var(--green)' : 'var(--danger)',
                }}>
                {c.ok ? 'PASS' : 'FAIL'}
              </span>
            </div>
            <div className="muted mt-2 text-[12.5px]">{c.detail}</div>
            {!c.ok && c.howToFix ? <p className="mt-2 text-sm">{c.howToFix}</p> : null}
          </div>
        ))}
      </div>

      <div className="mb-2.5 mt-6 text-[13px] font-semibold" style={{ color: 'var(--text2)' }}>RECENT ACTIVITY</div>
      <div className="rounded-[12px] border border-line bg-panel p-4">
        {[['Last scan', recent.last_scan?.summary],
          ['Last import', recent.last_import?.summary],
          ['Last error', recent.last_error]].map(([label, value], i) => (
          <div key={label} className={`flex gap-3 py-2 text-[13px] ${i < 2 ? 'border-b' : ''}`}
            style={{ borderColor: 'var(--hairline)' }}>
            <span className="w-24 shrink-0 text-muted">{label}</span>
            <span style={label === 'Last error' && value ? { color: 'var(--danger)' } : { color: 'var(--text2)' }}>
              {value || (label === 'Last error' ? 'No recent errors' : 'Nothing recorded')}
            </span>
          </div>
        ))}
      </div>

      {settings?.cards?.length ? (
        <>
          <div className="mb-2.5 mt-6 text-[13px] font-semibold" style={{ color: 'var(--text2)' }}>CONFIGURATION</div>
          <div className="settings-grid">
            {settings.cards.map((c, i) => (
              <div key={i} className="rounded-[12px] border bg-panel p-4"
                style={{ borderColor: c.ok ? 'var(--border)' : 'var(--danger-bd)' }}>
                <div className="flex items-center gap-2.5">
                  <b className="text-[14.5px]">{c.title}</b>
                  <span className="spacer" />
                  <span className="rounded-pill px-2.5 py-px text-[11px] font-semibold"
                    style={{
                      background: c.ok ? 'var(--green-tint-2)' : 'var(--danger-tint)',
                      color: c.ok ? 'var(--green)' : 'var(--danger)',
                    }}>
                    {c.ok ? 'PASS' : 'CHECK'}
                  </span>
                </div>
                <div className="mt-2.5">
                  {Object.entries(c.values || {}).map(([k, v]) => (
                    <div key={k} className="flex gap-3 border-b py-1.5 text-[12.5px] last:border-b-0"
                      style={{ borderColor: 'var(--hairline)' }}>
                      <span className="w-32 shrink-0 text-muted">{k}</span>
                      <span className="min-w-0 break-all font-mono text-[12px]" style={{ color: 'var(--text2)' }}>{String(v)}</span>
                    </div>
                  ))}
                </div>
                {c.fix && <div className="muted mt-2 text-[12px]">{c.fix}</div>}
              </div>
            ))}
          </div>
        </>
      ) : null}

      {settings?.examples?.docker_run || settings?.examples?.paths ? (
        <>
          <div className="mb-2.5 mt-6 text-[13px] font-semibold" style={{ color: 'var(--text2)' }}>UNRAID / DOCKER EXAMPLES</div>
          {[settings.examples.docker_run, settings.examples.paths].filter(Boolean).map((text, i) => (
            <div key={i} className="mb-2.5 overflow-x-auto whitespace-pre-wrap rounded-[12px] border border-line p-4 font-mono text-[12px] leading-[1.7]"
              style={{ background: 'var(--inset-deep)', color: 'var(--text2)' }}>
              {text}
            </div>
          ))}
        </>
      ) : null}
    </>
  )
}

// Guard rows: label/description + the default threshold each toggle enables.
const GUARDS = [
  { key: 'requireFullCoverage', on: true, off: false,
    label: 'Require full-album coverage',
    desc: 'only sources that visibly have every missing track — strict, slskd often under-reports folders' },
  { key: 'maxAlbumSizeMB', on: 500, off: 0,
    label: 'Cap album size at 500 MB',
    desc: 'skip huge 24-bit rips unless nothing else fits' },
  { key: 'minSpeedMbps', on: 1.0, off: 0,
    label: 'Minimum peer speed 1.0 MB/s',
    desc: 'avoid peers that will crawl' },
  { key: 'maxQueueLength', on: 20, off: 0,
    label: 'Skip peers with queue > 20',
    desc: 'avoid long remote wait times' },
]

const FALLBACKS = [['best', 'Pick best available'], ['ask', 'Ask me'], ['skip', 'Skip the gap']]

// Descriptions for the format tiers (backend ranks formats, not bitrates).
const RANK_DESC = {
  flac: 'lossless — CD quality and up, the target format',
  opus: 'efficient lossy fallback — transparent, small files',
}

function Prefs() {
  const { state, dispatch, pushToast } = useApp()
  const { prefs } = state
  // Local optimistic copy; PUT echoes the authoritative prefs back.
  const [local, setLocal] = useState(prefs)
  useEffect(() => { setLocal(prefs) }, [prefs])

  if (!local) return <p className="muted">Loading preferences…</p>
  const ranks = local.ranks || []
  const guards = local.guards || {}

  async function save(patch, optimistic) {
    setLocal(l => ({ ...l, ...optimistic }))
    try {
      const r = await put('/api/prefs', patch)
      setLocal(r.prefs)
      dispatch({ type: 'LOAD_SCREEN', data: { prefs: r.prefs } })
    } catch (e) {
      setLocal(prefs) // roll back
      pushToast(`Could not save preferences: ${e.message}`, 'error')
    }
  }

  function moveRank(i, delta) {
    const j = i + delta
    if (j < 0 || j >= ranks.length) return
    const next = ranks.slice()
    ;[next[i], next[j]] = [next[j], next[i]]
    save({ ranks: next.map(r => r.key) },
      { ranks: next.map((r, k) => ({ ...r, priority: k })) })
  }

  const arrowBtn = 'h-[18px] w-[26px] !rounded-[5px] !p-0 text-[11px] leading-none'

  return (
    <>
      <div className="mb-4 rounded-[14px] border border-line bg-panel p-5">
        <div className="text-[16px] font-semibold">Source ranking</div>
        <div className="muted mt-1 max-w-[620px] text-[13px] leading-[1.6]">
          When lb-bot finds sources for a gap, it walks this list top-to-bottom and
          picks the first tier it can satisfy — so there's always a fallback.
          Reorder to taste.
        </div>
        <div className="mt-4">
          {ranks.map((r, i) => (
            <div key={r.key} className="mb-2 flex items-center gap-3.5 rounded-[11px] border p-3.5"
              style={{ background: 'var(--inset-warm)', borderColor: 'var(--border-warm)' }}>
              <div className="w-5 font-mono text-[15px] font-semibold" style={{ color: 'var(--accent)' }}>{i + 1}</div>
              <Badge format={r.key} size="lg" />
              <div className="min-w-0 flex-1">
                <div className="text-[14px] font-semibold">{r.label}</div>
                <div className="muted text-[12px]">{RANK_DESC[r.key] || ''}</div>
              </div>
              <div className="flex flex-col gap-[3px]">
                <button className={arrowBtn} disabled={i === 0} aria-label={`Move ${r.label} up`}
                  style={{ background: 'var(--accent-btn)', borderColor: 'var(--border-warm)' }}
                  onClick={() => moveRank(i, -1)}>↑</button>
                <button className={arrowBtn} disabled={i === ranks.length - 1} aria-label={`Move ${r.label} down`}
                  style={{ background: 'var(--accent-btn)', borderColor: 'var(--border-warm)' }}
                  onClick={() => moveRank(i, 1)}>↓</button>
              </div>
            </div>
          ))}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-2.5 rounded-[11px] border p-3.5"
          style={{ background: 'var(--warn-tint)', borderColor: 'var(--accent-bd)' }}>
          <span className="font-mono text-[13px] font-semibold" style={{ color: 'var(--accent-2)' }}>fallback</span>
          <span className="text-[13px]" style={{ color: 'var(--text2)' }}>If nothing above is available:</span>
          <span className="spacer" />
          {FALLBACKS.map(([k, label]) => (
            <Chip key={k} active={local.fallback === k}
              onClick={() => save({ fallback: k }, { fallback: k })}>{label}</Chip>
          ))}
        </div>
      </div>

      {/* Which *copy* to prefer, as opposed to which format. Ranking above
          picks flac over opus; this decides between two flacs. It used to be
          implicit and unchangeable — the file scorer always favoured the
          higher bitrate, which is wrong for anyone who wants a standard CD rip
          rather than the largest possible file. */}
      {local.qualityOptions?.length > 0 && (
        <div className="mb-4 rounded-[14px] border border-line bg-panel p-5">
          <div className="text-[16px] font-semibold">Preferred quality</div>
          <div className="muted mt-1 max-w-[620px] text-[13px] leading-[1.6]">
            When several sources carry the same album, this decides which copy wins.
            It ranks folders by their dominant codec and bit depth — it never
            rejects a source outright, so a gap is still filled if only one copy exists.
          </div>
          <div className="mt-3.5 flex flex-col gap-2">
            {local.qualityOptions.map(o => {
              const active = local.quality === o.key
              return (
                <button key={o.key} className="!rounded-[11px] px-3.5 py-3 text-left"
                  aria-pressed={active}
                  style={{
                    background: active ? 'var(--accent-tint)' : 'var(--inset-warm)',
                    borderColor: active ? 'var(--accent)' : 'var(--border-warm)',
                    color: active ? 'var(--accent)' : 'var(--text2)',
                  }}
                  onClick={() => save({ quality: o.key }, { quality: o.key })}>
                  <div className="text-[14px] font-semibold">{o.label}</div>
                  <div className="text-[12px] opacity-80">{o.detail}</div>
                </button>
              )
            })}
          </div>
        </div>
      )}

      <div className="rounded-[14px] border border-line bg-panel p-5">
        <div className="mb-1 text-[16px] font-semibold">Guards</div>
        <div className="muted mb-3.5 text-[13px]">Applied before ranking — sources failing these are skipped.</div>
        {GUARDS.map(g => {
          const on = !!guards[g.key]
          return (
            <div key={g.key} className="flex items-center gap-3.5 border-b py-3 last:border-b-0"
              style={{ borderColor: 'var(--hairline)' }}>
              <div className="min-w-0 flex-1">
                <div className="text-[14px]">{g.label}</div>
                <div className="muted text-[12px]">{g.desc}</div>
              </div>
              <span className="font-mono text-[12px]" style={{ color: on ? 'var(--green)' : 'var(--faint)' }}>
                {on ? 'on' : 'off'}
              </span>
              <Toggle on={on} label={g.label}
                onChange={next => save(
                  { guards: { [g.key]: next ? g.on : g.off } },
                  { guards: { ...guards, [g.key]: next ? g.on : g.off } })} />
            </div>
          )
        })}
        {local.fixed && (
          <p className="muted mt-2.5 text-[12px]">
            Always on: availability ratio ≥ {local.fixed.minAvailabilityRatio} ·
            stall timeout {local.fixed.stallTimeoutSeconds}s of no progress → cancel + try next.
          </p>
        )}
      </div>
    </>
  )
}

function LogsView() {
  const { state, dispatch } = useApp()
  const { logEntries, logFilter } = state
  return (
    <>
      <div className="mb-3 flex flex-wrap gap-2">
        {LOG_FILTERS.map(([k, label]) => (
          <Chip key={k} active={logFilter === k}
            onClick={() => dispatch({ type: 'SET_LOG_FILTER', filter: k })}>{label}</Chip>
        ))}
      </div>
      {!logEntries ? <p className="muted">Loading logs…</p> : (
        <div className="max-h-[520px] overflow-y-auto rounded-[12px] border border-line p-4 font-mono text-[12.5px] leading-[1.9]"
          style={{ background: 'var(--inset-deep)' }}>
          {logEntries.slice().reverse().map((e, i) => (
            <div key={i} className="flex gap-3">
              <span className="whitespace-nowrap text-faint">{e.ts}</span>
              <span className="min-w-[66px] shrink-0 whitespace-nowrap"
                style={{ color: TAG_COLOR[e.tag] || 'var(--muted)' }}>
                {e.tag}
              </span>
              <span className="min-w-0 break-all"
                style={{ color: e.severity === 'error' ? 'var(--danger)' : 'var(--text2)' }}>
                {e.msg}
              </span>
            </div>
          ))}
          {!logEntries.length && <p className="muted">No log entries match.</p>}
        </div>
      )}
    </>
  )
}

export default function System() {
  const { state, dispatch } = useApp()
  const { sysTab } = state
  return (
    <div className="mx-auto max-w-[1000px]">
      <div className="mb-[22px]">
        <PageTitle eyebrow="System" title="Health & preferences" />
      </div>
      <div className="mb-5 flex w-fit gap-1 rounded-[10px] border border-line p-[3px]"
        style={{ background: 'var(--inset-track)' }}>
        {[['health', 'Health'], ['prefs', 'Source preferences'], ['logs', 'Logs']].map(([k, label]) => {
          const active = sysTab === k
          return (
            <button key={k}
              className={`!rounded-[8px] !border-0 !px-3.5 !py-1.5 text-[13.5px] ${active ? '!bg-accent !text-accent-fg font-semibold' : '!bg-transparent !text-muted'}`}
              onClick={() => navigate('System', k)}>{label}</button>
          )
        })}
      </div>
      {sysTab === 'health' && <Health />}
      {sysTab === 'prefs' && <Prefs />}
      {sysTab === 'logs' && <LogsView />}
    </div>
  )
}
