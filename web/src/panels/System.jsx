import { useEffect, useMemo, useState } from 'react'
import { useApp, navigate } from '../App.jsx'
import { api, post, put } from '../lib/api.js'
import { indexProgress } from '../lib/indexProgress.js'
import { AppearanceControls } from '../components/AppearanceMenu.jsx'
import Page, { Grp, Meter, Pick, Seg } from '../components/Page.jsx'
import { ago, Badge, fmtDuration, StatusChip, Toggle } from '../components/ui.jsx'
import OverridesTab from './Overrides.jsx'

// System: is lb-bot healthy, how does it choose sources, what is it doing in
// the background, what has it logged, and how this browser looks.
//
// Only Source rules is editable at runtime (PUT /api/prefs). Everything under
// Status is set by environment variables on the container and is shown, not
// edited.
const TABS = [['status', 'Status'], ['sources', 'Source rules'], ['overrides', 'Overrides'], ['activity', 'Activity'], ['logs', 'Logs'], ['appearance', 'Appearance']]

function SystemFrame({ tab, sub, bar, action, children }) {
  const { state } = useApp()
  const checks = state.health?.checks
  const failing = checks ? checks.filter(c => !c.ok).length : 0
  const line = sub ?? (checks
    ? (failing ? `${failing} check${failing === 1 ? '' : 's'} failing` : `All ${checks.length} checks passing`)
    : '')
  return (
    <Page title="System" sub={line} action={action} bar={bar}
      tabs={TABS} tab={tab} onTab={k => navigate('system', k)}>
      {children}
    </Page>
  )
}

// A labelled value, with an optional status at the end and a note beneath.
// `title` says what the row counts; `below` is a line of its own under it (a
// meter).
function KV({ label, children, st, tone, note, actions, mono, title, below }) {
  return (
    <div className="row wrap" title={title}>
      <span className="muted" style={{ width: '32%', minWidth: 110 }}>{label}</span>
      <span className={`grow${mono ? ' num' : ''}`} style={{ overflowWrap: 'anywhere', minWidth: 140 }}>
        {children}
        {note ? <span className="s block" style={{ whiteSpace: 'normal' }}>{note}</span> : null}
      </span>
      {actions}
      {st ? <span className={`st ${tone ?? (/^(pass|on|ok)$/i.test(st) ? 'ok' : /^(fail|check)$/i.test(st) ? 'bad' : '')}`}>{st}</span> : null}
      {below ? <div style={{ flexBasis: '100%' }}>{below}</div> : null}
    </div>
  )
}

// ── Sources ──────────────────────────────────────────────────────────────────
// Guards with the threshold each one enforces when on. The thresholds used to
// be fixed at these defaults behind an on/off toggle.
const GUARDS = [
  { key: 'requireFullCoverage', bool: true, label: 'Require full-album coverage',
    desc: 'Only sources that visibly have every missing track. Strict: slskd often under-reports folders.' },
  { key: 'maxAlbumSizeMB', def: 500, unit: 'MB', step: 50, min: 50, label: 'Skip albums larger than',
    desc: 'Leaves huge 24-bit rips alone unless nothing else fits.' },
  { key: 'minSpeedMbps', def: 1.0, unit: 'MB/s', step: 0.5, min: 0.1, label: 'Skip peers slower than',
    desc: 'Avoids uploads that will crawl.' },
  { key: 'maxQueueLength', def: 20, unit: 'in queue', step: 5, min: 1, label: 'Skip peers with more than',
    desc: 'Avoids long remote waits.' },
]

// What each format tier means (the backend ranks formats, not bitrates).
const RANK_DESC = {
  flac: 'Lossless. CD quality and up, the target format',
  opus: 'Efficient lossy fallback. Transparent, small files',
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
        style={{ width: 84 }} disabled={!on} min={g.min} step={g.step}
        value={draft} onChange={e => setDraft(e.target.value)} onBlur={commit}
        onKeyDown={e => { if (e.key === 'Enter') e.currentTarget.blur() }} />
      <span className="s">{g.unit}</span>
    </span>
  )
}

