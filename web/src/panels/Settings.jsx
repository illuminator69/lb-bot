import { useEffect, useMemo, useState } from 'react'
import { useApp, navigate } from '../App.jsx'
import { api, post, put } from '../lib/api.js'
import { AppearanceControls } from '../components/AppearanceMenu.jsx'
import {
  ago, Badge, Chip, fmtDuration, Notice, PageTitle, ProgressBar, SectionHeader, StatusChip,
  SubTabs, Toggle,
} from '../components/ui.jsx'

// Settings used to be "System": health, source preferences and an empty log
// view. It also owns what was scattered elsewhere — the task history (the old
// Advanced → Playlist page was the only place tasks showed), the library-index
// build (on the Artist tab), and appearance (the header popover, still there).
//
// Only Sources is editable at runtime (PUT /api/prefs). Everything under Status
// is set by environment variables on the container and is shown, not edited.
const SUBS = [['sources', 'Sources'], ['status', 'Status'], ['activity', 'Activity'], ['logs', 'Logs'], ['appearance', 'Appearance']]

// ── Sources ──────────────────────────────────────────────────────────────────
// Guards with the threshold each one enforces when on. The thresholds used to
// be fixed at these defaults behind an on/off toggle.
const GUARDS = [
  { key: 'requireFullCoverage', bool: true, label: 'Require full-album coverage',
    desc: 'Only sources that visibly have every missing track. Strict — slskd often under-reports folders.' },
  { key: 'maxAlbumSizeMB', def: 500, unit: 'MB', step: 50, min: 50, label: 'Skip albums larger than',
    desc: 'Leaves huge 24-bit rips alone unless nothing else fits.' },
  { key: 'minSpeedMbps', def: 1.0, unit: 'MB/s', step: 0.5, min: 0.1, label: 'Skip peers slower than',
    desc: 'Avoids uploads that will crawl.' },
  { key: 'maxQueueLength', def: 20, unit: 'in queue', step: 5, min: 1, label: 'Skip peers with more than',
    desc: 'Avoids long remote waits.' },
]

// What each format tier means (the backend ranks formats, not bitrates).
const RANK_DESC = {
  flac: 'lossless — CD quality and up, the target format',
  opus: 'efficient lossy fallback — transparent, small files',
}

// A threshold input that lets you type freely and saves on blur or Enter.
// Validating every keystroke made "300" impossible to type past its "3".
function GuardNumber({ guard: g, on, value, onCommit }) {
  const [draft, setDraft] = useState(String(value))
  useEffect(() => { setDraft(String(value)) }, [value])
  function commit() {
    const v = Number(draft)
    if (!Number.isFinite(v) || v < g.min) { setDraft(String(value)); return }
    if (v !== value) onCommit(v)
  }
  return (
    <span className="inline-flex items-center gap-1.5">
      <input type="number" aria-label={`${g.label} (${g.unit})`}
        className="w-[84px] !py-1 text-small" disabled={!on} min={g.min} step={g.step}
        value={draft} onChange={e => setDraft(e.target.value)} onBlur={commit}
        onKeyDown={e => { if (e.key === 'Enter') e.currentTarget.blur() }} />
      <span className="text-caption text-muted">{g.unit}</span>
    </span>
  )
}

function Sources() {
  const { state, dispatch, pushToast } = useApp()
  const { prefs } = state
  const [local, setLocal] = useState(prefs)
  useEffect(() => { setLocal(prefs) }, [prefs])
  if (!local) return <p className="text-caption text-muted">Loading preferences…</p>
  const ranks = local.ranks || []
  const guards = local.guards || {}

  async function save(patch, optimistic) {
    setLocal(l => ({ ...l, ...optimistic }))
    try {
      const r = await put('/api/prefs', patch)
      setLocal(r.prefs)
      dispatch({ type: 'LOAD_SCREEN', data: { prefs: r.prefs } })
    } catch (e) {
      setLocal(prefs)
      pushToast(`Could not save: ${e.message}`, 'error')
    }
  }

  function moveRank(i, delta) {
    const j = i + delta
    if (j < 0 || j >= ranks.length) return
    const next = ranks.slice()
    ;[next[i], next[j]] = [next[j], next[i]]
    save({ ranks: next.map(r => r.key) }, { ranks: next.map((r, k) => ({ ...r, priority: k })) })
  }
  const setGuard = (key, value) => save({ guards: { [key]: value } }, { guards: { ...guards, [key]: value } })

  // "ask" and "skip" have always done the same thing (return no source, so the
  // gap waits for you); they are one option now.
  const fallback = local.fallback === 'best' ? 'best' : 'skip'
  const arrowBtn = 'h-[18px] w-[26px] !rounded-[5px] !p-0 text-micro leading-none'

  return (
    <div className="max-w-[760px]">
      <div className="card mb-4 !p-5">
        <h2 className="text-lead font-semibold">Format ranking</h2>
        <p className="mt-1 text-small text-muted">
          When lb-bot finds sources, it prefers formats in this order. Anything not listed is rejected at search time —
          except MP3, which you can allow per album from Fill gaps.
        </p>
        <div className="mt-4">
          {ranks.map((r, i) => (
            <div key={r.key} className="mb-2 flex items-center gap-3.5 rounded-card border p-3.5"
              style={{ background: 'var(--inset-warm)', borderColor: 'var(--border-warm)' }}>
              <div className="w-5 font-mono text-body font-semibold" style={{ color: 'var(--accent)' }}>{i + 1}</div>
              <Badge format={r.key} size="lg" />
              <div className="min-w-0 flex-1">
                <div className="text-body font-semibold">{r.label}</div>
                <div className="text-caption text-muted">{RANK_DESC[r.key] || ''}</div>
              </div>
              <div className="flex flex-col gap-[3px]">
                <button className={arrowBtn} disabled={i === 0} aria-label={`Move ${r.label} up`} onClick={() => moveRank(i, -1)}>↑</button>
                <button className={arrowBtn} disabled={i === ranks.length - 1} aria-label={`Move ${r.label} down`} onClick={() => moveRank(i, 1)}>↓</button>
              </div>
            </div>
          ))}
        </div>
      </div>

      {local.qualityOptions?.length > 0 && (
        <div className="card mb-4 !p-5">
          <h2 className="text-lead font-semibold">Which copy wins</h2>
          <p className="mt-1 text-small text-muted">
            When several sources carry the same album, this decides between them by codec and bit depth. It never
            rejects a source, so a gap is still filled if only one copy exists.
          </p>
          <div className="mt-3.5 flex flex-col gap-2">
            {local.qualityOptions.map(o => {
              const active = local.quality === o.key
              return (
                <button key={o.key} className="!rounded-card px-3.5 py-3 text-left" aria-pressed={active}
                  style={{
                    background: active ? 'var(--accent-tint)' : 'var(--inset-warm)',
                    borderColor: active ? 'var(--accent)' : 'var(--border-warm)',
                    color: active ? 'var(--accent)' : 'var(--text2)',
                  }}
                  onClick={() => save({ quality: o.key }, { quality: o.key })}>
                  <div className="text-body font-semibold">{o.label}</div>
                  <div className="text-caption opacity-80">{o.detail}</div>
                </button>
              )
            })}
          </div>
        </div>
      )}

      <div className="card !p-5">
        <h2 className="text-lead font-semibold">Guards</h2>
        <p className="mb-3.5 mt-1 text-small text-muted">Applied before ranking — sources that fail one are skipped.</p>
        {GUARDS.map(g => {
          const value = guards[g.key]
          const on = !!value
          return (
            <div key={g.key} className="flex flex-wrap items-center gap-3.5 border-b py-3 last:border-b-0" style={{ borderColor: 'var(--hairline)' }}>
              <div className="min-w-[220px] flex-1">
                <div className="flex flex-wrap items-center gap-2 text-body">
                  {g.label}
                  {!g.bool && (
                    <GuardNumber guard={g} on={on} value={on ? value : g.def}
                      onCommit={v => setGuard(g.key, v)} />
                  )}
                </div>
                <div className="text-caption text-muted">{g.desc}</div>
              </div>
              <Toggle on={on} label={g.label} onChange={next => setGuard(g.key, next ? (g.bool ? true : g.def) : (g.bool ? false : 0))} />
            </div>
          )
        })}
        <div className="mt-3 flex flex-wrap items-center gap-2.5 rounded-card border p-3.5"
          style={{ background: 'var(--inset-warm)', borderColor: 'var(--border)' }}>
          <span className="text-small" style={{ color: 'var(--text2)' }}>If the guards reject every source:</span>
          <span className="spacer" />
          <Chip active={fallback === 'best'} onClick={() => save({ fallback: 'best' }, { fallback: 'best' })}
            title="Rescore without the guards and take the best">Use the best one anyway</Chip>
          <Chip active={fallback === 'skip'} onClick={() => save({ fallback: 'skip' }, { fallback: 'skip' })}
            title="Return no source; the album waits in Fill gaps for you">Leave it for me</Chip>
        </div>
        {local.fixed && (
          <p className="mt-2.5 text-caption text-muted">
            Always on: a source must have at least {Math.round(local.fixed.minAvailabilityRatio * 100)}% of its files
            available · a transfer with no progress for {local.fixed.stallTimeoutSeconds}s is cancelled and the next
            source tried.
          </p>
        )}
      </div>
    </div>
  )
}