function Sources() {
  const { state, dispatch, pushToast } = useApp()
  const { prefs } = state
  const [local, setLocal] = useState(prefs)
  useEffect(() => { setLocal(prefs) }, [prefs])
  if (!local) return <SystemFrame tab="sources"><p className="muted">Loading the rules…</p></SystemFrame>
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

  return (
    <SystemFrame tab="sources">
      <div style={{ maxWidth: 760 }}>
        <Grp>Format order</Grp>
        <div className="rows">
          {ranks.map((r, i) => (
            <div key={r.key} className="row">
              <span className="num muted" style={{ width: 14 }}>{i + 1}</span>
              <Badge format={r.key} />
              <div className="grow">
                <div className="t">{r.label}</div>
                <div className="s">{RANK_DESC[r.key] || ''}</div>
              </div>
              <button className="sm" disabled={i === 0} aria-label={`Move ${r.label} up`} onClick={() => moveRank(i, -1)}>↑</button>
              <button className="sm" disabled={i === ranks.length - 1} aria-label={`Move ${r.label} down`} onClick={() => moveRank(i, 1)}>↓</button>
            </div>
          ))}
          <div className="row">
            <span className="num muted" style={{ width: 14 }} />
            <Badge format="mp3" />
            <div className="grow">
              <div className="t">MP3</div>
              <div className="s">Rejected, unless you allow it for one album from that album’s page.</div>
            </div>
          </div>
        </div>
        <p className="muted mt-2">When lb-bot finds sources it prefers formats in this order. Anything not listed is rejected at search time.</p>

        {local.qualityOptions?.length > 0 && (
          <>
            <Grp>Which copy wins</Grp>
            <div className="rows">
              {local.qualityOptions.map(o => {
                const active = local.quality === o.key
                return (
                  <button key={o.key} className="row tap" aria-pressed={active} aria-selected={active}
                    onClick={() => save({ quality: o.key }, { quality: o.key })}>
                    <span className="grow">
                      <span className="t block">{o.label}</span>
                      <span className="s block" style={{ whiteSpace: 'normal' }}>{o.detail}</span>
                    </span>
                    {active && <span className="st ok">Chosen</span>}
                  </button>
                )
              })}
            </div>
            <p className="muted mt-2">
              When several sources carry the same album, this decides between them by codec and bit depth. It never
              rejects a source, so a gap is still filled if only one copy exists.
            </p>
          </>
        )}

        <Grp>Guards<span className="note">applied before ranking; a source that fails one is skipped</span></Grp>
        <div className="rows">
          {GUARDS.map(g => {
            const value = guards[g.key]
            const on = !!value
            return (
              <div key={g.key} className="row wrap">
                <div className="grow" style={{ minWidth: 220 }}>
                  <div className="flex flex-wrap items-center gap-2">
                    {g.label}
                    {!g.bool && (
                      <GuardNumber guard={g} on={on} value={on ? value : g.def}
                        onCommit={v => setGuard(g.key, v)} />
                    )}
                  </div>
                  <div className="s" style={{ whiteSpace: 'normal' }}>{g.desc}</div>
                </div>
                <Toggle on={on} label={g.label} onChange={next => setGuard(g.key, next ? (g.bool ? true : g.def) : (g.bool ? false : 0))} />
              </div>
            )
          })}
          <div className="row wrap">
            <span className="grow" style={{ minWidth: 220 }}>If the guards reject every source</span>
            <Seg label="If the guards reject every source" value={fallback}
              onChange={v => save({ fallback: v }, { fallback: v })}
              options={[['best', 'Use the best one anyway'], ['skip', 'Leave it for me']]} />
          </div>
        </div>
        {local.fixed && (
          <p className="muted mt-2">
            Always on: a source must have at least {Math.round(local.fixed.minAvailabilityRatio * 100)}% of its files
            available, and a transfer with no progress for {local.fixed.stallTimeoutSeconds} s is cancelled and the
            next source tried.
          </p>
        )}
      </div>
    </SystemFrame>
  )
}

// ── Status ───────────────────────────────────────────────────────────────────
const AUTO_INDEX_WORDS = {
  indexed: 'indexing: last tick scanned an artist',
  unresolved: 'indexing: last artist had no MusicBrainz match',
  idle: 'idle: every artist is indexed and fresh',
  paused: 'paused while a manual build runs',
  outage: 'paused: MusicBrainz is not answering',
  failed: 'indexing: last artist failed and waits for a retry',
  covered: 'indexing: last artist shares another artist\'s fresh row',
}