// ── Status ───────────────────────────────────────────────────────────────────
function Row({ label, children, tone }) {
  return (
    <div className="flex gap-3 border-b py-1.5 text-caption last:border-b-0" style={{ borderColor: 'var(--hairline)' }}>
      <span className="w-36 shrink-0 text-muted">{label}</span>
      <span className="min-w-0 break-words" style={{ color: tone || 'var(--text2)' }}>{children}</span>
    </div>
  )
}

function StatusCard({ title, ok, badge, children, actions }) {
  return (
    <div className="card" style={ok === false ? { borderColor: 'var(--danger-bd)' } : undefined}>
      <div className="mb-2 flex items-center gap-2.5">
        <h3 className="text-body font-semibold">{title}</h3>
        <span className="spacer" />
        {badge != null && (
          <span className="rounded-pill px-2.5 py-px text-micro font-semibold"
            style={{
              background: ok === false ? 'var(--danger-tint)' : ok ? 'var(--green-tint-2)' : 'var(--inset-warm)',
              color: ok === false ? 'var(--danger)' : ok ? 'var(--green)' : 'var(--muted)',
            }}>{badge}</span>
        )}
      </div>
      {children}
      {actions && <div className="mt-3 flex flex-wrap gap-2">{actions}</div>}
    </div>
  )
}

const AUTO_INDEX_WORDS = {
  indexed: 'indexing — last tick scanned an artist',
  unresolved: 'indexing — last artist had no MusicBrainz match',
  idle: 'idle — every artist is indexed and fresh',
  paused: 'paused while a manual build runs',
  outage: 'paused — MusicBrainz is not answering',
  failed: 'indexing — last artist failed and waits for a retry',
  covered: 'indexing — last artist shares another artist\'s fresh row',
}