// What the two counts count. There is one "up to date" figure and it is the
// worker's; the older "Artists indexed" row is shown only when the worker has
// no numbers (off, not there yet, or a manual build is running).
const UP_TO_DATE_TITLE = 'Library artists whose index is current: scanned with this version of the matcher and inside the rescan interval. The rest wait for the background worker.'
const INDEXED_TITLE = 'Library artists that have an index row at all. A stale one is older than the rescan interval, or was scanned by an older version of the matcher.'

// How far the library index is. `progress` is `indexProgress` of the summary's
// `index` block — the same numbers the sidebar shows (ruling U1) — so this
// page and the sidebar cannot disagree. A manual build is not drawn from it:
// its own row below shows that one, with its Cancel.
function IndexBuild({ progress }) {
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
  // The worker's own numbers, when it has them and no manual build is running.
  const worker = progress && progress.kind !== 'build' ? progress : null
  if (!s && !worker) return <KV label="Artists indexed">…</KV>
  const t = s?.task || {}
  const complete = !!s && s.artistsTotal > 0 && s.artistsIndexed >= s.artistsTotal
  const n = x => x.toLocaleString()
  // The one action about the index, on whichever row is the count.
  const actions = !s ? null : s.building ? (
    <button className="sm" disabled={cancelling || !t.id} onClick={async () => {
      setCancelling(true)
      try { await action(`/api/tasks/${t.id}/cancel`); pushToast('Cancelling the index build…') }
      catch (e) { setCancelling(false); pushToast(`Cancel failed: ${e.message}`, 'error') }
    }}>{cancelling ? 'Cancelling…' : 'Cancel build'}</button>
  ) : (!complete || s.artistsStale > 0) && (
    <button className="sm" title="Walk every artist now at MusicBrainz pace, instead of waiting for the background worker"
      onClick={async () => {
        try {
          await action('/api/library-index/build')
          setS(x => ({ ...x, building: true, task: null }))
          pushToast('Index build started. It runs at MusicBrainz pace')
        } catch (e) { pushToast(`Could not start: ${e.message}`, 'error') }
      }}>{complete ? 'Refresh stale artists now' : 'Build the full index now'}</button>
  )
  // "Now" only while an artist can be scanned: paused, waiting and idle are
  // the "Library index" row's to say, once.
  const now = worker?.kind === 'indexing' ? (worker.current ? `scanning ${worker.current}` : 'between two artists') : ''
  return (
    <>
      {worker ? (
        <>
          <KV label="Up to date" title={UP_TO_DATE_TITLE} actions={actions}
            below={worker.kind !== 'done' ? <Meter value={worker.pct} /> : null}>
            <span className="num">{n(worker.done)} of {n(worker.total)}</span> artists
            {worker.remaining ? ` · ${n(worker.remaining)} to go` : ''}
          </KV>
          {now && <KV label="Now">{now}</KV>}
        </>
      ) : (
        <KV label="Artists indexed" title={INDEXED_TITLE} actions={actions}>
          <span className="num">{s.artistsIndexed.toLocaleString()} of {s.artistsTotal.toLocaleString()}</span>
          {s.artistsStale ? ` · ${s.artistsStale} stale` : ''}
        </KV>
      )}
      {s?.building && (
        <div className="row col">
          <div className="s clip mb-1.5">Building {t.done ?? 0} of {t.total ?? '?'}{t.current ? ` · ${t.current}` : ''}</div>
          <Meter value={t.total ? (t.done / t.total) * 100 : 3} />
        </div>
      )}
    </>
  )
}

function Status() {
  const { state, dispatch, pushToast, confirmAction } = useApp()
  const { health, systemStatus: st } = state
  // The block the sidebar reads, on the app's own poll.
  const progress = indexProgress(state.summary?.index)
  const [settings, setSettings] = useState(null)
  const [stFailed, setStFailed] = useState(false)
  const [rechecking, setRechecking] = useState(false)
  useEffect(() => { api('/api/settings').then(setSettings).catch(() => {}) }, [])

  // Health and worker status, on their own slower cadence. Results go into app
  // state so the page's "N checks failing" line can read them.
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
    try { await post('/api/rescan', { full: false }); pushToast('Navidrome is rescanning. Re-run the checks in a minute') }
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
  // was behind and offered a rescan it could not run.
  const rescanFixes = c => !c.ok && c.label === 'Library path ↔ Navidrome' && /stale/i.test(c.detail || '')
  const now = Date.now() / 1000
  const ai = st?.autoIndex || {}
  const hub = st?.hubPush || {}
  const ac = st?.acoustid || {}
  const acOn = ac.keySet && ac.fpcalc

  return (
    <SystemFrame tab="status"
      action={<button disabled={rechecking} aria-busy={rechecking} onClick={recheck}>Re-run checks</button>}>
      <div style={{ maxWidth: 900 }}>
        <Grp>Connections<span className="note">{health?.lastRun ? `checked ${ago(health.lastRun)} ago` : ''}</span></Grp>
        {!health ? <p className="muted">Running the checks. This takes a few seconds…</p> : (
          <div className="rows">
            {health.checks.map(c => (
              <KV key={c.id} label={c.label} st={c.ok ? 'Pass' : 'Fail'}
                note={!c.ok && c.howToFix ? c.howToFix : null}
                actions={rescanFixes(c) && <button className="sm" onClick={rescanNavidrome}>Rescan Navidrome</button>}>
                {c.detail}
              </KV>
            ))}
          </div>
        )}
        {(health?.checks || []).some(rescanFixes) && (
          <p className="why">Navidrome’s index is behind the files on disk. A rescan usually fixes it. If it keeps failing, check the /music mount matches Navidrome’s.</p>
        )}

        {!st ? (
          <p className={`why ${stFailed ? 'bad' : 'quiet'}`}>
            {stFailed
              ? 'Worker and integration status is unavailable. The running lb-bot did not answer, and may be older than this page.'
              : 'Loading status…'}
          </p>
        ) : (
          <>
            <Grp>Background work</Grp>
            <div className="rows">
              <KV label="Library index"
                st={ai.enabled === false ? 'Off' : ai.running ? 'On' : null}
                note={[
                  ai.at ? `last tick ${ago(ai.at)} ago${ai.scannedThisRun ? `, ${ai.scannedThisRun} artist(s) since start` : ''}` : null,
                  `rescans after ${ai.ttlDays ?? 30} days`,
                  ai.pausedUntil > now ? `paused until ${new Date(ai.pausedUntil * 1000).toLocaleTimeString()}` : null,
                ].filter(Boolean).join(' · ')}>
                {ai.enabled === false ? 'Off (LB_BOT_AUTO_INDEX=0)'
                  : ai.running === false ? 'Not running (no Navidrome login)'
                  // "Idle" with artists left: they failed and wait for a retry.
                  : ai.outcome === 'idle' && progress?.kind === 'waiting' ? 'idle: the artists that are left wait for a retry'
                  : AUTO_INDEX_WORDS[ai.outcome] || 'starting'}
                {ai.failing ? <span className="warn"> · {ai.failing} artist(s) failed and wait for a retry</span> : null}
              </KV>
              <IndexBuild progress={progress} />
              <KV label="Hub push" st={hub.configured ? (hub.persistFailing ? 'Fail' : 'On') : 'Off'}
                note={hub.configured ? `${hub.url} · index head ${hub.headSeq ?? '—'}${hub.ackedSeq != null ? `, hub has ${hub.ackedSeq}` : ''}` : null}>
                {!hub.configured ? 'LB_BOT_HUB_URL and _TOKEN are not set, so the Feishin and Navic clients get no live updates.'
                  : hub.persistFailing ? <span className="bad">Can’t write the high-water mark. /config is not writable.</span>
                  : hub.attemptedAt ? `last push ${ago(hub.attemptedAt)} ago` : 'no push yet'}
              </KV>
              <KV label="Wishlist search" st="On">
                one album every {fmtDuration(st.wishlist?.intervalSeconds || 0)}, each at most every {fmtDuration(st.wishlist?.cooldownSeconds || 0)}
              </KV>
              <KV label="Audio fingerprint check" st={acOn ? 'On' : 'Off'}
                actions={ac.rejectedSources > 0 && <button className="sm danger" onClick={clearRejected}>Forget rejected peers</button>}
                note={[
                  'Checks a downloaded file is the right recording before it is placed. Off means placement trusts tags and filenames.',
                  ac.unavailable === 'fpcalc' ? 'At least one file couldn’t be fingerprinted since the last restart and was placed unverified.' : null,
                ].filter(Boolean).join(' ')}>
                AcoustID · {ac.keySet ? 'key set' : 'no key (ACOUSTID_API_KEY)'} · fpcalc {ac.fpcalc ? 'installed' : 'missing'}
                {' · '}minimum score {ac.minScore ?? '…'} · <span className="num">{ac.rejectedSources ?? 0}</span> peers rejected
                {ac.unavailable === 'import' && <span className="bad"> · pyacoustid is not installed, so verification is off</span>}
              </KV>
            </div>

            <Grp>Integrations</Grp>
            <div className="rows">
              <KV label="ListenBrainz" st={st.listenbrainz?.user ? 'On' : 'Off'}
                note={`Playlists scanned: ${(st.listenbrainz?.playlists || []).join(', ') || 'none'}`}>
                {st.listenbrainz?.user || 'no user set'}
              </KV>
              <KV label="Spotify" st={st.spotify?.configured ? 'On' : 'Off'}>
                {st.spotify?.configured ? 'Playlist scans and link lookups are available.'
                  : 'SPOTIFY_CLIENT_ID and _SECRET are not set, so Spotify playlists can’t be scanned. Spotify links still resolve by page title.'}
              </KV>
              <KV label="Last.fm" st={st.lastfm?.keySet ? 'On' : 'Off'}>
                {st.lastfm?.keySet ? 'Cross-checks similar artists alongside ListenBrainz.'
                  : 'LASTFM_API_KEY is not set, so similar artists come from ListenBrainz alone.'}
              </KV>
            </div>

            <Grp>Search limits</Grp>
            <div className="rows">
              <KV label="Search timeout" mono>{st.search?.timeoutSeconds ?? '…'} s</KV>
              <KV label="Stalled transfer">cancelled after <span className="num">{st.search?.stallTimeoutSeconds ?? '…'} s</span> without progress</KV>
              <KV label="Source failover">up to <span className="num">{st.search?.failoverMax ?? '…'}</span> sources in <span className="num">{st.search?.failoverDeadlineSeconds ?? '…'} s</span></KV>
            </div>

            <Grp>Build</Grp>
            <div className="rows">
              <KV label="Revision" mono
                note={st.revision === 'unknown' ? 'This image was built without LB_BOT_REVISION.' : null}>{st.revision || '…'}</KV>
              <KV label="Running for">{st.startedAt ? fmtDuration(now - st.startedAt) : '…'}</KV>
            </div>
          </>
        )}

        {settings?.cards?.length ? (
          <>
            <Grp>Configuration<span className="note">from environment variables on the container</span></Grp>
            {settings.cards.map((c, i) => (
              <div className="rows mb-2.5" key={i}>
                <KV label={<span className="t" style={{ color: 'var(--fg)' }}>{c.title}</span>} st={c.ok ? 'OK' : 'Check'}
                  note={!c.ok && c.fix ? c.fix : null} />
                {Object.entries(c.values || {}).map(([k, v]) => <KV key={k} label={k} mono>{String(v)}</KV>)}
              </div>
            ))}
          </>
        ) : null}
      </div>
    </SystemFrame>
  )
}