function IndexBuild() {
  const { action, pushToast } = useApp()
  const [s, setS] = useState(null)
  const [cancelling, setCancelling] = useState(false)
  useEffect(() => {
    let dead = false
    let timer = null
    const load = async () => {
      let building = false
      try {
        const r = await api('/api/library-index/status')
        if (dead) return
        building = !!r.building
        setS(r)
        if (!building) setCancelling(false)
      } catch { /* poll again */ }
      if (!dead) timer = setTimeout(load, building ? 2000 : 10000)
    }
    load()
    return () => { dead = true; clearTimeout(timer) }
  }, [])
  if (!s) return <p className="text-caption text-muted">Loading…</p>
  const t = s.task || {}
  const complete = s.artistsTotal > 0 && s.artistsIndexed >= s.artistsTotal
  return (
    <>
      <Row label="Artists indexed">{s.artistsIndexed.toLocaleString()} of {s.artistsTotal.toLocaleString()}{s.artistsStale ? ` · ${s.artistsStale} stale` : ''}</Row>
      {s.building ? (
        <div className="mt-2.5">
          <div className="mb-1.5 truncate text-caption text-muted">Building {t.done ?? 0}/{t.total ?? '?'}{t.current ? ` · ${t.current}` : ''}</div>
          <ProgressBar value={t.total ? (t.done / t.total) * 100 : 3} height={5} />
          <button className="sm mt-2" disabled={cancelling || !t.id} onClick={async () => {
            setCancelling(true)
            try { await action(`/api/tasks/${t.id}/cancel`); pushToast('Index build cancelling…') }
            catch (e) { setCancelling(false); pushToast(`Cancel failed: ${e.message}`, 'error') }
          }}>{cancelling ? 'Cancelling…' : 'Cancel build'}</button>
        </div>
      ) : (!complete || s.artistsStale > 0) && (
        <button className="sm mt-2.5" title="Walk every artist now at MusicBrainz pace, instead of waiting for the background worker"
          onClick={async () => {
            try {
              await action('/api/library-index/build')
              setS(x => ({ ...x, building: true, task: null }))
              pushToast('Index build started — it runs at MusicBrainz pace')
            } catch (e) { pushToast(`Could not start: ${e.message}`, 'error') }
          }}>{complete ? 'Refresh stale artists now' : 'Build the full index now'}</button>
      )}
    </>
  )
}