// ── Activity ─────────────────────────────────────────────────────────────────
// Every background task: scans, source searches, placements, downloads.
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

  const bar = state.tasks && (
    <>
      <Pick label="Show" value={filter} onChange={setFilter}
        options={TASK_FILTERS.map(([k, label]) => [k, `${label} (${k ? tasks.filter(t => t.status === k).length : tasks.length})`])} />
      <span className="muted">Tasks are kept in memory and cleared by a restart.</span>
    </>
  )

  if (!state.tasks) return <SystemFrame tab="activity"><p className="muted">Loading activity…</p></SystemFrame>
  return (
    <SystemFrame tab="activity" bar={bar}>
      <div style={{ maxWidth: 900 }}>
        <Grp>Most recent</Grp>
        <div className="rows">
          {[['Last scan', last('scan')], ['Last placement', last('placement')], ['Last failure', lastError]].map(([label, t]) => (
            <KV key={label} label={label}
              note={t ? [t.error || t.summary || t.status, t.finished_at ? `${ago(t.finished_at)} ago` : t.started_at ? `started ${ago(t.started_at)} ago` : null].filter(Boolean).join(' · ') : null}>
              {t ? t.label : <span className="muted">Nothing recorded since the last restart.</span>}
            </KV>
          ))}
        </div>

        <Grp n={`${shown.length} of ${tasks.length}`}>Tasks</Grp>
        {!shown.length ? <p className="muted">No tasks here.</p> : (
          <div className="rows">
            {shown.slice(0, 100).map(t => (
              <div key={t.id} className="row col">
                <div className="flex min-w-0 items-center gap-3">
                  <div className="grow">
                    <div className="t clip">{t.label}</div>
                    <div className={`s clip${t.error ? ' bad' : ''}`} title={t.error || t.summary || t.current}>
                      {[TASK_KIND[t.kind] || t.kind,
                        t.error || (t.status === 'running' ? t.current : t.summary),
                        t.started_at ? `${ago(t.started_at)} ago` : null].filter(Boolean).join(' · ')}
                    </div>
                  </div>
                  <StatusChip status={(TASK_STATUS[t.status] || ['queued'])[0]}
                    word={(TASK_STATUS[t.status] || [null, t.status])[1]} />
                  {t.status === 'running' && t.cancellable !== false && (
                    <button className="sm" onClick={() => action(`/api/tasks/${t.id}/cancel`)
                      .then(() => pushToast('Cancelling…')).catch(e => pushToast(`Cancel failed: ${e.message}`, 'error'))}>Cancel</button>
                  )}
                </div>
                {t.status === 'running' && <div className="mt-2" style={{ maxWidth: 420 }}><Meter value={t.percent || 3} /></div>}
              </div>
            ))}
          </div>
        )}
        <p className="muted mt-3">
          Downloads and album requests have their own page:{' '}
          <button className="link" onClick={() => navigate('activity')}>Activity</button>.
        </p>
      </div>
    </SystemFrame>
  )
}

// ── Logs ─────────────────────────────────────────────────────────────────────
const LOG_FILTERS = [['', 'All lines'], ['errors', 'Errors'], ['slskd', 'slskd'], ['navidrome', 'Navidrome'], ['placement', 'Placement'], ['scan', 'Scans']]

function Logs() {
  const { state, dispatch } = useApp()
  const { logEntries, logFilter } = state
  const [text, setText] = useState('')
  const q = text.trim().toLowerCase()
  const rows = (logEntries || []).filter(e => !q || (e.msg || '').toLowerCase().includes(q)).slice().reverse()
  const bar = (
    <>
      <Pick label="Show" value={logFilter} onChange={k => dispatch({ type: 'SET_LOG_FILTER', filter: k })} options={LOG_FILTERS} />
      <input className="find" type="search" placeholder="Filter text" aria-label="Filter log lines"
        value={text} onChange={e => setText(e.target.value)} />
      <span className="muted">{logEntries ? `${rows.length.toLocaleString()} lines, newest first` : ''}</span>
    </>
  )
  return (
    <SystemFrame tab="logs" bar={bar}>
      {!logEntries ? <p className="muted">Loading logs…</p> : !rows.length ? <p className="empty">No log lines match.</p> : (
        <div className="rows">
          {rows.map((e, i) => (
            <div key={i} className="row" style={{ alignItems: 'baseline', paddingBlock: 6 }}>
              <span className="num muted whitespace-nowrap">{e.ts}</span>
              <span className="num hide-sm" style={{ width: 76, flex: 'none' }}>{e.tag}</span>
              <span className={`grow${e.severity === 'error' ? ' bad' : e.severity === 'warn' ? ' warn' : ''}`}
                style={{ overflowWrap: 'anywhere' }}>{e.msg}</span>
            </div>
          ))}
        </div>
      )}
      <p className="muted mt-3">
        The last 1,000 lines lb-bot printed since it started, refreshed every few seconds. The full log is the
        container’s (<span className="num">deploy.sh logs</span>).
      </p>
    </SystemFrame>
  )
}

export default function System() {
  const { state } = useApp()
  const tab = TABS.some(([k]) => k === state.routeParams[0]) ? state.routeParams[0] : 'status'
  if (tab === 'sources') return <Sources />
  if (tab === 'overrides') {
    return <SystemFrame tab="overrides" sub="Everything lb-bot changed at your word, each with its undo"><OverridesTab /></SystemFrame>
  }
  if (tab === 'activity') return <Activity />
  if (tab === 'logs') return <Logs />
  if (tab === 'appearance') {
    return <SystemFrame tab="appearance" sub="Colours and theme for this browser"><AppearanceControls /></SystemFrame>
  }
  return <Status />
}