function Status() {
  const { state, dispatch, pushToast, confirmAction } = useApp()
  const { health, systemStatus: st } = state
  const [settings, setSettings] = useState(null)
  const [stFailed, setStFailed] = useState(false)
  const [rechecking, setRechecking] = useState(false)
  useEffect(() => { api('/api/settings').then(setSettings).catch(() => {}) }, [])

  // Health and worker status, on their own slower cadence. Results go into app
  // state so the header's "N checks failing" line can read them.
  const [tick, setTick] = useState(0)
  useEffect(() => {
    let dead = false
    api('/api/system/health').then(h => !dead && dispatch({ type: 'LOAD_SCREEN', data: { health: h } })).catch(() => {})
    api('/api/system/status')
      .then(x => { if (!dead) { setStFailed(false); dispatch({ type: 'LOAD_SCREEN', data: { systemStatus: x } }) } })
      .catch(() => !dead && setStFailed(true))
    const t = setTimeout(() => setTick(n => n + 1), 15000)
    return () => { dead = true; clearTimeout(t) }
  }, [tick, dispatch])
  const reload = () => setTick(n => n + 1)

  async function recheck() {
    setRechecking(true)
    try { await post('/api/system/recheck'); reload(); pushToast('Checks re-run') }
    catch (e) { pushToast(`Re-check failed: ${e.message}`, 'error') }
    finally { setRechecking(false) }
  }
  async function rescanNavidrome() {
    try { await post('/api/rescan', { full: false }); pushToast('Navidrome is rescanning — re-run the checks in a minute') }
    catch (e) { pushToast(`Could not start a Navidrome scan: ${e.message}`, 'error') }
  }
  async function clearRejected() {
    try {
      const r = await confirmAction(
        `Forget the ${st?.acoustid?.rejectedSources} peer(s) AcoustID caught sending the wrong recording? They may be offered again.`,
        '/api/acoustid/rejected/clear', { confirm: true }, { confirmLabel: 'Forget them', danger: true })
      if (r) { pushToast(`Cleared ${r.cleared}`); reload() }
    } catch (e) { pushToast(`Could not clear: ${e.message}`, 'error') }
  }

  // Only the path check's "index looks stale" verdict is one a rescan fixes.
  // Matching /navidrome/i told a down or unauthorised Navidrome that its index
  // was behind and offered a rescan it could not run — the substring-label
  // mistake _settings_cards had just been fixed for.
  const rescanFixes = c => !c.ok && c.label === 'Library path ↔ Navidrome' && /stale/i.test(c.detail || '')
  const navidromeTrouble = (health?.checks || []).some(rescanFixes)
  const now = Date.now() / 1000
  const ai = st?.autoIndex || {}
  const hub = st?.hubPush || {}
  const ac = st?.acoustid || {}

  return (
    <>
      <SectionHeader label="Health" sub={health?.lastRun ? `checked ${ago(health.lastRun)} ago` : ''}
        action={<button className="sm" disabled={rechecking} aria-busy={rechecking} onClick={recheck}>Re-run checks</button>} />
      {!health ? <p className="mb-6 text-caption text-muted">Running checks — this takes a few seconds…</p> : (
        <div className="settings-grid mb-6">
          {health.checks.map(c => (
            <StatusCard key={c.id} title={c.label} ok={c.ok} badge={c.ok ? 'PASS' : 'FAIL'}
              actions={rescanFixes(c) && <button className="sm primary" onClick={rescanNavidrome}>Rescan Navidrome</button>}>
              <div className="text-caption text-muted">{c.detail}</div>
              {!c.ok && c.howToFix ? <p className="mt-2 text-small">{c.howToFix}</p> : null}
            </StatusCard>
          ))}
        </div>
      )}
      {navidromeTrouble && (
        <div className="-mt-3 mb-6"><Notice>Navidrome's index is behind the files on disk. A rescan usually fixes it; if it keeps failing, check the /music mount matches Navidrome's.</Notice></div>
      )}

      {!st ? (
        <div className="mb-6">
          <Notice tone={stFailed ? 'danger' : 'quiet'} title={stFailed ? 'Worker and integration status unavailable' : 'Loading status…'}>
            {stFailed && 'The running lb-bot did not answer /api/system/status — it may be older than this page.'}
          </Notice>
        </div>
      ) : <>
      <SectionHeader label="This process" />
      <div className="settings-grid mb-6">
        <StatusCard title="Build">
          <Row label="Revision"><span className="font-mono">{st?.revision || '…'}</span></Row>
          <Row label="Running for">{st?.startedAt ? fmtDuration(now - st.startedAt) : '…'}</Row>
          {st?.revision === 'unknown' && <p className="mt-2 text-caption text-muted">This image was built without LB_BOT_REVISION.</p>}
        </StatusCard>
        <StatusCard title="Library index" ok={ai.running === false ? undefined : true}
          badge={ai.enabled === false ? 'AUTO OFF' : ai.running ? 'AUTO ON' : null}>
          <Row label="Background worker">
            {ai.enabled === false ? 'off (LB_BOT_AUTO_INDEX=0)'
              : ai.running === false ? 'not running (no Navidrome login)'
              : AUTO_INDEX_WORDS[ai.outcome] || 'starting'}
          </Row>
          {ai.at ? <Row label="Last tick">{ago(ai.at)} ago{ai.scannedThisRun ? ` · ${ai.scannedThisRun} artist(s) since start` : ''}</Row> : null}
          {ai.failing ? <Row label="Backing off" tone="var(--decide-fg)">{ai.failing} artist(s) failed and wait for a retry</Row> : null}
          {ai.pausedUntil > now ? <Row label="Paused until">{new Date(ai.pausedUntil * 1000).toLocaleTimeString()}</Row> : null}
          <Row label="Rescan after">{st?.autoIndex?.ttlDays ?? 30} days</Row>
          <IndexBuild />
        </StatusCard>
        <StatusCard title="Hub (navi-connect)" ok={hub.configured ? !hub.persistFailing : undefined}
          badge={hub.configured ? (hub.persistFailing ? 'DISK' : 'ON') : 'OFF'}>
          {!hub.configured ? <div className="text-caption text-muted">LB_BOT_HUB_URL / _TOKEN not set — the Feishin and Navic clients get no live updates.</div> : (
            <>
              <Row label="Hub">{hub.url}</Row>
              <Row label="Index head">{hub.headSeq ?? '—'}{hub.ackedSeq != null ? ` · hub has ${hub.ackedSeq}` : ''}</Row>
              {hub.attemptedAt ? <Row label="Last push">{ago(hub.attemptedAt)} ago</Row> : null}
              {hub.persistFailing && <Row label="Problem" tone="var(--danger)">can't write the high-water mark — /config is not writable</Row>}
            </>
          )}
        </StatusCard>
        <StatusCard title="Wishlist & search">
          <Row label="Wishlist re-search">one album every {fmtDuration(st?.wishlist?.intervalSeconds || 0)}, each at most every {fmtDuration(st?.wishlist?.cooldownSeconds || 0)}</Row>
          <Row label="Search timeout">{st?.search?.timeoutSeconds ?? '…'} s</Row>
          <Row label="Stalled transfer">cancelled after {st?.search?.stallTimeoutSeconds ?? '…'} s without progress</Row>
          <Row label="Source failover">up to {st?.search?.failoverMax ?? '…'} sources, {st?.search?.failoverDeadlineSeconds ?? '…'} s</Row>
        </StatusCard>
      </div>

      <SectionHeader label="Integrations" />
      <div className="settings-grid mb-6">
        <StatusCard title="AcoustID verification" ok={ac.keySet && ac.fpcalc ? true : undefined}
          badge={ac.keySet && ac.fpcalc ? 'ON' : 'OFF'}
          actions={ac.rejectedSources > 0 && <button className="sm danger" onClick={clearRejected}>Forget rejected peers…</button>}>
          <Row label="API key">{ac.keySet ? 'set' : 'not set (ACOUSTID_API_KEY)'}</Row>
          <Row label="fpcalc">{ac.fpcalc ? 'installed' : 'missing'}</Row>
          <Row label="Minimum score">{ac.minScore ?? '…'}</Row>
          <Row label="Peers rejected">{ac.rejectedSources ?? 0}</Row>
          {ac.unavailable === 'import' && <Row label="Problem" tone="var(--danger)">pyacoustid is not installed — verification is off</Row>}
          {ac.unavailable === 'fpcalc' && (
            <Row label="Note" tone="var(--decide-fg)">
              at least one file couldn’t be fingerprinted since the last restart (e.g. audio that doesn’t decode); it was placed unverified
            </Row>
          )}
          <p className="mt-2 text-caption text-muted">Checks a downloaded file is the right recording before it's placed. Off means placement trusts tags and filenames.</p>
        </StatusCard>
        <StatusCard title="ListenBrainz" ok={st?.listenbrainz?.user ? true : undefined} badge={st?.listenbrainz?.user ? 'ON' : 'OFF'}>
          <Row label="User">{st?.listenbrainz?.user || 'not set'}</Row>
          <Row label="Playlists scanned">{(st?.listenbrainz?.playlists || []).join(', ') || '—'}</Row>
        </StatusCard>
        <StatusCard title="Spotify" ok={st?.spotify?.configured ? true : undefined} badge={st?.spotify?.configured ? 'ON' : 'OFF'}>
          <div className="text-caption text-muted">
            {st?.spotify?.configured ? 'Playlist scans and link lookups are available.' : 'SPOTIFY_CLIENT_ID and _SECRET are not set, so Spotify playlists can’t be scanned. Spotify links still resolve by page title.'}
          </div>
        </StatusCard>
        <StatusCard title="Last.fm" ok={st?.lastfm?.keySet ? true : undefined} badge={st?.lastfm?.keySet ? 'ON' : 'OFF'}>
          <div className="text-caption text-muted">
            {st?.lastfm?.keySet ? 'Cross-checks similar artists alongside ListenBrainz.' : 'LASTFM_API_KEY not set — similar artists come from ListenBrainz alone.'}
          </div>
        </StatusCard>
      </div>

      </>}

      {settings?.cards?.length ? (
        <>
          <SectionHeader label="Configuration" sub="from environment variables" />
          <div className="settings-grid">
            {settings.cards.map((c, i) => (
              <StatusCard key={i} title={c.title} ok={c.ok} badge={c.ok ? 'OK' : 'CHECK'}>
                {Object.entries(c.values || {}).map(([k, v]) => (
                  <Row key={k} label={k}><span className="break-all font-mono">{String(v)}</span></Row>
                ))}
                {!c.ok && c.fix && <div className="mt-2 text-caption text-muted">{c.fix}</div>}
              </StatusCard>
            ))}
          </div>
        </>
      ) : null}
    </>
  )
}

// ── Activity ─────────────────────────────────────────────────────────────────
// Every background task: scans, source searches, placements, downloads. This
// was only visible on the old Advanced → Playlist page, as six unlabelled cards.
const TASK_FILTERS = [['', 'All'], ['running', 'Running'], ['error', 'Failed'], ['complete', 'Done'], ['cancelled', 'Cancelled']]
// How a task's status reads. A cancel is marked the moment it is asked for,
// while the worker may still be winding down — it is not "Done".
const TASK_STATUS = {
  error: ['failed', 'Failed'], running: ['active', 'Running'], queued: ['queued', 'Queued'],
  cancelled: ['cancelled', 'Cancelled'], complete: ['done', 'Done'],
}
const TASK_KIND = {
  'all-album-scan': 'Library scan', 'duplicate-scan': 'Duplicate scan', 'playlist-scan': 'Playlist scan',
  'spotify-scan': 'Spotify scan', 'artist-discography': 'Discography', 'library-index': 'Index build',
  placement: 'Placement', identify: 'Identify', 'source-search': 'Source search',
  'album-download': 'Album download', 'download-approved': 'Download', retag: 'Merge', search: 'Search',
}

function Activity() {
  const { state, action, pushToast } = useApp()
  const [filter, setFilter] = useState('')
  const tasks = useMemo(() => Object.values(state.tasks || {})
    .sort((a, b) => (b.started_at || 0) - (a.started_at || 0)), [state.tasks])
  const shown = filter ? tasks.filter(t => t.status === filter) : tasks
  const last = kind => tasks.find(t => (t.kind || '').includes(kind) && t.status !== 'running')
  const lastError = tasks.find(t => t.error)

  if (!state.tasks) return <p className="text-caption text-muted">Loading activity…</p>
  return (
    <>
      <div className="settings-grid mb-6">
        {[['Last scan', last('scan')], ['Last placement', last('placement')], ['Last failure', lastError]].map(([label, t]) => (
          <StatusCard key={label} title={label}>
            {t ? (
              <>
                <div className="text-small">{t.label}</div>
                <div className="mt-1 text-caption" style={{ color: t.error ? 'var(--danger)' : 'var(--text2)' }}>{t.error || t.summary || t.status}</div>
                <div className="mt-1 text-micro text-faint">{t.finished_at ? `${ago(t.finished_at)} ago` : t.started_at ? `started ${ago(t.started_at)} ago` : ''}</div>
              </>
            ) : <div className="text-caption text-muted">Nothing recorded since the last restart.</div>}
          </StatusCard>
        ))}
      </div>

      <SectionHeader label="Tasks" sub={`${shown.length} of ${tasks.length}`} />
      <div className="mb-3 flex flex-wrap gap-1.5">
        {TASK_FILTERS.map(([k, label]) => (
          <Chip key={k || 'all'} active={filter === k} onClick={() => setFilter(k)}
            count={k ? tasks.filter(t => t.status === k).length : tasks.length}>{label}</Chip>
        ))}
      </div>
      {!shown.length ? <p className="text-caption text-muted">No tasks here.</p> : shown.slice(0, 100).map(t => (
        <div key={t.id} className="mb-2 rounded-card border bg-panel px-4 py-3"
          style={{ borderColor: t.status === 'error' ? 'var(--danger-bd)' : 'var(--border)' }}>
          <div className="flex flex-wrap items-center gap-2.5">
            <span className="rounded-pill border border-line px-2 py-px text-micro text-muted">{TASK_KIND[t.kind] || t.kind}</span>
            <span className="min-w-0 flex-1 truncate text-small font-semibold">{t.label}</span>
            <StatusChip status={(TASK_STATUS[t.status] || ['queued'])[0]}
              word={(TASK_STATUS[t.status] || [null, t.status])[1]} />
            <span className="text-micro text-faint">{t.started_at ? `${ago(t.started_at)} ago` : ''}</span>
            {t.status === 'running' && t.cancellable !== false && (
              <button className="sm" onClick={() => action(`/api/tasks/${t.id}/cancel`)
                .then(() => pushToast('Cancelling…')).catch(e => pushToast(`Cancel failed: ${e.message}`, 'error'))}>Cancel</button>
            )}
          </div>
          {t.status === 'running' && <div className="mt-2 max-w-[420px]"><ProgressBar value={t.percent || 3} height={5} /></div>}
          {(t.error || t.summary || (t.status === 'running' && t.current)) && (
            <div className="mt-1 truncate text-caption" style={{ color: t.error ? 'var(--danger)' : 'var(--text2)' }}
              title={t.error || t.summary || t.current}>
              {t.error || (t.status === 'running' ? t.current : t.summary)}
            </div>
          )}
        </div>
      ))}
      <p className="mt-2 text-caption text-muted">
        Tasks are kept in memory and cleared by a restart. Album requests have their own history in{' '}
        <button className="link-inline" onClick={() => navigate('Downloads')}>Downloads</button>.
      </p>
    </>
  )
}

// ── Logs ─────────────────────────────────────────────────────────────────────
const LOG_FILTERS = [['', 'All'], ['errors', 'Errors'], ['slskd', 'slskd'], ['navidrome', 'Navidrome'], ['placement', 'Placement'], ['scan', 'Scans']]
const TAG_COLOR = { slskd: 'var(--accent-2)', task: 'var(--accent)', navidrome: 'var(--green)', placement: 'var(--green)' }

function Logs() {
  const { state, dispatch } = useApp()
  const { logEntries, logFilter } = state
  const [text, setText] = useState('')
  const q = text.trim().toLowerCase()
  const rows = (logEntries || []).filter(e => !q || (e.msg || '').toLowerCase().includes(q)).slice().reverse()
  return (
    <>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {LOG_FILTERS.map(([k, label]) => (
          <Chip key={k || 'all'} active={logFilter === k} onClick={() => dispatch({ type: 'SET_LOG_FILTER', filter: k })}>{label}</Chip>
        ))}
        <input type="search" className="w-56 !py-1.5 text-caption" placeholder="Filter text…" aria-label="Filter log lines"
          value={text} onChange={e => setText(e.target.value)} />
        <span className="spacer" />
        <span className="text-caption text-muted">{logEntries ? `${rows.length} line(s), newest first · refreshes every few seconds` : ''}</span>
      </div>
      {!logEntries ? <p className="text-caption text-muted">Loading logs…</p> : (
        <div className="max-h-[70vh] overflow-y-auto rounded-card border border-line p-4 font-mono text-caption leading-[1.8]"
          style={{ background: 'var(--inset-deep)' }}>
          {rows.map((e, i) => (
            <div key={i} className="flex gap-3">
              <span className="whitespace-nowrap text-faint">{e.ts}</span>
              <span className="min-w-[72px] shrink-0 whitespace-nowrap" style={{ color: TAG_COLOR[e.tag] || 'var(--muted)' }}>{e.tag}</span>
              <span className="min-w-0 break-all"
                style={{ color: e.severity === 'error' ? 'var(--danger)' : e.severity === 'warn' ? 'var(--decide-fg)' : 'var(--text2)' }}>{e.msg}</span>
            </div>
          ))}
          {!rows.length && <p className="text-muted">No log lines match.</p>}
        </div>
      )}
      <p className="mt-2 text-caption text-muted">The last 1,000 lines lb-bot printed since it started. The full log is the container's (<span className="font-mono">deploy.sh logs</span>).</p>
    </>
  )
}

export default function Settings() {
  const { state } = useApp()
  const sub = SUBS.some(([k]) => k === state.routeParams[0]) ? state.routeParams[0] : 'status'
  return (
    <>
      <div className="mb-4"><PageTitle eyebrow="Settings" title={SUBS.find(([k]) => k === sub)[1]} /></div>
      <SubTabs items={SUBS} value={sub} onChange={k => navigate('Settings', k)} label="Settings sections" />
      {sub === 'sources' && <Sources />}
      {sub === 'status' && <Status />}
      {sub === 'activity' && <Activity />}
      {sub === 'logs' && <Logs />}
      {sub === 'appearance' && <div className="card max-w-[380px] !p-5"><AppearanceControls /></div>}
    </>
  )
}
